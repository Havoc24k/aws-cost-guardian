# AWS Cost Guardian

Simple POC account budget protection for AWS. Monitors actual spend + projected costs and automatically stops resources when budget is exceeded. Includes Lambda spike detection for early warning of runaway costs.

## Quick Start

### Local Testing

```bash
# Install dependencies
uv sync

# Set AWS credentials
export AWS_PROFILE=your-profile

# Check current budget status
uv run python cli.py --budget 1000 --regions us-east-1 status

# Verbose output with resource details
uv run python cli.py --budget 1000 --regions us-east-1 status -v

# Multi-region check
uv run python cli.py --budget 2000 --regions us-east-1,eu-central-1 status
```

### Lambda Deployment

```bash
# Basic deployment
terraform init
terraform apply \
  -var="total_budget=1000" \
  -var="alert_email=team@example.com"

# Multi-region with custom thresholds
terraform apply \
  -var="total_budget=2000" \
  -var="alert_email=team@example.com" \
  -var='regions=["us-east-1","eu-central-1"]' \
  -var='alert_thresholds=[50,75,90]' \
  -var="auto_stop_threshold=100"

# With sensitive spike detection (5x threshold, 1 min window)
terraform apply \
  -var="total_budget=1000" \
  -var="alert_email=team@example.com" \
  -var="lambda_spike_threshold=5" \
  -var="lambda_spike_window_minutes=1"
```

## How It Works

### Budget Projection

```
Hourly Check --> Cost Explorer (actual spend)
                        +
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

### Lambda Cost Projection

Lambda costs are calculated from CloudWatch metrics:

```
invocations_per_hour = CloudWatch Invocations / lookback_hours
gb_seconds_per_hour = (duration_ms / 1000) * (memory_mb / 1024)

hourly_cost = invocations × $0.0000002
            + gb_seconds × $0.0000166667
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

### Lambda Spike Detection

Detects runaway Lambda costs within minutes by comparing current activity against historical baseline.

#### How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                        TIME WINDOWS                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  BASELINE WINDOW (7 days)                    SHORT WINDOW (5min)│
│  ◄──────────────────────────────────────────►◄────►             │
│  |                                            |    |             │
│  7 days ago                              5min ago  now           │
│                                                                  │
│  Total: 10,080 invocations                   180 invocations    │
│  Rate:  10,080 / (7*24*60) = 0.1/min         180 / 5 = 36/min   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘

SPIKE RATIO = current_rate / baseline_rate = 36 / 0.1 = 360x

If SPIKE RATIO >= THRESHOLD (default 10x) → ALERT!
```

#### Algorithm Steps

1. **Query SHORT window** (default 5 min): Get invocations from CloudWatch
2. **Query BASELINE window** (default 7 days): Get total invocations
3. **Calculate rates per minute**:
   - `current_rate = short_invocations / short_minutes`
   - `baseline_rate = baseline_invocations / baseline_minutes`
4. **Compute spike ratio**: `current_rate / baseline_rate`
5. **Compare to threshold**: If ratio >= 10x, trigger alert
6. **Project daily cost**: What it would cost if spike continues 24h

#### Real-World Example

```
Function: data-transformer (256MB)

Historical pattern (December):
  - 52,000 invocations/day = 36/min
  - Cost: $65/day

Current state (January - idle):
  - 200 invocations/day = 0.14/min
  - Cost: $0.01/day

If December spike happens again:
  Baseline (7 days at idle): 0.14/min
  Sudden spike:              36/min
  Ratio:                     257x
  Threshold:                 10x

  Result: SPIKE DETECTED in 5 minutes!
  Alert shows: "Projected $65/day if continues"
```

#### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spike_threshold` | 10 | Alert if rate >= Nx baseline |
| `spike_window` | 5 min | How recent to check |
| `baseline_hours` | 168 (7d) | Historical comparison period |

#### Edge Cases

| Scenario | Behavior |
|----------|----------|
| No baseline, has current activity | Ratio = 999 (new function alert) |
| No current activity | No spike (idle function) |
| Baseline higher than current | No spike (activity decreased) |

#### Detection Speed

| Spike Window | Detection Time |
|--------------|----------------|
| 5 min (default) | Alert within 5 minutes of spike |
| 1 min | Alert within 1 minute (more sensitive) |
| 15 min | Alert within 15 minutes (fewer false positives) |

## CLI Examples

### Basic Usage

```bash
# Check budget status
uv run python cli.py --budget 1000 --regions eu-central-1 status

# Output:
# Budget Status
# ========================================
# Actual Spend (MTD):    $17.64
# Hourly Cost:           $0.05
# Projected Total:       $50.23
# Budget:                $1000.00
# Budget Used:           5.0%
# Hours Until Month End: 679
#
# Running Resources
# ----------------------------------------
# EC2 Instances:         1
# RDS Instances:         0
# Lambda Functions:      2
#
# Action: OK
```

### Verbose Output

