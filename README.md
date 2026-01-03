# Cost Guardian

Real-time AWS cost projection and automated remediation system. Monitors CloudWatch metrics, calculates immediate projected costs (IPC), and executes remediation actions when thresholds breach.

**Supported Services:** Lambda, EC2, RDS, API Gateway, DynamoDB, SQS, Kinesis

## Architecture

```
CloudWatch Events (1min) ──► Lambda (Cost Guardian) ──► CloudWatch Metrics / EC2 / RDS APIs
                                      │
                                      ▼
                              Cost Projection Engine
                                      │
                              ┌───────┴───────┐
                              │  Rule Engine  │
                              └───────┬───────┘
                                      │
                    ┌─────────┬───────┼───────┬─────────┐
                    ▼         ▼       ▼       ▼         ▼
               Throttle    Stop    Stop    SNS      Step Functions
               Lambda      EC2     RDS     Alert    (Complex Flow)
```

## Pricing Models

### Per-Unit (Lambda, SQS, DynamoDB, API Gateway)
```
rate_per_second = metric_sum_in_lookback / lookback_seconds
projected_cost = rate_per_second * projection_seconds * unit_cost
```

### Hourly (EC2, RDS)
```
total_hourly_cost = sum(hourly_cost for each running instance)
projected_cost = total_hourly_cost * (projection_seconds / 3600)
```

## Quick Start

### 1. Configure Rules

Edit `rules.json` or generate templates:

```bash
# Generate rule templates
python cli.py generate --service lambda
python cli.py generate --service ec2
python cli.py generate --service rds
```

**Lambda example (per-unit pricing):**
```json
{
  "rule_id": "my-lambda-guard",
  "metric_namespace": "AWS/Lambda",
  "metric_name": "Invocations",
  "dimensions": [{"Name": "FunctionName", "Value": "my-function"}],
  "lookback_seconds": 120,
  "projection_seconds": 600,
  "unit_cost": "0.0000002",
  "threshold": "5.00",
  "remediation_action": "throttle_lambda",
  "remediation_params": {
    "function_name": "my-function",
    "concurrency": 10
  }
}
```

**EC2 example (hourly pricing):**
```json
{
  "rule_id": "ec2-dev-cost-limit",
  "metric_namespace": "AWS/EC2",
  "metric_name": "CPUUtilization",
  "dimensions": [],
  "lookback_seconds": 300,
  "projection_seconds": 3600,
  "pricing_model": "hourly",
  "threshold": "25.00",
  "fallback_hourly_cost": "0.10",
  "instance_filter": {
    "ec2_filters": [
      {"Name": "tag:Environment", "Values": ["development"]}
    ]
  },
  "remediation_action": "stop_ec2",
  "remediation_params": {
    "notify_before": true,
    "topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-alerts"
  }
}
```

**RDS example (hourly pricing):**
```json
{
  "rule_id": "rds-idle-detection",
  "metric_namespace": "AWS/RDS",
  "metric_name": "CPUUtilization",
  "dimensions": [],
  "lookback_seconds": 300,
  "projection_seconds": 3600,
  "pricing_model": "hourly",
  "threshold": "15.00",
  "fallback_hourly_cost": "0.15",
  "instance_filter": {
    "rds_filters": {}
  },
  "remediation_action": "stop_rds",
  "remediation_params": {
    "notify_before": true,
    "topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-alerts"
  }
}
```

### 2. Deploy

```bash
cd cost-guardian
terraform init
terraform apply -var="alert_email=ops@example.com"
```

### 3. Test

```bash
# Validate configuration
python cli.py validate --config rules.json

# Simulate projection
python cli.py simulate --rate 1000 --unit-cost 0.0000002 --threshold 1.00 \
  --lookback 120 --projection 600

# Dry-run against live metrics
python cli.py evaluate --config rules.json --dry-run
```

## Rule Configuration

| Field | Type | Description |
|-------|------|-------------|
| `rule_id` | string | Unique identifier |
| `metric_namespace` | string | CloudWatch namespace (AWS/Lambda, AWS/EC2, AWS/RDS, etc.) |
| `metric_name` | string | Metric name (Invocations, CPUUtilization, etc.) |
| `dimensions` | list | CloudWatch dimensions to filter metric (optional for EC2/RDS) |
| `lookback_seconds` | int | How far back to sample (minimum 60) |
| `projection_seconds` | int | How far forward to project |
| `pricing_model` | string | "per_unit" (default) or "hourly" for EC2/RDS |
| `unit_cost` | decimal | Cost per unit - required for per_unit pricing |
| `fallback_hourly_cost` | decimal | Fallback hourly cost when Pricing API fails (for hourly pricing) |
| `instance_filter` | dict | EC2/RDS instance filters (ec2_filters or rds_filters) |
| `threshold` | decimal | IPC threshold that triggers remediation |
| `remediation_action` | string | Action to execute on breach |
| `remediation_params` | dict | Parameters for the action |
| `statistic` | string | CloudWatch statistic (Sum, Average, Maximum) |
| `period` | int | CloudWatch period in seconds |

