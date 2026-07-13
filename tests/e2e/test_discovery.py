"""E2E: BudgetGuardian discovers resources seeded into Floci."""

from decimal import Decimal

import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def test_discovers_seeded_fleet(clean_account):
    seeds.seed_ec2(count=2, instance_type="t3.medium")
    seeds.seed_lambda("e2e-fn", memory=256)
    seeds.seed_ecs_fargate(cluster="e2e-cluster", service="e2e-svc")

    guardian = BudgetGuardian(regions=["us-east-1"], total_budget=Decimal("1000"))
    resources = guardian._discover_resources()

    assert len(resources["ec2"]) == 2
    assert {"ec2", "rds", "lambda", "ecs"} == set(resources)
    assert any(f["name"] == "e2e-fn" for f in resources["lambda"])
    assert any(s["name"] == "e2e-svc" for s in resources["ecs"])
