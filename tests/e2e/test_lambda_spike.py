"""E2E: a Lambda invocation spike seeded via CloudWatch is detected."""

from decimal import Decimal

import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def test_spike_detected_from_cloudwatch_metrics(clean_account):
    seeds.seed_lambda("e2e-spiky-fn")
    seeds.seed_lambda_spike_metrics("e2e-spiky-fn")

    guardian = BudgetGuardian(
        regions=["us-east-1"], total_budget=Decimal("1000000"), lambda_spike_threshold=10
    )
    status = guardian.check_budget()

    assert any(s.function_name == "e2e-spiky-fn" for s in status.lambda_spikes)
    assert status.action == "spike_alert"
