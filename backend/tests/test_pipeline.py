"""The ingestion pipeline: load, split, embed, store.

Real loading, real splitting, LangChain's DeterministicFakeEmbedding and a fake
store: the whole orchestration runs in milliseconds, with no database and no
2.2 GB of weights. That is the payoff of the pipeline depending on the
`Embeddings` abstraction and on a protocol rather than on PostgreSQL.
"""

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from prometheus_client import REGISTRY

from app.db.models import EMBEDDING_DIMENSIONS
from app.rag.pipeline import StoredFile, ingest_document
from tests.fakes import fake_embeddings

DOCUMENT_ID = uuid.uuid4()
OWNER = "alice-sub-0001"


class FakeStore:
    """Records what the pipeline did, in order."""

    def __init__(self, path: Path | None, mime_type: str = "text/plain") -> None:
        self._found = (
            StoredFile(path=path, mime_type=mime_type, filename="conges.txt")
            if path is not None
            else None
        )
        self.stages: list[str] = []
        self.result: dict[str, Any] | None = None
        self.failure: tuple[str, bool] | None = None

    async def load(self, document_id: uuid.UUID, owner_id: str) -> StoredFile | None:
        return self._found

    async def set_stage(self, document_id: uuid.UUID, stage: str) -> None:
        self.stages.append(stage)

    async def save_result(self, **kwargs: Any) -> None:
        self.result = kwargs

    async def mark_failed(self, document_id: uuid.UUID, reason: str, retryable: bool) -> None:
        self.failure = (reason, retryable)


class ExplodingEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("the GPU caught fire")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("the GPU caught fire")


def run(store: FakeStore, embeddings: Embeddings | None = None) -> None:
    anyio.run(
        lambda: ingest_document(
            store,
            embeddings or fake_embeddings(),
            embedding_model="fake-model",
            document_id=DOCUMENT_ID,
            owner_id=OWNER,
        )
    )


def write_note(tmp_path: Path, content: str = "") -> Path:
    path = tmp_path / "note.txt"
    path.write_text(content or "Les conges payes sont de 25 jours. " * 20)
    return path


# --- The happy path --------------------------------------------------------


def test_the_stages_follow_the_real_pipeline(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))

    run(store)

    # Exactly the stages of the contract, in order, and each written BEFORE
    # the step it names. Parsing needs no announcement: it is the initial state.
    assert store.stages == ["chunking", "embedding", "indexing"]
    assert store.failure is None


def test_the_result_carries_what_a_citation_needs(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))

    run(store)

    assert store.result is not None
    assert store.result["owner_id"] == OWNER
    assert store.result["filename"] == "conges.txt"
    # Stored beside the vectors: comparing vectors from two different models
    # is meaningless, so the mismatch must be detectable.
    assert store.result["embedding_model"] == "fake-model"
    assert store.result["page_count"] == 1

    chunks: Sequence[Document] = store.result["chunks"]
    vectors: Sequence[Sequence[float]] = store.result["vectors"]
    assert len(chunks) == len(vectors) > 0
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors)
    assert all(chunk.metadata["page_number"] == 1 for chunk in chunks)
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_each_vector_belongs_to_its_own_chunk(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path, "Premier paragraphe. " * 40 + "\n\n" + "Autre. " * 90))
    embeddings = fake_embeddings()

    run(store, embeddings)

    assert store.result is not None
    # DeterministicFakeEmbedding hashes the text, so a misaligned pair would
    # show here as a vector that belongs to a different chunk.
    for chunk, vector in zip(store.result["chunks"], store.result["vectors"], strict=True):
        assert vector == embeddings.embed_query(chunk.page_content)


# --- Failures, which is where a background job earns its keep --------------


def test_a_document_that_vanished_is_simply_skipped() -> None:
    store = FakeStore(None)

    run(store)

    assert store.stages == []
    assert store.failure is None


def test_a_file_without_usable_text_fails_without_a_retry_button(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path, "   \n\n  "))

    run(store)

    # A blank page will not become readable on a second attempt.
    assert store.failure == ("no_text_found", False)
    assert store.result is None


def test_an_unsupported_type_is_reported_as_such(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path), mime_type="application/zip")

    run(store)

    assert store.failure == ("unsupported_format", False)


def test_an_unexpected_crash_never_escapes_and_stays_retryable(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))

    # If this raised, nobody would catch it: the job runs in the worker. The
    # document would stay `processing` for ever, spinning.
    run(store, ExplodingEmbeddings())

    assert store.failure == ("processing_error", True)
    assert store.result is None


def test_a_failure_while_saving_is_reported_not_swallowed(tmp_path: Path) -> None:
    class BrokenStore(FakeStore):
        async def save_result(self, **kwargs: Any) -> None:
            raise ConnectionError("the database went away")

    store = BrokenStore(write_note(tmp_path))

    run(store)

    # The last stage announced was indexing, and it failed: the user gets a
    # retry button, not a document stuck in `processing`.
    assert store.stages[-1] == "indexing"
    assert store.failure == ("processing_error", True)


# --- What the worker exports ------------------------------------------------
#
# The API's metrics come from its own routes; these are the only numbers that
# say whether uploads are becoming passages at all. The API can answer every
# request while every document rots in `processing`.


def counted(outcome: str) -> float:
    return REGISTRY.get_sample_value("knowpilot_ingestions_total", {"outcome": outcome}) or 0.0


def measured() -> float:
    return REGISTRY.get_sample_value("knowpilot_ingestion_duration_seconds_count") or 0.0


def test_an_indexed_document_is_counted_with_its_passages(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path))
    before, timed = counted("indexed"), measured()

    run(store)

    assert counted("indexed") == before + 1
    assert measured() == timed + 1
    chunks = REGISTRY.get_sample_value("knowpilot_ingestion_chunks_count") or 0.0
    assert chunks > 0


def test_each_failure_is_counted_under_its_own_reason(tmp_path: Path) -> None:
    store = FakeStore(write_note(tmp_path), mime_type="application/x-tar")
    before = counted("unsupported_format")

    run(store)

    # The reason, not a single "failed": "this format is refused" and "the
    # disk is full" call for different people at different hours.
    assert counted("unsupported_format") == before + 1


def test_a_document_that_vanished_is_counted_but_not_timed(tmp_path: Path) -> None:
    store = FakeStore(None)
    before, timed = counted("gone"), measured()

    run(store)

    assert counted("gone") == before + 1
    # Deliberately NOT timed: nothing was done, and a pile of zero-second
    # ingestions would drag the histogram towards a speed nobody achieved.
    assert measured() == timed


def test_a_slow_failure_is_timed_like_a_success(tmp_path: Path) -> None:
    """A failure that takes nine minutes to arrive is its own problem."""
    store = FakeStore(write_note(tmp_path))
    timed = measured()

    run(store, ExplodingEmbeddings())

    assert measured() == timed + 1
