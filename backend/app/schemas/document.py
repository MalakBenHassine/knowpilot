"""What the API says about a document.

Built explicitly from the model, never by serialising it. The database row
holds `owner_id`, `storage_path` and `content_hash`; none of them belong in a
response, and a schema that mirrors the table would leak all three the day
somebody adds a column.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models import Document


class DocumentResponse(BaseModel):
    """One document, exactly as docs/api/contract.md describes it."""

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    # Only while processing. A real pipeline step, never a made-up percentage.
    stage: str | None
    page_count: int | None
    chunk_count: int | None
    # A code such as no_text_found. The wording belongs to the frontend.
    failure_reason: str | None
    # Only the backend knows whether trying again can succeed.
    retryable: bool
    created_at: datetime

    @classmethod
    def of(cls, document: Document) -> DocumentResponse:
        return cls(
            id=document.id,
            filename=document.filename,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            status=document.status,
            stage=document.stage,
            page_count=document.page_count,
            chunk_count=document.chunk_count,
            failure_reason=document.failure_reason,
            retryable=document.retryable,
            created_at=document.created_at,
        )


class DocumentListResponse(BaseModel):
    """An envelope, so `total` or `next_cursor` can appear later without
    breaking any client that already reads `items`."""

    items: list[DocumentResponse]
