"""The database side of the ingestion pipeline.

This is the adapter that turns the `IngestionStore` protocol into PostgreSQL
calls: document status through SQLAlchemy, chunks through LangChain's
`PGVectorStore`. It lives here, not in app/rag, so the pipeline keeps no
dependency on the database at all - which is what lets it be tested without one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from langchain_core.documents import Document
from langchain_postgres import PGVectorStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.documents import get_document, mark_failed, mark_ready, set_stage
from app.db.vector_store import replace_document_chunks
from app.rag.pipeline import StoredFile


class DatabaseIngestionStore:
    """Persists pipeline progress, one short transaction at a time.

    Each method opens its own session. Indexing a large document takes a
    minute, and holding one transaction open for the whole run would keep a
    connection busy, block autovacuum, and make the progress invisible, since
    nothing uncommitted can be read by the request that polls for it.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], vector_store: PGVectorStore
    ) -> None:
        self._sessions = session_factory
        self._vectors = vector_store

    async def load(self, document_id: uuid.UUID, owner_id: str) -> StoredFile | None:
        async with self._sessions() as session:
            document = await get_document(session, owner_id=owner_id, document_id=document_id)
            if document is None:
                return None
            return StoredFile(
                path=Path(document.storage_path),
                mime_type=document.mime_type,
                filename=document.filename,
            )

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
        filename: str,
        chunks: Sequence[Document],
        vectors: Sequence[Sequence[float]],
        embedding_model: str,
        page_count: int,
    ) -> None:
        # Chunks first, status second: a document is never advertised as ready
        # while its passages are still being written (see
        # replace_document_chunks for why the two are not one transaction).
        written = await replace_document_chunks(
            self._vectors,
            owner_id=owner_id,
            document_id=document_id,
            filename=filename,
            chunks=chunks,
            vectors=vectors,
            embedding_model=embedding_model,
        )
        async with self._sessions() as session:
            await mark_ready(
                session, document_id=document_id, page_count=page_count, chunk_count=written
            )
            await session.commit()

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None:
        async with self._sessions() as session:
            await mark_failed(session, document_id=document_id, reason=reason, retryable=retryable)
            await session.commit()
