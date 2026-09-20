"""Re-index every stored document.

    uv run python -m scripts.reindex

Needed after anything that changes what a chunk IS: the chunk size, the
overlap, the separators, the parser, or the embedding model. Documents already
in the database keep the cut they were indexed with, so a tuning decision
improves nothing until the existing corpus is put back through the pipeline.

It publishes jobs and returns immediately. The worker does the work, one
document at a time, and the interface shows the stages as usual - which is the
whole point of having a queue: re-indexing a library is a long job that must
not run inside the process serving requests.

Safe to run twice. `replace_document_chunks` deletes before it inserts, so a
document re-indexed a second time ends up with exactly one set of chunks.

This is an OPERATOR tool and it deliberately crosses every tenant boundary:
it reads the documents of all owners, which no request handler is ever allowed
to do. The owner still travels with each job, so the pipeline itself stays
scoped exactly as it is for an upload.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.queue import ArqJobQueue, create_queue
from app.db import documents as repository
from app.db.models import Document
from app.db.session import create_engine, create_session_factory


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    pool = await create_queue(settings.redis_url)
    queue = ArqJobQueue(pool)

    published = 0
    async with factory() as session:
        rows = (await session.execute(select(Document.id, Document.owner_id))).all()
        for document_id, _owner_id in rows:
            # Back to `processing` before the job is published, so the
            # interface shows the work rather than a stale `ready` that would
            # make the re-index look instantaneous and then silently change the
            # answers underneath the user.
            await repository.reset_for_retry(session, document_id=document_id)
        await session.commit()

    # Committed above, published here, for the reason the upload endpoint
    # learned the hard way: a worker in another process reads through its own
    # connection and cannot see an uncommitted row.
    for document_id, owner_id in rows:
        await queue.enqueue_ingestion(document_id=document_id, owner_id=owner_id)
        published += 1

    await pool.aclose()
    await engine.dispose()

    print(f"{published} document(s) re-queued. The worker must be running:")
    print("    uv run arq app.worker.WorkerSettings")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
