"""Handing ingestion to a worker, instead of doing it in the web process.

Until now the pipeline ran as a Starlette background task: inside the process
that serves requests, in the event loop that answers questions. Three things
were wrong with that, and only the first is about speed.

A thirty-page PDF holds CPU while other users wait for an answer. A restart
during ingestion loses the work with no trace, because nothing recorded that
it was owed. And the web tier and the ingestion tier cannot be scaled apart,
although one is bound by connections and the other by CPU.

A queue fixes all three by writing down the intention: enqueueing is a durable
fact in Redis, and whoever picks it up is somebody else's problem.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

# The name the producer enqueues and the worker registers. A constant, because
# a typo in a string that crosses a process boundary fails at runtime, in the
# worker, with the job simply never running.
INGEST_JOB = "ingest_document"


class JobQueue(Protocol):
    """What a route needs from a queue, and nothing more.

    The sixth Protocol in this codebase, for the same reason as the other five:
    the routes are tested against a fake that records, with no Redis anywhere
    near them, and the queue technology stays a deployment decision.
    """

    async def enqueue_ingestion(self, *, document_id: uuid.UUID, owner_id: str) -> None: ...


class ArqJobQueue:
    """Publishes into Redis. Owns no worker, runs no job."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_ingestion(self, *, document_id: uuid.UUID, owner_id: str) -> None:
        # The identifier is sent as a string: a job crosses a process boundary
        # as JSON-ish data, and a UUID that survives only because both sides
        # happen to use the same serialiser is a dependency nobody declared.
        await self._pool.enqueue_job(INGEST_JOB, str(document_id), owner_id)


async def create_queue(redis_url: str) -> ArqRedis:
    """One pool for the process, created in the lifespan and closed with it."""
    return await create_pool(RedisSettings.from_dsn(redis_url))
