"""Tests for the AWS-currency-audit bug fixes.

Each test here proves one real bug found by auditing the code against current AWS docs.
Every test FAILS against the pre-fix code and passes after the fix.
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.aws_cost_guardian import BudgetGuardian


def _mock_boto_factory(mock_boto):
    """Wire boto3.client(...) to per-service MagicMocks; returns the registry."""
    clients: dict = {}

    def get_client(service_name, **kwargs):
        if service_name not in clients:
            clients[service_name] = MagicMock()
        return clients[service_name]

    mock_boto.side_effect = get_client
    return clients


def _empty_paginated(clients, *services):
    """Give services an empty paginator so discovery finds nothing for them."""
    for svc in services:
        mock_svc = clients.setdefault(svc, MagicMock())
        paginator = MagicMock()
        mock_svc.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{"Reservations": [], "DBInstances": [], "Functions": []}]


def _price_entry(usagetype: str, price: str) -> str:
    """Build a Pricing API PriceList entry (a JSON string, as AWS returns)."""
    return json.dumps(
        {
            "product": {"attributes": {"usagetype": usagetype}},
            "terms": {
                "OnDemand": {"x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": price}}}}}
            },
        }
    )


def _guardian() -> BudgetGuardian:
    return BudgetGuardian(regions=["us-east-1"], total_budget=Decimal("1000"))


class TestRdsPricingEngineName:
    """Audit #1: the code sent boto3 engine codes ('postgres') to the Pricing API,
    which expects display names ('PostgreSQL'). Every RDS price lookup silently missed
    and fell back to a flat constant."""

    @patch("boto3.client")
    def test_engine_code_is_mapped_to_pricing_display_name(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        pricing = clients.setdefault("pricing", MagicMock())
        pricing.get_products.return_value = {
            "PriceList": [_price_entry("InstanceUsage:db.t3.medium", "0.068")]
        }
        clients.setdefault("ssm", MagicMock()).get_parameter.return_value = {
            "Parameter": {"Value": "US East (N. Virginia)"}
        }

        guardian = _guardian()
        price = guardian._get_rds_hourly_cost("db.t3.medium", "postgres", "us-east-1")

        filters = pricing.get_products.call_args.kwargs["Filters"]
        engine_filter = next(f for f in filters if f["Field"] == "databaseEngine")
        # The Pricing API only matches the DISPLAY name, never the boto3 engine code.
        assert engine_filter["Value"] == "PostgreSQL"
        assert price == Decimal("0.068")

    @patch("boto3.client")
    def test_aurora_engine_code_is_mapped(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        pricing = clients.setdefault("pricing", MagicMock())
        pricing.get_products.return_value = {"PriceList": [_price_entry("x", "0.29")]}
        clients.setdefault("ssm", MagicMock()).get_parameter.return_value = {
            "Parameter": {"Value": "US East (N. Virginia)"}
        }

        _guardian()._get_rds_hourly_cost("db.r5.large", "aurora-postgresql", "us-east-1")

        filters = pricing.get_products.call_args.kwargs["Filters"]
        engine_filter = next(f for f in filters if f["Field"] == "databaseEngine")
        assert engine_filter["Value"] == "Aurora PostgreSQL"


class TestRdsBillableStatusDiscovery:
    """Audit #9: discovery only counted DBInstanceStatus == 'available', but many other
    states are fully billed (backing-up, modifying, ...). Those were invisible to the
    guardian, so spend was under-counted."""

    @patch("boto3.client")
    def test_backing_up_instance_is_discovered(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        _empty_paginated(clients, "ec2", "lambda")
        clients.setdefault("ecs", MagicMock()).list_clusters.return_value = {"clusterArns": []}

        rds = clients.setdefault("rds", MagicMock())
        paginator = MagicMock()
        rds.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "db-backing-up",
                        "DBInstanceClass": "db.t3.medium",
                        "Engine": "postgres",
                        "DBInstanceStatus": "backing-up",  # billed, but not "available"
                    }
                ]
            }
        ]

        resources = _guardian()._discover_resources()
        assert [d["id"] for d in resources["rds"]] == ["db-backing-up"]

    @patch("boto3.client")
    def test_stopped_instance_is_not_discovered(self, mock_boto):
        """Guard against over-correction: a stopped DB costs no instance-hours."""
        clients = _mock_boto_factory(mock_boto)
        _empty_paginated(clients, "ec2", "lambda")
        clients.setdefault("ecs", MagicMock()).list_clusters.return_value = {"clusterArns": []}

        rds = clients.setdefault("rds", MagicMock())
        paginator = MagicMock()
        rds.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "db-stopped",
                        "DBInstanceClass": "db.t3.medium",
                        "Engine": "postgres",
                        "DBInstanceStatus": "stopped",
                    }
                ]
            }
        ]

        assert _guardian()._discover_resources()["rds"] == []


class TestAuroraRemediation:
    """Audit #6: Aurora cluster members cannot be stopped with stop_db_instance
    ('use StopDBCluster instead'). The guardian's stop attempt errored out and the
    Aurora cluster kept running and kept billing."""

    @patch("boto3.client")
    def test_aurora_uses_stop_db_cluster(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        rds = clients.setdefault("rds", MagicMock())

        resources = {
            "ec2": [],
            "lambda": [],
            "ecs": [],
            "rds": [
                {
                    "id": "aurora-member-1",
                    "class": "db.r5.large",
                    "engine": "aurora-postgresql",
                    "region": "us-east-1",
                    "cluster_id": "my-aurora-cluster",
                }
            ],
        }

        results = _guardian().stop_all_resources(resources, dry_run=False)

        rds.stop_db_cluster.assert_called_once_with(DBClusterIdentifier="my-aurora-cluster")
        rds.stop_db_instance.assert_not_called()
        assert results["rds"][0]["status"] == "stopped"

    @patch("boto3.client")
    def test_aurora_cluster_stopped_once_for_multiple_members(self, mock_boto):
        """Two members of one cluster must produce ONE StopDBCluster call, not two."""
        clients = _mock_boto_factory(mock_boto)
        rds = clients.setdefault("rds", MagicMock())

        member = {
            "class": "db.r5.large",
            "engine": "aurora-mysql",
            "region": "us-east-1",
            "cluster_id": "cluster-a",
        }
        resources = {
            "ec2": [],
            "lambda": [],
            "ecs": [],
            "rds": [{**member, "id": "m1"}, {**member, "id": "m2"}],
        }

        _guardian().stop_all_resources(resources, dry_run=False)

        assert rds.stop_db_cluster.call_count == 1

    @patch("boto3.client")
    def test_non_aurora_still_uses_stop_db_instance(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        rds = clients.setdefault("rds", MagicMock())

        resources = {
            "ec2": [],
            "lambda": [],
            "ecs": [],
            "rds": [
                {"id": "pg-1", "class": "db.t3.medium", "engine": "postgres", "region": "us-east-1"}
            ],
        }

        _guardian().stop_all_resources(resources, dry_run=False)

        rds.stop_db_instance.assert_called_once_with(DBInstanceIdentifier="pg-1")
        rds.stop_db_cluster.assert_not_called()


class TestRdsUnstoppableInstances:
    """Audit #8: AWS refuses to stop read replicas, replication sources, and SQL Server
    Multi-AZ instances. The guardian called stop_db_instance blindly and just errored."""

    @patch("boto3.client")
    def test_read_replica_is_skipped_not_errored(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        rds = clients.setdefault("rds", MagicMock())

        resources = {
            "ec2": [],
            "lambda": [],
            "ecs": [],
            "rds": [
                {
                    "id": "replica-1",
                    "class": "db.t3.medium",
                    "engine": "postgres",
                    "region": "us-east-1",
                    "is_read_replica": True,
                }
            ],
        }

        results = _guardian().stop_all_resources(resources, dry_run=False)

        rds.stop_db_instance.assert_not_called()
        assert results["rds"][0]["status"] == "skipped"
        assert "replica" in results["rds"][0]["reason"].lower()

    @patch("boto3.client")
    def test_sqlserver_multi_az_is_skipped(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        rds = clients.setdefault("rds", MagicMock())

        resources = {
            "ec2": [],
            "lambda": [],
            "ecs": [],
            "rds": [
                {
                    "id": "mssql-1",
                    "class": "db.m5.large",
                    "engine": "sqlserver-se",
                    "region": "us-east-1",
                    "multi_az": True,
                }
            ],
        }

        results = _guardian().stop_all_resources(resources, dry_run=False)

        rds.stop_db_instance.assert_not_called()
        assert results["rds"][0]["status"] == "skipped"


class TestCloudWatchDatapointsSummed:
    """Audit follow-up (found by the Floci e2e layer): the code read Datapoints[0],
    assuming CloudWatch returns exactly one bucket per window. AWS guarantees neither a
    single bucket nor any ordering, so [0] is an arbitrary bucket."""

    @patch("boto3.client")
    def test_spike_baseline_sums_all_datapoints(self, mock_boto):
        """Baseline split across two buckets. Reading only [0] (=0) makes baseline_rate 0,
        which the code treats as 'no baseline' -> spike_ratio 999 -> FALSE POSITIVE.
        Summing correctly gives a busy baseline, so there is no spike."""
        clients = _mock_boto_factory(mock_boto)
        cw = clients.setdefault("cloudwatch", MagicMock())

        def metrics(**kwargs):
            # short window (5 min) vs baseline window (168h), distinguished by Period
            if kwargs["Period"] == 5 * 60:
                return {"Datapoints": [{"Sum": 50.0}]}  # 50 / 5min = 10/min
            return {"Datapoints": [{"Sum": 0.0}, {"Sum": 100000.0}]}  # total 100000

        cw.get_metric_statistics.side_effect = metrics

        guardian = _guardian()
        spike = guardian._check_lambda_spike("fn", "us-east-1", 128)

        # baseline = 100000 / 10080min = 9.92/min; current = 10/min; ratio ~1.008 < 10.
        assert spike is None, "summing the baseline buckets must not report a spurious spike"

    @patch("boto3.client")
    def test_lambda_cost_sums_all_invocation_datapoints(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        cw = clients.setdefault("cloudwatch", MagicMock())

        def metrics(**kwargs):
            if kwargs["MetricName"] == "Invocations":
                return {"Datapoints": [{"Sum": 100.0}, {"Sum": 900.0}]}  # total 1000
            return {"Datapoints": [{"Sum": 0.0}]}  # zero duration -> only request cost

        cw.get_metric_statistics.side_effect = metrics

        guardian = BudgetGuardian(
            regions=["us-east-1"], total_budget=Decimal("1000"), lambda_lookback_hours=24
        )
        cost = guardian._get_lambda_hourly_cost("fn", "us-east-1", 128)

        # 1000 invocations / 24h = 41.667/hr * $0.0000002 per request
        expected = (Decimal("1000") / 24) * Decimal("0.0000002")
        assert cost == expected


class TestFargatePricingSelection:
    """Audit #3: the Fargate query matched FOUR products (Linux/x86, ARM, Windows, and
    ECS-on-EC2 at $0.00) but took PriceList[0] with MaxResults=1, so it could silently
    project ARM, Windows, or even $0.00 rates."""

    @patch("boto3.client")
    def test_picks_linux_x86_rate_not_arm_windows_or_zero(self, mock_boto):
        clients = _mock_boto_factory(mock_boto)
        pricing = clients.setdefault("pricing", MagicMock())
        clients.setdefault("ssm", MagicMock()).get_parameter.return_value = {
            "Parameter": {"Value": "US East (N. Virginia)"}
        }

        def get_products(**kwargs):
            fields = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
            if "cputype" in fields:
                return {
                    "PriceList": [
                        # The $0.00 ECS-on-EC2 entry deliberately comes FIRST.
                        _price_entry("USE1-ECS-EC2-vCPU-Hours", "0.0000000000"),
                        _price_entry("USE1-Fargate-ARM-vCPU-Hours:perCPU", "0.03238"),
                        _price_entry("USE1-Fargate-Windows-vCPU-Hours:perCPU", "0.046552"),
                        _price_entry("USE1-Fargate-vCPU-Hours:perCPU", "0.04048"),
                    ]
                }
            return {
                "PriceList": [
                    _price_entry("USE1-ECS-EC2-GB-Hours", "0.0000000000"),
                    _price_entry("USE1-Fargate-ARM-GB-Hours", "0.00356"),
                    _price_entry("USE1-Fargate-Windows-GB-Hours", "0.00511175"),
                    _price_entry("USE1-Fargate-GB-Hours", "0.004445"),
                ]
            }

        pricing.get_products.side_effect = get_products

        cpu_rate, memory_rate = _guardian()._get_fargate_unit_prices("us-east-1")

        assert cpu_rate == Decimal("0.04048"), "must select the Linux/x86 Fargate vCPU rate"
        assert memory_rate == Decimal("0.004445"), "must select the Linux/x86 Fargate GB rate"
