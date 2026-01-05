output "lambda_function_name" {
  description = "Name of the Cost Guardian Lambda function"
  value       = aws_lambda_function.guardian.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Cost Guardian Lambda function"
  value       = aws_lambda_function.guardian.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for alerts"
  value       = aws_sns_topic.alerts.arn
}

output "monitored_regions" {
  description = "List of AWS regions being monitored"
  value       = var.regions
}

output "budget" {
  description = "Configured budget amount"
  value       = "$${var.total_budget}"
}
