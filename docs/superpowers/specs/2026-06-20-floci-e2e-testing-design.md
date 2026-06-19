# Design: Floci End-to-End Test Layer (+ Drop App Runner)

- **Date:** 2026-06-20
- **Status:** Approved (design) — pending spec review
- **Branch:** `floci-e2e-testing`

## Context & Motivation

Cost Guardian's distinguishing value is **cost projection**: it reads month-to-date
spend from Cost Explorer, prices running resources via the Pricing API, and projects
total cost to the end of the budget period. Yet the current test suite
(`tests/test_budget_scenarios.py`) **patches out** `_get_actual_spend` and
`_calculate_hourly_cost`. The Cost Explorer, Pricing API, and CloudWatch integration
paths — the heart of the product — are therefore **never exercised by a test**.

A 2026-06-20 currency audit (19 agents, 63 assumptions checked against current AWS docs +
the public Price List offer files) confirmed this gap matters: it found 10 high-severity
issues, several of which are *silent* (e.g. the RDS Pricing lookup always falls back to a
flat constant because the code sends the boto3 engine code `postgres` where the Price List
expects the display name `PostgreSQL`). An end-to-end test against a real AWS wire protocol
is exactly the class of test that surfaces integration bugs like these.

**Floci** (https://github.com/floci-io/floci) is a 2026 open-source, zero-config, zero-auth
local AWS emulator — a drop-in LocalStack alternative on `localhost:4566` speaking the real
AWS wire protocol. Critically, it emulates **both** layers the guardian needs:

- Resource control plane: EC2, RDS, Lambda, ECS (real Docker-backed), SNS, SSM, STS, IAM.
- **Cost/pricing layer** (rare in emulators): Cost Explorer (`GetCostAndUsage`, DAILY/MONTHLY/
  HOURLY) with *"cost synthesized from Floci resource state and pricing snapshots"*, a bundled
  static **Pricing snapshot**, and CloudWatch metrics.

One gap: **Floci does not emulate App Runner.** Combined with the audit's findings that App
Runner is in AWS maintenance (closed to new customers as of 2026-04-30) and currently has an
account-wide crash bug, the decision is to **drop App Runner support** rather than carry it as
a permanently-untestable special case.

## Goals

1. **Remove App Runner** support entirely (discovery, cost, remediation, alerting, CLI/handler
   output, tests, docs). This also deletes audit findings #4/#5/#6 (the `Decimal("1 vCPU")`
   crash) outright.
2. **Add a Floci-backed E2E test layer** that runs the real `BudgetGuardian` against
   `localhost:4566`, covering the full flow: discovery → cost projection → action decision →
   remediation, for EC2 / RDS / Lambda / ECS, plus Lambda spike detection.
3. Wire the E2E suite into CI, and keep the default `uv run pytest` green with **no Docker
   required** (E2E auto-skips when Floci is unreachable).
4. Update README / docs / CLAUDE.md to reflect the dropped service.

## Non-Goals (explicit follow-up backlog)

This spec is **the test harness + dropping App Runner**, not a bug-fix sprint. The following
audit findings are *documented* by the new E2E suite (as `xfail` or range-tolerant tests) but
**fixed in a separate effort**, with the xfails serving as the backlog:

