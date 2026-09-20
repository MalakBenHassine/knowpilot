"""Publishing ingestion jobs.

Redis is faked: what matters here is the contract that crosses the process
boundary - the job name, and the arguments a worker will be handed. Those two
things are the only reason a job can silently never run.
"""

import uuid
from typing import Any

import anyio

from app.core.queue import INGEST_JOB, ArqJobQueue

DOCUMENT_ID = uuid.UUID("0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80")
ALICE = "alice-sub-0001"


class FakePool:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, name: str, *args: Any) -> None:
        self.jobs.append((name, args))


def publish(queue: ArqJobQueue) -> None:
    anyio.run(lambda: queue.enqueue_ingestion(document_id=DOCUMENT_ID, owner_id=ALICE))


def test_the_job_name_matches_the_one_the_worker_registers() -> None:
    """The failure this prevents is the quietest one in the system.

    A typo in a name that crosses a process boundary raises nothing: the job is
    published, no worker claims it, and the document stays `processing` for
    ever while every log says the upload succeeded.
    """
    pool = FakePool()

    publish(ArqJobQueue(pool))  # type: ignore[arg-type]

    assert pool.jobs[0][0] == INGEST_JOB


def test_the_identifier_crosses_as_a_string() -> None:
    """A job travels as data, not as Python objects.

    A UUID that survives only because both processes happen to use the same
    serialiser is a dependency nobody wrote down, and it breaks on the day the
    worker is upgraded alone.
    """
    pool = FakePool()

    publish(ArqJobQueue(pool))  # type: ignore[arg-type]

    name, args = pool.jobs[0]
    assert args == (str(DOCUMENT_ID), ALICE)
    assert all(isinstance(argument, str) for argument in args)


def test_the_owner_travels_with_the_job() -> None:
    """The worker is never asked to look the owner up.

    Every tenant-scoped call in this codebase takes the owner explicitly. A
    worker that resolved it from the document row would be one more place where
    forgetting it leaks, and it would do so in a process nobody is watching.
    """
    pool = FakePool()

    publish(ArqJobQueue(pool))  # type: ignore[arg-type]

    assert pool.jobs[0][1][1] == ALICE
