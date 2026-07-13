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
    # Discovery actually found the seeded instance — this is what proves the
    # discovery -> cost-calculation -> projection wiring ran end-to-end.
    assert len(status.resources["ec2"]) == 1
    # Floci does not implement TERM_MATCH filtering on the Pricing API, so the
    # guardian's instanceType-filtered GetProducts call returns zero results and
    # _get_ec2_hourly_cost() silently falls back to DEFAULT_EC2_HOURLY. This assertion
    # only proves the cost-calculation pipeline ran over the discovered resource and
    # produced a nonzero number via that fallback constant — it does NOT prove Floci's
    # Pricing API was successfully queried.
    assert status.hourly_cost > 0
    assert status.action == "ok"


def test_tiny_budget_triggers_stop(clean_account):
    seeds.seed_ec2(count=1)
    status = _guardian("0.01").check_budget()
    assert status.action == "stop_all"
