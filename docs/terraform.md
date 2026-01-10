# Terraform Deployment

## Module Usage

Use the module directly from GitHub:

```hcl
module "cost_guardian" {
  source = "github.com/Havoc24k/aws-cost-guardian?ref=v1.0.2"

  total_budget = 1000
  alert_email  = "ops@example.com"
  regions      = ["us-east-1"]
}
```

Multi-region with custom thresholds:

```hcl
module "cost_guardian" {
  source = "github.com/Havoc24k/aws-cost-guardian?ref=v1.0.2"

  total_budget            = 2000
  alert_email             = "team@example.com"
  regions                 = ["us-east-1", "eu-central-1"]
  alert_thresholds        = [50, 75, 90]
  auto_stop_threshold     = 100
  lambda_spike_threshold  = 5
}
```

Then deploy:

```bash
terraform init
terraform apply
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
| `budget_period_start` | `""` | Budget period start date (YYYY-MM-DD) |
| `budget_period_end` | `""` | Budget period end date (YYYY-MM-DD) |
| `dry_run` | `true` | Report actions without executing them |

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

variable "budget_period_start" {
  description = "Budget period start date (YYYY-MM-DD)"
  type        = string
  default     = ""
}

variable "budget_period_end" {
  description = "Budget period end date (YYYY-MM-DD)"
  type        = string
  default     = ""
}

variable "dry_run" {
  description = "Report actions without executing them"
  type        = bool
  default     = true
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

## Example: Custom Budget Period

Use explicit start/end dates for non-monthly budget periods:

```hcl
# terraform.tfvars - 35-day budget period
total_budget          = 1000
alert_email           = "team@example.com"
budget_period_start   = "2026-01-01"
budget_period_end     = "2026-02-04"
```

If `budget_period_start` and `budget_period_end` are not set, the budget defaults to the current calendar month.

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
| `BUDGET_PERIOD_START` | from var | Budget period start (YYYY-MM-DD) |
| `BUDGET_PERIOD_END` | from var | Budget period end (YYYY-MM-DD) |
| `DRY_RUN` | from var | Dry run mode (true/false) |

## Cleanup

```bash
terraform destroy
```
