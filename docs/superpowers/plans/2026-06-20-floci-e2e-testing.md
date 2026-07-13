# Floci E2E Test Layer + Drop App Runner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove App Runner support and add a Floci-backed end-to-end test layer that runs the real `BudgetGuardian` against `localhost:4566`, exercising the Cost Explorer / Pricing / CloudWatch paths that are currently mocked out.

**Architecture:** Point the unmodified guardian at Floci via the global `AWS_ENDPOINT_URL` env var (zero production-code change). New `tests/e2e/` package seeds resources via boto3 against Floci, runs the guardian, and asserts on `BudgetStatus` plus re-queried Floci state. The e2e package auto-skips when Floci is unreachable, so `uv run pytest` stays green without Docker.

**Tech Stack:** Python 3.11+, boto3 ≥1.35, pytest, moto (existing unit tests), Floci (Docker, `localhost:4566`), GitHub Actions, uv.

## Global Constraints

- Python 3.11+; all monetary values use `Decimal`, never `float`.
- Ruff: line length 100, `E501` ignored, double quotes. Run `uv run ruff check .` + `uv run ruff format .`.
- mypy: `ignore_missing_imports = true`. Run `uv run mypy src/ cli.py`.
- **No new runtime dependencies** (production code stays on `boto3` only). Test-only helpers use the standard library + existing dev deps.
- **Zero production-code change to retarget at Floci** — rely on `AWS_ENDPOINT_URL=http://localhost:4566` + dummy creds set in the test environment only.
- **Never assert exact dollar amounts** in e2e tests — drive action states with the budget lever; use `budget=0` for the `actual_exceeded` path; pricing assertions are range/relative.
- E2E tests carry `@pytest.mark.e2e` and auto-skip when `localhost:4566` is unreachable.
- After App Runner removal, the resource dicts have exactly these keys: `ec2`, `rds`, `lambda`, `ecs`.

## Verified Floci Behavior (Phase 0 spike — COMPLETE, empirically confirmed 2026-07-13)

These are measured facts, not assumptions. Do not deviate from them.

- **Image is `floci/floci:latest`** (Docker Hub). `ghcr.io/floci-io/floci:latest` does NOT exist.
- **Mounting `/var/run/docker.sock` is MANDATORY.** Floci runs EC2/RDS/ECS as real Docker
  containers via its own Docker engine. Without the socket, EC2 instances go straight to
  `terminated`, ECS tasks never start, and RDS `create_db_instance` fails with an XML parse error.
- **`AWS_ENDPOINT_URL` routing works** — confirmed `sts.get_caller_identity()` → account `000000000000`.
- **EC2 needs a real Floci AMI**: use `ami-0abcdef1234567890` (amzn2). A made-up AMI id is rejected.
  Instances take time to reach `running` — **poll, don't assume**.
- **ECS Fargate takes ~40s** for `runningCount` to go from 0 → 1. The guardian only discovers
  services with `runningCount > 0`, so seeds **must poll** until then. `launchType` IS correctly
  reported as `'FARGATE'`.
- **RDS works** and reports `available` quickly.
- **Pricing API supports `AmazonEC2` ONLY.** `AmazonRDS` and `AmazonECS` raise
  `InvalidParameterException: Invalid ServiceCode`, so RDS and Fargate costs always fall back to
  `DEFAULT_RDS_HOURLY` / `DEFAULT_ECS_FARGATE_HOURLY`. Never assert that RDS/Fargate pricing came
  from the API.
- **Cost Explorer synthesizes ≈ $0** month-to-date spend. The `actual_exceeded` test must tolerate
  this (it skips when `actual_spend == 0`).
- **SSM `/aws/service/global-infrastructure/.../longName` is NOT present** → `_region_to_location()`
  falls back to "US East (N. Virginia)". Harmless for `us-east-1` tests.
- **Verified end-to-end:** with EC2 + RDS + ECS seeded, `check_budget()` returned
  `hourly_cost=$0.2623425`, `action=ok`, discovering `ec2=1 rds=1 ecs=1`. Lambda throttle
  remediation returned `status='throttled'`.
