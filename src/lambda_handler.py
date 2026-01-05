"""
AWS Lambda Handler for Cost Guardian
Triggered by CloudWatch Events on a schedule (e.g., every hour).
"""

import json
import logging
import os

from aws_cost_guardian import BudgetGuardian

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def handler(event, _context):
    """Lambda entry point."""
    logger.info("Cost Guardian triggered. Event: %s", json.dumps(event))

    dry_run = event.get("dry_run", DRY_RUN)
    guardian = BudgetGuardian.from_env()

    logger.info("Checking budget: $%s across %s", guardian.budget, guardian.regions)

    result = guardian.run(dry_run=dry_run)
    status = result["status"]

    logger.info(
        "Budget check complete. Actual: $%.2f, Projected: $%.2f, Budget: %.1f%%, Action: %s",
        status.actual_spend,
        status.projected_total,
        status.budget_percent,
        status.action,
    )

    if status.action != "ok":
        logger.warning(
            "ACTION: %s - Thresholds breached: %s%%", status.action, status.thresholds_breached
        )

    return {
        "statusCode": 200,
        "body": {
            "actual_spend": float(status.actual_spend),
            "projected_total": float(status.projected_total),
            "budget": float(status.budget),
            "budget_percent": float(status.budget_percent),
            "action": status.action,
            "alert_sent": result["alert_sent"],
            "resources": {
                "ec2": len(status.resources["ec2"]),
                "rds": len(status.resources["rds"]),
                "lambda": len(status.resources["lambda"]),
            },
            "dry_run": dry_run,
        },
    }


if __name__ == "__main__":
    # Local test
    os.environ.setdefault("REGIONS", '["us-east-1"]')
    os.environ.setdefault("TOTAL_BUDGET", "1000")
    os.environ.setdefault("DRY_RUN", "true")

    test_result = handler({"dry_run": True}, None)
    print(json.dumps(test_result, indent=2))
