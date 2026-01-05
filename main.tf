# AWS Cost Guardian
# POC account budget protection with automatic resource shutdown

provider "aws" {
  region = var.aws_region
}

locals {
  function_name = "cost-guardian-${var.environment}"
  tags = {
    Application = "cost-guardian"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# SNS Topic for alerts
resource "aws_sns_topic" "alerts" {
  name = "${local.function_name}-alerts"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# IAM Role for Lambda
resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.function_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "CostExplorer"
        Effect = "Allow"
        Action = [
          "ce:GetCostAndUsage"
        ]
        Resource = "*"
      },
      {
        Sid    = "PricingAPI"
        Effect = "Allow"
        Action = [
          "pricing:GetProducts"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:StopInstances"
        ]
        Resource = "*"
      },
      {
        Sid    = "RDS"
        Effect = "Allow"
        Action = [
          "rds:DescribeDBInstances",
          "rds:StopDBInstance"
        ]
        Resource = "*"
      },
      {
        Sid    = "Lambda"
        Effect = "Allow"
        Action = [
          "lambda:ListFunctions",
          "lambda:GetFunctionConcurrency",
          "lambda:PutFunctionConcurrency"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics"
        ]
        Resource = "*"
      },
      {
        Sid    = "SNS"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

# Lambda function package
data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/.terraform/lambda.zip"

  source {
    content  = file("${path.module}/src/aws_cost_guardian.py")
    filename = "aws_cost_guardian.py"
  }

  source {
    content  = file("${path.module}/src/lambda_handler.py")
    filename = "lambda_handler.py"
  }
}

# Lambda function
resource "aws_lambda_function" "guardian" {
  function_name = local.function_name
  role          = aws_iam_role.lambda.arn
  handler       = "lambda_handler.handler"
  runtime       = "python3.12"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      REGIONS                     = jsonencode(var.regions)
      TOTAL_BUDGET                = tostring(var.total_budget)
      ALERT_THRESHOLDS            = jsonencode(var.alert_thresholds)
      AUTO_STOP_THRESHOLD         = tostring(var.auto_stop_threshold)
      SNS_TOPIC_ARN               = aws_sns_topic.alerts.arn
      DRY_RUN                     = "false"
      LAMBDA_LOOKBACK_HOURS       = tostring(var.lambda_lookback_hours)
      LAMBDA_SPIKE_THRESHOLD      = tostring(var.lambda_spike_threshold)
      LAMBDA_SPIKE_WINDOW_MINUTES = tostring(var.lambda_spike_window_minutes)
      LAMBDA_BASELINE_HOURS       = tostring(var.lambda_baseline_hours)
    }
  }

  tags = local.tags
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
  tags              = local.tags
}

# CloudWatch Event Rule - Scheduled trigger
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.function_name}-schedule"
  description         = "Trigger Cost Guardian check"
  schedule_expression = var.check_interval
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "cost-guardian-lambda"
  arn       = aws_lambda_function.guardian.arn
}

resource "aws_lambda_permission" "cloudwatch" {
  statement_id  = "AllowCloudWatchInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.guardian.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
