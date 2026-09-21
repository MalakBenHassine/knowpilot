"""full-text search on chunks

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21 17:00:00

The keyword half of hybrid retrieval (ADR-0015). Three objects:

1. The `unaccent` extension, and a text search configuration that applies it
   before the French stemmer. Users type "numero" and "conges"; documents say
   "numéro" and "congés". Without folding, the two never meet - measured on the
   first probe of this feature.
2. A GENERATED column holding the tsvector of each chunk. PostgreSQL keeps it
   in sync on every insert and update, so no application code - and in
   particular not LangChain's PGVectorStore, which knows nothing about it - can
   forget to fill it.
3. A GIN index on that column, so a keyword search is an index lookup instead
   of re-parsing every chunk of the owner on every question.

`to_tsvector(regconfig, text)` is IMMUTABLE when the configuration is named
explicitly, which is what a generated column requires. `unaccent()` on its own
is only STABLE and could not be used here; wrapping it inside a text search
configuration is the standard way around that.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A "trusted" extension since PostgreSQL 13: the database owner can create
    # it without superuser rights, which a managed database will not grant.
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE TEXT SEARCH CONFIGURATION french_unaccent (COPY = french)")
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION french_unaccent "
        "ALTER MAPPING FOR hword, hword_part, word WITH unaccent, french_stem"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN text_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('french_unaccent'::regconfig, text)) STORED"
    )
    op.execute("CREATE INDEX ix_document_chunks_text_tsv ON document_chunks USING gin (text_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_text_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS text_tsv")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS french_unaccent")
    # The extension is left in place: another object may depend on it, and an
    # unused extension costs nothing.
