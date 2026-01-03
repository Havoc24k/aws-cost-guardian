"""
Cost Projection Engine
Calculates immediate projected costs from CloudWatch metrics and evaluates remediation rules.
"""

import boto3
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Callable, Any, Optional
from decimal import Decimal
import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Default fallback hourly costs when Pricing API fails
DEFAULT_EC2_HOURLY_COST = os.environ.get('DEFAULT_EC2_HOURLY_COST', '0.10')
DEFAULT_RDS_HOURLY_COST = os.environ.get('DEFAULT_RDS_HOURLY_COST', '0.15')


@dataclass
class CostRule:
    """Defines a cost projection rule with threshold and remediation action."""
    rule_id: str
    metric_namespace: str
    metric_name: str
    dimensions: list[dict]
    lookback_seconds: int  # How far back to sample metrics
    projection_seconds: int  # How far forward to project costs
    unit_cost: Decimal  # Cost per unit (invocation, GB-second, request, etc.)
    threshold: Decimal  # IPC threshold that triggers remediation
    remediation_action: str  # Action identifier
    remediation_params: dict = field(default_factory=dict)
    statistic: str = "Sum"  # CloudWatch statistic: Sum, Average, Maximum, etc.
    period: int = 60  # CloudWatch period in seconds
    pricing_model: str = "per_unit"  # "per_unit" (Lambda, SQS) or "hourly" (EC2, RDS)
    instance_filter: dict = field(default_factory=dict)  # Filters for EC2/RDS instance discovery
    fallback_hourly_cost: Optional[Decimal] = None  # Optional rule-level fallback for hourly pricing


@dataclass
class CostProjection:
    """Result of cost projection calculation."""
    rule_id: str
    metric_value: float
    rate_per_second: float
    projected_cost: Decimal
    threshold: Decimal
    breach: bool
    timestamp: datetime
    raw_datapoints: list