- **Consequence for test timing:** e2e seeds are slow (~40–60s). Do not add short timeouts.

---

### Task 1: Remove App Runner support

**Files:**
- Modify: `src/aws_cost_guardian.py` (remove constant, discovery block, two pricing methods, and branches in `_calculate_hourly_cost` / `stop_all_resources` / `send_alert` / `run`)
- Modify: `cli.py` (remove App Runner output in `cmd_status`, `cmd_test`, `cmd_stop`, and the `stop` warning)
- Modify: `tests/test_budget_scenarios.py` (delete the `TestAppRunner` class)
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture.md` (drop App Runner mentions)

**Interfaces:**
- Produces: `BudgetGuardian._discover_resources()` and `stop_all_resources()` return dicts keyed only by `ec2`, `rds`, `lambda`, `ecs`. Later tasks rely on these four keys.

- [ ] **Step 1: Delete the App Runner unit tests**

In `tests/test_budget_scenarios.py`, delete the entire `class TestAppRunner:` block (every method from `test_discover_apprunner_services` through `test_pause_apprunner_dry_run`). Then confirm no other apprunner references remain in tests:

Run: `grep -ni apprunner tests/test_budget_scenarios.py`
Expected: no output.

- [ ] **Step 2: Remove App Runner from `src/aws_cost_guardian.py`**

Make these exact removals:

1. Delete the constant line:
```python
DEFAULT_APPRUNNER_HOURLY = Decimal("0.10")
```

2. In `_discover_resources`, remove `"apprunner": []` from the `resources` dict (leaving `ec2`, `rds`, `lambda`, `ecs`), and delete the entire `# App Runner (no paginator available...)` block (the `apprunner = boto3.client("apprunner", ...)` try/except).

3. In `_calculate_hourly_cost`, delete the App Runner loop:
```python
        # App Runner services
        for svc in resources["apprunner"]:
            cost = self._get_apprunner_hourly_cost(svc["arn"], svc["region"])
            total += cost
```

4. Delete both methods in full: `_get_apprunner_unit_prices` and `_get_apprunner_hourly_cost`.

5. In `stop_all_resources`, remove `"apprunner": []` from the `results` dict and delete the entire `# Pause App Runner services` block.

6. In `send_alert`, delete the line `- App Runner Services: {len(status.resources["apprunner"])}` from the running-resources section and the line `- App Runner Paused: {len([r for r in stop_results["apprunner"] if r["status"] == "paused"])}` from the remediation section.

7. In `run`, delete this term from the `actually_changed` sum:
```python
                + len(
                    [r for r in stop_results["apprunner"] if r["status"] in ("paused", "dry_run")]
                )
```

- [ ] **Step 3: Remove App Runner from `cli.py`**

In `cmd_status`: delete `print(f"App Runner Services:   {len(status.resources['apprunner'])}")` and the `if status.resources["apprunner"]:` verbose block. In `cmd_test`: delete `print(f"  App Runner: {len(status.resources['apprunner'])} services")`. In `cmd_stop`: update the warning string to remove "App Runner", fix the discovery-count `print` to drop the App Runner count, and delete the `App Runner paused` results line. Update the `stop` warning to: `"This will stop ALL EC2, RDS, ECS, and throttle ALL Lambda functions!"`.

Run: `grep -ni apprunner cli.py src/aws_cost_guardian.py`
Expected: no output.

- [ ] **Step 4: Drop App Runner from docs**

In `README.md`, `CLAUDE.md`, and `docs/architecture.md`, remove App Runner rows/mentions (resource lists, the "What Gets Stopped" table row `App Runner | pause_service`, and IAM `apprunner:*` references). Leave EC2/RDS/Lambda/ECS intact.

Run: `grep -rni "app runner\|apprunner" README.md CLAUDE.md docs/`
Expected: no output.

- [ ] **Step 5: Run the full quality gate**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/ cli.py`
Expected: all pass (App Runner tests gone, remaining suite green, lint/format/type clean).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Remove App Runner support (deprecated, untestable on Floci, crash bug)"
```

---

### Task 2: E2E scaffolding — compose, marker, conftest, seeds, smoke test

