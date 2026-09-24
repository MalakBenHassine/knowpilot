"""The ingestion worker: a second process, running the same pipeline.

Started with `arq app.worker.WorkerSettings`. It shares the database, the
Redis instance and the uploads volume with the API, and shares no event loop
with it - which is the entire point. A long document now competes with other
ingestions, never with somebody waiting for an answer.

The pipeline takes an `IngestionStore` and LangChain `Embeddings` rather than
reaching for a database or a model of its own, so running it in another
process is a matter of building those two objects here.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from arq import func
from arq.connections import RedisSettings
from prometheus_client import start_http_server

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.queue import INGEST_JOB
from app.core.tracing import ensure_tracing_is_deliberate
from app.db.ingestion import DatabaseIngestionStore
from app.db.session import create_engine, create_session_factory
from app.db.vector_store import create_vector_store, load_checked_embeddings
from app.rag.pipeline import MAX_INGESTION_SECONDS, ingest_document

if TYPE_CHECKING:
    from arq.typing import WorkerCoroutine

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)

# One document at a time. The work is CPU-bound, so running four in parallel on
# a shared vCPU finishes all four later than running them one after another -
# and holds four documents worth of tensors in memory to do it. Concurrency
# helps when tasks wait; these do not.
MAX_CONCURRENT_JOBS = 1

# The net, not the rule. The pipeline refuses a document it cannot index
# inside MAX_INGESTION_SECONDS before it starts (app/rag/pipeline.py), so a
# job that reaches THIS limit is stuck somewhere unforeseen rather than
# merely slow. Five minutes above the budget, so the two never race: the
# inner decision always wins, and the user gets a written reason instead of
# a killed job that retries and is killed again.
#
# It used to be a flat 600s while the parser accepted 500 pages - which at
# the measured 0.25 pages/s needed 2000s. Every document above roughly 150
# pages was accepted, indexed for ten minutes, killed, retried once and
# killed again (docs/performance.md).
JOB_TIMEOUT_SECONDS = MAX_INGESTION_SECONDS + 300

# Retried by arq if the worker dies mid-job, which is exactly the loss the
# queue exists to prevent. Expected failures never reach this: the pipeline
# catches them and writes a failure_reason instead of raising.
MAX_TRIES = 2


async def ingest(context: dict[str, Any], document_id: str, owner_id: str) -> None:
    """Run the pipeline for one document.

    The pipeline never raises, so a job that ends without an exception says
    nothing about whether the document succeeded - that verdict is in the
    database, where the interface reads it.
    """
    await ingest_document(
        context["store"],
        context["embeddings"],
        embedding_model=settings.embedding_model,
        document_id=uuid.UUID(document_id),
        owner_id=owner_id,
    )


async def startup(context: dict[str, Any]) -> None:
    """Build the database, the model and the vector store once, not per job.

    Loading BGE-M3 costs roughly twenty seconds and 2.2 GB. Paying that per job
    would make a five-second ingestion take half a minute, and a burst of
    uploads would spend most of its time loading the same weights again.
    """
    # The worker handles every document of every user: the same privacy rule
    # as the API, checked before anything is loaded.
    ensure_tracing_is_deliberate(allowed=settings.langsmith_tracing_allowed)

    # Started before the model loads, which takes twenty seconds: a scrape
    # during start-up should find a worker that is up and not yet ready,
    # rather than a connection refused that looks like a dead process.
    # A background thread serving one page; it costs nothing while idle.
    start_http_server(settings.worker_metrics_port)
    logger.info("worker metrics on :%d", settings.worker_metrics_port)

    engine = create_engine(settings.database_url)
    context["engine"] = engine

    embeddings = load_checked_embeddings(settings.embedding_model, settings.embedding_cache_dir)
    context["embeddings"] = embeddings
    context["store"] = DatabaseIngestionStore(
        create_session_factory(engine), await create_vector_store(engine, embeddings)
    )
    logger.info("ingestion worker ready with %s", settings.embedding_model)


async def shutdown(context: dict[str, Any]) -> None:
    await context["engine"].dispose()


class WorkerSettings:
    """Read by the `arq` command line. Plain attributes, no framework magic."""

    # arq declares its job type as (ctx, *args, **kwargs), which every
    # concretely typed function fails to match. Adopting that signature to
    # satisfy the checker would throw away the typing of our own job, so the
    # cast states the intent instead: `ingest` is deliberately narrower than
    # what arq accepts, and the enqueue site is the only caller.
    functions = [
        func(
            cast("WorkerCoroutine", ingest),
            name=INGEST_JOB,
            timeout=JOB_TIMEOUT_SECONDS,
            max_tries=MAX_TRIES,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = MAX_CONCURRENT_JOBS
    # Results are kept only long enough to be useful in the logs. The real
    # record of what happened to a document is the document row itself, so
    # keeping job results for days would duplicate the truth and let the two
    # disagree.
    keep_result = 300
    # How often the worker writes its health key to Redis, which lives
    # interval + 1 seconds. `arq --check` - the container healthcheck - fails
    # once the key is gone. arq's default is an HOUR, which made the check
    # meaningless: a dead worker would have looked healthy for 59 minutes.
    # Sixty seconds rather than five, because the key is written between jobs
    # and a long document must not make a busy worker look dead.
    health_check_interval = 60