class CostEngine:
    """Ingests CloudWatch metrics and calculates cost projections."""

    def __init__(self, region: str = None):
        self.region = region
        self.cloudwatch = boto3.client('cloudwatch', region_name=region)
        self.pricing = boto3.client('pricing', region_name='us-east-1')  # Pricing API only in us-east-1
        self.ec2 = boto3.client('ec2', region_name=region)
        self.rds = boto3.client('rds', region_name=region)
        self._price_cache: dict[str, Decimal] = {}
    
    def get_lambda_invocation_cost(self, region: str = 'us-east-1') -> Decimal:
        """
        Fetch Lambda invocation cost from AWS Pricing API.
        Returns cost per invocation (not per million).
        """
        cache_key = f"lambda_invocation_{region}"
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]
        
        try:
            response = self.pricing.get_products(
                ServiceCode='AWSLambda',
                Filters=[
                    {'Type': 'TERM_MATCH', 'Field': 'regionCode', 'Value': region},
                    {'Type': 'TERM_MATCH', 'Field': 'group', 'Value': 'AWS-Lambda-Requests'},
                ],
                MaxResults=1
            )
            
            if response['PriceList']:
                price_item = json.loads(response['PriceList'][0])
                terms = price_item['terms']['OnDemand']
                for term in terms.values():
                    for price_dimension in term['priceDimensions'].values():
                        # Price is per 1 million requests
                        price_per_million = Decimal(price_dimension['pricePerUnit']['USD'])
                        cost_per_invocation = price_per_million / Decimal('1000000')
                        self._price_cache[cache_key] = cost_per_invocation
                        return cost_per_invocation
            
            # Fallback to known price if API fails
            fallback = Decimal('0.0000002')  # $0.20 per million
            self._price_cache[cache_key] = fallback
            return fallback
            
        except Exception as e:
            logger.warning(f"Failed to fetch Lambda pricing: {e}. Using fallback.")
            fallback = Decimal('0.0000002')
            self._price_cache[cache_key] = fallback
            return fallback

    def get_ec2_hourly_cost(self, instance_type: str, region: str = None,
                            fallback: Optional[Decimal] = None) -> Decimal:
        """
        Fetch EC2 On-Demand hourly cost from AWS Pricing API.

        Args:
            instance_type: EC2 instance type (e.g., "t3.micro", "m5.large")
            region: AWS region (defaults to engine's region)
            fallback: Optional fallback cost if API fails

        Returns:
            Decimal cost per hour
        """
        region = region or self.region or 'us-east-1'
        cache_key = f"ec2_{instance_type}_{region}"
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]

        # Region code to location name mapping for Pricing API
        region_name_map = {
            'us-east-1': 'US East (N. Virginia)',
            'us-east-2': 'US East (Ohio)',
            'us-west-1': 'US West (N. California)',
            'us-west-2': 'US West (Oregon)',
            'eu-west-1': 'EU (Ireland)',
            'eu-west-2': 'EU (London)',
            'eu-central-1': 'EU (Frankfurt)',
            'ap-southeast-1': 'Asia Pacific (Singapore)',
            'ap-southeast-2': 'Asia Pacific (Sydney)',
            'ap-northeast-1': 'Asia Pacific (Tokyo)',
        }

        try:
            response = self.pricing.get_products(
                ServiceCode='AmazonEC2',
                Filters=[
                    {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': instance_type},
                    {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': region_name_map.get(region, 'US East (N. Virginia)')},
                    {'Type': 'TERM_MATCH', 'Field': 'operatingSystem', 'Value': 'Linux'},
                    {'Type': 'TERM_MATCH', 'Field': 'tenancy', 'Value': 'Shared'},
                    {'Type': 'TERM_MATCH', 'Field': 'preInstalledSw', 'Value': 'NA'},
                    {'Type': 'TERM_MATCH', 'Field': 'capacitystatus', 'Value': 'Used'},
                ],
                MaxResults=1
            )

            if response['PriceList']:
                price_item = json.loads(response['PriceList'][0])
                terms = price_item['terms']['OnDemand']
                for term in terms.values():
                    for price_dimension in term['priceDimensions'].values():
                        hourly_cost = Decimal(price_dimension['pricePerUnit']['USD'])
                        self._price_cache[cache_key] = hourly_cost
                        return hourly_cost

            # Use fallback if API returns empty
            fallback_cost = fallback or Decimal(DEFAULT_EC2_HOURLY_COST)
            self._price_cache[cache_key] = fallback_cost
            return fallback_cost

        except Exception as e:
            logger.warning(f"Failed to fetch EC2 pricing for {instance_type}: {e}. Using fallback.")
            fallback_cost = fallback or Decimal(DEFAULT_EC2_HOURLY_COST)
            self._price_cache[cache_key] = fallback_cost
            return fallback_cost

    def get_rds_hourly_cost(self, instance_class: str, engine: str = 'mysql',
                            region: str = None, fallback: Optional[Decimal] = None) -> Decimal:
        """
        Fetch RDS On-Demand hourly cost from AWS Pricing API.

        Args:
            instance_class: RDS instance class (e.g., "db.t3.micro", "db.m5.large")
            engine: Database engine ("mysql", "postgres", "mariadb", etc.)
            region: AWS region (defaults to engine's region)
            fallback: Optional fallback cost if API fails

        Returns:
            Decimal cost per hour
        """
        region = region or self.region or 'us-east-1'
        cache_key = f"rds_{instance_class}_{engine}_{region}"
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]

        region_name_map = {
            'us-east-1': 'US East (N. Virginia)',
            'us-east-2': 'US East (Ohio)',
            'us-west-1': 'US West (N. California)',
            'us-west-2': 'US West (Oregon)',
            'eu-west-1': 'EU (Ireland)',
            'eu-west-2': 'EU (London)',
            'eu-central-1': 'EU (Frankfurt)',
            'ap-southeast-1': 'Asia Pacific (Singapore)',
            'ap-southeast-2': 'Asia Pacific (Sydney)',
            'ap-northeast-1': 'Asia Pacific (Tokyo)',
        }

        # Map common engine names to Pricing API values
        engine_map = {
            'mysql': 'MySQL',
            'postgres': 'PostgreSQL',
            'postgresql': 'PostgreSQL',
            'mariadb': 'MariaDB',
            'oracle': 'Oracle',
            'sqlserver': 'SQL Server',
        }

        try:
            response = self.pricing.get_products(
                ServiceCode='AmazonRDS',
                Filters=[
                    {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': instance_class},
                    {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': region_name_map.get(region, 'US East (N. Virginia)')},
                    {'Type': 'TERM_MATCH', 'Field': 'databaseEngine', 'Value': engine_map.get(engine.lower(), engine)},
                    {'Type': 'TERM_MATCH', 'Field': 'deploymentOption', 'Value': 'Single-AZ'},
                ],
                MaxResults=1
            )

            if response['PriceList']:
                price_item = json.loads(response['PriceList'][0])
                terms = price_item['terms']['OnDemand']
                for term in terms.values():
                    for price_dimension in term['priceDimensions'].values():
                        hourly_cost = Decimal(price_dimension['pricePerUnit']['USD'])
                        self._price_cache[cache_key] = hourly_cost
                        return hourly_cost

            fallback_cost = fallback or Decimal(DEFAULT_RDS_HOURLY_COST)
            self._price_cache[cache_key] = fallback_cost
            return fallback_cost

        except Exception as e:
            logger.warning(f"Failed to fetch RDS pricing for {instance_class}: {e}. Using fallback.")
            fallback_cost = fallback or Decimal(DEFAULT_RDS_HOURLY_COST)
            self._price_cache[cache_key] = fallback_cost
            return fallback_cost

    def get_running_ec2_instances(self, filters: Optional[list] = None) -> list[dict]:
        """
        Get all running EC2 instances matching filters.

        Args:
            filters: List of EC2 filter dicts, e.g.:
                [{'Name': 'tag:Environment', 'Values': ['production']}]

        Returns:
            List of dicts with instance_id, instance_type, launch_time, tags
        """
        base_filters = [{'Name': 'instance-state-name', 'Values': ['running']}]
        if filters:
            base_filters.extend(filters)

        instances = []
        paginator = self.ec2.get_paginator('describe_instances')

        for page in paginator.paginate(Filters=base_filters):
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    instances.append({
                        'instance_id': instance['InstanceId'],
                        'instance_type': instance['InstanceType'],
                        'launch_time': instance['LaunchTime'],
                        'tags': {t['Key']: t['Value'] for t in instance.get('Tags', [])}
                    })

        return instances

    def get_running_rds_instances(self, filters: Optional[dict] = None) -> list[dict]:
        """
        Get all running RDS instances.

        Args:
            filters: Dict with optional keys: engines (list of engine names)

        Returns:
            List of dicts with db_instance_id, instance_class, engine, tags
        """
        instances = []
        paginator = self.rds.get_paginator('describe_db_instances')

        for page in paginator.paginate():
            for db in page['DBInstances']:
                if db['DBInstanceStatus'] == 'available':
                    instance_data = {
                        'db_instance_id': db['DBInstanceIdentifier'],
                        'instance_class': db['DBInstanceClass'],
                        'engine': db['Engine'],
                        'tags': {}
                    }

                    # Apply engine filter if provided
                    if filters and 'engines' in filters:
                        if db['Engine'].lower() not in [e.lower() for e in filters['engines']]:
                            continue

                    instances.append(instance_data)

        return instances

    def get_metric_data(self, rule: CostRule) -> list[dict]:
        """Fetch CloudWatch metric datapoints for the lookback period."""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(seconds=rule.lookback_seconds)
        
        response = self.cloudwatch.get_metric_statistics(
            Namespace=rule.metric_namespace,
            MetricName=rule.metric_name,
            Dimensions=rule.dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=rule.period,
            Statistics=[rule.statistic]
        )
        
        return sorted(response['Datapoints'], key=lambda x: x['Timestamp'])
    
    def calculate_projection(self, rule: CostRule) -> CostProjection:
        """
        Calculate immediate projected cost for a rule.

        Supports two pricing models:
        - per_unit: rate * projection_seconds * unit_cost (Lambda, SQS, etc.)
        - hourly: instance_count * (projection_seconds / 3600) * hourly_cost (EC2, RDS)
        """
        if rule.pricing_model == 'hourly':
            return self._calculate_hourly_projection(rule)
        return self._calculate_per_unit_projection(rule)

    def _calculate_per_unit_projection(self, rule: CostRule) -> CostProjection:
        """Calculate projected cost for per-unit pricing (Lambda, SQS, DynamoDB)."""
        datapoints = self.get_metric_data(rule)

        if not datapoints:
            return CostProjection(
                rule_id=rule.rule_id,
                metric_value=0,
                rate_per_second=0,
                projected_cost=Decimal('0'),
                threshold=rule.threshold,
                breach=False,
                timestamp=datetime.now(timezone.utc),
                raw_datapoints=[]
            )

        # Sum all datapoints in lookback window
        total_value = sum(dp[rule.statistic] for dp in datapoints)

        # Calculate rate per second
        rate_per_second = total_value / rule.lookback_seconds

        # Project cost
        projected_units = rate_per_second * rule.projection_seconds
        projected_cost = Decimal(str(projected_units)) * rule.unit_cost

        breach = projected_cost > rule.threshold

        return CostProjection(
            rule_id=rule.rule_id,
            metric_value=total_value,
            rate_per_second=rate_per_second,
            projected_cost=projected_cost,
            threshold=rule.threshold,
            breach=breach,
            timestamp=datetime.now(timezone.utc),
            raw_datapoints=datapoints
        )

    def _calculate_hourly_projection(self, rule: CostRule) -> CostProjection:
        """
        Calculate projected cost for hourly-billed resources (EC2, RDS).

        Formula: instance_count * (projection_seconds / 3600) * hourly_cost
        """
        instances = []
        total_hourly_cost = Decimal('0')
        fallback = rule.fallback_hourly_cost

        if rule.metric_namespace == 'AWS/EC2':
            ec2_filters = rule.instance_filter.get('ec2_filters', [])
            instances = self.get_running_ec2_instances(filters=ec2_filters)

            for inst in instances:
                hourly_cost = self.get_ec2_hourly_cost(
                    inst['instance_type'],
                    fallback=fallback
                )
                total_hourly_cost += hourly_cost

        elif rule.metric_namespace == 'AWS/RDS':
            rds_filters = rule.instance_filter.get('rds_filters', {})
            instances = self.get_running_rds_instances(filters=rds_filters)

            for inst in instances:
                hourly_cost = self.get_rds_hourly_cost(
                    inst['instance_class'],
                    inst['engine'],
                    fallback=fallback
                )
                total_hourly_cost += hourly_cost

        # Project forward
        projection_hours = Decimal(str(rule.projection_seconds)) / Decimal('3600')
        projected_cost = total_hourly_cost * projection_hours

        # Calculate effective rate per second for consistency
        rate_per_second = float(total_hourly_cost) / 3600 if total_hourly_cost else 0

        breach = projected_cost > rule.threshold

        return CostProjection(
            rule_id=rule.rule_id,
            metric_value=len(instances),
            rate_per_second=rate_per_second,
            projected_cost=projected_cost,
            threshold=rule.threshold,
            breach=breach,
            timestamp=datetime.now(timezone.utc),
            raw_datapoints=[{'instances': instances, 'total_hourly_cost': str(total_hourly_cost)}]
        )


class RemediationExecutor:
    """Executes remediation actions when cost thresholds are breached."""

    def __init__(self, region: str = None):
        self.lambda_client = boto3.client('lambda', region_name=region)
        self.sns = boto3.client('sns', region_name=region)
        self.autoscaling = boto3.client('application-autoscaling', region_name=region)
        self.sfn = boto3.client('stepfunctions', region_name=region)
        self.ec2 = boto3.client('ec2', region_name=region)
        self.rds = boto3.client('rds', region_name=region)

        self._actions: dict[str, Callable] = {
            'throttle_lambda': self._throttle_lambda,
            'disable_lambda': self._disable_lambda,
            'scale_down': self._scale_down,
            'notify_sns': self._notify_sns,
            'start_step_function': self._start_step_function,
            'set_reserved_concurrency': self._set_reserved_concurrency,
            'stop_ec2': self._stop_ec2,
            'stop_rds': self._stop_rds,
        }
    
    def execute(self, action: str, params: dict, projection: CostProjection) -> dict:
        """Execute a remediation action."""
        if action not in self._actions:
            raise ValueError(f"Unknown remediation action: {action}")
        
        logger.info(f"Executing remediation: {action} for rule {projection.rule_id}")
        return self._actions[action](params, projection)
    
    def _throttle_lambda(self, params: dict, projection: CostProjection) -> dict:
        """Set Lambda reserved concurrency to throttle invocations."""
        function_name = params['function_name']
        concurrency = params.get('concurrency', 1)
        
        response = self.lambda_client.put_function_concurrency(
            FunctionName=function_name,
            ReservedConcurrentExecutions=concurrency
        )
        
        return {
            'action': 'throttle_lambda',
            'function_name': function_name,
            'concurrency_set': response['ReservedConcurrentExecutions']
        }
    
    def _disable_lambda(self, params: dict, projection: CostProjection) -> dict:
        """Disable Lambda by setting concurrency to 0."""
        function_name = params['function_name']
        
        response = self.lambda_client.put_function_concurrency(
            FunctionName=function_name,
            ReservedConcurrentExecutions=0
        )
        
        return {
            'action': 'disable_lambda',
            'function_name': function_name,
            'disabled': True
        }
    
    def _set_reserved_concurrency(self, params: dict, projection: CostProjection) -> dict:
        """Set specific reserved concurrency on Lambda."""
        function_name = params['function_name']
        concurrency = params['concurrency']
        
        response = self.lambda_client.put_function_concurrency(
            FunctionName=function_name,
            ReservedConcurrentExecutions=concurrency
        )
        
        return {
            'action': 'set_reserved_concurrency',
            'function_name': function_name,
            'concurrency': response['ReservedConcurrentExecutions']
        }
    
    def _scale_down(self, params: dict, projection: CostProjection) -> dict:
        """Scale down an Application Auto Scaling target."""
        service_namespace = params['service_namespace']
        resource_id = params['resource_id']
        scalable_dimension = params['scalable_dimension']
        min_capacity = params.get('min_capacity', 0)
        max_capacity = params.get('max_capacity', 1)
        
        response = self.autoscaling.register_scalable_target(
            ServiceNamespace=service_namespace,
            ResourceId=resource_id,
            ScalableDimension=scalable_dimension,
            MinCapacity=min_capacity,
            MaxCapacity=max_capacity
        )
        
        return {
            'action': 'scale_down',
            'resource_id': resource_id,
            'min_capacity': min_capacity,
            'max_capacity': max_capacity
        }
    
    def _notify_sns(self, params: dict, projection: CostProjection) -> dict:
        """Send notification to SNS topic."""
        topic_arn = params['topic_arn']
        
        message = {
            'rule_id': projection.rule_id,
            'projected_cost': str(projection.projected_cost),
            'threshold': str(projection.threshold),
            'rate_per_second': projection.rate_per_second,
            'timestamp': projection.timestamp.isoformat(),
            'breach': projection.breach
        }
        
        response = self.sns.publish(
            TopicArn=topic_arn,
            Subject=f"Cost Alert: {projection.rule_id}",
            Message=json.dumps(message, indent=2)
        )
        
        return {
            'action': 'notify_sns',
            'message_id': response['MessageId']
        }
    
    def _start_step_function(self, params: dict, projection: CostProjection) -> dict:
        """Start a Step Functions state machine for complex remediation workflows."""
        state_machine_arn = params['state_machine_arn']
        
        input_data = {
            'rule_id': projection.rule_id,
            'projected_cost': str(projection.projected_cost),
            'threshold': str(projection.threshold),
            'rate_per_second': projection.rate_per_second,
            'timestamp': projection.timestamp.isoformat(),
            'additional_params': params.get('additional_params', {})
        }
        
        response = self.sfn.start_execution(
            stateMachineArn=state_machine_arn,
            input=json.dumps(input_data)
        )

        return {
            'action': 'start_step_function',
            'execution_arn': response['executionArn']
        }

    def _stop_ec2(self, params: dict, projection: CostProjection) -> dict:
        """
        Stop EC2 instances.

        Params:
            instance_ids: List of instance IDs to stop (optional)
            notify_before: bool - Send SNS notification before stopping
            topic_arn: SNS topic for notifications
        """
        instance_ids = params.get('instance_ids', [])

        # If no explicit IDs provided, get from projection data
        if not instance_ids and projection.raw_datapoints:
            instances_data = projection.raw_datapoints[0].get('instances', [])
            instance_ids = [i['instance_id'] for i in instances_data]

        if not instance_ids:
            return {'action': 'stop_ec2', 'result': 'no_instances_to_stop', 'count': 0}

        # Pre-notification
        if params.get('notify_before') and params.get('topic_arn'):
            self.sns.publish(
                TopicArn=params['topic_arn'],
                Subject=f"Cost Guardian: Stopping {len(instance_ids)} EC2 instances",
                Message=json.dumps({
                    'rule_id': projection.rule_id,
                    'action': 'stop_ec2',
                    'instance_ids': instance_ids,
                    'projected_cost': str(projection.projected_cost),
                    'threshold': str(projection.threshold),
                }, indent=2)
            )

        # Stop instances
        response = self.ec2.stop_instances(InstanceIds=instance_ids)
        stopped = [i['InstanceId'] for i in response['StoppingInstances']]

        logger.info(f"Stopped {len(stopped)} EC2 instances: {stopped}")

        return {
            'action': 'stop_ec2',
            'stopped_instances': stopped,
            'count': len(stopped)
        }

    def _stop_rds(self, params: dict, projection: CostProjection) -> dict:
        """
        Stop RDS instances.

        Params:
            db_instance_ids: List of DB instance identifiers to stop (optional)
            notify_before: bool - Send SNS notification before stopping
            topic_arn: SNS topic for notifications
        """
        db_instance_ids = params.get('db_instance_ids', [])

        # If no explicit IDs provided, get from projection data
        if not db_instance_ids and projection.raw_datapoints:
            instances_data = projection.raw_datapoints[0].get('instances', [])
            db_instance_ids = [i['db_instance_id'] for i in instances_data]

        if not db_instance_ids:
            return {'action': 'stop_rds', 'result': 'no_instances_to_stop', 'count': 0}

        # Pre-notification
        if params.get('notify_before') and params.get('topic_arn'):
            self.sns.publish(
                TopicArn=params['topic_arn'],
                Subject=f"Cost Guardian: Stopping {len(db_instance_ids)} RDS instances",
                Message=json.dumps({
                    'rule_id': projection.rule_id,
                    'action': 'stop_rds',
                    'db_instance_ids': db_instance_ids,
                    'projected_cost': str(projection.projected_cost),
                    'threshold': str(projection.threshold),
                }, indent=2)
            )

        stopped = []
        errors = []

        for db_id in db_instance_ids:
            try:
                self.rds.stop_db_instance(DBInstanceIdentifier=db_id)
                stopped.append(db_id)
                logger.info(f"Stopped RDS instance: {db_id}")
            except Exception as e:
                logger.error(f"Failed to stop RDS instance {db_id}: {e}")
                errors.append({'db_instance_id': db_id, 'error': str(e)})

        return {
            'action': 'stop_rds',
            'stopped_instances': stopped,
            'errors': errors if errors else None,
            'count': len(stopped)
        }


class CostGuardian:
    """
    Main orchestrator that combines cost engine and remediation executor.
    Evaluates all rules and triggers remediation on breaches.
    """
    
    def __init__(self, region: str = None):
        self.engine = CostEngine(region)
        self.executor = RemediationExecutor(region)
        self.rules: list[CostRule] = []
    
    def add_rule(self, rule: CostRule):
        """Register a cost rule."""
        self.rules.append(rule)
    
    def load_rules_from_config(self, config: dict):
        """Load rules from a configuration dictionary."""
        for rule_config in config.get('rules', []):
            # Parse fallback_hourly_cost if provided
            fallback_cost = None
            if 'fallback_hourly_cost' in rule_config:
                fallback_cost = Decimal(str(rule_config['fallback_hourly_cost']))

            rule = CostRule(
                rule_id=rule_config['rule_id'],
                metric_namespace=rule_config['metric_namespace'],
                metric_name=rule_config['metric_name'],
                dimensions=rule_config.get('dimensions', []),
                lookback_seconds=rule_config['lookback_seconds'],
                projection_seconds=rule_config['projection_seconds'],
                unit_cost=Decimal(str(rule_config.get('unit_cost', '0'))),
                threshold=Decimal(str(rule_config['threshold'])),
                remediation_action=rule_config['remediation_action'],
                remediation_params=rule_config.get('remediation_params', {}),
                statistic=rule_config.get('statistic', 'Sum'),
                period=rule_config.get('period', 60),
                pricing_model=rule_config.get('pricing_model', 'per_unit'),
                instance_filter=rule_config.get('instance_filter', {}),
                fallback_hourly_cost=fallback_cost,
            )
            self.add_rule(rule)
    
    def evaluate(self, dry_run: bool = False) -> list[dict]:
        """
        Evaluate all rules and execute remediation for breaches.
        
        Args:
            dry_run: If True, only calculate projections without executing remediation.
        
        Returns:
            List of evaluation results with projections and remediation outcomes.
        """
        results = []
        
        for rule in self.rules:
            projection = self.engine.calculate_projection(rule)
            
            result = {
                'rule_id': rule.rule_id,
                'projection': {
                    'metric_value': projection.metric_value,
                    'rate_per_second': projection.rate_per_second,
                    'projected_cost': str(projection.projected_cost),
                    'threshold': str(projection.threshold),
                    'breach': projection.breach,
                    'timestamp': projection.timestamp.isoformat()
                },
                'remediation': None
            }
            
            if projection.breach and not dry_run:
                try:
                    remediation_result = self.executor.execute(
                        rule.remediation_action,
                        rule.remediation_params,
                        projection
                    )
                    result['remediation'] = remediation_result
                except Exception as e:
                    logger.error(f"Remediation failed for {rule.rule_id}: {e}")
                    result['remediation'] = {'error': str(e)}
            
            results.append(result)
        
        return results
