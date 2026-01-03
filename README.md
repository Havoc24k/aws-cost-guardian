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
terraform init
terraform apply \
  -var="total_budget=1000" \
  -var="alert_email=team@example.com"
```

See [Terraform docs](docs/terraform.md) for all options.

## Documentation

- [Architecture](docs/architecture.md) - How budget projection works
- [Spike Detection](docs/spike-detection.md) - Lambda spike detection algorithm
- [CLI Reference](docs/cli.md) - Command line usage and examples
- [Terraform Deployment](docs/terraform.md) - Infrastructure variables and examples

## Files

```
aws_cost_guardian.py  # Core logic (~500 lines)
lambda_handler.py     # Lambda entry point (~70 lines)
cli.py                # Local CLI (~190 lines)
main.tf               # Terraform infrastructure
pyproject.toml        # Project dependencies
docs/                 # Documentation
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