```bash
uv run python cli.py --budget 1000 --regions eu-central-1 status -v

# Additional output:
# Resource Details
# ----------------------------------------
# EC2:
#   - i-0b60b23df8c0fdff0 (t3.medium) in eu-central-1
# Lambda:
#   - data-transformer (256MB) in eu-central-1
#   - api-handler (128MB) in eu-central-1
```

### Spike Detection

```bash
# Default: alert if 10x spike in 5 min window
uv run python cli.py --budget 1000 --regions eu-central-1 status

# More sensitive: alert if 5x spike
uv run python cli.py --spike-threshold 5 --regions eu-central-1 status

# Faster detection: 1 min window
uv run python cli.py --spike-window 1 --regions eu-central-1 status

# When spike detected:
# LAMBDA SPIKES DETECTED
# ----------------------------------------
#   data-transformer
#     Current:   36.0/min
#     Baseline:  0.20/min
#     Ratio:     180x
#     Projected: $64.93/day
```

### Lambda Lookback

```bash
# Use 7-day average for cost projection (smoother)
uv run python cli.py --lambda-lookback 168 --regions eu-central-1 status

# Use 1-hour for recent activity
uv run python cli.py --lambda-lookback 1 --regions eu-central-1 status
```

### Emergency Stop

```bash
# See what would be stopped
uv run python cli.py --regions eu-central-1 stop --dry-run

# Actually stop everything (requires confirmation)
uv run python cli.py --regions eu-central-1 stop --confirm
```

## CLI Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--budget` | 1000 | Total budget in USD |
| `--regions` | us-east-1 | Comma-separated regions |
| `--lambda-lookback` | 24 | Hours for Lambda cost projection |
| `--spike-threshold` | 10 | Alert if rate exceeds Nx baseline |
| `--spike-window` | 5 | Minutes to check for spikes |

## Terraform Variables

### Required

| Variable | Description |
|----------|-------------|
| `total_budget` | Total POC budget in USD |
| `alert_email` | Email for alerts |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `regions` | `["us-east-1"]` | Regions to monitor |
| `alert_thresholds` | `[50, 75, 90]` | Alert percentages |
| `auto_stop_threshold` | `100` | Stop at this percentage |
| `check_interval` | `rate(1 hour)` | Check frequency |
| `lambda_lookback_hours` | `24` | Hours for Lambda cost projection |
| `lambda_spike_threshold` | `10` | Alert if rate exceeds Nx baseline |
| `lambda_spike_window_minutes` | `5` | Minutes to check for spikes |
| `lambda_baseline_hours` | `168` | Baseline period for comparison |

### Terraform Examples

```hcl
# variables.tf - All available variables
variable "total_budget" {
  description = "Total POC budget in USD"
  type        = number
}

variable "alert_email" {
  description = "Email for budget alerts"
  type        = string
}

variable "regions" {
  description = "AWS regions to monitor"
  type        = list(string)
  default     = ["us-east-1"]
}

variable "alert_thresholds" {
  description = "Budget percentage thresholds for alerts"
  type        = list(number)
  default     = [50, 75, 90]
}

variable "auto_stop_threshold" {
  description = "Budget percentage to trigger auto-stop"
  type        = number
  default     = 100
}

variable "lambda_spike_threshold" {
  description = "Alert if Lambda rate exceeds Nx baseline"
  type        = number
  default     = 10
}

variable "lambda_spike_window_minutes" {
  description = "Minutes to check for Lambda spikes"
  type        = number
  default     = 5
}
```

### Example: POC Account with Spike Detection

```hcl
# terraform.tfvars
total_budget              = 1000
alert_email               = "team@example.com"
regions                   = ["us-east-1", "eu-central-1"]
alert_thresholds          = [50, 75, 90]
auto_stop_threshold       = 100
lambda_spike_threshold    = 5    # Alert on 5x spike
lambda_spike_window_minutes = 1  # Check every minute
```

### Lambda Environment Variables

These are set automatically by Terraform in the Lambda function:

| Variable | Default | Description |
|----------|---------|-------------|
| `REGIONS` | from var | JSON list of regions |
| `TOTAL_BUDGET` | from var | Budget in USD |
| `ALERT_THRESHOLDS` | from var | JSON list of percentages |
| `AUTO_STOP_THRESHOLD` | from var | Stop threshold |
| `SNS_TOPIC_ARN` | from resource | SNS topic for alerts |
| `LAMBDA_LOOKBACK_HOURS` | 24 | Hours for cost projection |
| `LAMBDA_SPIKE_THRESHOLD` | from var | Spike alert multiplier |
| `LAMBDA_SPIKE_WINDOW_MINUTES` | from var | Spike detection window |
| `LAMBDA_BASELINE_HOURS` | 168 | Baseline period (7 days) |

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

## Files

```
aws_cost_guardian.py # Core logic (~500 lines)
lambda_handler.py    # Lambda entry point (~70 lines)
cli.py               # Local CLI (~190 lines)
main.tf              # Terraform infrastructure
pyproject.toml       # Project dependencies
```

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run linters
uv run ruff check *.py
uv run mypy *.py

# Format code
uv run ruff format *.py
```

## Cleanup

```bash
terraform destroy \
  -var="total_budget=1000" \
  -var="alert_email=team@example.com"
```
