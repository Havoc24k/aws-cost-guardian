from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from src.aws_cost_guardian import BudgetGuardian


@pytest.fixture
def mock_cost_response():
    """Factory for Cost Explorer responses."""

    def _make_response(amount: str):
        return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}]}

    return _make_response


class TestBudgetDecisions:
    """Test _determine_action logic."""

    def test_ok_when_under_50_percent(self):
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        action, breached, actual_exceeded = guardian._determine_action(
            budget_percent=Decimal("40"),
            actual_spend=Decimal("300"),
        )
        assert action == "ok"
        assert breached == []
        assert actual_exceeded is False

    def test_alert_at_50_percent(self):
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        action, breached, actual_exceeded = guardian._determine_action(
            budget_percent=Decimal("55"),
            actual_spend=Decimal("400"),
        )
        assert action == "alert"
        assert 50 in breached
        assert actual_exceeded is False

    def test_alert_at_75_percent(self):
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        action, breached, actual_exceeded = guardian._determine_action(
            budget_percent=Decimal("80"),
            actual_spend=Decimal("600"),
        )
        assert action == "alert"
        assert 50 in breached
        assert 75 in breached
        assert actual_exceeded is False

    def test_stop_all_at_100_percent(self):
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        action, breached, actual_exceeded = guardian._determine_action(
            budget_percent=Decimal("105"),
            actual_spend=Decimal("800"),
        )
        assert action == "stop_all"
        assert actual_exceeded is False

    def test_stop_all_immediate_when_actual_exceeds(self):
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        action, breached, actual_exceeded = guardian._determine_action(
            budget_percent=Decimal("150"),
            actual_spend=Decimal("1100"),
        )
        assert action == "stop_all"
        assert actual_exceeded is True


class TestWithMockedAWS:
    """Integration tests with mocked AWS services."""

    @mock_aws
    def test_discover_resources_finds_ec2(self):
        """Test that EC2 instances are discovered."""
        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.run_instances(
            ImageId="ami-12345678",
            InstanceType="t3.micro",
            MinCount=1,
            MaxCount=1,
        )

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        resources = guardian._discover_resources()
        assert len(resources["ec2"]) == 1
        assert resources["ec2"][0]["type"] == "t3.micro"

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_hours_until_period_end")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_check_budget_returns_ok(self, mock_spikes, mock_hours, mock_hourly, mock_spend):
        """Test full budget check with mocked internals."""
        mock_spend.return_value = Decimal("100")
        mock_hourly.return_value = Decimal("0.10")
        mock_hours.return_value = 500
        mock_spikes.return_value = []

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        status = guardian.check_budget()

        # projected = 100 + (0.10 * 500) = $150 = 15%
        assert status.action == "ok"
        assert status.budget_percent < 50

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_hours_until_period_end")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_check_budget_triggers_alert(self, mock_spikes, mock_hours, mock_hourly, mock_spend):
        """Test alert when approaching budget."""
        mock_spend.return_value = Decimal("600")
        mock_hourly.return_value = Decimal("0.50")
        mock_hours.return_value = 400
        mock_spikes.return_value = []

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        status = guardian.check_budget()

        # projected = 600 + (0.50 * 400) = $800 = 80%
        assert status.action == "alert"
        assert 50 in status.thresholds_breached
        assert 75 in status.thresholds_breached

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_hours_until_period_end")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_check_budget_triggers_stop(self, mock_spikes, mock_hours, mock_hourly, mock_spend):
        """Test stop_all when over budget."""
        mock_spend.return_value = Decimal("900")
        mock_hourly.return_value = Decimal("1.00")
        mock_hours.return_value = 200
        mock_spikes.return_value = []

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        status = guardian.check_budget()

        # projected = 900 + (1.00 * 200) = $1100 = 110%
        assert status.action == "stop_all"


