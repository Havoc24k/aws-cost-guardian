# Basic Example

This example deploys AWS Cost Guardian with default settings.

## Usage

```bash
terraform init
terraform plan -var="total_budget=1000" -var="alert_email=ops@example.com"
terraform apply -var="total_budget=1000" -var="alert_email=ops@example.com"
```

## What gets deployed

- Lambda function (runs hourly)
- SNS topic for alerts
- CloudWatch Event Rule (scheduler)
- IAM role and policy

## Cleanup

```bash
terraform destroy -var="total_budget=1000" -var="alert_email=ops@example.com"
```
