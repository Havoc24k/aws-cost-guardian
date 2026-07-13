"""E2E: stop_all actually mutates Floci state (proven by re-query)."""

from decimal import Decimal

import boto3
import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def test_stop_all_stops_ec2_and_throttles_lambda(clean_account):
    [instance_id] = seeds.seed_ec2(count=1)
    seeds.seed_lambda("e2e-throttle-fn")

    guardian = BudgetGuardian(regions=["us-east-1"], total_budget=Decimal("0.01"))
    result = guardian.run(dry_run=False)
    assert result["status"].action == "stop_all"

    ec2 = boto3.client("ec2", region_name="us-east-1")
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("stopping", "stopped")

    lam = boto3.client("lambda", region_name="us-east-1")
    concurrency = lam.get_function_concurrency(FunctionName="e2e-throttle-fn")
    assert concurrency.get("ReservedConcurrentExecutions") == 0