class TestLambdaSpikeDetection:
    """Test Lambda spike detection logic."""

    @patch("boto3.client")
    def test_spike_detected_when_ratio_exceeds_threshold(self, mock_boto):
        """Test spike detection with high current rate."""
        mock_cw = MagicMock()
        mock_boto.return_value = mock_cw

        # Current: 180 invocations in 5 min = 36/min
        # Baseline: 1008 invocations in 7 days = 0.1/min
        # Ratio: 36 / 0.1 = 360x
        mock_cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 180}]},
            {"Datapoints": [{"Sum": 1008}]},
        ]

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
            lambda_spike_threshold=10,
        )
        spike = guardian._check_lambda_spike("test-func", "us-east-1", 128)

        assert spike is not None
        assert spike.spike_ratio >= 10
        assert spike.function_name == "test-func"

    @patch("boto3.client")
    def test_no_spike_when_under_threshold(self, mock_boto):
        """Test no spike when ratio is low."""
        mock_cw = MagicMock()
        mock_boto.return_value = mock_cw

        # Current: 5 invocations in 5 min = 1/min
        # Baseline: 10080 invocations in 7 days = 1/min
        # Ratio: 1 / 1 = 1x (no spike)
        mock_cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 5}]},
            {"Datapoints": [{"Sum": 10080}]},
        ]

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
            lambda_spike_threshold=10,
        )
        spike = guardian._check_lambda_spike("test-func", "us-east-1", 128)

        assert spike is None