- RDS `databaseEngine` value mapping (boto3 code → Price List display name). *(audit #1)*
- Fargate Pricing query non-unique match / `MaxResults=1` ambiguity. *(audit #3)*
- ECS capacity-provider Fargate discovery (`launchType` omitted). *(audit #7)*
- Aurora handling: `stop_db_cluster` vs `stop_db_instance`, `describe_db_clusters`. *(audit #6, mediums)*
- RDS discovery status filter too narrow (only `available`). *(audit #9)*
- RDS `stop_db_instance` constraints + 7-day auto-restart durability. *(audit #8)*

## Key Technical Enabler

The guardian hardcodes `boto3.client(...)` everywhere (including `region_name="us-east-1"` for
`ce` / `pricing` / `ssm`) with **no endpoint override**. This is *not* a blocker: botocore
(since ~1.31; the project is on `boto3 >= 1.35`) honors the global **`AWS_ENDPOINT_URL`**
environment variable, redirecting *every* service client. Therefore retargeting the entire
guardian at Floci needs **zero production-code changes** — only test-environment config:

```
AWS_ENDPOINT_URL=http://localhost:4566
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
```

(Floci is zero-auth, so dummy credentials suffice. Verifying this routing is the first task in
the Phase 0 spike.)

## Architecture & Components

```
docker-compose.yml                 # NEW: floci service on :4566 (only new infra)
tests/
  e2e/
    __init__.py
    conftest.py                    # reachability-skip, env setup, seed + teardown fixtures
    test_discovery.py              # seed fleet → assert _discover_resources() counts/ids
    test_projection.py             # budget-as-lever → assert action ok / alert / stop_all
    test_remediation.py            # run stop_all → RE-QUERY floci to prove state changed
    test_actual_exceeded.py        # budget=$0 → guaranteed immediate stop_all path
    test_lambda_spike.py           # put_metric_data → assert spike detected
    test_known_gaps.py             # xfail tests documenting the audit backlog (capacity-provider, etc.)
pyproject.toml                     # register `e2e` pytest marker
.github/workflows/ci.yml           # NEW (no CI exists today): unit-test job + floci e2e job
```

### Component responsibilities

- **`docker-compose.yml`** — defines the `floci` service (image, port 4566, healthcheck). The
  only new infrastructure. Developers and CI run `docker compose up -d floci` before E2E.
- **`tests/e2e/conftest.py`**
  - `floci_endpoint` (session): returns `http://localhost:4566`; **skips the entire e2e
    package** if the port is unreachable, so `uv run pytest` stays green without Docker.
  - Sets the `AWS_ENDPOINT_URL` + dummy-cred env for the test process.
  - `seed_*` fixtures: create resources via boto3 against Floci.
  - Teardown: `try/finally` cleanup of created resources (unique names per test).
- **Seed helpers** — `run_instances` (EC2), `create_db_instance` (RDS), `create_function`
  (Lambda), `create_cluster`/`create_service`/`register_task_definition` (ECS),
  `put_metric_data` (CloudWatch, for spikes).
- **Test modules** — see data flow below.

## Test Data Flow

```
seed fixture (boto3 → Floci) 
    → set AWS_ENDPOINT_URL (global client redirect)
    → guardian = BudgetGuardian(regions=["us-east-1"], total_budget=<lever>)
    → status = guardian.run(dry_run=False)
    → assert on returned BudgetStatus (counts, action, spikes)
    → RE-QUERY Floci to prove remediation mutated real state
    → teardown (delete created resources)
```

## Determinism Strategy ("emulating costs")

Floci computes cost as **current resource state × a static bundled pricing snapshot**, with an
*undocumented* time/accrual model, and snapshot prices that will not match real AWS to the
cent. Tests therefore **never assert exact dollar amounts**. Instead:

- **Drive action states with the budget lever, not the cost.** Tiny budget → `stop_all`; huge
  budget → `ok`; mid → `alert`. Deterministic regardless of Floci's exact synthesized number.
- **`actual_exceeded` path:** set `total_budget=0` → any nonzero synthesized spend exceeds it →
  guaranteed immediate `stop_all`. (The CLI `stop` subcommand already uses this `budget=0`
  trick.)
- **Pricing assertions are range/relative** (e.g. "hourly_cost > 0", "projection scales with
  instance count"), never exact.

## What This Layer Catches — and What It Deliberately Does Not

**Catches ✅:** client/endpoint wiring; discovery correctness (counts, ids, fields); the full
flow end-to-end; action/threshold logic; **remediation actually changing state** (re-query
proves EC2 stopped, RDS stopped, Lambda `ReservedConcurrentExecutions=0`, ECS `desiredCount=0`);
Lambda spike detection; and — via an `xfail` test — the ECS capacity-provider discovery gap.

**Does NOT catch ❌:** the audit's **pricing-dialect bugs** (RDS `postgres` vs `PostgreSQL`,
App Runner attribute names, Fargate multi-match). Floci uses its own snapshot and may filter
leniently, so it will not reproduce real-AWS Price List quirks. Those stay covered by the audit
plus a small **follow-up pricing-contract unit test** (assert the code's filter values against
known Price List display names). **Floci E2E complements the audit; it does not replace it.**

## Isolation Strategy

Floci has **no reset/admin endpoint** (confirmed via README/CHANGELOG) and *persists* some state
(EC2, CloudFormation) across restarts; its endorsed isolation pattern is separate container
instances (Testcontainers-style). Because the guardian discovers *all* running resources, a
shared Floci instance risks cross-test contamination. Chosen approach (KISS, no new dependency):

- A single `docker-compose` Floci instance for the suite.
- **Per-test `try/finally` teardown** that deletes everything the test created, using
  **uniquely-named resources** per test to avoid collisions.

Alternative (stronger isolation, deferred): `testcontainers-python` for a fresh container per
test module. Adds a dev dependency; revisit only if teardown-based isolation proves flaky.

## Drop-App-Runner Change List

- `src/aws_cost_guardian.py`: remove `DEFAULT_APPRUNNER_HOURLY`; the `apprunner` key + discovery
  block in `_discover_resources`; `_get_apprunner_unit_prices`; `_get_apprunner_hourly_cost`; the
  apprunner branches in `_calculate_hourly_cost`, `stop_all_resources`, `send_alert`, and `run`.
- `cli.py`: remove App Runner lines from `status`, `test`, and `stop` output.
- `tests/test_budget_scenarios.py`: delete the `TestAppRunner` class.
- `README.md`, `docs/*`, `CLAUDE.md`: remove App Runner references.
- `main.tf`: no change needed (App Runner IAM was never added — consistent with dropping it).
- Net effect: also removes audit findings #4/#5/#6 (the account-wide `Decimal("1 vCPU")` crash).

## Phase 0: Floci Fidelity Spike (first implementation step)

A throwaway spike, before any assertions are written, that de-risks the undocumented unknowns:

1. Stand up Floci via `docker compose up`.
2. Confirm `AWS_ENDPOINT_URL` routes *all* guardian clients (ec2, rds, lambda, ecs, ce, pricing,
   ssm, cloudwatch, sns, sts) to Floci.
3. Confirm `GetCostAndUsage` returns nonzero synthesized cost for seeded resources, and observe
   its time model (point-in-time vs accrual).
4. Confirm the Pricing snapshot returns *something* for the guardian's EC2/RDS/Fargate filters.
5. Confirm `put_metric_data` → `get_metric_statistics` round-trips (needed for spike tests).
6. Confirm teardown-based isolation is reliable (no resource bleed across two sequential runs).

Findings from the spike feed back into the concrete assertions.

## CI Integration

**The repo has no CI today** — this introduces `.github/workflows/ci.yml` with two jobs:
1. **unit** — `uv run pytest` (+ `ruff`, `mypy`); needs no Docker (the e2e package self-skips
   when Floci is unreachable).
2. **e2e** — starts Floci (service container or `docker compose up -d`), waits for `:4566`
   health, then runs `uv run pytest -m e2e`.

The two jobs never conflict because the e2e package self-skips without Floci.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Floci's CE time-model makes spend non-deterministic | Budget-as-lever + `budget=0`; never assert exact spend (Phase 0 confirms) |
| Floci pricing snapshot diverges from real AWS | Range/relative pricing assertions; pricing-dialect correctness stays in the audit + follow-up unit test |
| Teardown leaves orphan resources → cross-test bleed | Unique names + `try/finally`; escalate to per-module containers if flaky |
| Floci lacks a service/op the guardian calls | Phase 0 enumerates required ops up front; fall back to moto for any unsupported op |
| Docker unavailable locally | E2E auto-skips on unreachable `:4566`; `uv run pytest` stays green |

## Open Question (resolved)

Fold the audit's pricing/remediation fixes into this effort? **No** — kept as the follow-up
backlog above, so this spec stays focused on the test harness and the App Runner removal.
