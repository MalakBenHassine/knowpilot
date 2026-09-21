"""The embedding model: BGE-M3 running locally on CPU (ADR-0006), as LangChain
`Embeddings`.

Every other module depends on the abstract `langchain_core.embeddings.Embeddings`
and never on this file. That is what lets the test suite run in milliseconds
with LangChain's own `DeterministicFakeEmbedding`, and what makes changing the
model - or moving it behind an API - a change in a single function.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings
    from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-m3"

# Encoding one text at a time wastes most of the CPU: the model multiplies
# matrices, so a batch of 16 costs barely more than a batch of 1. Larger
# batches are faster still, until memory spikes and the process is killed -
# which on Kubernetes means a pod restarting in a loop, not a slow upload.
BATCH_SIZE = 16

# BGE-M3 needs no "query:" prefix, unlike the E5 family, so queries and
# documents are encoded the same way. Should the model ever change for one that
# does, `query_encode_kwargs` below is where the prefix belongs.


class EmbeddingError(Exception):
    """The model returned something we cannot trust."""


def load_embeddings(
    name: str = DEFAULT_MODEL_NAME, cache_dir: str | None = None
) -> HuggingFaceEmbeddings:
    """Load the weights once. Costs about 20 seconds and 2.2 GB of memory."""
    # Imported inside the function on purpose: it pulls in torch, which costs
    # seconds and hundreds of megabytes even when nothing is ever embedded.
    # The test suite must never pay for it.
    from langchain_huggingface import HuggingFaceEmbeddings

    started = time.monotonic()
    logger.info("loading embedding model %s", name)
    embeddings = HuggingFaceEmbeddings(
        model_name=name,
        cache_folder=cache_dir,
        model_kwargs={"device": "cpu"},
        encode_kwargs={
            # Unit length: cosine similarity becomes a plain dot product, and a
            # long passage no longer ranks higher merely because its vector is
            # longer. The HNSW index uses cosine ops (ADR-0006) to match.
            "normalize_embeddings": True,
            "batch_size": BATCH_SIZE,
        },
    )
    logger.info("loaded %s in %.1fs", name, time.monotonic() - started)
    return embeddings


def dimensions_of(embeddings: Embeddings) -> int:
    """Ask the model for its dimension by embedding a probe.

    Measured rather than read from a constant or a private attribute: the
    column is vector(1024), and a model of any other width would write rows
    PostgreSQL rejects - or, worse, vectors nobody can compare. The caller
    fails at startup, loudly, rather than halfway through someone's upload.
    """
    vector = embeddings.embed_query("dimension probe")
    if not vector:
        raise EmbeddingError("the model returned an empty vector")
    return len(vector)
