"""Shared fixtures for Floci-backed end-to-end tests."""

import os
import socket
from pathlib import Path

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


def pytest_collection_modifyitems(items):
    """Apply the `e2e` marker to every test in this package, even if a module
    forgets its own `pytestmark`. Without this, a marker-less module would be
    excluded from `-m e2e` (CI) AND skipped in the unit job — it would run
    nowhere and vanish silently.

    conftest.py hooks are registered session-wide regardless of directory, so
    this must explicitly scope to files under this package (`tests/e2e/`) —
    otherwise every test in the repo gets marked `e2e`, which silently breaks
    the unit job's `-m "not e2e"` filter (it would collect zero tests).
    """
    package_dir = Path(__file__).parent
    for item in items:
        if package_dir in item.path.parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def floci_endpoint() -> str:
    if not _floci_reachable():
        if os.environ.get("CI"):
            pytest.fail(
                "Floci unreachable on localhost:4566 in CI — e2e tests must not silently skip.",
                pytrace=False,
            )
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
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


@pytest.fixture
def clean_account():
    """Ensure a clean slate before and after each test."""
    from tests.e2e.seeds import teardown_all

    teardown_all()
    yield
    teardown_all()
