"""add filename to chunks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21 16:00:00

LangChain returns each retrieved passage as a Document whose metadata is its
row, so the filename a citation needs is copied onto every chunk.

Written in three steps - add nullable, backfill, then enforce NOT NULL - rather
than as one `add_column(nullable=False)`. On a table that already holds rows
the single step fails outright, and on a large one the backfill is where the
time goes, so it is kept a separate, visible statement.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("filename", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE document_chunks AS chunk
        SET filename = document.filename
        FROM documents AS document
        WHERE document.id = chunk.document_id
        """
    )
    op.alter_column("document_chunks", "filename", nullable=False)


def downgrade() -> None:
    op.drop_column("document_chunks", "filename")
