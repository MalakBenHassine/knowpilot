"""Server-side sessions for the Backend-for-Frontend (ADR-0004).

The browser only ever holds an opaque random identifier in a cookie. Tokens,
identity and the CSRF secret live here, in Redis, so a session can be revoked
instantly and an XSS has nothing reusable to steal.
"""

import json
import secrets
import time
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from app.core.config import Settings

SESSION_COOKIE = "__Host-session"
# __Host- forces Secure, no Domain and Path=/: the cookie cannot be injected
# from a sibling subdomain.
LOGIN_TX_COOKIE = "__Host-oidc-tx"
CSRF_HEADER = "X-CSRF-Token"

_SESSION_PREFIX = "session:"
_LOGIN_TX_PREFIX = "oidc-tx:"
# A login must be completed quickly; the transaction dies with it.
LOGIN_TX_TTL_SECONDS = 300


@dataclass(slots=True)
class SessionData:
    sub: str
    email: str
    display_name: str
    access_token: str
    refresh_token: str
    id_token: str
    csrf_token: str
    created_at: float

    @property
    def is_expired_absolutely(self) -> bool:
        return False  # replaced by SessionStore, which knows the settings


@dataclass(slots=True)
class LoginTransaction:
    """The short-lived state of one in-flight login."""

    state: str
    nonce: str
    code_verifier: str


def new_token(length: int = 32) -> str:
    """Cryptographically strong random identifier, URL safe."""
    return secrets.token_urlsafe(length)


class SessionStore:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # --- login transaction -------------------------------------------------

    async def start_login(self, transaction: LoginTransaction) -> str:
        """Stores the transaction and returns the id to put in a cookie."""
        tx_id = new_token(16)
        await self._redis.set(
            f"{_LOGIN_TX_PREFIX}{tx_id}",
            json.dumps(asdict(transaction)),
            ex=LOGIN_TX_TTL_SECONDS,
        )
        return tx_id

    async def pop_login(self, tx_id: str) -> LoginTransaction | None:
        """Reads the transaction and deletes it: a code is used exactly once."""
        key = f"{_LOGIN_TX_PREFIX}{tx_id}"
        raw = await self._redis.get(key)
        await self._redis.delete(key)
        if raw is None:
            return None
        return LoginTransaction(**json.loads(raw))

    # --- session -----------------------------------------------------------

    async def create(self, data: SessionData) -> str:
        session_id = new_token(32)
        await self._redis.set(
            f"{_SESSION_PREFIX}{session_id}",
            json.dumps(asdict(data)),
            ex=self._settings.session_idle_seconds,
        )
        return session_id

    async def get(self, session_id: str) -> SessionData | None:
        """Returns the session, refreshing the idle window, or None.

        Two independent lifetimes: Redis enforces the sliding idle timeout,
        and the absolute limit is checked here because it is never extended.
        """
        key = f"{_SESSION_PREFIX}{session_id}"
        raw = await self._redis.get(key)
        if raw is None:
            return None

        data = SessionData(**json.loads(raw))
        age = time.time() - data.created_at
        if age > self._settings.session_absolute_seconds:
            await self._redis.delete(key)
            return None

        # Sliding window: activity keeps the session alive.
        await self._redis.expire(key, self._settings.session_idle_seconds)
        return data

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(f"{_SESSION_PREFIX}{session_id}")

    async def ping(self) -> bool:
        return bool(await self._redis.ping())
