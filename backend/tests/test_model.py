"""The real BGE-M3 backend.

Downloading 2.2 GB and running a transformer is not something the normal suite
should do, so those tests are opt-in:

    KP_RUN_MODEL_TESTS=1 uv run pytest tests/test_model.py -v

What they prove cannot be proven with a fake: that French is understood, and
that similarity really does rank the relevant passage first.
"""

import math
import os
from pathlib import Path

import pytest

QUESTION = "Combien de jours de conges payes ai-je droit ?"
RELEVANT = "Le salarie beneficie de 25 jours de conges payes par annee complete."
UNRELATED = "Le remboursement du materiel est possible sous 30 jours."

model_tests = pytest.mark.skipif(
    os.getenv("KP_RUN_MODEL_TESTS") != "1",
    reason="set KP_RUN_MODEL_TESTS=1 to download and exercise BGE-M3",
)


def test_the_lock_file_keeps_the_cpu_build_of_torch() -> None:
    """A regression guard bought with 4.4 GB of experience.

    `tool.uv.sources` only governs direct dependencies. The day someone drops
    the explicit `torch` entry, the CUDA build returns through
    sentence-transformers: gigabytes of GPU drivers in an image that runs on
    CPU, and a far larger surface for Trivy to scan. Nothing would fail, which
    is precisely why this is a test.
    """
    lock = (Path(__file__).resolve().parents[1] / "uv.lock").read_text()

    assert "nvidia-" not in lock
    assert 'name = "triton"' not in lock


@model_tests
def test_the_model_reports_its_own_identity() -> None:
    from app.rag.model import LocalEmbeddingModel

    model = LocalEmbeddingModel.load()

    # Both are written next to the index: changing either means re-indexing.
    assert model.name == "BAAI/bge-m3"
    assert model.dimensions == 1024


@model_tests
def test_vectors_come_back_normalised() -> None:
    from app.rag.model import LocalEmbeddingModel

    vector = LocalEmbeddingModel.load().encode(["une phrase quelconque"])[0]

    # Length 1 means cosine similarity is a plain dot product, and that a long
    # passage cannot outrank a short one merely by being long.
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-5)


@model_tests
def test_the_relevant_french_passage_is_closer_than_the_unrelated_one() -> None:
    from app.rag.model import LocalEmbeddingModel

    model = LocalEmbeddingModel.load()
    question, relevant, unrelated = model.encode([QUESTION, RELEVANT, UNRELATED])

    def similarity(left: list[float], right: list[float]) -> float:
        return sum(x * y for x, y in zip(left, right, strict=True))

    # The whole point of the project in one assertion: the question asks
    # "combien de jours" and the answer says "25 jours" - different words, same
    # meaning. Keyword search cannot do this; that is why embeddings exist.
    assert similarity(question, relevant) > similarity(question, unrelated)