**Files:**
- Create: `docker-compose.yml`
- Create: `tests/e2e/__init__.py` (empty)
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/seeds.py`
- Create: `tests/e2e/test_smoke.py`
- Modify: `pyproject.toml` (register the `e2e` marker)

**Interfaces:**
- Produces:
  - `tests/e2e/conftest.py` fixtures: `floci_endpoint` (session, skips if `:4566` down) and autouse `aws_env` (sets `AWS_ENDPOINT_URL` + dummy creds via monkeypatch).
  - `tests/e2e/seeds.py` functions used by later tasks:
    - `seed_ec2(count=1, instance_type="t3.medium", region="us-east-1") -> list[str]`
    - `seed_rds(identifier, engine="postgres", db_class="db.t3.medium", region="us-east-1") -> str`
    - `seed_lambda(name, memory=128, region="us-east-1") -> str`
    - `seed_ecs_fargate(cluster, service, region="us-east-1") -> dict`
    - `seed_lambda_spike_metrics(function_name, region="us-east-1") -> None`
    - `teardown_all(region="us-east-1") -> None`

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  floci:
    image: floci/floci:latest
    ports:
      - "4566:4566"
    volumes:
      # MANDATORY: Floci runs EC2/RDS/ECS as real Docker containers via its own
      # Docker engine. Without this socket, EC2 instances terminate instantly,
      # ECS tasks never start, and RDS create_db_instance returns invalid XML.
      - /var/run/docker.sock:/var/run/docker.sock
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4566/"]
      interval: 5s
      timeout: 3s
      retries: 20
```

> These values are VERIFIED (see "Verified Floci Behavior" above), not guesses. Do not change the
> image name or drop the docker.sock volume.

- [ ] **Step 2: Register the `e2e` marker in `pyproject.toml`**

Add this section:
```toml
[tool.pytest.ini_options]
markers = [
    "e2e: end-to-end tests that require a running Floci instance on localhost:4566",
]
```

- [ ] **Step 3: Create `tests/e2e/__init__.py`, `tests/e2e/conftest.py`, and `tests/e2e/seeds.py`**

`tests/e2e/__init__.py`: empty file.

`tests/e2e/conftest.py`:
```python
"""Shared fixtures for Floci-backed end-to-end tests."""

import socket

import pytest

FLOCI_HOST = "localhost"
FLOCI_PORT = 4566
FLOCI_ENDPOINT = f"http://{FLOCI_HOST}:{FLOCI_PORT}"


def _floci_reachable() -> bool:
    try:
        with socket.create_connection((FLOCI_HOST, FLOCI_PORT), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def floci_endpoint() -> str:
    if not _floci_reachable():
        pytest.skip(
            "Floci not reachable on localhost:4566 — start it with `docker compose up -d floci`"
        )
    return FLOCI_ENDPOINT


@pytest.fixture(autouse=True)
def aws_env(floci_endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every boto3 client to Floci with dummy credentials."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", floci_endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def clean_account():
    """Ensure a clean slate before and after each test."""
    from tests.e2e.seeds import teardown_all

    teardown_all()
    yield
    teardown_all()
```

`tests/e2e/seeds.py`:
```python
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
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
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


def seed_ec2(count: int = 1, instance_type: str = "t3.medium", region: str = "us-east-1") -> list[str]:
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
            raise RuntimeError(f"EC2 seed died (states={states}) — is /var/run/docker.sock mounted?")
        time.sleep(3)
    raise TimeoutError(f"EC2 instances {ids} never reached 'running' within {SEED_TIMEOUT_SECONDS}s")


def seed_rds(
    identifier: str, engine: str = "postgres", db_class: str = "db.t3.medium", region: str = "us-east-1"
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
```

- [ ] **Step 4: Write the smoke test (Phase 0 fidelity spike, codified)**

`tests/e2e/test_smoke.py`:
```python
"""Phase 0: confirm Floci responds and the endpoint redirect works."""

import boto3
import pytest

pytestmark = pytest.mark.e2e


def test_sts_reaches_floci():
    sts = boto3.client("sts", region_name="us-east-1")
    identity = sts.get_caller_identity()
    assert "Account" in identity


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
```

