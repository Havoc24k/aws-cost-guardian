# CLI Reference

## Basic Usage

```bash
# Check budget status
uv run python cli.py --budget 1000 --regions eu-central-1 status

# Output:
# Budget Status
# ========================================
# Actual Spend (MTD):    $17.64
# Hourly Cost:           $0.05
# Projected Total:       $50.23
# Budget:                $1000.00
# Budget Used:           5.0%
# Hours Until Month End: 679
#
# Running Resources
# ----------------------------------------
# EC2 Instances:         1
# RDS Instances:         0
# Lambda Functions:      2
#
# Action: OK
```

## Verbose Output

```bash
uv run python cli.py --budget 1000 --regions eu-central-1 status -v

# Additional output:
# Resource Details
# ----------------------------------------
# EC2:
#   - i-0b60b23df8c0fdff0 (t3.medium) in eu-central-1
# Lambda:
#   - data-transformer (256MB) in eu-central-1
#   - api-handler (128MB) in eu-central-1
```

## Spike Detection

```bash
# Default: alert if 10x spike in 5 min window
uv run python cli.py --budget 1000 --regions eu-central-1 status

# More sensitive: alert if 5x spike
uv run python cli.py --spike-threshold 5 --regions eu-central-1 status

# Faster detection: 1 min window
uv run python cli.py --spike-window 1 --regions eu-central-1 status

# When spike detected:
# LAMBDA SPIKES DETECTED
# ----------------------------------------
#   data-transformer
#     Current:   36.0/min
#     Baseline:  0.20/min
#     Ratio:     180x
#     Projected: $64.93/day
```

## Lambda Lookback

```bash
# Use 7-day average for cost projection (smoother)
uv run python cli.py --lambda-lookback 168 --regions eu-central-1 status

# Use 1-hour for recent activity
uv run python cli.py --lambda-lookback 1 --regions eu-central-1 status
```

## Emergency Stop

```bash
# See what would be stopped
uv run python cli.py --regions eu-central-1 stop --dry-run

# Actually stop everything (requires confirmation)
uv run python cli.py --regions eu-central-1 stop --confirm
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--budget` | 1000 | Total budget in USD |
| `--regions` | us-east-1 | Comma-separated regions |
| `--lambda-lookback` | 24 | Hours for Lambda cost projection |
| `--spike-threshold` | 10 | Alert if rate exceeds Nx baseline |
| `--spike-window` | 5 | Minutes to check for spikes |
