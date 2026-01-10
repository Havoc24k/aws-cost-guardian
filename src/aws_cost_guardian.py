"""
AWS Cost Guardian - POC Account Cost Protection
Simple budget monitoring with automatic resource shutdown.
"""

import json
import os
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Default hourly costs when Pricing API fails
DEFAULT_EC2_HOURLY = Decimal("0.10")
DEFAULT_RDS_HOURLY = Decimal("0.15")
DEFAULT_APPRUNNER_HOURLY = Decimal("0.10")
DEFAULT_ECS_FARGATE_HOURLY = Decimal("0.05")

# Lambda pricing constants
LAMBDA_PRICE_PER_REQUEST = Decimal("0.0000002")  # $0.20 per 1M requests
LAMBDA_PRICE_PER_GB_SECOND = Decimal("0.0000166667")  # $0.0000166667 per GB-second
DEFAULT_LAMBDA_LOOKBACK_HOURS = 24  # Default hours to look back for usage metrics

# Lambda spike detection
DEFAULT_LAMBDA_SPIKE_THRESHOLD = 10  # Alert if rate is 10x baseline
DEFAULT_LAMBDA_SPIKE_WINDOW_MINUTES = 5  # Check last N minutes for spike
DEFAULT_LAMBDA_BASELINE_HOURS = 168  # 7 days baseline for comparison


@dataclass
class LambdaSpike:
    """Detected Lambda usage spike."""

    function_name: str
    region: str
    current_rate: Decimal  # invocations per minute
    baseline_rate: Decimal  # invocations per minute
    spike_ratio: Decimal
    projected_daily_cost: Decimal


@dataclass
class BudgetStatus:
    """Result of a budget check with projected costs and recommended action."""

    actual_spend: Decimal
    hourly_cost: Decimal
    projected_total: Decimal
    budget: Decimal
    budget_percent: Decimal
    remaining_hours: int
    resources: dict[str, list[dict[str, Any]]]
    action: str  # 'ok', 'alert', 'stop_all', 'spike_alert'
    thresholds_breached: list[int]
    lambda_spikes: list[LambdaSpike]
    actual_exceeded: bool = False  # True if actual_spend > budget