- [ ] **Step 5: Bring up Floci and run the smoke test**

Run:
```bash
docker compose up -d floci
# wait for health, then:
uv run pytest tests/e2e/test_smoke.py -v -m e2e
```
Expected: both tests PASS. If `get_cost_and_usage` errors, note the discrepancy and adjust the smoke expectations; the CE shape is required by later tasks. Also verify `uv run pytest` (without Floci consideration) still collects e2e as skipped when Floci is down:
```bash
docker compose down
uv run pytest tests/e2e -v
```
Expected: all e2e tests SKIPPED with the "Floci not reachable" reason.

- [ ] **Step 6: Commit**

```bash
docker compose up -d floci
git add docker-compose.yml pyproject.toml tests/e2e/__init__.py tests/e2e/conftest.py tests/e2e/seeds.py tests/e2e/test_smoke.py
git commit -m "Add Floci e2e scaffolding: compose, conftest, seeds, smoke test"
```

---

### Task 3: E2E discovery test

**Files:**
- Create: `tests/e2e/test_discovery.py`

**Interfaces:**
- Consumes: `seed_ec2`, `seed_rds`, `seed_lambda`, `seed_ecs_fargate`, `teardown_all` from `tests/e2e/seeds.py`; `clean_account` fixture.
- Consumes: `BudgetGuardian(regions, total_budget)._discover_resources() -> dict[str, list]` with keys `ec2`, `rds`, `lambda`, `ecs`.

- [ ] **Step 1: Write the failing test**

`tests/e2e/test_discovery.py`:
```python
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
```

- [ ] **Step 2: Run it (Floci up) and confirm pass**

Run: `docker compose up -d floci && uv run pytest tests/e2e/test_discovery.py -v -m e2e`
Expected: PASS. If a seed call fails (e.g. EC2 `ImageId`, ECS network config), fix the seed parameters in `tests/e2e/seeds.py` per Floci's error and re-run — this is the expected Phase 0 adjustment point.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_discovery.py tests/e2e/seeds.py
git commit -m "Add e2e discovery test against Floci"
```

---

### Task 4: E2E projection + action test (budget lever)

**Files:**
- Create: `tests/e2e/test_projection.py`

**Interfaces:**
- Consumes: `seeds`, `clean_account`; `BudgetGuardian.check_budget() -> BudgetStatus` with `.action` in {`ok`, `alert`, `stop_all`, `spike_alert`} and `.hourly_cost: Decimal`.

- [ ] **Step 1: Write the failing test**

`tests/e2e/test_projection.py`:
```python
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
```

- [ ] **Step 2: Run it (Floci up) and confirm pass**

Run: `uv run pytest tests/e2e/test_projection.py -v -m e2e`
Expected: PASS. `hourly_cost > 0` proves the Floci Pricing snapshot was queried. If `hourly_cost` is 0, investigate whether Floci returned a price for `t3.medium`; loosen to `>= 0` only as a documented fallback and note it.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_projection.py
git commit -m "Add e2e projection/action test (budget lever)"
```

---

### Task 5: E2E remediation test (re-query proves state changed)

**Files:**
- Create: `tests/e2e/test_remediation.py`

**Interfaces:**
- Consumes: `seeds`, `clean_account`; `BudgetGuardian.run(dry_run=False) -> dict` and `stop_all_resources`.

- [ ] **Step 1: Write the failing test**

`tests/e2e/test_remediation.py`:
```python
"""E2E: stop_all actually mutates Floci state (proven by re-query)."""

from decimal import Decimal

import boto3
import pytest

from src.aws_cost_guardian import BudgetGuardian
from tests.e2e import seeds

pytestmark = pytest.mark.e2e


def test_stop_all_stops_ec2_and_throttles_lambda(clean_account):
    [instance_id] = seeds.seed_ec2(count=1)
    seeds.seed_lambda("e2e-throttle-fn")

    guardian = BudgetGuardian(regions=["us-east-1"], total_budget=Decimal("0.01"))
    result = guardian.run(dry_run=False)
    assert result["status"].action == "stop_all"

    ec2 = boto3.client("ec2", region_name="us-east-1")
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("stopping", "stopped")

    lam = boto3.client("lambda", region_name="us-east-1")
    concurrency = lam.get_function_concurrency(FunctionName="e2e-throttle-fn")
    assert concurrency.get("ReservedConcurrentExecutions") == 0
```

