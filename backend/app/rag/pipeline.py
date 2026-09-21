"""The ingestion pipeline: a file goes in, searchable passages come out.

The canonical LangChain indexing flow - load, split, embed, store - with each
step a standard component:

    UploadedFileLoader (BaseLoader)  ->  Documents, one per page
    RecursiveCharacterTextSplitter   ->  Documents, one per chunk
    Embeddings.aembed_documents      ->  vectors
    PGVectorStore (via the store)    ->  rows in document_chunks

This module knows nothing about PostgreSQL: persistence arrives as an
`IngestionStore`, which is why the whole orchestration is tested in
milliseconds against a fake store and LangChain's fake embeddings, with real
parsing and real splitting.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio.to_thread
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.rag.chunking import split_pages
from app.rag.loaders import UploadedFileLoader
from app.rag.parsing import (
    DocumentTooLargeError,
    NoTextFoundError,
    ParsingError,
    UnsupportedFormatError,
)

logger = logging.getLogger(__name__)

# Exception -> (failure_reason, retryable). Only the backend can judge whether
# a second attempt has any chance: a scanned page will never become readable,
# while a full disk or a dropped connection might well clear by itself.
FAILURES: tuple[tuple[type[ParsingError], str, bool], ...] = (
    (UnsupportedFormatError, "unsupported_format", False),
    (NoTextFoundError, "no_text_found", False),
    (DocumentTooLargeError, "too_large", False),
)


@dataclass(frozen=True)
class StoredFile:
    path: Path
    mime_type: str
    # The name the user chose. Copied onto every chunk as metadata, so a
    # citation can name its file without a second query at answer time.
    filename: str


class IngestionStore(Protocol):
    """What the pipeline needs from persistence, and nothing more."""

    async def load(self, document_id: uuid.UUID, owner_id: str) -> StoredFile | None: ...

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None: ...

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
    ) -> None: ...

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None: ...


def _classify(error: ParsingError) -> tuple[str, bool]:
    for kind, reason, retryable in FAILURES:
        if isinstance(error, kind):
            return reason, retryable
    return "processing_error", True


async def ingest_document(
    store: IngestionStore,
    embeddings: Embeddings,
    *,
    embedding_model: str,
    document_id: uuid.UUID,
    owner_id: str,
) -> None:
    """Load, split, embed and index one document.

    This function never raises. It runs in the worker, where there is nobody
    left to receive an error: an escaped exception would leave the document
    stuck in `processing` for ever, showing a spinner the user cannot stop.
    """
    found = await store.load(document_id, owner_id)
    if found is None:
        # The user deleted the document in between, or the upload rolled back.
        # Nothing to do, and certainly nothing to fail about.
        logger.info("document %s is gone, skipping indexing", document_id)
        return

    try:
        # Loading and splitting are CPU-bound and blocking. Called directly,
        # they would freeze the event loop; run_sync moves them to a thread.
        loader = UploadedFileLoader(found.path, found.mime_type, filename=found.filename)
        pages = await anyio.to_thread.run_sync(loader.load)

        # The stage is written before each step, not after: the user sees what
        # is happening now, not what has just finished.
        await store.set_stage(document_id, "chunking")
        chunks = await anyio.to_thread.run_sync(split_pages, pages)
        if not chunks:
            # Parsing found text but splitting kept none of it - whitespace, a
            # page of dashes. Indexing nothing would leave a document that is
            # ready and answers no question at all.
            raise NoTextFoundError("parsing produced no usable passage")

        await store.set_stage(document_id, "embedding")
        # aembed_documents runs the model in a thread pool: torch releases the
        # GIL while it computes, so the thread genuinely runs in parallel.
        vectors = await embeddings.aembed_documents([chunk.page_content for chunk in chunks])

        await store.set_stage(document_id, "indexing")
        page_count = int(pages[0].metadata["total_pages"]) if pages else 0
        await store.save_result(
            document_id=document_id,
            owner_id=owner_id,
            filename=found.filename,
            chunks=chunks,
            vectors=vectors,
            embedding_model=embedding_model,
            page_count=page_count,
        )
        logger.info(
            "indexed document %s: %d pages, %d chunks, %d pages via ocr",
            document_id,
            page_count,
            len(chunks),
            sum(1 for page in pages if page.metadata.get("extraction") == "ocr"),
        )
    except ParsingError as error:
        reason, retryable = _classify(error)
        # Expected failures: a scanned image, a format we refuse. Logged at
        # info level because they are the system working, not breaking.
        logger.info("document %s failed with %s", document_id, reason)
        await store.mark_failed(document_id, reason, retryable)
    except Exception:
        # Anything unforeseen: a full disk, a killed connection, a bug of ours.
        # The cause goes to the logs with its traceback; the user gets a code
        # and a retry button, never an internal message.
        logger.exception("document %s failed unexpectedly", document_id)
        await store.mark_failed(document_id, "processing_error", True)
