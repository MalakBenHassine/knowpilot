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
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio.to_thread
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.core.metrics import INGESTION_CHUNKS, INGESTION_DURATION, INGESTIONS
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
# How long this deployment is willing to spend indexing ONE document, and
# what a page costs. Measured at 0.25 pages/s on a laptop CPU
# (docs/performance.md); six seconds leaves room for a slower machine.
#
# The check happens BEFORE the embedding starts, not as a deadline around
# it, and that is deliberate: `aembed_documents` hands one blocking call to
# a worker thread, and a cancel scope cannot interrupt a thread that is
# already inside torch. A timeout that cannot fire until the work it guards
# has finished is decoration. Once the page count is known, the cost is
# predictable, so the decision is taken while it can still be acted on -
# in seconds, with a reason, instead of half an hour later with a killed job.
#
# arq's own timeout (app/worker.py) sits ABOVE this on purpose: it is the
# net for a job that hangs somewhere unforeseen, never the thing that stops
# a document we could have refused.
MAX_INGESTION_SECONDS = 1800
SECONDS_PER_PAGE = 6


def affordable_pages() -> int:
    """The most pages this deployment can index inside its own budget."""
    return MAX_INGESTION_SECONDS // SECONDS_PER_PAGE


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
    started = time.perf_counter()

    found = await store.load(document_id, owner_id)
    if found is None:
        # The user deleted the document in between, or the upload rolled back.
        # Nothing to do, and certainly nothing to fail about.
        logger.info("document %s is gone, skipping indexing", document_id)
        INGESTIONS.labels(outcome="gone").inc()
        return

    try:
        # Loading and splitting are CPU-bound and blocking. Called directly,
        # they would freeze the event loop; run_sync moves them to a thread.
        loader = UploadedFileLoader(found.path, found.mime_type, filename=found.filename)
        pages = await anyio.to_thread.run_sync(loader.load)

        # The stage is written before each step, not after: the user sees what
        # is happening now, not what has just finished.
        # MAX_PAGES (app/rag/parsing.py) is a guard on MEMORY, checked while
        # the file is read. This one is a guard on TIME, and it can only be
        # checked here, where the page count is finally known.
        if len(pages) > affordable_pages():
            raise DocumentTooLargeError(
                f"{len(pages)} pages would need about "
                f"{len(pages) * SECONDS_PER_PAGE}s to index, and this "
                f"deployment allows {MAX_INGESTION_SECONDS}s per document"
            )

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
        INGESTIONS.labels(outcome="indexed").inc()
        INGESTION_CHUNKS.observe(len(chunks))
    except ParsingError as error:
        reason, retryable = _classify(error)
        # Expected failures: a scanned image, a format we refuse. Logged at
        # info level because they are the system working, not breaking.
        logger.info("document %s failed with %s", document_id, reason)
        await store.mark_failed(document_id, reason, retryable)
        INGESTIONS.labels(outcome=reason).inc()
    except Exception:
        # Anything unforeseen: a full disk, a killed connection, a bug of ours.
        # The cause goes to the logs with its traceback; the user gets a code
        # and a retry button, never an internal message.
        logger.exception("document %s failed unexpectedly", document_id)
        await store.mark_failed(document_id, "processing_error", True)
        INGESTIONS.labels(outcome="processing_error").inc()
    finally:
        # Measured for every verdict, not only for the good one: a failure
        # that takes nine minutes to arrive is its own problem.
        INGESTION_DURATION.observe(time.perf_counter() - started)