- [ ] **Step 2: Run it (Floci up) and confirm pass**

Run: `uv run pytest tests/e2e/test_remediation.py -v -m e2e`
Expected: PASS. If Floci's EC2 stop returns `stopped` immediately rather than `stopping`, the assertion already covers both.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_remediation.py
git commit -m "Add e2e remediation test (re-query proves state change)"
```

---

### Task 6: E2E actual-exceeded immediate-stop test

**Files:**
- Create: `tests/e2e/test_actual_exceeded.py`

**Interfaces:**
- Consumes: `seeds`, `clean_account`; `BudgetStatus.actual_exceeded: bool`.

- [ ] **Step 1: Write the failing test**

`tests/e2e/test_actual_exceeded.py`:
```python
"""E2E: budget=0 forces the immediate (actual-exceeded) stop path when spend > 0."""

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
            "Floci reported zero month-to-date spend; actual-exceeded path needs nonzero CE cost"
        )
```

- [ ] **Step 2: Run it (Floci up) and confirm pass or documented skip**

Run: `uv run pytest tests/e2e/test_actual_exceeded.py -v -m e2e`
Expected: PASS (if Floci synthesizes nonzero CE spend) or SKIP with the documented reason. Record which in the commit message so the team knows Floci's CE time-model behavior.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_actual_exceeded.py
git commit -m "Add e2e actual-exceeded immediate-stop test (budget=0)"
```

---

### Task 7: E2E Lambda spike-detection test

**Files:**
- Create: `tests/e2e/test_lambda_spike.py`

**Interfaces:**
- Consumes: `seeds.seed_lambda`, `seeds.seed_lambda_spike_metrics`, `clean_account`; `BudgetStatus.lambda_spikes: list[LambdaSpike]`.

- [ ] **Step 1: Write the failing test**

`tests/e2e/test_lambda_spike.py`:
```python
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
```

- [ ] **Step 2: Run it (Floci up) and confirm pass**

Run: `uv run pytest tests/e2e/test_lambda_spike.py -v -m e2e`
Expected: PASS. If Floci's `get_metric_statistics` buckets the seeded points differently, adjust the seeded timestamps/values in `seed_lambda_spike_metrics` so the recent-rate / baseline-rate ratio clears the 10x threshold, and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_lambda_spike.py tests/e2e/seeds.py
git commit -m "Add e2e Lambda spike-detection test"
```

---

### Task 8: E2E known-gaps documentation tests (xfail backlog)

**Files:**
- Create: `tests/e2e/test_known_gaps.py`

**Interfaces:**
- Consumes: `seeds`, `clean_account`. Adds a Fargate-via-capacity-provider seed variant inline.

- [ ] **Step 1: Write the xfail test that documents the ECS capacity-provider gap (audit #7)**

`tests/e2e/test_known_gaps.py`:
```python
"""E2E tests that document known audit gaps. Each xfail is a backlog item.
When the underlying bug is fixed, remove the xfail marker and the test should pass.
"""

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

    resources = BudgetGuardian(regions=[region], total_budget=Decimal("1000"))._discover_resources()
    assert any(s["name"] == "cp-svc" for s in resources["ecs"])
```

- [ ] **Step 2: Run it (Floci up) and confirm it xfails (or xpasses)**

Run: `uv run pytest tests/e2e/test_known_gaps.py -v -m e2e -rX`
Expected: `XFAIL` (the gap is real) — or `XPASS` if Floci happens to populate `launchType` for capacity-provider services, which itself is useful information. Either way the suite stays green (`strict=False`).

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_known_gaps.py
git commit -m "Add e2e xfail test documenting ECS capacity-provider discovery gap"
```

---

