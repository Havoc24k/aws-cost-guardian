"""E2E: action transitions are driven by the budget lever, not exact cost."""

from decimal import Decimal

import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def _guardian(budget: str) -> BudgetGuardian:
    return BudgetGuardian(regions=["us-east-1"], total_budget=Decimal(budget))


def test_huge_budget_is_ok(clean_account):
    seeds.seed_ec2(count=1)
    status = _guardian("100000000").check_budget()
    assert status.hourly_cost > 0  # pricing path actually ran
    assert status.action == "ok"


def test_tiny_budget_triggers_stop(clean_account):
    seeds.seed_ec2(count=1)
    status = _guardian("0.01").check_budget()
    assert status.action == "stop_all"
