# Basic example of AWS Cost Guardian module
# Monitors a POC account with a $1000 budget

module "cost_guardian" {
  source = "../.."

  total_budget = 1000
  alert_email  = "ops@example.com"
  regions      = ["us-east-1"]
}

output "lambda_function_name" {
  value = module.cost_guardian.lambda_function_name
}

output "lambda_function_arn" {
  value = module.cost_guardian.lambda_function_arn
}

output "sns_topic_arn" {
  value = module.cost_guardian.sns_topic_arn
}
