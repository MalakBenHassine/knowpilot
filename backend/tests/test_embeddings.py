"""Embedding orchestration.

No model is loaded: a fake backend records how it was called. What is tested is
batching, ordering and the checks that stop a broken backend from silently
misaligning every vector with its text.
"""

import math

import pytest

from app.rag.chunking import Chunk
from app.rag.embeddings import (
    EmbeddingError,
    embed_chunks,
    embed_query,
    normalise,
)


class FakeModel:
    """Returns a vector derived from the text, and records every batch size."""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.batch_sizes: list[int] = []

    @property
    def name(self) -> str:
        return "fake-model"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def encode(self, texts):  # type: ignore[no-untyped-def]
        self.batch_sizes.append(len(texts))
        return [[float(len(text))] * self._dimensions for text in texts]


class TruncatingModel(FakeModel):
    """A backend that quietly drops the last result of every batch."""

    def encode(self, texts):  # type: ignore[no-untyped-def]
        return super().encode(texts)[:-1]


class WrongSizeModel(FakeModel):
    """A backend that returns the wrong number of dimensions."""

    def encode(self, texts):  # type: ignore[no-untyped-def]
        super().encode(texts)
        return [[0.1, 0.2] for _ in texts]


def chunks(count: int) -> list[Chunk]:
    return [
        Chunk(index=number, text=f"passage numero {number}", page_number=1)
        for number in range(count)
    ]


# --- Batching --------------------------------------------------------------


def test_chunks_are_encoded_in_batches_not_one_by_one() -> None:
    model = FakeModel()

    embed_chunks(chunks(36), model, batch_size=16)

    # 5x faster than one call per chunk, and the last batch is the remainder.
    assert model.batch_sizes == [16, 16, 4]


def test_an_empty_list_never_touches_the_model() -> None:
    model = FakeModel()

    assert embed_chunks([], model) == []
    assert model.batch_sizes == []


# --- Ordering: the silent killer -------------------------------------------


def test_every_vector_stays_paired_with_its_own_chunk() -> None:
    model = FakeModel()
    source = chunks(20)

    embedded = embed_chunks(source, model, batch_size=7)

    assert [item.chunk for item in embedded] == source
    for item in embedded:
        # FakeModel encodes the text length, so a misalignment is visible.
        assert item.vector[0] == float(len(item.chunk.text))


# --- Guards against a broken backend ---------------------------------------


def test_a_backend_that_drops_a_result_is_rejected() -> None:
    # Without this check the mistake is invisible: every chunk would silently
    # receive the vector of the next one, and the assistant would cite the
    # wrong passage with complete confidence.
    with pytest.raises(EmbeddingError):
        embed_chunks(chunks(5), TruncatingModel(), batch_size=5)


def test_a_backend_returning_the_wrong_dimensions_is_rejected() -> None:
    # Usually means the model was swapped without re-indexing (ADR-0006).
    with pytest.raises(EmbeddingError):
        embed_chunks(chunks(3), WrongSizeModel(), batch_size=3)


def test_a_batch_size_of_zero_is_refused() -> None:
    with pytest.raises(ValueError):
        embed_chunks(chunks(3), FakeModel(), batch_size=0)


# --- Queries ---------------------------------------------------------------


def test_a_question_is_embedded_by_the_same_model_in_a_single_call() -> None:
    model = FakeModel()

    vector = embed_query("  Quelle est la duree de garantie ?  ", model)

    assert model.batch_sizes == [1]
    assert len(vector) == model.dimensions


def test_an_empty_question_is_refused() -> None:
    with pytest.raises(ValueError):
        embed_query("   ", FakeModel())


# --- Normalisation ---------------------------------------------------------


def test_normalising_gives_a_vector_of_length_one() -> None:
    vector = normalise([3.0, 4.0])

    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
    assert math.isclose(vector[0], 0.6)


def test_a_long_text_does_not_outrank_a_short_one_after_normalising() -> None:
    # Same direction, different magnitude: after normalising they are equal,
    # so length can no longer be mistaken for relevance.
    assert normalise([1.0, 2.0]) == normalise([10.0, 20.0])


def test_a_zero_vector_does_not_divide_by_zero() -> None:
    assert normalise([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]
