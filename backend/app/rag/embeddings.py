"""Turn chunks into vectors.

Nothing here knows which model runs. ADR-0006 chose BGE-M3, but it chose it
*behind an interface*: this module encodes, batches and validates, and a
concrete backend is plugged in at startup. That is what lets the whole test
suite run in milliseconds without downloading two gigabytes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.rag.chunking import Chunk

# Encoding one text at a time wastes most of the CPU: the model multiplies
# matrices, so a batch of 16 costs barely more than a batch of 1. Larger
# batches are faster still, until memory spikes and the process is killed -
# which on Kubernetes means a pod restarting in a loop, not a slow upload.
DEFAULT_BATCH_SIZE = 16


class EmbeddingError(Exception):
    """The backend returned something we cannot trust."""


class EmbeddingModel(Protocol):
    """The minimum we need from any embedding backend.

    `name` and `dimensions` are not decoration: they are stored next to the
    index, because changing the model invalidates every vector ever written
    and we must be able to *detect* that rather than serve nonsense.
    """

    @property
    def name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk: Chunk
    vector: list[float]


def normalise(vector: Sequence[float]) -> list[float]:
    """Scale a vector to length 1.

    Cosine similarity then becomes a plain dot product, and - more importantly -
    a long passage no longer ranks higher merely because its vector is longer.
    Only direction carries meaning.
    """
    length = math.sqrt(sum(value * value for value in vector))
    if length == 0.0:
        # A zero vector has no direction. Dividing would raise; returning it
        # unchanged keeps the pipeline alive and the caller can spot it.
        return list(vector)
    return [value / length for value in vector]


def _encode_in_batches(
    texts: Sequence[str], model: EmbeddingModel, batch_size: int
) -> list[list[float]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        produced = model.encode(batch)

        # Anti-corruption boundary. A backend that silently drops or reorders
        # results would misalign every vector with its text, and retrieval
        # would return confident, perfectly cited nonsense.
        if len(produced) != len(batch):
            raise EmbeddingError(f"model returned {len(produced)} vectors for {len(batch)} texts")
        for vector in produced:
            if len(vector) != model.dimensions:
                raise EmbeddingError(
                    f"model returned {len(vector)} dimensions, expected {model.dimensions}"
                )
        vectors.extend(produced)

    return vectors


def embed_chunks(
    chunks: Sequence[Chunk],
    model: EmbeddingModel,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[EmbeddedChunk]:
    """Embed chunks in order, keeping each vector paired with its chunk."""
    if not chunks:
        # No chunks, no call: loading or waking a model for nothing is waste.
        return []

    vectors = _encode_in_batches([chunk.text for chunk in chunks], model, batch_size)
    return [
        EmbeddedChunk(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def embed_query(question: str, model: EmbeddingModel) -> list[float]:
    """Embed one question, with the very same model that indexed the chunks.

    BGE-M3 needs no `query:` prefix, unlike the E5 family. Should we ever swap
    the model for one that does, the prefix belongs in that backend - not here.
    """
    question = question.strip()
    if not question:
        raise ValueError("cannot embed an empty question")

    vectors = _encode_in_batches([question], model, batch_size=1)
    return vectors[0]
