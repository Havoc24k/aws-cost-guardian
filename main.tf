# Cost Guardian Infrastructure
# Deploys the Lambda-based cost monitoring and remediation system

terraform {
  required_version = ">= 1.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "evaluation_schedule" {
  description = "CloudWatch Events schedule expression for rule evaluation"
  type        = string
  default     = "rate(1 minute)"
}

variable "alert_email" {
  description = "Email address for cost alerts"
  type        = string
  default     = ""
}

variable "default_ec2_hourly_cost" {
  description = "Fallback hourly cost for EC2 when Pricing API fails"
  type        = string
  default     = "0.10"
}

variable "default_rds_hourly_cost" {
  description = "Fallback hourly cost for RDS when Pricing API fails"
  type        = string
  default     = "0.15"
}

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

# S3 bucket for configuration and Lambda code
resource "aws_s3_bucket" "config" {
  bucket_prefix = "cost-guardian-${var.environment}-"
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Upload rules configuration
resource "aws_s3_object" "rules_config" {
  bucket = aws_s3_bucket.config.id
  key    = "cost-guardian/rules.json"
  source = "${path.module}/rules.json"
  etag   = filemd5("${path.module}/rules.json")
}

# SNS Topic for alerts
resource "aws_sns_topic" "cost_alerts" {
  name = "cost-guardian-alerts-${var.environment}"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# IAM Role for Lambda
resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
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
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics"
        ]
        Resource = "*"
      },
      {
        Sid    = "S3Config"
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.config.arn}/*"
      },
      {
        Sid    = "SSMParameter"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter"
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/cost-guardian/*"
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
        Sid    = "LambdaRemediation"
        Effect = "Allow"
        Action = [
          "lambda:PutFunctionConcurrency",
          "lambda:DeleteFunctionConcurrency",
          "lambda:GetFunctionConcurrency"
        ]
        Resource = "*"
      },
      {
        Sid    = "SNSNotification"
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.cost_alerts.arn
      },
      {
        Sid    = "StepFunctions"
        Effect = "Allow"
        Action = [
          "states:StartExecution"
        ]
        Resource = "arn:aws:states:${var.aws_region}:*:stateMachine:*"
      },
      {
        Sid    = "AutoScaling"
        Effect = "Allow"
        Action = [
          "application-autoscaling:RegisterScalableTarget",
          "application-autoscaling:DeregisterScalableTarget"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2Remediation"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:StopInstances"
        ]
        Resource = "*"
      },
      {
        Sid    = "RDSRemediation"
        Effect = "Allow"
        Action = [
          "rds:DescribeDBInstances",
          "rds:StopDBInstance"
        ]
        Resource = "*"
      }
    ]
  })
}

# Lambda function package
data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/.terraform/lambda.zip"
  
  source {
    content  = file("${path.module}/cost_engine.py")
    filename = "cost_engine.py"
  }
  
  source {
    content  = file("${path.module}/lambda_handler.py")
    filename = "lambda_handler.py"
  }
}

# Lambda function
resource "aws_lambda_function" "cost_guardian" {
  function_name = local.function_name
  role          = aws_iam_role.lambda.arn
  handler       = "lambda_handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256
  
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  
  environment {
    variables = {
      CONFIG_S3_BUCKET          = aws_s3_bucket.config.id
      CONFIG_S3_KEY             = "cost-guardian/rules.json"
      DRY_RUN                   = "false"
      AWS_REGION                = var.aws_region
      DEFAULT_EC2_HOURLY_COST   = var.default_ec2_hourly_cost
      DEFAULT_RDS_HOURLY_COST   = var.default_rds_hourly_cost
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
  description         = "Trigger Cost Guardian evaluation"
  schedule_expression = var.evaluation_schedule
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "cost-guardian-lambda"
  arn       = aws_lambda_function.cost_guardian.arn
}

resource "aws_lambda_permission" "cloudwatch" {
  statement_id  = "AllowCloudWatchInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_guardian.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# CloudWatch Dashboard
resource "aws_cloudwatch_dashboard" "cost_guardian" {
  dashboard_name = "CostGuardian-${var.environment}"
  
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Cost Guardian Invocations"
          region = var.aws_region
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", local.function_name],
            [".", "Errors", ".", "."],
            [".", "Duration", ".", ".", { stat = "Average" }]
          ]
          period = 60
          stat   = "Sum"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Cost Breaches (Last Hour)"
          region = var.aws_region
          query  = "SOURCE '/aws/lambda/${local.function_name}' | fields @timestamp, @message | filter @message like /BREACH/ | sort @timestamp desc | limit 50"
        }
      }
    ]
  })
}

# Outputs
output "lambda_function_name" {
  value = aws_lambda_function.cost_guardian.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.cost_guardian.arn
}

output "config_bucket" {
  value = aws_s3_bucket.config.id
}

output "sns_topic_arn" {
  value = aws_sns_topic.cost_alerts.arn
}

output "dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=CostGuardian-${var.environment}"
}
