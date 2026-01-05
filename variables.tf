# Required variables
variable "total_budget" {
  description = "Total POC budget in USD"
  type        = number
}

variable "alert_email" {
  description = "Email for budget alerts"
  type        = string
}

# Optional variables with sensible defaults
variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "regions" {
  description = "AWS regions to monitor for resources"
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

variable "check_interval" {
  description = "How often to check budget (CloudWatch schedule expression)"
  type        = string
  default     = "rate(1 hour)"
}

variable "lambda_lookback_hours" {
  description = "Hours to look back for Lambda cost projection"
  type        = number
  default     = 24
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

variable "lambda_baseline_hours" {
  description = "Hours of baseline for spike comparison"
  type        = number
  default     = 168
}

variable "environment" {
  description = "Environment name (used in resource naming)"
  type        = string
  default     = "poc"
}

variable "dry_run" {
  description = "When true, report actions via email without executing them"
  type        = bool
  default     = true
}