class TestBudgetPeriod:
    """Test budget period impact on projections.

    Key insight: $1000/month vs $10000/year have very different
    implications for the same hourly cost rate.
    """

    def test_period_bounds_with_custom_dates(self):
        """Test that custom period dates are parsed correctly."""
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
            budget_period_start="2026-01-01",
            budget_period_end="2026-02-28",
        )
        start, end = guardian._get_period_bounds()

        assert start.year == 2026
        assert start.month == 1
        assert start.day == 1
        assert end.year == 2026
        assert end.month == 2
        assert end.day == 28

    def test_remaining_hours_calculation(self):
        """Test hours remaining in budget period."""
        # Use a far future date so test doesn't become flaky
        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
            budget_period_start="2030-01-01",
            budget_period_end="2030-01-31",
        )
        hours = guardian._hours_until_period_end()

        # Should be positive (period is in future)
        assert hours > 0

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_short_period_triggers_stop(self, mock_spikes, mock_hourly, mock_spend):
        """Short period (1 week) with moderate hourly cost → over budget.

        Scenario: $1000 budget for 1 week
        - Actual spend: $500
        - Hourly cost: $5/hr
        - Remaining: ~168 hours (1 week)
        - Projected: $500 + ($5 * 168) = $1340 = 134% → STOP
        """
        mock_spend.return_value = Decimal("500")
        mock_hourly.return_value = Decimal("5.00")
        mock_spikes.return_value = []

        # 1 week period starting now
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end_date = datetime(now.year, now.month, now.day + 7 if now.day < 24 else 28)
        end = end_date.strftime("%Y-%m-%d")

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
            budget_period_start=start,
            budget_period_end=end,
        )
        status = guardian.check_budget()

        # With ~168 hours remaining: 500 + (5 * 168) = $1340 > $1000
        assert status.action == "stop_all"
        assert status.budget_percent > 100

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_hours_until_period_end")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_long_period_returns_ok(self, mock_spikes, mock_hours, mock_hourly, mock_spend):
        """Long period (1 year) with moderate hourly cost → under budget.

        Scenario: $10000 budget for 1 year
        - Actual spend: $500
        - Hourly cost: $0.50/hr
        - Remaining: 8760 hours (1 year)
        - Projected: $500 + ($0.50 * 8760) = $4880 = 49% → OK
        """
        mock_spend.return_value = Decimal("500")
        mock_hourly.return_value = Decimal("0.50")
        mock_hours.return_value = 8760  # 1 year in hours
        mock_spikes.return_value = []

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("10000"),
        )
        status = guardian.check_budget()

        # 500 + (0.50 * 8760) = $4880 = 49% of $10000
        assert status.action == "ok"
        assert status.budget_percent < 50

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_same_spend_different_periods(self, mock_spikes, mock_hourly, mock_spend):
        """Same actual spend, same hourly rate - different periods = different outcomes.

        This is the critical test: proves period matters.
        - $500 spent, $1/hr rate
        - 1 month (720 hrs, $1000 budget): projected = $500 + ($1 * 720) = $1220 = 122% → STOP
        - 1 year (8760 hrs, $10000 budget): projected = $500 + ($1 * 8760) = $9260 = 93% → ALERT
        """
        mock_spend.return_value = Decimal("500")
        mock_hourly.return_value = Decimal("1.00")
        mock_spikes.return_value = []

        # Short period: 1 month (720 hours), $1000 budget
        with patch.object(BudgetGuardian, "_hours_until_period_end", return_value=720):
            guardian_short = BudgetGuardian(
                regions=["us-east-1"],
                total_budget=Decimal("1000"),
            )
            status_short = guardian_short.check_budget()

        # Long period: 1 year (8760 hours), $10000 budget
        with patch.object(BudgetGuardian, "_hours_until_period_end", return_value=8760):
            guardian_long = BudgetGuardian(
                regions=["us-east-1"],
                total_budget=Decimal("10000"),
            )
            status_long = guardian_long.check_budget()

        # Short period should trigger stop (>100%)
        assert status_short.action == "stop_all"
        assert status_short.budget_percent > 100

        # Long period should only trigger alert (50-99%)
        assert status_long.action == "alert"
        assert 50 < status_long.budget_percent < 100

    @mock_aws
    @patch.object(BudgetGuardian, "_get_actual_spend")
    @patch.object(BudgetGuardian, "_calculate_hourly_cost")
    @patch.object(BudgetGuardian, "_detect_lambda_spikes")
    def test_period_near_end_amplifies_urgency(self, mock_spikes, mock_hourly, mock_spend):
        """Near end of period, even small hourly cost matters less.

        When period is almost over, remaining hours are low,
        so projection is mostly actual spend.
        - $800 spent, $10/hr rate
        - 10 hours remaining
        - Projected: $800 + ($10 * 10) = $900 = 90% → ALERT (not stop)
        """
        mock_spend.return_value = Decimal("800")
        mock_hourly.return_value = Decimal("10.00")
        mock_spikes.return_value = []

        # Period ends very soon (mock remaining hours)
        with patch.object(BudgetGuardian, "_hours_until_period_end", return_value=10):
            guardian = BudgetGuardian(
                regions=["us-east-1"],
                total_budget=Decimal("1000"),
            )
            status = guardian.check_budget()

        # 800 + (10 * 10) = $900 = 90%
        assert status.action == "alert"
        assert 90 <= status.budget_percent < 100


