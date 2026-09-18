from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.routes import auth, documents, health
from app.core.config import get_settings
from app.core.oidc import OidcClient
from app.core.session import SessionStore
from app.core.storage import FileStorage
from app.db.models import EMBEDDING_DIMENSIONS
from app.db.session import create_engine, create_session_factory
from app.rag.model import LocalEmbeddingModel

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

    engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(engine)

    async def database_ready() -> bool:
        # SELECT 1 rather than "is the pool object present": what matters is
        # that a query actually completes, not that an object exists.
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    health.READINESS_CHECKS["database"] = database_ready

    # Created eagerly so a missing or unwritable volume fails at startup rather
    # than on the first upload of the day.
    upload_root = Path(settings.upload_root)
    # ASYNC240 warns about blocking filesystem calls inside async code, and it
    # is right in a request handler. Here nothing else is running yet and no
    # connection is served until the lifespan returns, so one mkdir blocks
    # nobody. Same reasoning as loading the model below.
    upload_root.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    app.state.file_storage = FileStorage(upload_root)

    # Loaded once, here, before the application accepts a single connection.
    # Blocking the event loop is correct at this point: nothing else is running
    # yet, and FastAPI serves nothing until the lifespan has finished - so the
    # readiness probe physically cannot answer "ready" while the weights load.
    app.state.embedding_model = None
    if settings.embeddings_enabled:
        model = LocalEmbeddingModel.load(settings.embedding_model, settings.embedding_cache_dir)
        # The column is vector(1024). A model of a different size would write
        # rows PostgreSQL rejects, or worse, vectors nobody can compare. Fail
        # here, loudly, rather than halfway through somebody's first upload.
        if model.dimensions != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"{model.name} produces {model.dimensions} dimensions, "
                f"but the schema stores {EMBEDDING_DIMENSIONS}. "
                "Changing the model requires a migration and a full re-index."
            )
        app.state.embedding_model = model

        async def embeddings_ready() -> bool:
            return app.state.embedding_model is not None

        health.READINESS_CHECKS["embeddings"] = embeddings_ready

    yield

    # Torn down in the reverse order of construction.
    health.READINESS_CHECKS.pop("embeddings", None)
    health.READINESS_CHECKS.pop("database", None)
    health.READINESS_CHECKS.pop("cache", None)
    await engine.dispose()
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
app.include_router(documents.router, prefix="/api")