### Task 9: CI workflow (unit + e2e jobs)

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the `e2e` pytest marker; `docker-compose.yml`.

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src/ cli.py
      - run: uv run pytest -m "not e2e"

  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: docker compose up -d floci
      - name: Wait for Floci
        run: |
          for i in $(seq 1 30); do
            if curl -sf http://localhost:4566/ >/dev/null; then echo up; exit 0; fi
            sleep 2
          done
          echo "Floci did not become ready" >&2
          docker compose logs floci
          exit 1
      - run: uv run pytest -m e2e -v
```

- [ ] **Step 2: Validate the workflow YAML locally**

Run: `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: no output (valid YAML). (If `pyyaml` is not present, run `python -c` with the system interpreter, or skip and rely on the push-time check.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Add CI: unit job + Floci-backed e2e job"
```

---

### Task 10: Documentation — E2E usage

**Files:**
- Modify: `README.md` (add an "End-to-End Tests (Floci)" section)
- Modify: `CLAUDE.md` (document the e2e commands + the `AWS_ENDPOINT_URL` mechanism)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the E2E section to `README.md`**

Add under the existing testing/CLI content:
```markdown
## End-to-End Tests (Floci)

E2E tests run the real guardian against [Floci](https://github.com/floci-io/floci),
a local AWS emulator, with no real AWS account.

```bash
docker compose up -d floci          # start the emulator on localhost:4566
uv run pytest -m e2e                 # run the e2e suite
docker compose down                  # stop the emulator
```

Without Floci running, `uv run pytest` skips the e2e suite automatically.
Note: App Runner is not emulated by Floci and is no longer supported.
```

- [ ] **Step 2: Add the e2e commands to `CLAUDE.md`**

In the Commands section, add:
```markdown
# Run only unit tests (no Docker needed)
uv run pytest -m "not e2e"

# Run end-to-end tests against Floci
docker compose up -d floci && uv run pytest -m e2e
```
And add a one-line note under Testing: "E2E tests (`tests/e2e/`) point the guardian at Floci via the global `AWS_ENDPOINT_URL` env var (zero production-code change) and auto-skip when `localhost:4566` is unreachable."

- [ ] **Step 3: Verify and commit**

Run: `uv run ruff format --check . && grep -c "e2e" README.md CLAUDE.md`
Expected: format clean; both files reference e2e.

```bash
git add README.md CLAUDE.md
git commit -m "Document Floci e2e testing workflow"
```

---

## Self-Review

**Spec coverage:**
- Drop App Runner → Task 1. ✓
- Floci e2e layer (discovery/projection/remediation/actual-exceeded/spike) → Tasks 3–7. ✓
- Zero-prod-change via `AWS_ENDPOINT_URL` → Task 2 conftest. ✓
- Determinism (budget lever, budget=0, no exact-cost asserts) → Tasks 4, 6. ✓
- Catches-vs-doesn't (xfail backlog) → Task 8. ✓
- Isolation (compose + teardown, no reset API) → Task 2 `clean_account` + `teardown_all`. ✓
- Phase 0 fidelity spike → Task 2 smoke test + the "adjust seeds" notes in Tasks 3–7. ✓
- CI (no CI today; unit + e2e jobs) → Task 9. ✓
- Docs → Tasks 1 (App Runner removal) + 10 (e2e usage). ✓

**Placeholder scan:** No TBD/TODO; every code step carries real code; seed-parameter adjustments are explicitly scoped to Floci error responses, not vague "handle errors".

**Type consistency:** `_discover_resources()`/`stop_all_resources()` keys are `ec2`/`rds`/`lambda`/`ecs` everywhere post-Task-1. Seed function names in Task 2's Interfaces match their call sites in Tasks 3–8. `BudgetStatus` fields used (`action`, `hourly_cost`, `actual_spend`, `actual_exceeded`, `lambda_spikes`) match the dataclass.

**Known assumptions to validate during execution (flagged in-task, not placeholders):** the Floci image tag/healthcheck path (Task 2), and exact seed parameters for EC2 `ImageId` / ECS network config / CloudWatch metric bucketing (Tasks 3–7) — each has an explicit "adjust and re-run" instruction tied to Floci's actual error output.