class TestAppRunner:
    """Test App Runner resource discovery and pausing."""

    @patch("boto3.client")
    def test_discover_apprunner_services(self, mock_boto):
        """Test that running App Runner services are discovered."""
        # Mock clients for different services
        mock_clients = {}

        def get_mock_client(service_name, **kwargs):
            if service_name not in mock_clients:
                mock_clients[service_name] = MagicMock()
            return mock_clients[service_name]

        mock_boto.side_effect = get_mock_client

        # Set up AppRunner mock (uses list_services directly, no paginator)
        mock_apprunner = mock_clients.setdefault("apprunner", MagicMock())
        mock_apprunner.list_services.return_value = {
            "ServiceSummaryList": [
                {
                    "ServiceName": "my-app",
                    "ServiceArn": "arn:aws:apprunner:us-east-1:123456789012:service/my-app/abc123",
                    "Status": "RUNNING",
                },
                {
                    "ServiceName": "paused-app",
                    "ServiceArn": "arn:aws:apprunner:us-east-1:123456789012:service/paused-app/def456",
                    "Status": "PAUSED",  # Should be excluded
                },
            ]
        }

        # Set up empty mocks for other services
        for svc in ["ec2", "rds", "lambda"]:
            mock_svc = mock_clients.setdefault(svc, MagicMock())
            mock_paginator = MagicMock()
            mock_svc.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = [
                {"Reservations": [], "DBInstances": [], "Functions": []}
            ]

        # Set up empty ECS mock
        mock_ecs = mock_clients.setdefault("ecs", MagicMock())
        mock_ecs.list_clusters.return_value = {"clusterArns": []}

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        resources = guardian._discover_resources()

        # Only RUNNING services should be discovered
        assert len(resources["apprunner"]) == 1
        assert resources["apprunner"][0]["name"] == "my-app"
        assert "arn" in resources["apprunner"][0]

    @patch("boto3.client")
    def test_apprunner_hourly_cost_calculation(self, mock_boto):
        """Test App Runner cost calculation from instance config."""
        mock_apprunner = MagicMock()
        mock_boto.return_value = mock_apprunner

        # 1 vCPU (1024 millicores), 2 GB memory
        mock_apprunner.describe_service.return_value = {
            "Service": {
                "InstanceConfiguration": {
                    "Cpu": "1024",
                    "Memory": "2048",
                }
            }
        }

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        cost = guardian._get_apprunner_hourly_cost(
            "arn:aws:apprunner:us-east-1:123456789012:service/test/abc",
            "us-east-1",
        )

        # Expected: 1 vCPU * $0.064 + 2 GB * $0.007 = $0.064 + $0.014 = $0.078
        assert cost == Decimal("0.078")

    @patch("boto3.client")
    def test_pause_apprunner_service(self, mock_boto):
        """Test pausing App Runner services."""
        mock_apprunner = MagicMock()
        mock_boto.return_value = mock_apprunner

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )

        resources = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [
                {
                    "name": "my-app",
                    "arn": "arn:aws:apprunner:us-east-1:123456789012:service/my-app/abc123",
                    "region": "us-east-1",
                }
            ],
            "ecs": [],
        }

        results = guardian.stop_all_resources(resources, dry_run=False)

        # Verify pause_service was called
        mock_apprunner.pause_service.assert_called_once_with(
            ServiceArn="arn:aws:apprunner:us-east-1:123456789012:service/my-app/abc123"
        )
        assert len(results["apprunner"]) == 1
        assert results["apprunner"][0]["status"] == "paused"

    @patch("boto3.client")
    def test_pause_apprunner_dry_run(self, mock_boto):
        """Test dry run does not actually pause services."""
        mock_apprunner = MagicMock()
        mock_boto.return_value = mock_apprunner

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )

        resources = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [
                {
                    "name": "my-app",
                    "arn": "arn:aws:apprunner:us-east-1:123456789012:service/my-app/abc123",
                    "region": "us-east-1",
                }
            ],
            "ecs": [],
        }

        results = guardian.stop_all_resources(resources, dry_run=True)

        # pause_service should NOT be called in dry run
        mock_apprunner.pause_service.assert_not_called()
        assert results["apprunner"][0]["status"] == "dry_run"


