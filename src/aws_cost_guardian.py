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

        # Clients (Cost Explorer is global, others are regional)
        self.ce = boto3.client("ce", region_name="us-east-1")
        self.sns = boto3.client("sns") if sns_topic_arn else None
        self._account_info: Optional[dict[str, str]] = None
        self._pricing_client: Optional[Any] = None

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
        )

    def check_budget(self) -> BudgetStatus:
        """Main budget check - returns current status and recommended action."""
        # 1. Get actual month-to-date spend
        actual_spend = self._get_actual_spend()

        # 2. Discover running resources across all regions
        resources = self._discover_resources()

        # 3. Calculate hourly cost of running resources
        hourly_cost = self._calculate_hourly_cost(resources)

        # 4. Project to end of month
        remaining_hours = self._hours_until_month_end()
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
        """Get total account spend from Cost Explorer (up to 13 months history).

        See: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html
        """
        now = datetime.now(timezone.utc)
        end = now.strftime("%Y-%m-%d")
        # Cost Explorer supports up to 13 months of historical data
        # Ref: https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html
        start = (now - timedelta(days=395)).strftime("%Y-%m-%d")

        try:
            response = self.ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            # Sum all months
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

        return total

    def _get_ec2_hourly_cost(self, instance_type: str, region: str) -> Decimal:
        """Get EC2 hourly cost. Uses fallback if Pricing API fails."""
        try:
            response = self._get_pricing_client().get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": self._region_to_location(region)},
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
                    {"Type": "TERM_MATCH", "Field": "location", "Value": self._region_to_location(region)},
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

            # STEP 1: Get current invocation rate from SHORT window
            # Example: Last 5 minutes of activity
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

            # STEP 2: Get baseline invocation rate from LONG window
            # Example: Last 7 days (168 hours) of activity
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

            # STEP 3: Extract invocation counts from responses
            current_invocations = Decimal("0")
            short_datapoints = short_response.get("Datapoints", [])
            if short_datapoints:
                current_invocations = Decimal(str(short_datapoints[0].get("Sum", 0)))

            baseline_invocations = Decimal("0")
            baseline_datapoints = baseline_response.get("Datapoints", [])
            if baseline_datapoints:
                baseline_invocations = Decimal(str(baseline_datapoints[0].get("Sum", 0)))

            # STEP 4: Calculate rates per minute
            # current_rate: invocations per minute in short window
            # baseline_rate: average invocations per minute over baseline period
            current_rate = current_invocations / self.lambda_spike_window_minutes
            baseline_minutes = self.lambda_baseline_hours * 60
            baseline_rate = baseline_invocations / baseline_minutes

            # STEP 5: Calculate spike ratio
            if baseline_rate > 0:
                # Normal case: compare current to baseline
                spike_ratio = current_rate / baseline_rate
            elif current_rate > 0:
                # Edge case: no baseline but has current activity
                # This is a new function or was completely idle - flag as potential spike
                spike_ratio = Decimal("999")
            else:
                # No activity at all - nothing to detect
                return None

            # STEP 6: Check if spike exceeds threshold
            if spike_ratio >= self.lambda_spike_threshold:
                # STEP 7: Calculate projected daily cost at current rate
                # This shows how much it would cost if spike continues for 24h
                invocations_per_day = current_rate * 60 * 24

                # Estimate compute cost (assume 1 second duration if no baseline)
                avg_duration_ms = Decimal("1000")  # Conservative 1s estimate
                memory_gb = Decimal(str(memory_mb)) / 1024
                gb_seconds_per_day = (avg_duration_ms / 1000) * memory_gb * invocations_per_day

                # Lambda pricing:
                # - $0.0000002 per request ($0.20 per 1M)
                # - $0.0000166667 per GB-second
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

    @staticmethod
    def _region_to_location(region: str) -> str:
        """Convert AWS region code to Pricing API location name."""
        locations = {
            "us-east-1": "US East (N. Virginia)",
            "us-east-2": "US East (Ohio)",
            "us-west-1": "US West (N. California)",
            "us-west-2": "US West (Oregon)",
            "eu-west-1": "EU (Ireland)",
            "eu-west-2": "EU (London)",
            "eu-west-3": "EU (Paris)",
            "eu-central-1": "EU (Frankfurt)",
            "ap-northeast-1": "Asia Pacific (Tokyo)",
            "ap-southeast-1": "Asia Pacific (Singapore)",
            "ap-southeast-2": "Asia Pacific (Sydney)",
        }
        return locations.get(region, "US East (N. Virginia)")

    def _hours_until_month_end(self) -> int:
        """Calculate remaining hours in current month."""
        now = datetime.now(timezone.utc)
        days_in_month = monthrange(now.year, now.month)[1]
        end_of_month = now.replace(day=days_in_month, hour=23, minute=59, second=59)
        remaining = end_of_month - now
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
        results: dict[str, list[dict[str, Any]]] = {"ec2": [], "rds": [], "lambda": []}

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

Hourly Cost: ${status.hourly_cost:.2f}
Hours Until Month End: {status.remaining_hours}

Thresholds Breached: {", ".join(map(str, status.thresholds_breached))}%
"""

        if stop_results:
            message += f"""
Remediation Executed:
- EC2 Stopped: {len([r for r in stop_results["ec2"] if r["status"] == "stopped"])}
- RDS Stopped: {len([r for r in stop_results["rds"] if r["status"] == "stopped"])}
- Lambda Throttled: {len([r for r in stop_results["lambda"] if r["status"] == "throttled"])}
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
                + len([r for r in stop_results["lambda"] if r["status"] in ("throttled", "dry_run")])
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
