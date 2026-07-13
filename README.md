# AWS Cost Guardian

Simple POC account budget protection for AWS. Monitors total account spend and automatically stops resources when budget is exceeded. Includes Lambda spike detection for early warning of runaway costs.

## Terraform Module Usage

Use directly from GitHub:

```hcl
module "cost_guardian" {
  source = "github.com/Havoc24k/aws-cost-guardian?ref=v2.0.1"

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

## End-to-End Tests (Floci)

E2E tests run the real guardian against [Floci](https://github.com/floci-io/floci), a local
AWS emulator (Docker), instead of a real AWS account. They exercise discovery, cost projection,
remediation, and Lambda spike detection end to end (see caveats below for what's *not* covered).

```bash
docker compose up -d floci      # start the emulator on localhost:4566
uv run pytest -m e2e            # run the e2e suite
uv run pytest -m "not e2e"      # unit tests only (no Docker needed)
docker compose down             # stop the emulator
```

Without Floci running, `uv run pytest` skips the e2e suite automatically — no Docker required
for everyday development.

Notes:
- **Slow.** Floci launches real Docker containers for EC2/RDS/ECS/Lambda, and seed helpers poll
  for up to 180s for resources to become genuinely available. Expect the e2e job to take minutes,
  not seconds.
- **Pricing is not validated.** Floci's Pricing API doesn't implement `TERM_MATCH` filtering, so
  every resource price falls back to the hardcoded `DEFAULT_*` constants. The e2e layer proves the
  discovery/projection/remediation *logic*, not real AWS pricing correctness.
- **Cost Explorer reports ~$0.** Floci synthesizes near-zero month-to-date spend, so the
  "already over budget on first deploy" (`actual_exceeded`) scenario can't be exercised there —
  it's skipped in the e2e suite and covered by a unit test instead.
- If you see spurious "seed died" errors, an orphaned Floci container may be squatting the port.
  Run `docker compose down -v --remove-orphans` first, then retry.
- App Runner is not emulated by Floci and is no longer supported by this project.

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
cli.py                # Local CLI for testing
docs/                 # Documentation
tests/                # Unit tests + e2e (Floci) suite
docker-compose.yml    # Floci emulator for e2e tests
```

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run linters (matches CI, which lints the whole repo including tests/e2e/)
uv run ruff check .
uv run ruff format .
```
