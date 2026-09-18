from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.routes import auth, health
from app.core.config import get_settings
from app.core.oidc import OidcClient
from app.core.session import SessionStore

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One Redis connection pool and one OIDC client for the whole process.

    Created at startup and closed at shutdown: never per request.
    """
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = SessionStore(redis, settings)
    app.state.session_store = store
    app.state.oidc = OidcClient(settings)

    # Now that the dependency exists, the readiness probe can report on it.
    health.READINESS_CHECKS["cache"] = store.ping

    yield

    health.READINESS_CHECKS.pop("cache", None)
    await redis.aclose()


app = FastAPI(
    title="KnowPilot API",
    lifespan=lifespan,
    # The interactive docs are useful while developing and are a needless
    # disclosure in production, where they are disabled.
    docs_url="/api/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.environment != "production" else None,
)

# Every route lives under /api: the reverse proxy serves the SPA at / and
# forwards /api here, so both share one origin (no CORS, first-party cookie).
app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
