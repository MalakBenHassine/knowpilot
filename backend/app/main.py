import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.routes import auth, chat, documents, health
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import HttpMetricsMiddleware, TokenMetrics
from app.core.oidc import OidcClient
from app.core.queue import ArqJobQueue, create_queue
from app.core.quota import QuotaTracker
from app.core.rate_limit import RateLimiter
from app.core.session import SessionStore
from app.core.storage import FileStorage
from app.core.tracing import ensure_tracing_is_deliberate
from app.db.session import create_engine, create_session_factory
from app.db.vector_store import create_vector_store, load_checked_embeddings
from app.rag.generation import ANSWER_PROMPT
from app.rag.llm import build_advisory_chain, build_answer_chain, create_chat_model
from app.rag.retrieval import RetrieverFactory
from app.rag.subjects import SUBJECT_PROMPT, SubjectCheck, Subjects

settings = get_settings()
# Before anything else: a module that logs during import would otherwise write
# into a void.
configure_logging(settings.log_level)
# Before any chain exists: a trace of a question carries users' documents.
ensure_tracing_is_deliberate(allowed=settings.langsmith_tracing_allowed)


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
    #
    # The API only embeds questions; the worker embeds documents. Both load the
    # same model, from the same setting, because a question and a passage must
    # land in the same vector space for their distance to mean anything.
    app.state.retrievers = None
    if settings.embeddings_enabled:
        embeddings = load_checked_embeddings(settings.embedding_model, settings.embedding_cache_dir)
        app.state.retrievers = RetrieverFactory(
            await create_vector_store(engine, embeddings),
            k=settings.top_k,
            max_distance=settings.max_distance,
            engine=engine,
            keyword_search=settings.keyword_search_enabled,
            min_keyword_coverage=settings.min_keyword_coverage,
            context_window=settings.passage_context_enabled,
        )

        async def embeddings_ready() -> bool:
            return app.state.retrievers is not None

        health.READINESS_CHECKS["embeddings"] = embeddings_ready

    # A second Redis connection, on purpose: arq speaks its own protocol on
    # its own keys, and sharing the session pool would tie the lifetime of
    # logins to the lifetime of a job queue.
    queue_pool = await create_queue(settings.redis_url)
    app.state.job_queue = ArqJobQueue(queue_pool)

    # Counters live in Redis, so every worker and every replica spends from
    # the same daily budget. Four processes with four dictionaries would
    # allow four times the quota, which is the failure this prevents.
    # Same Redis, same reason: a per-process counter would allow one limit
    # per worker.
    app.state.rate_limiter = RateLimiter(
        redis,
        limits={
            "chat": settings.chat_requests_per_minute,
            "upload": settings.upload_requests_per_minute,
        },
    )
    app.state.quota = QuotaTracker(
        redis,
        per_user=settings.daily_questions_per_user,
        per_service=settings.daily_questions_per_service,
    )

    # One HTTP client for the whole process, not one per request: each new
    # client means a fresh TCP connection and a fresh TLS handshake, paid on
    # every question. Closed below, in reverse order of construction.
    http_client = httpx.AsyncClient()
    # One handler for both models: it only increments global counters.
    token_metrics = [TokenMetrics()]
    app.state.answer_chain = None
    app.state.subject_check = None
    if settings.generation_enabled:
        # Built once: an LCEL chain is immutable and safe to share between
        # concurrent requests, so there is nothing to gain from one per call.
        app.state.answer_chain = build_answer_chain(
            ANSWER_PROMPT,
            create_chat_model(
                settings.groq_api_key, settings.groq_model, http_client, callbacks=token_metrics
            ),
        )

        if settings.subject_check_enabled:
            app.state.subject_check = SubjectCheck(
                build_advisory_chain(
                    SUBJECT_PROMPT,
                    create_chat_model(
                        settings.groq_api_key,
                        settings.groq_check_model,
                        http_client,
                        # A JSON list of a few names: 300 tokens leave room
                        # for the model's short reasoning and nothing else.
                        max_tokens=300,
                        callbacks=token_metrics,
                    ),
                    Subjects,
                ),
                engine,
            )

        async def generation_ready() -> bool:
            # Deliberately NOT a call to the provider: a readiness probe runs
            # every few seconds, and spending the daily budget to prove the
            # budget exists would be its own outage.
            return app.state.answer_chain is not None

        health.READINESS_CHECKS["generation"] = generation_ready
    else:
        # Loud, because the symptom - every question answered with a 503 -
        # looks like a provider outage rather than a missing variable.
        logging.getLogger("app").warning(
            "KP_GROQ_API_KEY is not set: answering is disabled, uploads still work"
        )

    yield

    # Torn down in the reverse order of construction.
    health.READINESS_CHECKS.pop("generation", None)
    health.READINESS_CHECKS.pop("embeddings", None)
    health.READINESS_CHECKS.pop("database", None)
    health.READINESS_CHECKS.pop("cache", None)
    await http_client.aclose()
    await queue_pool.aclose()
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
app.include_router(chat.router, prefix="/api")

# The scrape itself is not measured, and neither are the probes: an
# orchestrator polling every few seconds would otherwise be most of the
# traffic, and the request rate would measure the scrape interval.
app.add_middleware(
    HttpMetricsMiddleware,
    excluded=frozenset({"/metrics", "/api/health/live", "/api/health/ready"}),
)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """The Prometheus scrape endpoint - deliberately OUTSIDE /api.

    The reverse proxy forwards /api to this service and serves the SPA for
    everything else, so /metrics is unreachable from the internet and only
    answers on the internal network, where Prometheus runs. Usage counts are
    not secret the way documents are, but they describe the business, and a
    public page of them is a gift to anyone sizing an attack (ADR-0019).

    A plain `def`: generating the text is CPU work on in-memory counters, and
    FastAPI runs it in its thread pool rather than on the event loop.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
