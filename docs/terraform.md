# Terraform Deployment

## Quick Deploy

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

## Variables

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

## Variable Definitions

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

## Example: POC Account with Spike Detection

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

## Lambda Environment Variables

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

## Cleanup

```bash
terraform destroy \
  -var="total_budget=1000" \
  -var="alert_email=team@example.com"
```
