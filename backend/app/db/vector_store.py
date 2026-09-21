"""The chunk table, exposed to LangChain as a `PGVectorStore`.

`langchain-postgres` can create its own tables, and we deliberately do not let
it. `PGVectorStore.create` is pointed at the `document_chunks` table that
Alembic owns, with an explicit column mapping. That one decision keeps three
properties LangChain's default table would have lost:

- `owner_id` is a real, indexed column, so the tenant filter is a WHERE clause
  PostgreSQL applies before ranking - not a JSON lookup on a metadata blob;
- `document_id` is a foreign key with ON DELETE CASCADE, so deleting a document
  deletes its vectors in the same statement, and no vector can outlive its file;
- the schema stays under migrations, where every change is reviewed.

Every function here that reads or writes chunks takes `owner_id` as a
keyword-only, required argument. Forgetting it raises a TypeError the first
time the code runs, instead of silently touching somebody else's passages.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_postgres import PGEngine, PGVectorStore
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import EMBEDDING_DIMENSIONS
from app.db.models import Document as DocumentRow
from app.rag.embeddings import dimensions_of, load_embeddings

TABLE_NAME = "document_chunks"

# The columns LangChain maps to `Document.metadata`. Real columns, not a JSON
# blob: they are filtered on, joined on, and constrained by the database.
METADATA_COLUMNS = [
    "document_id",
    "owner_id",
    "filename",
    "chunk_index",
    "page_number",
    "embedding_model",
]

# A fixed namespace for chunk identifiers. uuid5 is a hash, so the same
# (document, position) always produces the same id - the idempotency key of
# LangChain's indexing guidance, and what lets a retried job overwrite its own
# rows with ON CONFLICT instead of doubling them.
_CHUNK_NAMESPACE = uuid.UUID("5b0e7a52-0f1c-4a7e-9a55-6f1e3b0c2d11")


def chunk_id(document_id: uuid.UUID, chunk_index: int) -> str:
    return str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}:{chunk_index}"))


async def create_vector_store(engine: AsyncEngine, embeddings: Embeddings) -> PGVectorStore:
    """Wrap our existing table and our existing connection pool.

    `PGEngine.from_engine` reuses the application's SQLAlchemy engine rather
    than opening a second pool: one set of connections to size, to monitor
    and to close at shutdown.

    `metadata_json_column=None` means there is no catch-all JSON column. A
    metadata key with no column of its own is dropped rather than stored
    somewhere nobody looks - so what is stored is exactly what is declared.
    """
    return await PGVectorStore.create(
        PGEngine.from_engine(engine),
        embedding_service=embeddings,
        table_name=TABLE_NAME,
        id_column="id",
        content_column="text",
        embedding_column="embedding",
        metadata_columns=METADATA_COLUMNS,
        metadata_json_column=None,
    )


def load_checked_embeddings(name: str, cache_dir: str | None) -> Embeddings:
    """Load the model and refuse to go on if it does not fit the column.

    The column is vector(1024). A model of a different size would write rows
    PostgreSQL rejects, or worse, vectors nobody can compare. Checked here, at
    startup of both the API and the worker, rather than halfway through
    somebody's first upload.
    """
    embeddings = load_embeddings(name, cache_dir)
    dimensions = dimensions_of(embeddings)
    if dimensions != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"{name} produces {dimensions} dimensions, but the schema stores "
            f"{EMBEDDING_DIMENSIONS}. Changing the model requires a migration and a full re-index."
        )
    return embeddings


def owner_filter(owner_id: str) -> dict[str, object]:
    """The one filter every read and every delete goes through.

    A function rather than a literal repeated at each call site: the day the
    filter needs a second condition, there is one place to add it.
    """
    if not owner_id:
        # An empty owner would match nothing today, and a future operator such
        # as $in could make it match everything. Refused outright.
        raise ValueError("owner_id is required")
    return {"owner_id": {"$eq": owner_id}}


async def replace_document_chunks(
    store: PGVectorStore,
    *,
    owner_id: str,
    document_id: uuid.UUID,
    filename: str,
    chunks: Sequence[Document],
    vectors: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Write the chunks of one document, replacing whatever was there before.

    Deleting first removes chunks a previous, longer cut produced; the
    deterministic identifiers make the insert itself idempotent. Together
    they make a retried job converge to one set of rows.

    Not atomic with the `ready` status, and that is a known trade-off of the
    LangChain store, which commits per row: a crash midway leaves some chunks
    of a document still marked `processing`, and the retry that follows
    deletes and rewrites them. What can never happen is a document advertised
    as `ready` with half its passages - the status is written after this call
    returns.
    """
    if len(chunks) != len(vectors):
        # Anti-corruption boundary. A backend that dropped a vector would
        # misalign every following text with the wrong embedding, and retrieval
        # would return confident, perfectly cited nonsense.
        raise ValueError(f"{len(vectors)} vectors for {len(chunks)} chunks")

    await store.adelete(
        filter={"$and": [owner_filter(owner_id), {"document_id": {"$eq": document_id}}]}
    )
    if not chunks:
        return 0

    await store.aadd_embeddings(
        texts=[chunk.page_content for chunk in chunks],
        embeddings=[list(vector) for vector in vectors],
        metadatas=[
            {
                "document_id": document_id,
                "owner_id": owner_id,
                "filename": filename,
                "chunk_index": chunk.metadata["chunk_index"],
                "page_number": chunk.metadata["page_number"],
                "embedding_model": embedding_model,
            }
            for chunk in chunks
        ],
        ids=[chunk_id(document_id, chunk.metadata["chunk_index"]) for chunk in chunks],
    )
    return len(chunks)


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
        delete(DocumentRow)
        .where(DocumentRow.id == document_id, DocumentRow.owner_id == owner_id)
        .returning(DocumentRow.id)
    )
    return result.scalar_one_or_none() is not None
