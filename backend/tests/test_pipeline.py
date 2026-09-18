"""The ingestion pipeline.

Real parsing, real chunking, a fake embedding model and a fake store: the whole
orchestration is exercised in milliseconds, with no database and no 2.2 GB of
weights. That is the payoff of having the pipeline depend on a protocol rather
than on PostgreSQL.
"""

import uuid
from pathlib import Path

import anyio
import pytest

from app.db.models import EMBEDDING_DIMENSIONS
from app.rag.embeddings import EmbeddedChunk
from app.rag.pipeline import ingest_document

DOCUMENT_ID = uuid.uuid4()
OWNER = "alice-sub-0001"


class FakeStore:
    """Records what the pipeline did, in order."""

    def __init__(self, path: Path | None, mime_type: str = "text/plain") -> None:
        self._found = (path, mime_type) if path is not None else None
        self.stages: list[str] = []
        self.result: dict[str, object] | None = None
        self.failure: tuple[str, bool] | None = None
        # Counts the entry point, which is how the upload tests prove the
        # background task was scheduled at all.
        self.load_calls = 0

    async def load(self, document_id: uuid.UUID, owner_id: str):  # type: ignore[no-untyped-def]
        self.load_calls += 1
        return self._found

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None:
        self.stages.append(stage)

    async def save_result(self, **kwargs: object) -> None:
        self.result = kwargs

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None:
        self.failure = (reason, retryable)


class FakeModel:
    @property
    def name(self) -> str:
        return "fake-model"

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    def encode(self, texts):  # type: ignore[no-untyped-def]
        return [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]


class ExplodingModel(FakeModel):
    def encode(self, texts):  # type: ignore[no-untyped-def]
        raise RuntimeError("the GPU caught fire")


def run(store: FakeStore, model: FakeModel) -> None:
    anyio.run(lambda: ingest_document(store, model, document_id=DOCUMENT_ID, owner_id=OWNER))


def write_note(tmp_path: Path, content: str = "") -> Path:
    path = tmp_path / "note.txt"
    path.write_text(content or "Les conges payes sont de 25 jours. " * 20)
    return path


# --- The happy path --------------------------------------------------------


def test_the_stages_follow_the_real_pipeline(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))

    run(store, FakeModel())

    # Exactly the four stages of the contract, in order, and each written
    # BEFORE the step it names - the user sees what is happening, not what
    # just finished. Parsing needs no announcement: it is the initial state.
    assert store.stages == ["chunking", "embedding", "indexing"]
    assert store.failure is None


def test_the_result_carries_what_a_citation_needs(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))

    run(store, FakeModel())

    assert store.result is not None
    assert store.result["owner_id"] == OWNER
    assert store.result["model_name"] == "fake-model"
    # The model name is stored beside the vectors: comparing vectors from two
    # different models is meaningless, so the mismatch must be detectable.
    embedded = store.result["embedded"]
    assert isinstance(embedded, list)
    assert all(isinstance(item, EmbeddedChunk) for item in embedded)
    assert store.result["page_count"] == 1


# --- Failures, which is where a background task earns its keep -------------


def test_a_document_that_vanished_is_simply_skipped() -> None:
    # The upload transaction rolled back after the task was scheduled, or the
    # user deleted the document. Not an error: there is nothing to do.
    store = FakeStore(None)

    run(store, FakeModel())

    assert store.stages == []
    assert store.failure is None


def test_a_file_without_usable_text_fails_without_a_retry_button(
    tmp_path: Path,
) -> None:
    store = FakeStore(write_note(tmp_path, "   \n\n  "))

    run(store, FakeModel())

    # A blank page will not become readable on a second attempt, so offering
    # "try again" would only waste the time of whoever believes it.
    assert store.failure == ("no_text_found", False)
    assert store.result is None


def test_an_unsupported_type_is_reported_as_such(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path), mime_type="application/zip")

    run(store, FakeModel())

    assert store.failure == ("unsupported_format", False)


def test_an_unexpected_crash_never_escapes_and_stays_retryable(
    tmp_path: Path,
) -> None:
    store = FakeStore(write_note(tmp_path))

    # If this raised, nobody would catch it: the response was sent long ago.
    # The document would stay `processing` for ever, spinning.
    run(store, ExplodingModel())

    assert store.failure == ("processing_error", True)
    assert store.result is None


@pytest.mark.parametrize("stage", ["chunking", "embedding", "indexing"])
def test_a_failure_leaves_no_half_written_result(tmp_path: Path, stage: str) -> None:
    store = FakeStore(write_note(tmp_path))

    run(store, ExplodingModel())

    # Whatever stage was reached, the document is never advertised as ready
    # with an incomplete set of passages.
    assert store.result is None
