"""Reading and writing chunk vectors, with the owner always in the query.

Every function here takes `owner_id` as a **keyword-only, required** argument.
That is the entire point of the module: forgetting it raises a TypeError the
first time the code runs, instead of silently returning passages that belong to
somebody else.

A leak here would not look like a bug. The assistant would answer fluently,
cite a real page of a real document, and the user would have no way to tell the
document was not theirs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk
from app.rag.embeddings import EmbeddedChunk


@dataclass(frozen=True)
class RetrievedChunk:
    """A passage found by a search, with everything a citation needs."""

    document_id: uuid.UUID
    # Joined from `documents`, because a citation the user cannot recognise is
    # not a citation. The docstring above already promised "everything a
    # citation needs"; until now it was not true.
    filename: str
    chunk_index: int
    page_number: int
    text: str
    # Cosine distance: 0 is identical, 1 is unrelated, 2 is opposite. Kept so
    # the caller can refuse a weak match rather than answer from a passage that
    # happens to be the least bad one in the library.
    distance: float


async def replace_document_chunks(
    session: AsyncSession,
    *,
    owner_id: str,
    document_id: uuid.UUID,
    embedded: Sequence[EmbeddedChunk],
    embedding_model: str,
) -> int:
    """Write the chunks of one document, replacing whatever was there before.

    Idempotent on purpose: indexing runs in the background and may be retried
    after a crash. Deleting first means a retry converges to the same rows
    instead of doubling them.

    No commit here. The caller owns the transaction, so the document row and
    its chunks succeed or fail together.
    """
    await session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.document_id == document_id,
            # Redundant with document_id, and kept anyway: a delete that can
            # only ever touch one owner is a delete that cannot be turned into
            # a weapon by a wrong identifier.
            DocumentChunk.owner_id == owner_id,
        )
    )

    session.add_all(
        [
            DocumentChunk(
                document_id=document_id,
                owner_id=owner_id,
                chunk_index=item.chunk.index,
                page_number=item.chunk.page_number,
                text=item.chunk.text,
                embedding=item.vector,
                embedding_model=embedding_model,
            )
            for item in embedded
        ]
    )
    await session.flush()
    return len(embedded)


async def search_chunks(
    session: AsyncSession,
    *,
    owner_id: str,
    query_vector: Sequence[float],
    limit: int = 5,
    max_distance: float | None = None,
) -> list[RetrievedChunk]:
    """Return the passages of **this owner** closest to the query vector.

    The owner filter is applied by the database before any ranking, so another
    account cannot appear in the results however good a match it would be.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    # `<=>` in PostgreSQL. Cosine, matching both the HNSW index and the
    # normalised vectors we store; the default operator would rank differently.
    distance = DocumentChunk.embedding.cosine_distance(list(query_vector))

    # The join is on the chunk table that already carries `owner_id`, so the
    # filter below still stands alone: the join adds a name to rows that were
    # already restricted, it is never what restricts them. One statement rather
    # than a second round trip, and the planner joins at most `limit` rows
    # because the join happens after the index has ranked them.
    statement = (
        select(DocumentChunk, Document.filename, distance.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.owner_id == owner_id)
    )
    if max_distance is not None:
        statement = statement.where(distance <= max_distance)
    statement = statement.order_by(distance).limit(limit)

    rows = await session.execute(statement)
    return [
        RetrievedChunk(
            document_id=chunk.document_id,
            filename=filename,
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            text=chunk.text,
            distance=float(value),
        )
        for chunk, filename, value in rows.all()
    ]


async def delete_document(session: AsyncSession, *, owner_id: str, document_id: uuid.UUID) -> bool:
    """Delete one document and, by cascade, every chunk it produced.

    Returns False both when the document does not exist and when it belongs to
    somebody else. The caller answers 404 in both cases: a 403 would confirm
    that the identifier is real, which is enough to enumerate other accounts
    (contract.md).
    """
    # RETURNING asks PostgreSQL to hand back what it deleted, so one round trip
    # both performs the delete and tells us whether anything matched. A SELECT
    # followed by a DELETE would be two trips and a race between them.
    result = await session.execute(
        delete(Document)
        .where(Document.id == document_id, Document.owner_id == owner_id)
        .returning(Document.id)
    )
    return result.scalar_one_or_none() is not None
