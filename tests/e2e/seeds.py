"""Resource-seeding and teardown helpers for Floci e2e tests.

Every call relies on AWS_ENDPOINT_URL (set by the aws_env fixture) to reach Floci.
These calls are standard AWS API calls — they would also work against real AWS / moto.
"""

import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone

import boto3

_ASSUME_ROLE = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def _lambda_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.py", "def handler(event, context):\n    return {}\n")
    return buf.getvalue()


# VERIFIED: a real AMI that Floci ships. A made-up AMI id makes the instance terminate instantly.
FLOCI_AMI = "ami-0abcdef1234567890"

# Floci starts real Docker containers, so resources are NOT instantly available.
SEED_TIMEOUT_SECONDS = 180


def seed_ec2(
    count: int = 1, instance_type: str = "t3.medium", region: str = "us-east-1"
) -> list[str]:
    """Launch EC2 instances and WAIT until they are 'running'.

    The guardian only discovers instances in the 'running' state, so returning before
    they get there would make discovery find nothing.
    """
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.run_instances(
        ImageId=FLOCI_AMI, MinCount=count, MaxCount=count, InstanceType=instance_type
    )
    ids = [i["InstanceId"] for i in resp["Instances"]]

    deadline = time.time() + SEED_TIMEOUT_SECONDS
    while time.time() < deadline:
        states = [
            i["State"]["Name"]
            for r in ec2.describe_instances(InstanceIds=ids)["Reservations"]
            for i in r["Instances"]
        ]
        if all(s == "running" for s in states):
            return ids
        if any(s in ("terminated", "shutting-down") for s in states):
            raise RuntimeError(
                f"EC2 seed died (states={states}) — is /var/run/docker.sock mounted?"
            )
        time.sleep(3)
    raise TimeoutError(
        f"EC2 instances {ids} never reached 'running' within {SEED_TIMEOUT_SECONDS}s"
    )


def seed_rds(
    identifier: str,
    engine: str = "postgres",
    db_class: str = "db.t3.medium",
    region: str = "us-east-1",
) -> str:
    rds = boto3.client("rds", region_name=region)
    rds.create_db_instance(
        DBInstanceIdentifier=identifier,
        Engine=engine,
        DBInstanceClass=db_class,
        AllocatedStorage=20,
        MasterUsername="admin",
        MasterUserPassword="Password123!",
    )
    for _ in range(30):
        status = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0][
            "DBInstanceStatus"
        ]
        if status == "available":
            break
        time.sleep(1)
    return identifier


def seed_lambda(name: str, memory: int = 128, region: str = "us-east-1") -> str:
    iam = boto3.client("iam", region_name=region)
    role_name = f"{name}-role"
    try:
        arn = iam.create_role(
            RoleName=role_name, AssumeRolePolicyDocument=json.dumps(_ASSUME_ROLE)
        )["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    lam = boto3.client("lambda", region_name=region)
    lam.create_function(
        FunctionName=name,
        Runtime="python3.12",
        Role=arn,
        Handler="index.handler",
        Code={"ZipFile": _lambda_zip()},
        MemorySize=memory,
        Timeout=30,
    )
    return name


def seed_ecs_fargate(cluster: str, service: str, region: str = "us-east-1") -> dict:
    ecs = boto3.client("ecs", region_name=region)
    ecs.create_cluster(clusterName=cluster)
    td_arn = ecs.register_task_definition(
        family=f"{service}-td",
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[{"name": "app", "image": "public.ecr.aws/nginx/nginx:latest"}],
    )["taskDefinition"]["taskDefinitionArn"]
    ecs.create_service(
        cluster=cluster,
        serviceName=service,
        taskDefinition=td_arn,
        desiredCount=1,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {"subnets": ["subnet-12345678"], "assignPublicIp": "ENABLED"}
        },
    )

    # VERIFIED: Floci takes ~40s to actually start the Fargate task. The guardian only
    # discovers services with runningCount > 0, so we must wait for it.
    deadline = time.time() + SEED_TIMEOUT_SECONDS
    while time.time() < deadline:
        desc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
        if desc.get("runningCount", 0) > 0:
            return {"cluster": cluster, "service": service, "task_definition": td_arn}
        time.sleep(3)
    raise TimeoutError(
        f"ECS service {service} never reached runningCount>0 within {SEED_TIMEOUT_SECONDS}s "
        "— is /var/run/docker.sock mounted?"
    )


def seed_lambda_spike_metrics(function_name: str, region: str = "us-east-1") -> None:
    """Low baseline over 7 days, high burst in the last few minutes."""
    cw = boto3.client("cloudwatch", region_name=region)
    now = datetime.now(timezone.utc)
    points = [
        {"Timestamp": now - timedelta(days=5), "Value": 10.0},  # baseline
        {"Timestamp": now - timedelta(minutes=2), "Value": 5000.0},  # spike
    ]
    cw.put_metric_data(
        Namespace="AWS/Lambda",
        MetricData=[
            {
                "MetricName": "Invocations",
                "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
                "Timestamp": p["Timestamp"],
                "Value": p["Value"],
                "Unit": "Count",
            }
            for p in points
        ],
    )


def teardown_all(region: str = "us-east-1") -> None:
    """Best-effort removal of everything e2e tests create. Safe to call repeatedly."""
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ids = [
            i["InstanceId"]
            for r in ec2.describe_instances()["Reservations"]
            for i in r["Instances"]
            if i["State"]["Name"] not in ("terminated", "shutting-down")
        ]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
    except Exception:
        pass
    rds = boto3.client("rds", region_name=region)
    try:
        for db in rds.describe_db_instances()["DBInstances"]:
            rds.delete_db_instance(
                DBInstanceIdentifier=db["DBInstanceIdentifier"], SkipFinalSnapshot=True
            )
    except Exception:
        pass
    lam = boto3.client("lambda", region_name=region)
    try:
        for fn in lam.list_functions()["Functions"]:
            lam.delete_function(FunctionName=fn["FunctionName"])
    except Exception:
        pass
    ecs = boto3.client("ecs", region_name=region)
    try:
        for c in ecs.list_clusters()["clusterArns"]:
            for s in ecs.list_services(cluster=c)["serviceArns"]:
                ecs.update_service(cluster=c, service=s, desiredCount=0)
                ecs.delete_service(cluster=c, service=s, force=True)
            ecs.delete_cluster(cluster=c)
    except Exception:
        pass
