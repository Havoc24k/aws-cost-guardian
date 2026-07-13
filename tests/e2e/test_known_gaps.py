"""E2E tests that document known audit gaps. Each xfail is a backlog item.
When the underlying bug is fixed, remove the xfail marker and the test should pass.
"""

import time
from decimal import Decimal

import boto3
import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(
    reason="audit #7: Fargate services on capacityProviderStrategy have launchType omitted, "
    "so the `launchType == 'FARGATE'` discovery check misses them",
    strict=False,
)
def test_capacity_provider_fargate_is_discovered(clean_account):
    region = "us-east-1"
    ecs = boto3.client("ecs", region_name=region)
    ecs.create_cluster(clusterName="cp-cluster", capacityProviders=["FARGATE"])
    td = ecs.register_task_definition(
        family="cp-td",
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[{"name": "app", "image": "public.ecr.aws/nginx/nginx:latest"}],
    )["taskDefinition"]["taskDefinitionArn"]
    ecs.create_service(
        cluster="cp-cluster",
        serviceName="cp-svc",
        taskDefinition=td,
        desiredCount=1,
        capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        networkConfiguration={
            "awsvpcConfiguration": {"subnets": ["subnet-12345678"], "assignPublicIp": "ENABLED"}
        },
    )

    # Floci starts a real Docker container for the task; wait for it like seed_ecs_fargate does,
    # otherwise the assertion below would fail on runningCount==0 instead of on the launchType gap.
    deadline = time.time() + seeds.SEED_TIMEOUT_SECONDS
    while time.time() < deadline:
        desc = ecs.describe_services(cluster="cp-cluster", services=["cp-svc"])["services"][0]
        if desc.get("runningCount", 0) > 0:
            break
        time.sleep(3)

    resources = BudgetGuardian(regions=[region], total_budget=Decimal("1000"))._discover_resources()
    assert any(s["name"] == "cp-svc" for s in resources["ecs"])
