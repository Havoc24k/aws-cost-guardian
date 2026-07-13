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