class TestECS:
    """Test ECS Fargate resource discovery and scaling down."""

    @patch("boto3.client")
    def test_discover_ecs_fargate_services(self, mock_boto):
        """Test that running ECS Fargate services are discovered."""
        mock_clients = {}

        def get_mock_client(service_name, **kwargs):
            if service_name not in mock_clients:
                mock_clients[service_name] = MagicMock()
            return mock_clients[service_name]

        mock_boto.side_effect = get_mock_client

        # Set up ECS mock
        mock_ecs = mock_clients.setdefault("ecs", MagicMock())
        mock_ecs.list_clusters.return_value = {
            "clusterArns": ["arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster"]
        }
        mock_ecs.list_services.return_value = {
            "serviceArns": ["arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-service"]
        }
        mock_ecs.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "my-service",
                    "serviceArn": "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-service",
                    "runningCount": 2,
                    "launchType": "FARGATE",
                    "taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/my-task:1",
                },
                {
                    "serviceName": "ec2-service",
                    "serviceArn": "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/ec2-service",
                    "runningCount": 1,
                    "launchType": "EC2",  # Should be excluded
                    "taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/ec2-task:1",
                },
            ]
        }

        # Set up empty mocks for other services
        for svc in ["ec2", "rds", "lambda", "apprunner"]:
            mock_svc = mock_clients.setdefault(svc, MagicMock())
            if svc == "apprunner":
                mock_svc.list_services.return_value = {"ServiceSummaryList": []}
            else:
                mock_paginator = MagicMock()
                mock_svc.get_paginator.return_value = mock_paginator
                mock_paginator.paginate.return_value = [
                    {"Reservations": [], "DBInstances": [], "Functions": []}
                ]

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        resources = guardian._discover_resources()

        # Only FARGATE services with running tasks should be discovered
        assert len(resources["ecs"]) == 1
        assert resources["ecs"][0]["name"] == "my-service"
        assert resources["ecs"][0]["running_count"] == 2

    @patch("boto3.client")
    def test_ecs_hourly_cost_calculation(self, mock_boto):
        """Test ECS Fargate cost calculation from task definition."""
        mock_ecs = MagicMock()
        mock_boto.return_value = mock_ecs

        # 1 vCPU (1024 units), 2 GB memory (2048 MB)
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "cpu": "1024",
                "memory": "2048",
            }
        }

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )
        cost = guardian._get_ecs_hourly_cost(
            "arn:aws:ecs:us-east-1:123456789012:task-definition/my-task:1",
            running_count=2,
            region="us-east-1",
        )

        # Expected per task: 1 vCPU * $0.04048 + 2 GB * $0.004445 = $0.04048 + $0.00889 = $0.04937
        # With 2 tasks: $0.04937 * 2 = $0.09874
        expected = (Decimal("0.04048") + Decimal("2") * Decimal("0.004445")) * 2
        assert cost == expected

    @patch("boto3.client")
    def test_scale_down_ecs_service(self, mock_boto):
        """Test scaling down ECS services."""
        mock_ecs = MagicMock()
        mock_boto.return_value = mock_ecs

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )

        resources = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [],
            "ecs": [
                {
                    "name": "my-service",
                    "arn": "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-service",
                    "cluster": "arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster",
                    "region": "us-east-1",
                    "running_count": 2,
                    "task_definition": "arn:aws:ecs:us-east-1:123456789012:task-definition/my-task:1",
                }
            ],
        }

        results = guardian.stop_all_resources(resources, dry_run=False)

        # Verify update_service was called with desiredCount=0
        mock_ecs.update_service.assert_called_once_with(
            cluster="arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster",
            service="my-service",
            desiredCount=0,
        )
        assert len(results["ecs"]) == 1
        assert results["ecs"][0]["status"] == "scaled_down"

    @patch("boto3.client")
    def test_scale_down_ecs_dry_run(self, mock_boto):
        """Test dry run does not actually scale down services."""
        mock_ecs = MagicMock()
        mock_boto.return_value = mock_ecs

        guardian = BudgetGuardian(
            regions=["us-east-1"],
            total_budget=Decimal("1000"),
        )

        resources = {
            "ec2": [],
            "rds": [],
            "lambda": [],
            "apprunner": [],
            "ecs": [
                {
                    "name": "my-service",
                    "arn": "arn:aws:ecs:us-east-1:123456789012:service/my-cluster/my-service",
                    "cluster": "arn:aws:ecs:us-east-1:123456789012:cluster/my-cluster",
                    "region": "us-east-1",
                    "running_count": 2,
                    "task_definition": "arn:aws:ecs:us-east-1:123456789012:task-definition/my-task:1",
                }
            ],
        }

        results = guardian.stop_all_resources(resources, dry_run=True)

        # update_service should NOT be called in dry run
        mock_ecs.update_service.assert_not_called()
        assert results["ecs"][0]["status"] == "dry_run"
