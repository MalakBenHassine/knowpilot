"""The database side of the ingestion pipeline.

This is the adapter that turns the `IngestionStore` protocol into PostgreSQL
calls. It lives here, not in app/rag, so the pipeline keeps no dependency on
the database at all - which is what lets it be tested without one.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.documents import get_document, mark_failed, mark_ready, set_stage
from app.db.vector_store import replace_document_chunks
from app.rag.embeddings import EmbeddedChunk


class DatabaseIngestionStore:
    """Persists pipeline progress, one short transaction at a time.

    Each method opens its own session. That is deliberate: indexing a large
    document takes a minute, and holding one transaction open for the whole
    run would keep a connection busy, block autovacuum, and - worse - make the
    progress invisible, since nothing uncommitted can be read by the request
    that polls for it.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load(self, document_id: uuid.UUID, owner_id: str) -> tuple[Path, str] | None:
        async with self._sessions() as session:
            document = await get_document(session, owner_id=owner_id, document_id=document_id)
            if document is None:
                return None
            return Path(document.storage_path), document.mime_type

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None:
        async with self._sessions() as session:
            await set_stage(session, document_id=document_id, stage=stage)
            # Committed immediately so the interface, which polls every second
            # and a half, actually sees the pipeline move.
            await session.commit()

    async def save_result(
        self,
        *,
        document_id: uuid.UUID,
        owner_id: str,
        embedded: list[EmbeddedChunk],
        model_name: str,
        page_count: int,
    ) -> None:
        async with self._sessions() as session:
            # Chunks and the ready status in ONE transaction: a document is
            # never advertised as ready while its passages are half written.
            await replace_document_chunks(
                session,
                owner_id=owner_id,
                document_id=document_id,
                embedded=embedded,
                embedding_model=model_name,
            )
            await mark_ready(
                session,
                document_id=document_id,
                page_count=page_count,
                chunk_count=len(embedded),
            )
            await session.commit()

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None:
        async with self._sessions() as session:
            await mark_failed(session, document_id=document_id, reason=reason, retryable=retryable)
            await session.commit()
