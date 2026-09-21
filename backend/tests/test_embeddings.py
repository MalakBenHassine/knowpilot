"""The embedding boundary.

No model is loaded here: loading BGE-M3 costs 20 seconds and 2.2 GB. What is
tested is our code around LangChain's `Embeddings` - the startup check that
refuses a model whose width does not fit the column.
"""

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from app.db import vector_store
from app.db.models import EMBEDDING_DIMENSIONS
from app.rag.embeddings import EmbeddingError, dimensions_of


def test_the_dimension_is_measured_not_assumed() -> None:
    assert dimensions_of(DeterministicFakeEmbedding(size=384)) == 384


def test_an_empty_vector_is_refused() -> None:
    class Empty(DeterministicFakeEmbedding):
        def embed_query(self, text: str) -> list[float]:
            return []

    with pytest.raises(EmbeddingError):
        dimensions_of(Empty(size=1))


def test_a_model_of_the_wrong_width_stops_the_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    # The column is vector(1024). A 384-dimension model would write rows
    # PostgreSQL rejects - or, if the schema were ever loosened, vectors that
    # cannot be compared with anything already indexed.
    monkeypatch.setattr(
        vector_store, "load_embeddings", lambda name, cache: DeterministicFakeEmbedding(size=384)
    )

    with pytest.raises(RuntimeError, match="migration"):
        vector_store.load_checked_embeddings("small-model", None)


def test_a_model_of_the_right_width_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DeterministicFakeEmbedding(size=EMBEDDING_DIMENSIONS)
    monkeypatch.setattr(vector_store, "load_embeddings", lambda name, cache: fake)

    assert vector_store.load_checked_embeddings("bge-m3", None) is fake
