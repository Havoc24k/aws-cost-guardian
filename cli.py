#!/usr/bin/env python3
"""
AWS Cost Guardian CLI
Check budget status and test locally.
"""

import argparse
import sys
from decimal import Decimal

from src.aws_cost_guardian import BudgetGuardian, BudgetStatus  # noqa: E402


def _create_guardian(args, budget: Decimal | None = None) -> BudgetGuardian:
    """Create BudgetGuardian from CLI args."""
    return BudgetGuardian(
        regions=args.regions.split(","),
        total_budget=budget if budget is not None else Decimal(args.budget),
        alert_thresholds=[50, 75, 90],
        auto_stop_threshold=100,
        lambda_lookback_hours=getattr(args, "lambda_lookback", 24),
        lambda_spike_threshold=getattr(args, "spike_threshold", 10),
        lambda_spike_window_minutes=getattr(args, "spike_window", 5),
    )


def _format_action(status: BudgetStatus) -> str:
    """Format action string with actual_exceeded indicator."""
    if status.actual_exceeded:
        return f"{status.action.upper()} (immediate - actual spend exceeded)"
    return status.action.upper()


def cmd_status(args):
    """Check current budget status."""
    guardian = _create_guardian(args)

    print(f"Checking budget across regions: {guardian.regions}")
    print(f"Total budget: ${guardian.budget}")
    print()

    status = guardian.check_budget()

    print("Budget Status")
    print("=" * 40)
    print(f"Total Spend:           ${status.actual_spend:.2f}")
    print(f"Hourly Cost:           ${status.hourly_cost:.2f}")
    print(f"Projected Total:       ${status.projected_total:.2f}")
    print(f"Budget:                ${status.budget:.2f}")
    print(f"Budget Used:           {status.budget_percent:.1f}%")
    if status.actual_exceeded:
        print("STATUS:                ACTUAL SPEND EXCEEDED")
    print(f"Hours Until Month End: {status.remaining_hours}")
    print()
    print("Running Resources")
    print("-" * 40)
    print(f"EC2 Instances:         {len(status.resources['ec2'])}")
    print(f"RDS Instances:         {len(status.resources['rds'])}")
    print(f"Lambda Functions:      {len(status.resources['lambda'])}")
    print()
    print(f"Action: {_format_action(status)}")

    if status.thresholds_breached:
        print(f"Thresholds Breached: {status.thresholds_breached}%")

    if status.lambda_spikes:
        print()
        print("LAMBDA SPIKES DETECTED")
        print("-" * 40)
        for spike in status.lambda_spikes:
            print(f"  {spike.function_name}")
            print(f"    Current:   {spike.current_rate:.1f}/min")
            print(f"    Baseline:  {spike.baseline_rate:.2f}/min")
            print(f"    Ratio:     {spike.spike_ratio:.0f}x")
            print(f"    Projected: ${spike.projected_daily_cost:.2f}/day")

    if args.verbose:
        print()
        print("Resource Details")
        print("-" * 40)
        if status.resources["ec2"]:
            print("EC2:")
            for r in status.resources["ec2"]:
                print(f"  - {r['id']} ({r['type']}) in {r['region']}")
        if status.resources["rds"]:
            print("RDS:")
            for r in status.resources["rds"]:
                print(f"  - {r['id']} ({r['class']}) in {r['region']}")
        if status.resources["lambda"]:
            print("Lambda:")
            for r in status.resources["lambda"]:
                print(f"  - {r['name']} ({r['memory_mb']}MB) in {r['region']}")

    return 0


def cmd_test(args):
    """Test budget check with dry run."""
    guardian = _create_guardian(args)

    print("Testing budget guardian (dry run)")
    print(f"Regions: {guardian.regions}")
    print(f"Budget: ${guardian.budget}")
    print()

    result = guardian.run(dry_run=True)
    status = result["status"]

    print(f"Actual Spend: ${status.actual_spend:.2f}")
    print(f"Projected Total: ${status.projected_total:.2f}")
    print(f"Budget Used: {status.budget_percent:.1f}%")
    print(f"Action: {_format_action(status)}")
    print()

    if status.action == "stop_all":
        if status.actual_exceeded:
            print("ACTUAL SPEND EXCEEDED - Resources would be stopped immediately:")
        else:
            print("Resources that would be stopped (dry run):")
        print(f"  EC2: {len(status.resources['ec2'])} instances")
        print(f"  RDS: {len(status.resources['rds'])} instances")
        print(f"  Lambda: {len(status.resources['lambda'])} functions")

    return 0


def cmd_stop(args):
    """Stop all resources (use with caution)."""
    if not args.confirm:
        print("This will stop ALL EC2, RDS, and throttle ALL Lambda functions!")
        print("Use --confirm to proceed.")
        return 1

    guardian = _create_guardian(args, budget=Decimal("0"))

    print(f"Discovering resources in: {guardian.regions}")
    status = guardian.check_budget()

    print(
        f"Found: {len(status.resources['ec2'])} EC2, {len(status.resources['rds'])} RDS, {len(status.resources['lambda'])} Lambda"
    )

    if args.dry_run:
        print("Dry run - no resources stopped")
        return 0

    print("Stopping all resources...")
    results = guardian.stop_all_resources(status.resources, dry_run=False)

    print()
    print("Results:")
    print(f"  EC2 stopped: {len([r for r in results['ec2'] if r['status'] == 'stopped'])}")
    print(f"  RDS stopped: {len([r for r in results['rds'] if r['status'] == 'stopped'])}")
    print(
        f"  Lambda throttled: {len([r for r in results['lambda'] if r['status'] == 'throttled'])}"
    )

    return 0


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AWS Cost Guardian CLI - POC account budget protection"
    )
    parser.add_argument(
        "--regions", default="us-east-1", help="Comma-separated list of AWS regions"
    )
    parser.add_argument("--budget", default="1000", help="Total budget in USD")
    parser.add_argument(
        "--lambda-lookback",
        type=int,
        default=24,
        help="Hours to look back for Lambda usage metrics (default: 24)",
    )
    parser.add_argument(
        "--spike-threshold",
        type=int,
        default=10,
        help="Alert if Lambda rate is Nx above baseline (default: 10)",
    )
    parser.add_argument(
        "--spike-window",
        type=int,
        default=5,
        help="Minutes to check for spike detection (default: 5)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # status command
    status_parser = subparsers.add_parser("status", help="Check current budget status")
    status_parser.add_argument("-v", "--verbose", action="store_true", help="Show resource details")

    # test command
    subparsers.add_parser("test", help="Test budget check (dry run)")

    # stop command
    stop_parser = subparsers.add_parser("stop", help="Stop all resources")
    stop_parser.add_argument("--confirm", action="store_true", help="Confirm stop action")
    stop_parser.add_argument("--dry-run", action="store_true", help="Show what would be stopped")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "status": cmd_status,
        "test": cmd_test,
        "stop": cmd_stop,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
