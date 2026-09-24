"""Measures retrieval, one layer at a time.

    uv run python -m perf.retrieval

"Is it performant?" is not a question anybody can answer. These are:

  - what does embedding ONE question cost? (the model, on this CPU)
  - what does the vector search cost, and how does it grow with the corpus?
  - what do the keyword search and the context window add on top?
  - how much of the total is ours, and how much is the provider's?

Each step is timed separately, because they have different shapes: the model
is a fixed cost per question, the search grows with the corpus, and the
generation is somebody else's server. Reporting one number for all three
would hide every one of them.

The model is loaded once. The first call after loading is discarded - it
warms caches and allocates buffers that the second call reuses, and shipping
that number would flatter or damn the system depending on which one was
picked.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.config import get_settings
from app.db.session import create_engine
from app.db.vector_store import create_vector_store, load_checked_embeddings
from app.rag.retrieval import RetrieverFactory
from perf.seed import PERF_OWNER

# Questions of the shape the product actually receives: a few content words,
# no full sentence. A one-word question would make the keyword search look
# faster than it is, and a paragraph would make the model look slower.
QUESTIONS = (
    "quel est le preavis de resiliation",
    "combien coute l abonnement mensuel",
    "quelle est la franchise en cas de sinistre",
    "combien de jours de conges apres trois ans",
    "que couvre la garantie sur les reparations",
)

WARMUP = 2
RUNS = 20


@dataclass(frozen=True)
class Measurement:
    name: str
    samples: list[float]

    def line(self) -> str:
        ordered = sorted(self.samples)
        p50 = statistics.median(ordered)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        return (
            f"  {self.name:<34} "
            f"p50 {p50 * 1000:7.1f} ms   "
            f"p95 {p95 * 1000:7.1f} ms   "
            f"min {ordered[0] * 1000:7.1f} ms"
        )


async def measure(name: str, call: Callable[[str], Awaitable[object]]) -> Measurement:
    for index in range(WARMUP):
        await call(QUESTIONS[index % len(QUESTIONS)])

    samples = []
    for index in range(RUNS):
        question = QUESTIONS[index % len(QUESTIONS)]
        started = time.perf_counter()
        await call(question)
        samples.append(time.perf_counter() - started)
    return Measurement(name, samples)


async def run(corpus: int) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    embeddings = load_checked_embeddings(settings.embedding_model, settings.embedding_cache_dir)
    store = await create_vector_store(engine, embeddings)

    async def embed(question: str) -> object:
        return await embeddings.aembed_query(question)

    # Three retrievers, built exactly as the API builds them, differing only
    # in which layers are switched on. Each line below is the cost of what the
    # line above does NOT do.
    vector_only = RetrieverFactory(
        store, k=settings.top_k, max_distance=settings.max_distance
    ).for_owner(PERF_OWNER)
    hybrid = RetrieverFactory(
        store,
        k=settings.top_k,
        max_distance=settings.max_distance,
        engine=engine,
        min_keyword_coverage=settings.min_keyword_coverage,
    ).for_owner(PERF_OWNER)
    full = RetrieverFactory(
        store,
        k=settings.top_k,
        max_distance=settings.max_distance,
        engine=engine,
        min_keyword_coverage=settings.min_keyword_coverage,
        context_window=True,
    ).for_owner(PERF_OWNER)

    print(f"corpus: {corpus} chunks, owner {PERF_OWNER}")
    print(f"top_k={settings.top_k}  max_distance={settings.max_distance}")
    print()

    results = [
        await measure("embed the question (model)", embed),
        await measure("+ vector search (HNSW)", vector_only.ainvoke),
        await measure("+ keyword search (GIN)", hybrid.ainvoke),
        await measure("+ context window", full.ainvoke),
    ]
    for result in results:
        print(result.line())

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=int,
        default=0,
        help="for the report only: how many chunks perf.seed put in the table",
    )
    asyncio.run(run(parser.parse_args().corpus))


if __name__ == "__main__":
    main()
