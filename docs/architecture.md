# Architecture

## How It Works

### Budget Projection

```
Hourly Check --> Cost Explorer (total account spend)
                        |
                        v
              total > budget?
                   |
          YES -----+-----> STOP ALL (immediate)
                   |
                   NO
                   |
                   v
              EC2/RDS/Lambda Discovery (hourly cost)
                        |
                        v
              Budget Calculation
              actual + (hourly_cost * remaining_hours)
                        |
             +----------+----------+
             v          v          v
          < 75%      75-100%     > 100%
          (OK)      (ALERT)   (STOP ALL)
```

### Immediate Remediation

If actual spend already exceeds the budget, resources are stopped immediately
regardless of projected costs. This handles deployments to accounts that have
already overspent:

```
Actual Spend: $1,200
Budget:       $1,000
              |
              v
     actual > budget = TRUE
              |
              v
     STOP ALL (immediate remediation)
```

This ensures that deploying to an account that's already over budget triggers
immediate protection - no waiting for the next projection cycle.

### Lambda Cost Projection

Lambda costs are calculated from CloudWatch metrics:

```
invocations_per_hour = CloudWatch Invocations / lookback_hours
gb_seconds_per_hour = (duration_ms / 1000) * (memory_mb / 1024)

hourly_cost = invocations x $0.0000002
            + gb_seconds x $0.0000166667
```

Example with real data:
```
Function: data-transformer (256MB memory)
Last 24h: 52,000 invocations, 4.3 hours total duration

Hourly rate:   2,166 invocations/hour
GB-seconds:    162/hour
Hourly cost:   $0.0004 (requests) + $2.70 (compute) = $2.70/hour
Daily cost:    $64.80
Monthly:       ~$1,950
```

## What Gets Stopped

| Service | Action |
|---------|--------|
| EC2 | `stop_instances` |
| RDS | `stop_db_instance` |
| Lambda | `put_function_concurrency(0)` |

## IAM Permissions

```json
{
  "Action": [
    "ce:GetCostAndUsage",
    "ec2:DescribeInstances",
    "ec2:StopInstances",
    "rds:DescribeDBInstances",
    "rds:StopDBInstance",
    "lambda:ListFunctions",
    "lambda:PutFunctionConcurrency",
    "cloudwatch:GetMetricStatistics",
    "sns:Publish",
    "pricing:GetProducts"
  ]
}
```
