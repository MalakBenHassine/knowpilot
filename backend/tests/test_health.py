from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.routes import health
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_checks() -> Iterator[None]:
    """Each test starts from an empty registry and restores it afterwards."""
    original = dict(health.READINESS_CHECKS)
    health.READINESS_CHECKS.clear()
    yield
    health.READINESS_CHECKS.clear()
    health.READINESS_CHECKS.update(original)


def test_live_is_public_and_minimal() -> None:
    response = client.get("/api/health/live")
    assert response.status_code == 200
    # Exactly one field: no version, no hostname, nothing to fingerprint.
    assert response.json() == {"status": "ok"}


def test_ready_without_dependencies() -> None:
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {}}


def test_ready_reports_a_failing_dependency_as_503() -> None:
    async def failing() -> bool:
        return False

    async def working() -> bool:
        return True

    health.READINESS_CHECKS.update({"database": working, "cache": failing})

    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "cache": "error"},
    }


def test_ready_hides_the_cause_of_a_crash() -> None:
    async def exploding() -> bool:
        raise RuntimeError("password=hunter2 host=db.internal")

    health.READINESS_CHECKS["database"] = exploding

    response = client.get("/api/health/ready")
    assert response.status_code == 503
    # The exception message must never reach the client.
    assert response.json() == {"status": "not_ready", "checks": {"database": "error"}}
    assert "hunter2" not in response.text
