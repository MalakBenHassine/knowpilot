"""The concrete embedding backend: BGE-M3, running locally on CPU (ADR-0006).

This is the only module in the project that knows a machine-learning library
exists. Keeping it alone is what lets every other test run in milliseconds, and
what makes changing the model a change in a single file.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.rag.embeddings import normalise

if TYPE_CHECKING:
    # Imported for typing only. `from __future__ import annotations` turns the
    # annotations into strings, so nothing is imported when the module loads.
    from sentence_transformers import SentenceTransformer

    from app.rag.embeddings import EmbeddingModel

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-m3"


class LocalEmbeddingModel:
    """Wraps a sentence-transformers model behind our own small interface."""

    def __init__(self, model: SentenceTransformer, name: str, dimensions: int) -> None:
        self._model = model
        self._name = name
        self._dimensions = dimensions

    @property
    def name(self) -> str:
        """Stored beside the index: a different name invalidates every vector."""
        return self._name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @classmethod
    def load(
        cls, name: str = DEFAULT_MODEL_NAME, cache_dir: str | None = None
    ) -> LocalEmbeddingModel:
        """Load the weights once. Costs about 20 seconds and 2.2 GB of memory."""
        # Imported inside the function on purpose: importing sentence_transformers
        # pulls in torch, which costs seconds and hundreds of megabytes even when
        # no embedding is ever computed. The test suite must never pay for it.
        from sentence_transformers import SentenceTransformer

        started = time.monotonic()
        logger.info("loading embedding model %s", name)

        model = SentenceTransformer(name, device="cpu", cache_folder=cache_dir)

        # Ask the model for its own dimension rather than hard-coding 1024: a
        # constant would silently disagree the day the model changes, and the
        # disagreement would only surface as poor search results.
        dimensions = model.get_sentence_embedding_dimension()
        if dimensions is None:
            raise RuntimeError(f"{name} does not report an embedding dimension")

        logger.info(
            "loaded %s (%d dimensions) in %.1fs",
            name,
            dimensions,
            time.monotonic() - started,
        )
        return cls(model, name, int(dimensions))

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode one batch.

        Blocking and CPU-bound: calling this from an `async def` route would
        freeze the event loop, and with it every other request in flight.
        """
        vectors = self._model.encode(
            list(texts),
            # Batching is decided in embeddings.py. Letting the library add a
            # second layer with a different size would make the memory peak
            # impossible to reason about.
            batch_size=max(len(texts), 1),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=False,
        )
        # Normalised here rather than by the library flag, so the guarantee
        # lives in our code and holds for any backend. It costs well under one
        # percent of the model call.
        return [normalise([float(value) for value in vector]) for vector in vectors]


if TYPE_CHECKING:

    def _satisfies_the_protocol(model: LocalEmbeddingModel) -> EmbeddingModel:
        """A test that runs in zero time: mypy fails here if the class drifts."""
        return model
