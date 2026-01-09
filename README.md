# AWS Cost Guardian

Simple POC account budget protection for AWS. Monitors total account spend and automatically stops resources when budget is exceeded. Includes Lambda spike detection for early warning of runaway costs.

## Terraform Module Usage

Use directly from GitHub:

```hcl
module "cost_guardian" {
  source = "github.com/Havoc24k/aws-cost-guardian"

  total_budget = 1000
  alert_email  = "ops@example.com"
  regions      = ["us-east-1", "eu-central-1"]
}
```

### Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `total_budget` | Yes | - | Total budget in USD |
| `alert_email` | Yes | - | Email for alerts |
| `regions` | No | `["us-east-1"]` | Regions to monitor |
| `alert_thresholds` | No | `[50, 75, 90]` | Alert percentages |
| `auto_stop_threshold` | No | `100` | Stop at this percentage |
| `check_interval` | No | `rate(1 hour)` | Check frequency |
| `lambda_spike_threshold` | No | `10` | Alert if Lambda rate >= Nx baseline |
| `budget_period_start` | No | `""` | Budget period start (YYYY-MM-DD) |
| `budget_period_end` | No | `""` | Budget period end (YYYY-MM-DD) |
| `dry_run` | No | `true` | Report actions without executing |

### Outputs

| Output | Description |
|--------|-------------|
| `lambda_function_name` | Name of the Lambda function |
| `lambda_function_arn` | ARN of the Lambda function |
| `sns_topic_arn` | ARN of the SNS topic |

See [examples/](examples/) for complete usage examples.

**Note:** If deploying to an account where total spend already exceeds the budget,
resources will be stopped immediately on first run.

## Local CLI Testing

```bash
# Install dependencies
uv sync

# Set AWS credentials
export AWS_PROFILE=your-profile

# Check current budget status
uv run python cli.py --budget 1000 --regions us-east-1 status

# Verbose output with resource details
uv run python cli.py --budget 1000 --regions us-east-1 status -v
```

## Documentation

- [Architecture](docs/architecture.md) - How budget projection works
- [Spike Detection](docs/spike-detection.md) - Lambda spike detection algorithm
- [CLI Reference](docs/cli.md) - Command line usage and examples
- [Terraform Deployment](docs/terraform.md) - Infrastructure variables and examples

## Project Structure

```
main.tf               # Terraform resources
variables.tf          # Input variables
outputs.tf            # Output values
versions.tf           # Provider requirements
src/                  # Lambda code
  aws_cost_guardian.py
  lambda_handler.py
examples/             # Usage examples
  basic/
cli.py                # Local CLI for testing
docs/                 # Documentation
```

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run linters
uv run ruff check src/*.py cli.py
uv run ruff format src/*.py cli.py
```
