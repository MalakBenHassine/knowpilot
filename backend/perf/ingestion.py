"""Measures how fast a document becomes searchable passages.

    uv run python -m perf.ingestion --pages 20

The read path is measured in perf/retrieval.py. This is the other half, and
the one a user actually waits for: an upload is accepted in milliseconds, but
the document only answers questions once the worker has parsed, split and
embedded it.

It runs the REAL pipeline - the same `ingest_document` the worker calls, the
same splitter, the same model - against a store that keeps nothing. The
database is left out on purpose: writing the rows is a fraction of the cost
and would drag the machine's disk into a number that is about the CPU.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.vector_store import load_checked_embeddings
from app.rag.pipeline import StoredFile, ingest_document
from perf.seed import vocabulary

# About 400 words, the order of a dense A4 page of a contract.
WORDS_PER_PAGE = 400


class NullStore:
    """Accepts everything the pipeline produces and keeps none of it."""

    def __init__(self, path: Path) -> None:
        self._file = StoredFile(path=path, mime_type="text/plain", filename=path.name)
        self.chunks = 0

    async def load(self, document_id: uuid.UUID, owner_id: str) -> StoredFile:
        return self._file

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None:
        return None

    async def save_result(self, **kwargs: Any) -> None:
        self.chunks = len(kwargs["chunks"])

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None:
        raise AssertionError(f"the benchmark document failed to index: {reason}")


def write_document(pages: int, path: Path) -> None:
    rng = random.Random(20260924)
    words = vocabulary(20_000)
    paragraphs = []
    for page in range(pages):
        sentences = []
        for _ in range(WORDS_PER_PAGE // 20):
            sentences.append(" ".join(rng.choice(words) for _ in range(20)).capitalize() + ".")
        paragraphs.append(f"Article {page + 1}. " + " ".join(sentences))
    path.write_text("\n\n".join(paragraphs), encoding="utf-8")


async def run(path: Path, pages: int, repeats: int) -> None:
    settings = get_settings()
    embeddings = load_checked_embeddings(settings.embedding_model, settings.embedding_cache_dir)

    samples = []
    store = NullStore(path)
    for index in range(repeats + 1):
        started = time.perf_counter()
        await ingest_document(
            store,
            embeddings,
            embedding_model=settings.embedding_model,
            document_id=uuid.uuid4(),
            owner_id="perf-benchmark-owner",
        )
        elapsed = time.perf_counter() - started
        # The first run pays for warm caches and buffers the others reuse.
        if index > 0:
            samples.append(elapsed)

    best = min(samples)
    median = sorted(samples)[len(samples) // 2]
    print(f"passages produced: {store.chunks}")
    print(f"  median {median:5.2f}s   best {best:5.2f}s")
    print(f"  {pages / median:5.1f} pages/s   {store.chunks / median:5.1f} passages/s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    arguments = parser.parse_args()

    # Written and removed here, not inside the coroutine: blocking file
    # calls have no business in async code, benchmark or not.
    path = Path("/tmp/perf-ingestion.txt")  # noqa: S108
    write_document(arguments.pages, path)
    print(f"document: {arguments.pages} pages, {path.stat().st_size / 1024:.0f} KiB")
    try:
        asyncio.run(run(path, arguments.pages, arguments.repeats))
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