## Remediation Actions

### `throttle_lambda`
Sets Lambda reserved concurrency to limit invocations.

```json
{
  "remediation_action": "throttle_lambda",
  "remediation_params": {
    "function_name": "my-function",
    "concurrency": 10
  }
}
```

### `disable_lambda`
Sets Lambda concurrency to 0, stopping all invocations.

```json
{
  "remediation_action": "disable_lambda",
  "remediation_params": {
    "function_name": "my-function"
  }
}
```

### `notify_sns`
Publishes alert to SNS topic.

```json
{
  "remediation_action": "notify_sns",
  "remediation_params": {
    "topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-alerts"
  }
}
```

### `start_step_function`
Triggers Step Functions workflow for complex remediation.

```json
{
  "remediation_action": "start_step_function",
  "remediation_params": {
    "state_machine_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:Remediation",
    "additional_params": {
      "notify_slack": true
    }
  }
}
```

### `stop_ec2`
Stops EC2 instances matching the rule's instance filter.

```json
{
  "remediation_action": "stop_ec2",
  "remediation_params": {
    "notify_before": true,
    "topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-alerts"
  }
}
```

### `stop_rds`
Stops RDS instances matching the rule's instance filter.

```json
{
  "remediation_action": "stop_rds",
  "remediation_params": {
    "notify_before": true,
    "topic_arn": "arn:aws:sns:us-east-1:123456789012:cost-alerts"
  }
}
```

### `scale_down`
Reduces Application Auto Scaling capacity.

```json
{
  "remediation_action": "scale_down",
  "remediation_params": {
    "service_namespace": "dynamodb",
    "resource_id": "table/my-table",
    "scalable_dimension": "dynamodb:table:WriteCapacityUnits",
    "min_capacity": 1,
    "max_capacity": 5
  }
}
```

## AWS Pricing Reference

| Service | Metric | Approx Unit Cost |
|---------|--------|------------------|
| Lambda | Invocation | $0.0000002 |
| Lambda | GB-second | $0.0000166667 |
| API Gateway | Request | $0.0000035 |
| DynamoDB | WCU | $0.00000125 |
| DynamoDB | RCU | $0.00000025 |
| SQS | Request | $0.0000004 |
| Kinesis | PUT record | $0.000000014 |
| S3 | PUT request | $0.000005 |

Fetch live pricing:

```bash
python cli.py pricing --service lambda --region us-east-1
```

## Log-Based Detection

Beyond metrics, `log_detector.py` analyzes CloudWatch Logs for cost-impacting patterns:

- Lambda timeouts (wasted compute)
- Memory exceeded (OOM kills)
- Retry exhaustion (cascading failures)
- Throttling events
- Connection timeouts

## Step Functions Workflow

`remediation-workflow.asl.json` defines a severity-based remediation flow:

- **Critical ($100+)**: Immediate throttle + PagerDuty + incident log
- **High ($50+)**: Moderate throttle + Slack + approval gate
- **Medium ($10+)**: Alert + wait + re-evaluate
- **Low (<$10)**: Alert only

## Files

```
cost-guardian/
├── cost_engine.py          # Core projection engine
├── lambda_handler.py       # AWS Lambda entry point
├── log_detector.py         # Log-based anomaly detection
├── cli.py                  # Local testing CLI
├── rules.json              # Rule configuration
├── main.tf                 # Terraform infrastructure
└── remediation-workflow.asl.json  # Step Functions definition
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CONFIG_S3_BUCKET` | S3 bucket with rules.json |
| `CONFIG_S3_KEY` | S3 key path (default: cost-guardian/rules.json) |
| `CONFIG_SSM_PARAMETER` | SSM parameter with rules JSON |
| `COST_GUARDIAN_CONFIG` | Inline rules JSON |
| `DRY_RUN` | Skip remediation (true/false) |
| `AWS_REGION` | AWS region |
| `DEFAULT_EC2_HOURLY_COST` | Fallback hourly cost for EC2 when Pricing API fails (default: 0.10) |
| `DEFAULT_RDS_HOURLY_COST` | Fallback hourly cost for RDS when Pricing API fails (default: 0.15) |

## IAM Permissions Required

```json
{
  "Effect": "Allow",
  "Action": [
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:GetMetricData",
    "pricing:GetProducts",
    "lambda:PutFunctionConcurrency",
    "lambda:DeleteFunctionConcurrency",
    "sns:Publish",
    "states:StartExecution",
    "application-autoscaling:RegisterScalableTarget",
    "ec2:DescribeInstances",
    "ec2:StopInstances",
    "rds:DescribeDBInstances",
    "rds:StopDBInstance"
  ],
  "Resource": "*"
}
```

## Extending

Add custom remediation actions in `cost_engine.py`:

```python
def _my_custom_action(self, params: dict, projection: CostProjection) -> dict:
    # Implementation
    return {'action': 'my_custom_action', 'result': 'done'}

# Register in __init__
self._actions['my_custom_action'] = self._my_custom_action
```
