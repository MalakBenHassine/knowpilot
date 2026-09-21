"""The database schema.

Documents and their vectors live in the same database on purpose: deleting a
document deletes its chunks in the same transaction, so no vector can outlive
the document it came from and answer a question about a file the user removed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# BGE-M3 produces 1024 dimensions (ADR-0006). This is part of the schema, so
# changing the model means a migration AND re-indexing every document. The
# application refuses to start if the loaded model disagrees with this number.
EMBEDDING_DIMENSIONS = 1024

# The same closed sets as docs/api/contract.md and the frontend types, written
# here as database constraints as well: the API can be bypassed, the database
# cannot.
DOCUMENT_STATUSES = ("uploading", "processing", "ready", "failed")
INGESTION_STAGES = ("parsing", "chunking", "embedding", "indexing")
FAILURE_REASONS = (
    "no_text_found",
    "unsupported_format",
    "too_large",
    "processing_error",
)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    """Build a SQL IN clause. NULL passes it, which nullable columns need."""
    quoted = [repr(value) for value in values]
    separator = ", "
    return f"{column} IN ({separator.join(quoted)})"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # The sub claim from Keycloak: stable, unlike an email address. Indexed
    # because every single query filters on it.
    owner_id: Mapped[str] = mapped_column(String(255), index=True)

    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int]
    # SHA-256 of the file. Lets us refuse the same upload twice for one user,
    # and detect that a re-uploaded file is unchanged.
    content_hash: Mapped[str] = mapped_column(String(64))
    # Where the bytes live on disk. Never returned by the API.
    storage_path: Mapped[str] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(32), default="processing")
    stage: Mapped[str | None] = mapped_column(String(32), default=None)
    failure_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    retryable: Mapped[bool] = mapped_column(default=False)

    page_count: Mapped[int | None] = mapped_column(default=None)
    chunk_count: Mapped[int | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        # The database performs the cascade (ON DELETE CASCADE). passive_deletes
        # stops SQLAlchemy from loading every chunk into memory to delete them
        # one at a time.
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # The same file uploaded twice by one user is one document. Two
        # different users uploading the same file stay fully independent.
        UniqueConstraint("owner_id", "content_hash", name="owner_content"),
        CheckConstraint(_one_of("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint(_one_of("stage", INGESTION_STAGES), name="stage"),
        CheckConstraint(_one_of("failure_reason", FAILURE_REASONS), name="failure_reason"),
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    # Denormalised deliberately. It could be reached through documents, but a
    # search must filter on the owner BEFORE comparing vectors, and a join that
    # someone forgets to write returns private passages belonging to another
    # user with no error at all. Isolation must not depend on remembering a
    # join.
    owner_id: Mapped[str] = mapped_column(String(255), index=True)

    # Denormalised as well, and for a different reason: LangChain returns each
    # passage as a Document whose metadata is this row. Carrying the filename
    # here means a citation names its file without a join or a second query at
    # answer time. Safe because a filename never changes after upload; the day
    # renaming exists, it must update both tables in one transaction.
    filename: Mapped[str] = mapped_column(String(255))

    chunk_index: Mapped[int]
    # Carried all the way from parsing so an answer can cite a page number.
    page_number: Mapped[int]
    text: Mapped[str] = mapped_column(Text)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Which model produced this vector. Comparing vectors from two different
    # models is meaningless, so we store the name and can detect the mismatch.
    embedding_model: Mapped[str] = mapped_column(String(128))

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="document_chunk"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            # Cosine, matching the normalised vectors we produce (ADR-0006).
            # The default operator class is L2, which would rank differently.
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )
