"""Phase 0: confirm Floci responds and the endpoint redirect works."""

import boto3
import pytest

pytestmark = pytest.mark.e2e


def test_sts_reaches_floci():
    sts = boto3.client("sts", region_name="us-east-1")
    identity = sts.get_caller_identity()
    assert identity["Account"] == "000000000000", (
        "Expected Floci's dummy account — refusing to run destructive e2e tests against real AWS"
    )


def test_cost_explorer_responds():
    from datetime import datetime, timedelta, timezone

    ce = boto3.client("ce", region_name="us-east-1")
    now = datetime.now(timezone.utc)
    resp = ce.get_cost_and_usage(
        TimePeriod={
            "Start": (now - timedelta(days=2)).strftime("%Y-%m-%d"),
            "End": now.strftime("%Y-%m-%d"),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
    )
    assert "ResultsByTime" in resp
