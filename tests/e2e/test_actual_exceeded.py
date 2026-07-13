"""E2E canary: budget=0 forces the immediate (actual-exceeded) stop path when spend > 0.

The `actual_exceeded` decision logic itself is already fully covered by the unit test
`test_stop_all_immediate_when_actual_exceeds` in tests/test_budget_scenarios.py, which
proves `_determine_action()` forces `stop_all` (with `actual_exceeded=True`) whenever
`actual_spend > budget`, regardless of projection.

What this e2e test can additionally prove — and what the unit test cannot — is that the
REAL Cost Explorer call against Floci returns a nonzero month-to-date spend that flows
through `check_budget()` into that same decision. As of this writing, Floci's Cost
Explorer synthesizes ~$0 month-to-date spend (observed values like `0E-10` and
`1.2E-9`), so `actual_spend > Decimal("0")` is False and the immediate-stop path is
never actually exercised end-to-end. Rather than fake a nonzero spend to force a "pass",
this test skips with an explicit reason when that happens. It is a canary: it will start
asserting for real, with no code changes needed, if/when Floci's Cost Explorer starts
accruing nonzero cost.
"""

from decimal import Decimal

import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def test_zero_budget_with_spend_is_immediate_stop(clean_account):
    seeds.seed_ec2(count=1)
    status = BudgetGuardian(regions=["us-east-1"], total_budget=Decimal("0")).check_budget()

    # Only assert actual_exceeded if Floci synthesized nonzero month-to-date spend.
    if status.actual_spend > 0:
        assert status.actual_exceeded is True
        assert status.action == "stop_all"
    else:
        pytest.skip(
            f"Floci Cost Explorer reported zero month-to-date spend "
            f"(actual_spend={status.actual_spend}), so 'actual_spend > budget' cannot be "
            "true and the immediate-stop path can't be exercised end-to-end. This is a "
            "known Floci limitation, not a guardian bug — see module docstring."
        )
