"""Reading and writing the documents table.

Like the vector store, every function takes `owner_id` as a required
keyword-only argument. A listing that forgets it shows the whole platform to
whoever asks.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document


async def create_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    owner_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    content_hash: str,
    storage_path: str,
) -> Document:
    """Insert a document that is about to be processed.

    The identifier is passed in rather than generated here: the file was
    already written under that name, so the row and the bytes agree even if
    this transaction is rolled back.
    """
    document = Document(
        id=document_id,
        owner_id=owner_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_path=storage_path,
        status="processing",
        stage="parsing",
    )
    session.add(document)
    await session.flush()
    return document


async def find_by_content_hash(
    session: AsyncSession, *, owner_id: str, content_hash: str
) -> Document | None:
    """Find an identical file already uploaded by this user.

    Scoped to the owner on purpose. Answering across users would turn the
    hash into an oracle: an attacker could learn that some other account holds
    a specific file simply by uploading a copy of it.
    """
    # scalars().first() rather than scalar(): it is the form SQLAlchemy types
    # precisely, so mypy knows a Document comes back and not Any.
    result = await session.scalars(
        select(Document).where(Document.owner_id == owner_id, Document.content_hash == content_hash)
    )
    return result.first()


async def list_documents(session: AsyncSession, *, owner_id: str) -> Sequence[Document]:
    """Newest first, as the contract says. Empty is a normal answer, not a 404."""
    result = await session.scalars(
        select(Document).where(Document.owner_id == owner_id).order_by(Document.created_at.desc())
    )
    return result.all()


async def get_document(
    session: AsyncSession, *, owner_id: str, document_id: uuid.UUID
) -> Document | None:
    """None both when it does not exist and when it belongs to someone else.

    The caller answers 404 either way: a 403 would confirm the identifier is
    real, which is all an attacker needs to enumerate other accounts.
    """
    result = await session.scalars(
        select(Document).where(Document.id == document_id, Document.owner_id == owner_id)
    )
    return result.first()


async def set_stage(session: AsyncSession, *, document_id: uuid.UUID, stage: str) -> None:
    """Record the real pipeline step the document is going through.

    The user sees `embedding`, not an invented percentage: a made-up progress
    bar that jumps from 30 to 90 destroys more trust than an honest label.
    """
    document = await session.get(Document, document_id)
    if document is None:
        return
    document.status = "processing"
    document.stage = stage
    await session.flush()


async def mark_ready(
    session: AsyncSession, *, document_id: uuid.UUID, page_count: int, chunk_count: int
) -> None:
    document = await session.get(Document, document_id)
    if document is None:
        return
    document.status = "ready"
    # Cleared together with the status: a ready document showing a stage would
    # leave the interface in a state the frontend types say cannot exist.
    document.stage = None
    document.failure_reason = None
    document.retryable = False
    document.page_count = page_count
    document.chunk_count = chunk_count
    await session.flush()


async def mark_failed(
    session: AsyncSession, *, document_id: uuid.UUID, reason: str, retryable: bool
) -> None:
    """Record a failure as a code, never as a message.

    The wording belongs to the frontend, which can translate it. A technical
    message would leak internals and could not be localised.
    """
    document = await session.get(Document, document_id)
    if document is None:
        return
    document.status = "failed"
    document.stage = None
    document.failure_reason = reason
    document.retryable = retryable
    await session.flush()