class BudgetGuardian:
    """Account-wide budget monitoring and protection."""

    def __init__(
        self,
        regions: list[str],
        total_budget: Decimal,
        alert_thresholds: Optional[list[int]] = None,
        auto_stop_threshold: int = 100,
        sns_topic_arn: Optional[str] = None,
        lambda_lookback_hours: int = DEFAULT_LAMBDA_LOOKBACK_HOURS,
        lambda_spike_threshold: int = DEFAULT_LAMBDA_SPIKE_THRESHOLD,
        lambda_spike_window_minutes: int = DEFAULT_LAMBDA_SPIKE_WINDOW_MINUTES,
        lambda_baseline_hours: int = DEFAULT_LAMBDA_BASELINE_HOURS,
        exclude_lambdas: Optional[list[str]] = None,
        budget_period_start: str = "",
        budget_period_end: str = "",
    ):
        self.regions = regions
        self.budget = Decimal(str(total_budget))
        self.alert_thresholds = alert_thresholds or [50, 75, 90]
        self.auto_stop_threshold = auto_stop_threshold
        self.sns_topic_arn = sns_topic_arn
        self.lambda_lookback_hours = lambda_lookback_hours
        self.lambda_spike_threshold = lambda_spike_threshold
        self.lambda_spike_window_minutes = lambda_spike_window_minutes
        self.lambda_baseline_hours = lambda_baseline_hours
        self.exclude_lambdas = set(exclude_lambdas or [])
        self.budget_period_start = budget_period_start
        self.budget_period_end = budget_period_end

        # Clients (Cost Explorer is global, others are regional)
        self.ce = boto3.client("ce", region_name="us-east-1")
        self.sns = boto3.client("sns") if sns_topic_arn else None
        self._account_info: Optional[dict[str, str]] = None
        self._pricing_client: Optional[Any] = None
        self._region_locations: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> "BudgetGuardian":
        """Create instance from Lambda environment variables."""
        # Auto-exclude the current Lambda function (AWS_LAMBDA_FUNCTION_NAME is set by AWS)
        exclude = []
        current_function = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        if current_function:
            exclude.append(current_function)

        return cls(
            regions=json.loads(os.environ.get("REGIONS", '["us-east-1"]')),
            total_budget=Decimal(os.environ.get("TOTAL_BUDGET", "1000")),
            alert_thresholds=json.loads(os.environ.get("ALERT_THRESHOLDS", "[50, 75, 90]")),
            auto_stop_threshold=int(os.environ.get("AUTO_STOP_THRESHOLD", "100")),
            sns_topic_arn=os.environ.get("SNS_TOPIC_ARN"),
            lambda_lookback_hours=int(os.environ.get("LAMBDA_LOOKBACK_HOURS", "24")),
            lambda_spike_threshold=int(os.environ.get("LAMBDA_SPIKE_THRESHOLD", "10")),
            lambda_spike_window_minutes=int(os.environ.get("LAMBDA_SPIKE_WINDOW_MINUTES", "5")),
            lambda_baseline_hours=int(os.environ.get("LAMBDA_BASELINE_HOURS", "168")),
            exclude_lambdas=exclude,
            budget_period_start=os.environ.get("BUDGET_PERIOD_START", ""),
            budget_period_end=os.environ.get("BUDGET_PERIOD_END", ""),
        )

    def check_budget(self) -> BudgetStatus:
        """Main budget check - returns current status and recommended action."""
        # 1. Get actual month-to-date spend
        actual_spend = self._get_actual_spend()

        # 2. Discover running resources across all regions
        resources = self._discover_resources()

        # 3. Calculate hourly cost of running resources
        hourly_cost = self._calculate_hourly_cost(resources)

        # 4. Project to end of budget period
        remaining_hours = self._hours_until_period_end()
        projected_additional = hourly_cost * remaining_hours
        projected_total = actual_spend + projected_additional

        # 5. Detect Lambda spikes
        lambda_spikes = self._detect_lambda_spikes(resources)

        # 6. Determine action
        budget_percent = (projected_total / self.budget) * 100 if self.budget > 0 else Decimal("0")
        action, thresholds_breached, actual_exceeded = self._determine_action(
            budget_percent, actual_spend
        )

        # Override action if spike detected (but not if already stopping)
        if lambda_spikes and action == "ok":
            action = "spike_alert"

        return BudgetStatus(
            actual_spend=actual_spend,
            hourly_cost=hourly_cost,
            projected_total=projected_total,
            budget=self.budget,
            budget_percent=budget_percent,
            remaining_hours=remaining_hours,
            resources=resources,
            action=action,
            thresholds_breached=thresholds_breached,
            lambda_spikes=lambda_spikes,
            actual_exceeded=actual_exceeded,
        )

    def _get_actual_spend(self) -> Decimal:
        """Get actual spend for the current budget period from Cost Explorer.

        See: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html
        """
        period_start, _ = self._get_period_bounds()
        now = datetime.now(timezone.utc)

        start = period_start.strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")

        # Ensure start is not before end (can happen on first day of period)
        if start >= end:
            return Decimal("0")

        try:
            response = self.ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
            # Sum all days in the period
            total_cost = Decimal("0")
            for result in response.get("ResultsByTime", []):
                total = result.get("Total", {})
                cost_data = total.get("UnblendedCost", {})
                amount = cost_data.get("Amount", "0")
                total_cost += Decimal(amount)
            return total_cost
        except (KeyError, IndexError, ValueError, BotoCoreError, ClientError) as e:
            print(f"Cost Explorer error: {e}")
            return Decimal("0")

    def _discover_resources(self) -> dict[str, list[dict[str, Any]]]:
        """Discover running resources across all regions."""
        resources: dict[str, list[dict[str, Any]]] = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [],
            "ecs": [],
        }

        for region in self.regions:
            # EC2
            ec2 = boto3.client("ec2", region_name=region)
            try:
                paginator = ec2.get_paginator("describe_instances")
                for page in paginator.paginate(
                    Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
                ):
                    for reservation in page.get("Reservations", []):
                        for instance in reservation.get("Instances", []):
                            instance_id = instance.get("InstanceId")
                            instance_type = instance.get("InstanceType")
                            launch_time = instance.get("LaunchTime")
                            if instance_id and instance_type and launch_time:
                                resources["ec2"].append(
                                    {
                                        "id": instance_id,
                                        "type": instance_type,
                                        "region": region,
                                        "launch_time": launch_time.isoformat(),
                                    }
                                )
            except (KeyError, AttributeError, BotoCoreError, ClientError) as e:
                print(f"EC2 discovery error in {region}: {e}")

            # RDS
            rds = boto3.client("rds", region_name=region)
            try:
                rds_paginator = rds.get_paginator("describe_db_instances")
                for rds_page in rds_paginator.paginate():
                    for db in rds_page.get("DBInstances", []):
                        if db.get("DBInstanceStatus") == "available":
                            db_id = db.get("DBInstanceIdentifier")
                            db_class = db.get("DBInstanceClass")
                            engine = db.get("Engine")
                            if db_id and db_class and engine:
                                resources["rds"].append(
                                    {
                                        "id": db_id,
                                        "class": db_class,
                                        "engine": engine,
                                        "region": region,
                                    }
                                )
            except (KeyError, AttributeError, BotoCoreError, ClientError) as e:
                print(f"RDS discovery error in {region}: {e}")

            # Lambda (with memory for cost projection)
            lam = boto3.client("lambda", region_name=region)
            try:
                lam_paginator = lam.get_paginator("list_functions")
                for lam_page in lam_paginator.paginate():
                    for func in lam_page.get("Functions", []):
                        func_name = func.get("FunctionName")
                        memory_mb = func.get("MemorySize", 128)
                        # Skip excluded functions (e.g., cost-guardian itself)
                        if func_name and func_name not in self.exclude_lambdas:
                            resources["lambda"].append(
                                {
                                    "name": func_name,
                                    "region": region,
                                    "memory_mb": memory_mb,
                                }
                            )
            except (KeyError, AttributeError, BotoCoreError, ClientError) as e:
                print(f"Lambda discovery error in {region}: {e}")

            # App Runner (no paginator available, use NextToken manually)
            apprunner = boto3.client("apprunner", region_name=region)
            try:
                next_token = None
                while True:
                    if next_token:
                        response = apprunner.list_services(NextToken=next_token)
                    else:
                        response = apprunner.list_services()
                    for svc in response.get("ServiceSummaryList", []):
                        if svc.get("Status") == "RUNNING":
                            resources["apprunner"].append(
                                {
                                    "name": svc.get("ServiceName"),
                                    "arn": svc.get("ServiceArn"),
                                    "region": region,
                                }
                            )
                    next_token = response.get("NextToken")
                    if not next_token:
                        break
            except (KeyError, AttributeError, BotoCoreError, ClientError) as e:
                print(f"AppRunner discovery error in {region}: {e}")

            # ECS (Fargate services only - EC2 launch type is covered by EC2 discovery)
            ecs = boto3.client("ecs", region_name=region)
            try:
                # List all clusters
                cluster_arns = []
                clusters_token = None
                while True:
                    if clusters_token:
                        clusters_resp = ecs.list_clusters(nextToken=clusters_token)
                    else:
                        clusters_resp = ecs.list_clusters()
                    cluster_arns.extend(clusters_resp.get("clusterArns", []))
                    clusters_token = clusters_resp.get("nextToken")
                    if not clusters_token:
                        break

                # For each cluster, list services with running tasks
                for cluster_arn in cluster_arns:
                    services_token = None
                    while True:
                        if services_token:
                            services_resp = ecs.list_services(
                                cluster=cluster_arn, nextToken=services_token
                            )
                        else:
                            services_resp = ecs.list_services(cluster=cluster_arn)
                        service_arns = services_resp.get("serviceArns", [])

                        if service_arns:
                            # Get service details
                            desc_resp = ecs.describe_services(
                                cluster=cluster_arn, services=service_arns
                            )
                            for svc in desc_resp.get("services", []):
                                # Only include services with running tasks on Fargate
                                if (
                                    svc.get("runningCount", 0) > 0
                                    and svc.get("launchType") == "FARGATE"
                                ):
                                    resources["ecs"].append(
                                        {
                                            "name": svc.get("serviceName"),
                                            "arn": svc.get("serviceArn"),
                                            "cluster": cluster_arn,
                                            "region": region,
                                            "running_count": svc.get("runningCount", 0),
                                            "task_definition": svc.get("taskDefinition"),
                                        }
                                    )

                        services_token = services_resp.get("nextToken")
                        if not services_token:
                            break
            except (KeyError, AttributeError, BotoCoreError, ClientError) as e:
                print(f"ECS discovery error in {region}: {e}")

        return resources

    def _get_pricing_client(self) -> Any:
        """Get cached Pricing API client (always us-east-1)."""
        if self._pricing_client is None:
            self._pricing_client = boto3.client("pricing", region_name="us-east-1")
        return self._pricing_client

    def _extract_price_from_response(self, response: dict) -> Optional[Decimal]:
        """Extract price from AWS Pricing API response."""
        if response.get("PriceList"):
            price_data = json.loads(response["PriceList"][0])
            terms = price_data.get("terms", {}).get("OnDemand", {})
            for term in terms.values():
                for price_dim in term.get("priceDimensions", {}).values():
                    return Decimal(price_dim["pricePerUnit"]["USD"])
        return None

    def _calculate_hourly_cost(self, resources: dict[str, list[dict[str, Any]]]) -> Decimal:
        """Calculate total hourly cost of running resources."""
        total = Decimal("0")

        # EC2 instances
        for instance in resources["ec2"]:
            cost = self._get_ec2_hourly_cost(instance["type"], instance["region"])
            total += cost

        # RDS instances
        for db in resources["rds"]:
            cost = self._get_rds_hourly_cost(db["class"], db["engine"], db["region"])
            total += cost

        # Lambda functions (based on recent usage)
        for func in resources["lambda"]:
            cost = self._get_lambda_hourly_cost(func["name"], func["region"], func["memory_mb"])
            total += cost

        # App Runner services
        for svc in resources["apprunner"]:
            cost = self._get_apprunner_hourly_cost(svc["arn"], svc["region"])
            total += cost

        # ECS Fargate services
        for svc in resources["ecs"]:
            cost = self._get_ecs_hourly_cost(
                svc["task_definition"], svc["running_count"], svc["region"]
            )
            total += cost

        return total

    def _get_ec2_hourly_cost(self, instance_type: str, region: str) -> Decimal:
        """Get EC2 hourly cost. Uses fallback if Pricing API fails."""
        try:
            response = self._get_pricing_client().get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {
                        "Type": "TERM_MATCH",
                        "Field": "location",
                        "Value": self._region_to_location(region),
                    },
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )
            price = self._extract_price_from_response(response)
            if price is not None:
                return price
        except (KeyError, IndexError, ValueError, BotoCoreError, ClientError) as e:
            print(f"Pricing API error for EC2 {instance_type}: {e}")

        return DEFAULT_EC2_HOURLY

    def _get_rds_hourly_cost(self, instance_class: str, engine: str, region: str) -> Decimal:
        """Get RDS hourly cost. Uses fallback if Pricing API fails."""
        try:
            response = self._get_pricing_client().get_products(
                ServiceCode="AmazonRDS",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
                    {
                        "Type": "TERM_MATCH",
                        "Field": "location",
                        "Value": self._region_to_location(region),
                    },
                    {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine},
                ],
                MaxResults=1,
            )
            price = self._extract_price_from_response(response)
            if price is not None:
                return price
        except (KeyError, IndexError, ValueError, BotoCoreError, ClientError) as e:
            print(f"Pricing API error for RDS {instance_class}: {e}")

        return DEFAULT_RDS_HOURLY

    def _get_lambda_hourly_cost(self, function_name: str, region: str, memory_mb: int) -> Decimal:
        """Calculate Lambda hourly cost based on recent CloudWatch metrics."""
        try:
            cw = boto3.client("cloudwatch", region_name=region)
            now = datetime.now(timezone.utc)
            lookback = self.lambda_lookback_hours
            start_time = now - timedelta(hours=lookback)

            # Get invocation count
            invocations_response = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start_time,
                EndTime=now,
                Period=lookback * 3600,  # Single period for sum
                Statistics=["Sum"],
            )

            # Get total duration
            duration_response = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Duration",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start_time,
                EndTime=now,
                Period=lookback * 3600,
                Statistics=["Sum"],
            )

            # Extract values
            invocations = Decimal("0")
            total_duration_ms = Decimal("0")

            inv_datapoints = invocations_response.get("Datapoints", [])
            if inv_datapoints:
                invocations = Decimal(str(inv_datapoints[0].get("Sum", 0)))

            dur_datapoints = duration_response.get("Datapoints", [])
            if dur_datapoints:
                total_duration_ms = Decimal(str(dur_datapoints[0].get("Sum", 0)))

            if invocations == 0:
                return Decimal("0")

            # Calculate hourly rates
            invocations_per_hour = invocations / lookback
            duration_ms_per_hour = total_duration_ms / lookback

            # Convert to GB-seconds
            memory_gb = Decimal(str(memory_mb)) / 1024
            duration_seconds_per_hour = duration_ms_per_hour / 1000
            gb_seconds_per_hour = duration_seconds_per_hour * memory_gb

            # Calculate cost
            request_cost = invocations_per_hour * LAMBDA_PRICE_PER_REQUEST
            compute_cost = gb_seconds_per_hour * LAMBDA_PRICE_PER_GB_SECOND

            return request_cost + compute_cost

        except (BotoCoreError, ClientError) as e:
            print(f"CloudWatch error for Lambda {function_name}: {e}")
            return Decimal("0")

    def _get_apprunner_hourly_cost(self, service_arn: str, region: str) -> Decimal:
        """Get App Runner hourly cost based on configuration.

        App Runner pricing (provisioned):
        - $0.064 per vCPU-hour
        - $0.007 per GB-hour (memory)
        """
        try:
            apprunner = boto3.client("apprunner", region_name=region)
            response = apprunner.describe_service(ServiceArn=service_arn)
            config = response["Service"]["InstanceConfiguration"]

            # Cpu is in millicores (e.g., "1024" = 1 vCPU)
            cpu = Decimal(config.get("Cpu", "1024")) / 1024
            # Memory is in MB (e.g., "2048" = 2 GB)
            memory = Decimal(config.get("Memory", "2048")) / 1024

            # Pricing: $0.064/vCPU-hour + $0.007/GB-hour
            cpu_cost = cpu * Decimal("0.064")
            memory_cost = memory * Decimal("0.007")

            return cpu_cost + memory_cost
        except (BotoCoreError, ClientError) as e:
            print(f"AppRunner pricing error for {service_arn}: {e}")
            return DEFAULT_APPRUNNER_HOURLY

    def _get_fargate_unit_prices(self, region: str) -> tuple[Decimal, Decimal]:
        """Get Fargate vCPU and memory hourly prices from Pricing API.

        Returns (cpu_price_per_vcpu_hour, memory_price_per_gb_hour).
        Falls back to default prices if API fails.
        """
        location = self._region_to_location(region)
        cpu_price = None
        memory_price = None

        try:
            # Query for Fargate vCPU pricing
            cpu_response = self._get_pricing_client().get_products(
                ServiceCode="AmazonECS",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "cputype", "Value": "perCPU"},
                ],
                MaxResults=1,
            )
            cpu_price = self._extract_price_from_response(cpu_response)

            # Query for Fargate memory pricing
            memory_response = self._get_pricing_client().get_products(
                ServiceCode="AmazonECS",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "memorytype", "Value": "perGB"},
                ],
                MaxResults=1,
            )
            memory_price = self._extract_price_from_response(memory_response)
        except (KeyError, IndexError, ValueError, BotoCoreError, ClientError) as e:
            print(f"Pricing API error for Fargate in {region}: {e}")

        # Use defaults if API fails
        return (
            cpu_price if cpu_price is not None else Decimal("0.04048"),
            memory_price if memory_price is not None else Decimal("0.004445"),
        )

    def _get_ecs_hourly_cost(
        self, task_definition: str, running_count: int, region: str
    ) -> Decimal:
        """Get ECS Fargate hourly cost based on task definition.

        Uses Pricing API for vCPU and memory rates with fallback to defaults.
        Cost is multiplied by running_count (number of tasks).
        """
        try:
            ecs = boto3.client("ecs", region_name=region)
            response = ecs.describe_task_definition(taskDefinition=task_definition)
            task_def = response["taskDefinition"]

            # Fargate task definitions have cpu and memory at the task level
            # cpu is in units (256 = 0.25 vCPU, 1024 = 1 vCPU)
            cpu_units = int(task_def.get("cpu", "256"))
            cpu = Decimal(cpu_units) / 1024

            # memory is in MB
            memory_mb = int(task_def.get("memory", "512"))
            memory = Decimal(memory_mb) / 1024

            # Get prices from Pricing API (with fallback)
            cpu_rate, memory_rate = self._get_fargate_unit_prices(region)

            cpu_cost = cpu * cpu_rate
            memory_cost = memory * memory_rate

            return (cpu_cost + memory_cost) * running_count
        except (BotoCoreError, ClientError) as e:
            print(f"ECS pricing error for {task_definition}: {e}")
            return DEFAULT_ECS_FARGATE_HOURLY * running_count

    def _detect_lambda_spikes(self, resources: dict) -> list[LambdaSpike]:
        """
        Detect Lambda functions with abnormal usage spikes.

        Spike detection works by comparing two time windows:
        1. SHORT WINDOW (default 5 min): Recent activity rate
        2. BASELINE WINDOW (default 7 days): Normal activity rate

        If current_rate / baseline_rate >= spike_threshold, alert is triggered.

        Example:
            Baseline (7 days): 1,000 invocations = 0.1/min average
            Current (5 min):   180 invocations = 36/min
            Ratio: 36 / 0.1 = 360x
            Threshold: 10x
            Result: SPIKE DETECTED (360x > 10x)

        This catches runaway Lambda costs within minutes, not hours.
        """
        spikes: list[LambdaSpike] = []

        for func in resources["lambda"]:
            spike = self._check_lambda_spike(func["name"], func["region"], func["memory_mb"])
            if spike:
                spikes.append(spike)

        return spikes

    def _check_lambda_spike(
        self, function_name: str, region: str, memory_mb: int
    ) -> Optional[LambdaSpike]:
        """
        Check if a single Lambda function has a usage spike.

        Algorithm:
        1. Query CloudWatch for invocations in SHORT window (e.g., last 5 min)
        2. Query CloudWatch for invocations in BASELINE window (e.g., last 7 days)
        3. Calculate rate per minute for both windows
        4. Compute spike_ratio = current_rate / baseline_rate
        5. If spike_ratio >= threshold, return LambdaSpike with projected cost

        Rate calculation:
            current_rate = invocations_in_short_window / short_window_minutes
            baseline_rate = invocations_in_baseline_window / baseline_window_minutes

        Example with real numbers:
            Short window (5 min):   180 invocations -> 36/min
            Baseline (7 days):      10,080 invocations -> 0.1/min
            Spike ratio:            36 / 0.1 = 360x
            Threshold:              10x
            Result:                 SPIKE (360x >= 10x)

        Edge cases:
            - No baseline activity but current activity: ratio = 999 (new function)
            - No current activity: no spike (function is idle)
        """
        try:
            cw = boto3.client("cloudwatch", region_name=region)
            now = datetime.now(timezone.utc)

            # Get current invocation rate from short window (e.g., last 5 minutes)
            short_start = now - timedelta(minutes=self.lambda_spike_window_minutes)
            short_response = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=short_start,
                EndTime=now,
                Period=self.lambda_spike_window_minutes * 60,  # Period in seconds
                Statistics=["Sum"],
            )

            # Get baseline invocation rate from long window (e.g., last 7 days)
            baseline_start = now - timedelta(hours=self.lambda_baseline_hours)
            baseline_response = cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=baseline_start,
                EndTime=now,
                Period=self.lambda_baseline_hours * 3600,  # Entire baseline as one period
                Statistics=["Sum"],
            )

            # Extract invocation counts from responses
            current_invocations = Decimal("0")
            short_datapoints = short_response.get("Datapoints", [])
            if short_datapoints:
                current_invocations = Decimal(str(short_datapoints[0].get("Sum", 0)))

            baseline_invocations = Decimal("0")
            baseline_datapoints = baseline_response.get("Datapoints", [])
            if baseline_datapoints:
                baseline_invocations = Decimal(str(baseline_datapoints[0].get("Sum", 0)))

            # Calculate rates per minute for both windows
            current_rate = current_invocations / self.lambda_spike_window_minutes
            baseline_minutes = self.lambda_baseline_hours * 60
            baseline_rate = baseline_invocations / baseline_minutes

            # Calculate spike ratio (current rate vs baseline rate)
            if baseline_rate > 0:
                spike_ratio = current_rate / baseline_rate
            elif current_rate > 0:
                # No baseline but has current activity - new function or was idle
                spike_ratio = Decimal("999")
            else:
                # No activity at all
                return None

            # Check if spike exceeds threshold and calculate projected daily cost
            if spike_ratio >= self.lambda_spike_threshold:
                invocations_per_day = current_rate * 60 * 24
                memory_gb = Decimal(str(memory_mb)) / 1024
                # Assume 1 second duration for cost projection
                gb_seconds_per_day = memory_gb * invocations_per_day
                request_cost = invocations_per_day * LAMBDA_PRICE_PER_REQUEST
                compute_cost = gb_seconds_per_day * LAMBDA_PRICE_PER_GB_SECOND
                projected_daily_cost = request_cost + compute_cost

                return LambdaSpike(
                    function_name=function_name,
                    region=region,
                    current_rate=current_rate,
                    baseline_rate=baseline_rate,
                    spike_ratio=spike_ratio,
                    projected_daily_cost=projected_daily_cost,
                )

            return None

        except (BotoCoreError, ClientError) as e:
            print(f"Spike detection error for Lambda {function_name}: {e}")
            return None

    def _region_to_location(self, region: str) -> str:
        """Convert AWS region code to Pricing API location name.

        Uses SSM public parameters to dynamically fetch region names,
        with caching to avoid repeated API calls.
        """
        if region in self._region_locations:
            return self._region_locations[region]

        try:
            ssm = boto3.client("ssm", region_name="us-east-1")
            response = ssm.get_parameter(
                Name=f"/aws/service/global-infrastructure/regions/{region}/longName"
            )
            location = response["Parameter"]["Value"]
            self._region_locations[region] = location
            return location
        except (BotoCoreError, ClientError):
            return "US East (N. Virginia)"

    def _get_period_bounds(self) -> tuple[datetime, datetime]:
        """Get start and end of budget period.

        If budget_period_start and budget_period_end are set, parse them.
        Otherwise, default to current month (1st to last day).
        """
        now = datetime.now(timezone.utc)

        if self.budget_period_start and self.budget_period_end:
            # Parse explicit dates (YYYY-MM-DD format)
            start = datetime.strptime(self.budget_period_start, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            end = datetime.strptime(self.budget_period_end, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        else:
            # Default: current month
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            days_in_month = monthrange(now.year, now.month)[1]
            end = now.replace(day=days_in_month, hour=23, minute=59, second=59)

        return start, end

    def _hours_until_period_end(self) -> int:
        """Calculate remaining hours in current budget period."""
        now = datetime.now(timezone.utc)
        _, period_end = self._get_period_bounds()
        remaining = period_end - now
        return max(0, int(remaining.total_seconds() / 3600))

    def _determine_action(
        self, budget_percent: Decimal, actual_spend: Decimal
    ) -> tuple[str, list[int], bool]:
        """Determine action based on budget percentage and actual spend.

        Returns:
            tuple of (action, thresholds_breached, actual_exceeded)
        """
        breached = [t for t in self.alert_thresholds if budget_percent >= t]
        actual_exceeded = actual_spend > self.budget

        # If actual spend already exceeds budget, force stop_all immediately
        if actual_exceeded:
            return "stop_all", list(self.alert_thresholds), True

        if budget_percent >= self.auto_stop_threshold:
            return "stop_all", breached, False
        if breached:
            return "alert", breached, False
        return "ok", [], False

    def stop_all_resources(
        self, resources: dict[str, Any], dry_run: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        """Stop all running resources."""
        results: dict[str, list[dict[str, Any]]] = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [],
            "ecs": [],
        }

        # Stop EC2 instances
        for instance in resources["ec2"]:
            try:
                ec2 = boto3.client("ec2", region_name=instance["region"])
                if not dry_run:
                    ec2.stop_instances(InstanceIds=[instance["id"]])
                results["ec2"].append(
                    {
                        "id": instance["id"],
                        "status": "stopped" if not dry_run else "dry_run",
                    }
                )
            except (BotoCoreError, ClientError) as e:
                results["ec2"].append({"id": instance["id"], "status": "error", "error": str(e)})

        # Stop RDS instances
        for db in resources["rds"]:
            try:
                rds = boto3.client("rds", region_name=db["region"])
                if not dry_run:
                    rds.stop_db_instance(DBInstanceIdentifier=db["id"])
                results["rds"].append(
                    {"id": db["id"], "status": "stopped" if not dry_run else "dry_run"}
                )
            except (BotoCoreError, ClientError) as e:
                results["rds"].append({"id": db["id"], "status": "error", "error": str(e)})

        # Throttle Lambda functions (set concurrency to 0)
        for func in resources["lambda"]:
            try:
                lam = boto3.client("lambda", region_name=func["region"])
                # Check if already throttled
                try:
                    current = lam.get_function_concurrency(FunctionName=func["name"])
                    if current.get("ReservedConcurrentExecutions") == 0:
                        results["lambda"].append(
                            {"name": func["name"], "status": "already_throttled"}
                        )
                        continue
                except ClientError:
                    pass  # No concurrency set, proceed to throttle

                if not dry_run:
                    lam.put_function_concurrency(
                        FunctionName=func["name"], ReservedConcurrentExecutions=0
                    )
                results["lambda"].append(
                    {
                        "name": func["name"],
                        "status": "throttled" if not dry_run else "dry_run",
                    }
                )
            except (BotoCoreError, ClientError) as e:
                results["lambda"].append({"name": func["name"], "status": "error", "error": str(e)})

        # Pause App Runner services
        for svc in resources["apprunner"]:
            try:
                apprunner = boto3.client("apprunner", region_name=svc["region"])
                if not dry_run:
                    apprunner.pause_service(ServiceArn=svc["arn"])
                results["apprunner"].append(
                    {
                        "name": svc["name"],
                        "status": "paused" if not dry_run else "dry_run",
                    }
                )
            except (BotoCoreError, ClientError) as e:
                results["apprunner"].append(
                    {"name": svc["name"], "status": "error", "error": str(e)}
                )

        # Scale down ECS Fargate services (set desired count to 0)
        for svc in resources["ecs"]:
            try:
                ecs = boto3.client("ecs", region_name=svc["region"])
                if not dry_run:
                    ecs.update_service(
                        cluster=svc["cluster"],
                        service=svc["name"],
                        desiredCount=0,
                    )
                results["ecs"].append(
                    {
                        "name": svc["name"],
                        "status": "scaled_down" if not dry_run else "dry_run",
                    }
                )
            except (BotoCoreError, ClientError) as e:
                results["ecs"].append({"name": svc["name"], "status": "error", "error": str(e)})

        return results

    def _get_account_info(self) -> dict[str, str]:
        """Get AWS account info (ID, name, organization)."""
        if self._account_info is not None:
            return self._account_info

        info: dict[str, str] = {
            "account_id": "unknown",
            "account_name": "",
            "org_id": "",
            "org_management_id": "",
        }

        # Get account ID from STS
        try:
            sts = boto3.client("sts")
            identity = sts.get_caller_identity()
            info["account_id"] = identity.get("Account", "unknown")
        except (BotoCoreError, ClientError):
            pass

        # Get account alias (name) from IAM
        try:
            iam = boto3.client("iam")
            aliases = iam.list_account_aliases().get("AccountAliases", [])
            if aliases:
                info["account_name"] = aliases[0]
        except (BotoCoreError, ClientError):
            pass

        # Get organization info if in an org
        try:
            orgs = boto3.client("organizations")
            org = orgs.describe_organization().get("Organization", {})
            info["org_id"] = org.get("Id", "")
            info["org_management_id"] = org.get("MasterAccountId", "")
        except (BotoCoreError, ClientError):
            pass  # Not in an organization or no permission

        self._account_info = info
        return info

    def send_alert(
        self,
        status: BudgetStatus,
        stop_results: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> Optional[str]:
        """Send alert via SNS."""
        if not self.sns_topic_arn or not self.sns:
            return None

        # Build subject prefix for dry_run mode
        prefix = "[DRY RUN] " if dry_run else ""

        # Different subject based on whether actual spend exceeded budget
        if status.actual_exceeded:
            subject = f"{prefix}BUDGET EXCEEDED: ${status.actual_spend:.2f} spent > ${status.budget} budget"
        else:
            subject = f"{prefix}Budget Alert: {status.budget_percent:.0f}% of ${status.budget}"

        # Status line differs based on actual exceeded and dry_run
        if dry_run:
            status_line = "DRY RUN - Actions that WOULD be taken (no changes made)"
        elif status.actual_exceeded:
            status_line = "ACTUAL SPEND EXCEEDED - Immediate remediation triggered"
        else:
            status_line = status.action.upper()

        # Get account info
        account = self._get_account_info()
        account_line = f"Account ID: {account['account_id']}"
        if account["account_name"]:
            account_line += f" ({account['account_name']})"
        if account["org_id"]:
            account_line += f"\nOrganization: {account['org_id']}"
            if account["org_management_id"]:
                account_line += f" (Management: {account['org_management_id']})"

        message = f"""AWS Cost Guardian Alert

{account_line}

Status: {status_line}
Budget: ${status.budget}
Actual Spend: ${status.actual_spend:.2f}
Projected Total: ${status.projected_total:.2f}
Budget Used: {status.budget_percent:.1f}%

Running Resources:
- EC2 Instances: {len(status.resources["ec2"])}
- RDS Instances: {len(status.resources["rds"])}
- Lambda Functions: {len(status.resources["lambda"])}
- App Runner Services: {len(status.resources["apprunner"])}
- ECS Fargate Services: {len(status.resources["ecs"])}

Hourly Cost: ${status.hourly_cost:.2f}
Hours Until Period End: {status.remaining_hours}

Thresholds Breached: {", ".join(map(str, status.thresholds_breached))}%
"""

        if stop_results:
            message += f"""
Remediation Executed:
- EC2 Stopped: {len([r for r in stop_results["ec2"] if r["status"] == "stopped"])}
- RDS Stopped: {len([r for r in stop_results["rds"] if r["status"] == "stopped"])}
- Lambda Throttled: {len([r for r in stop_results["lambda"] if r["status"] == "throttled"])}
- App Runner Paused: {len([r for r in stop_results["apprunner"] if r["status"] == "paused"])}
- ECS Scaled Down: {len([r for r in stop_results["ecs"] if r["status"] == "scaled_down"])}
"""

        try:
            response = self.sns.publish(
                TopicArn=self.sns_topic_arn, Subject=subject[:100], Message=message
            )
            return response.get("MessageId")
        except (BotoCoreError, ClientError) as e:
            print(f"SNS error: {e}")
            return None

    def run(self, dry_run: bool = False) -> dict[str, Any]:
        """Main entry point - check budget and take action."""
        status = self.check_budget()
        stop_results: Optional[dict[str, list[dict[str, Any]]]] = None
        alert_sent = False

        if status.action == "stop_all":
            stop_results = self.stop_all_resources(status.resources, dry_run=dry_run)
            # Only send alert if at least one resource was actually changed (or would be in dry_run)
            actually_changed = (
                len([r for r in stop_results["ec2"] if r["status"] in ("stopped", "dry_run")])
                + len([r for r in stop_results["rds"] if r["status"] in ("stopped", "dry_run")])
                + len(
                    [r for r in stop_results["lambda"] if r["status"] in ("throttled", "dry_run")]
                )
                + len(
                    [r for r in stop_results["apprunner"] if r["status"] in ("paused", "dry_run")]
                )
                + len([r for r in stop_results["ecs"] if r["status"] in ("scaled_down", "dry_run")])
            )
            if actually_changed > 0:
                alert_sent = self.send_alert(status, stop_results, dry_run=dry_run) is not None

        elif status.action == "alert":
            alert_sent = self.send_alert(status) is not None

        return {
            "status": status,
            "alert_sent": alert_sent,
            "stop_results": stop_results,
        }
