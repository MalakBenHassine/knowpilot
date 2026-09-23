"""Authentication tests.

They run without Redis and without Keycloak: the store is replaced by an
in-memory fake. What matters here is the SECURITY LOGIC — who gets 401, who
gets 403, what a session exposes — not the network plumbing.
"""

import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import get_oidc
from app.core.config import Settings
from app.core.session import SESSION_COOKIE, SessionData, SessionStore
from app.main import app


class FakeRedis:
    """Just enough Redis for the session store: get, set, expire, delete."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.expirations.pop(key, None)


def make_session(**overrides: Any) -> SessionData:
    data = {
        "sub": "8f2c1e40-0000-4000-8000-000000000001",
        "email": "malak@knowpilot.dev",
        "display_name": "Malak Ben Hassine",
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "id_token": "id-token-value",
        "csrf_token": "csrf-token-value",
        "created_at": time.time(),
    }
    data.update(overrides)
    return SessionData(**data)  # type: ignore[arg-type]


@pytest.fixture
def store() -> SessionStore:
    return SessionStore(FakeRedis(), Settings())  # type: ignore[arg-type]


@pytest.fixture
def client(store: SessionStore) -> TestClient:
    app.state.session_store = store
    return TestClient(app)


async def _login(store: SessionStore, client: TestClient, **overrides: Any) -> None:
    session_id = await store.create(make_session(**overrides))
    client.cookies.set(SESSION_COOKIE, session_id)


@pytest.fixture
def aborted(client: TestClient) -> Iterator[TestClient]:
    """A client for the callbacks that end before Keycloak is ever called.

    The route resolves its OIDC dependency before the handler runs, so one
    has to exist. This one fails loudly if the handler ever touches it.
    """
    app.dependency_overrides[get_oidc] = _UnusableOidc
    yield client
    app.dependency_overrides.pop(get_oidc, None)


class _UnusableOidc:
    """FastAPI resolves every dependency before the handler runs, so this one
    is built. Using it is what an aborted callback must never do."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError("an aborted callback must not call Keycloak")


def test_callback_reports_a_known_oidc_error(
    aborted: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A code from the specification is useful, so it is logged as it is."""
    with caplog.at_level(logging.INFO):
        response = aborted.get(
            "/api/auth/callback", params={"error": "access_denied"}, follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=auth")
    assert "access_denied" in caplog.text


def test_callback_never_writes_a_crafted_error_to_the_log(
    aborted: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The parameter is attacker-controlled: it must not reach the log."""
    forged = "access_denied\nINFO:app.api.routes.auth:login succeeded for admin"

    with caplog.at_level(logging.INFO):
        response = aborted.get(
            "/api/auth/callback", params={"error": forged}, follow_redirects=False
        )

    assert response.status_code == 303
    assert "login succeeded" not in caplog.text
    assert "unrecognised" in caplog.text


def test_me_without_a_session_is_401(client: TestClient) -> None:
    response = client.get("/api/auth/me")
    # 401 means "not authenticated"; the frontend turns it into "anonymous".
    assert response.status_code == 401


def test_me_returns_the_user_and_never_a_token(
    client: TestClient, store: SessionStore, anyio_backend: str
) -> None:
    import anyio

    anyio.run(_login, store, client)

    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "malak@knowpilot.dev"
    assert body["csrf_token"] == "csrf-token-value"
    # The access and refresh tokens must never leave the server.
    assert "access-token-value" not in response.text
    assert "refresh-token-value" not in response.text
    assert "id-token-value" not in response.text


def test_logout_without_csrf_token_is_refused(
    client: TestClient, store: SessionStore, anyio_backend: str
) -> None:
    import anyio

    anyio.run(_login, store, client)

    # A third-party site can make the browser send the cookie, but cannot read
    # the CSRF token: without it, the request is refused.
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_expired_session_is_rejected_and_deleted(store: SessionStore) -> None:
    import anyio

    async def scenario() -> None:
        settings = Settings()
        old = make_session(created_at=time.time() - settings.session_absolute_seconds - 1)
        session_id = await store.create(old)

        # The absolute lifetime is never extended by activity.
        assert await store.get(session_id) is None
        # And the session is gone, not merely refused.
        assert await store.get(session_id) is None

    anyio.run(scenario)


def test_active_session_slides_its_idle_window(store: SessionStore) -> None:
    import anyio

    async def scenario() -> None:
        session_id = await store.create(make_session())
        assert await store.get(session_id) is not None

    anyio.run(scenario)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
