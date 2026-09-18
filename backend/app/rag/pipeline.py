"""The ingestion pipeline: a file goes in, searchable passages come out.

This module orchestrates parsing, chunking, embedding and indexing. It knows
nothing about PostgreSQL: persistence arrives as an `IngestionStore`, which is
why the whole orchestration can be tested in milliseconds against a fake, with
real parsing and real chunking.
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from pathlib import Path
from typing import Protocol

import anyio.to_thread

from app.rag.chunking import chunk_document
from app.rag.embeddings import EmbeddedChunk, EmbeddingModel, embed_chunks
from app.rag.parsing import (
    DocumentTooLargeError,
    NoTextFoundError,
    ParsingError,
    UnsupportedFormatError,
    parse_document,
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


class IngestionStore(Protocol):
    """What the pipeline needs from persistence, and nothing more."""

    async def load(self, document_id: uuid.UUID, owner_id: str) -> tuple[Path, str] | None: ...

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None: ...

    async def save_result(
        self,
        *,
        document_id: uuid.UUID,
        owner_id: str,
        embedded: list[EmbeddedChunk],
        model_name: str,
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
    model: EmbeddingModel,
    *,
    document_id: uuid.UUID,
    owner_id: str,
) -> None:
    """Parse, chunk, embed and index one document.

    This function never raises. It runs after the response has been sent, so
    there is nobody left to receive an error: an escaped exception would leave
    the document stuck in `processing` for ever, showing a spinner the user
    cannot stop and cannot retry.
    """
    found = await store.load(document_id, owner_id)
    if found is None:
        # The upload transaction rolled back after this task was scheduled, or
        # the user deleted the document in between. Nothing to do, and
        # certainly nothing to fail about.
        logger.info("document %s is gone, skipping indexing", document_id)
        return

    path, mime_type = found

    try:
        # Every step here is CPU-bound and blocking. Called directly, it would
        # freeze the event loop and with it every other request in flight.
        # run_sync moves it to a worker thread, and torch releases the GIL
        # while it computes, so the thread genuinely runs in parallel.
        parsed = await anyio.to_thread.run_sync(partial(parse_document, path, mime_type))

        # The stage is written before each step, not after: the user sees what
        # is happening now, not what has just finished.
        await store.set_stage(document_id, "chunking")
        chunks = await anyio.to_thread.run_sync(chunk_document, parsed)
        if not chunks:
            # Parsing found text but chunking kept none of it - whitespace, a
            # single character, a page of dashes. Indexing nothing would leave
            # a document that is ready and answers no question at all.
            raise NoTextFoundError("parsing produced no usable passage")

        await store.set_stage(document_id, "embedding")
        embedded = await anyio.to_thread.run_sync(partial(embed_chunks, chunks, model))

        await store.set_stage(document_id, "indexing")
        await store.save_result(
            document_id=document_id,
            owner_id=owner_id,
            embedded=embedded,
            model_name=model.name,
            page_count=parsed.page_count,
        )
        logger.info(
            "indexed document %s: %d pages, %d chunks, %d pages via ocr",
            document_id,
            parsed.page_count,
            len(chunks),
            parsed.ocr_page_count,
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
