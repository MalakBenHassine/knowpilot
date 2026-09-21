"""The retrievers and their fusion, without a database.

The real-PostgreSQL version of these guarantees is in test_vector_store.py;
this file pins down the logic in milliseconds.
"""

import uuid
from typing import Any

import anyio
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForRetrieverRun
from langchain_core.documents import Document
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from app.rag.retrieval import (
    ContextWindowRetriever,
    HybridRetriever,
    KeywordRetriever,
    RetrieverFactory,
    SemanticRetriever,
    fuse,
    join_overlapping,
)
from tests.fakes import FakeVectorStore, passage


def semantic(store: FakeVectorStore, owner_id: str = "alice", **policy: Any) -> SemanticRetriever:
    return SemanticRetriever(
        vector_store=store,
        owner_id=owner_id,
        k=policy.get("k", 8),
        max_distance=policy.get("max_distance", 0.6),
    )


def chunk(name: str, **metadata: Any) -> Document:
    """A retrieved chunk whose identity is its name."""
    return Document(
        page_content=name,
        id=name,
        metadata={"document_id": uuid.uuid4(), "chunk_index": 0, **metadata},
    )


# --- SemanticRetriever -----------------------------------------------------


def test_the_owner_is_part_of_every_search() -> None:
    store = FakeVectorStore()

    anyio.run(semantic(store, "alice").ainvoke, "question")

    assert store.filters == [{"owner_id": {"$eq": "alice"}}]


@pytest.mark.parametrize("owner", ["", None])
def test_a_retriever_without_an_owner_does_not_exist(owner: str | None) -> None:
    with pytest.raises(ValidationError):
        semantic(FakeVectorStore(), owner)  # type: ignore[arg-type]


@pytest.mark.parametrize(("k", "max_distance"), [(0, 0.6), (21, 0.6), (8, 0.0), (8, 2.5)])
def test_an_absurd_policy_is_refused(k: int, max_distance: float) -> None:
    with pytest.raises(ValidationError):
        semantic(FakeVectorStore(), k=k, max_distance=max_distance)


def test_passages_beyond_the_ceiling_are_dropped() -> None:
    store = FakeVectorStore(
        [(passage("proche"), 0.2), (passage("limite"), 0.6), (passage("loin"), 0.61)]
    )

    found = anyio.run(semantic(store).ainvoke, "question")

    assert [doc.page_content for doc in found] == ["proche", "limite"]


def test_the_distance_travels_with_the_passage() -> None:
    store = FakeVectorStore([(passage("proche"), 0.25)])

    found = anyio.run(semantic(store).ainvoke, "question")

    # Kept for the evaluation report - never sent to the model, never returned
    # to the browser (see test_chat_api).
    assert found[0].metadata["distance"] == 0.25


def test_the_synchronous_path_is_refused() -> None:
    # A sync call would block the event loop for an embedding and a query.
    with pytest.raises(NotImplementedError):
        semantic(FakeVectorStore()).invoke("question")


# --- fuse: reciprocal rank fusion ------------------------------------------


def test_a_passage_found_by_both_legs_comes_first() -> None:
    fused = fuse(
        [chunk("a", distance=0.2), chunk("both", distance=0.3)],
        [chunk("both", keyword_coverage=1.0), chunk("b", keyword_coverage=0.6)],
        k=8,
    )

    # Agreement between meaning and exact words is the strongest signal.
    assert [doc.id for doc in fused][0] == "both"


def test_the_metadata_of_both_legs_is_kept() -> None:
    fused = fuse([chunk("x", distance=0.4)], [chunk("x", keyword_coverage=0.8)], k=8)

    # What EnsembleRetriever would have lost: one side's metadata.
    assert fused[0].metadata["distance"] == 0.4
    assert fused[0].metadata["keyword_coverage"] == 0.8
    assert fused[0].metadata["match"] == "both"


def test_each_passage_records_which_leg_found_it() -> None:
    fused = fuse([chunk("meaning")], [chunk("words")], k=8)

    assert {doc.id: doc.metadata["match"] for doc in fused} == {
        "meaning": "semantic",
        "words": "keyword",
    }


def test_the_fusion_stops_at_k() -> None:
    fused = fuse([chunk(f"s{i}") for i in range(6)], [chunk(f"w{i}") for i in range(6)], k=4)

    assert len(fused) == 4


def test_a_fused_passage_appears_once() -> None:
    fused = fuse([chunk("x")], [chunk("x")], k=8)

    assert len(fused) == 1


# --- HybridRetriever: the admission rule -----------------------------------


class Preset(KeywordRetriever):
    """A keyword leg that returns what the test needs, without a database."""

    engine: Any = None
    found: list[Document] = []

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        return [Document(d.page_content, id=d.id, metadata=dict(d.metadata)) for d in self.found]


def hybrid(
    store: FakeVectorStore, keyword_hits: list[Document], owner: str = "alice"
) -> HybridRetriever:
    return HybridRetriever(
        semantic=semantic(store, "alice"),
        keyword=Preset(owner_id=owner, k=8, found=keyword_hits),
        k=8,
        min_keyword_coverage=0.5,
    )


def test_keywords_can_add_a_passage_the_embeddings_missed() -> None:
    # The phone number case: no passage close enough in meaning, but one that
    # literally contains the number asked about.
    store = FakeVectorStore([(passage("loin"), 0.9)])
    number = chunk("Le syndic est joignable au 04 72 55 18 90.", keyword_coverage=1.0)

    found = anyio.run(hybrid(store, [number]).ainvoke, "04 72 55 18 90")

    assert [doc.page_content for doc in found] == [number.page_content]
    assert found[0].metadata["match"] == "keyword"


def test_a_passage_sharing_a_few_words_is_not_evidence() -> None:
    # Sharing "jour" with a question about holidays proves nothing.
    store = FakeVectorStore()
    noise = chunk("Le jour de la reunion.", keyword_coverage=0.2)

    found = anyio.run(hybrid(store, [noise]).ainvoke, "Combien de jours de conges ?")

    assert found == []


def test_keywords_never_lower_the_bar_for_meaning() -> None:
    # A passage too far in meaning stays out unless the keyword leg brings it.
    store = FakeVectorStore([(passage("sans rapport"), 0.9)])

    assert anyio.run(hybrid(store, []).ainvoke, "question") == []


def test_both_legs_must_serve_the_same_owner() -> None:
    # Two legs for two owners would fuse two tenants into one prompt.
    with pytest.raises(ValidationError, match="same owner"):
        hybrid(FakeVectorStore(), [], owner="bob")


# --- RetrieverFactory --------------------------------------------------------


def test_without_an_engine_the_retrieval_is_vector_only() -> None:
    factory = RetrieverFactory(FakeVectorStore(), k=8, max_distance=0.6)

    # How the gain of the keyword leg was measured: the same code, one leg off.
    assert isinstance(factory.for_owner("alice"), SemanticRetriever)


def test_every_retriever_is_built_for_one_owner() -> None:
    store = FakeVectorStore()
    factory = RetrieverFactory(store, k=5, max_distance=0.6)

    anyio.run(factory.for_owner("alice").ainvoke, "question")
    anyio.run(factory.for_owner("bob").ainvoke, "question")

    assert store.filters == [{"owner_id": {"$eq": "alice"}}, {"owner_id": {"$eq": "bob"}}]
    assert store.k == [5, 5]


# --- ContextWindowRetriever (ADR-0017) ---------------------------------------

# Never connected: creating an engine opens nothing. Any query through it would
# fail, which is what the tests below rely on to prove no query was made.
UNREACHABLE = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")


def test_the_overlap_of_two_chunks_is_read_once() -> None:
    before = "Avenant applicable au 1er octobre. La cotisation est portée à 59 euros."
    after = "La cotisation est portée à 59 euros. Les autres clauses demeurent."

    assert join_overlapping(before, after) == (
        "Avenant applicable au 1er octobre. La cotisation est portée à 59 euros. "
        "Les autres clauses demeurent."
    )


def test_chunks_without_overlap_are_joined_whole() -> None:
    assert join_overlapping("Premier.", "Second.") == "Premier.\nSecond."


def test_a_coincidental_match_of_a_few_characters_is_not_an_overlap() -> None:
    # "de " ends one chunk and starts the next: merging would delete text.
    assert join_overlapping("la franchise de ", "de 350 euros") == "la franchise de \nde 350 euros"


def test_the_first_chunk_of_a_page_needs_no_query() -> None:
    store = FakeVectorStore([(passage("Titre.", distance=0.2), 0.2)])
    window = ContextWindowRetriever(retriever=semantic(store), engine=UNREACHABLE, owner_id="alice")

    found = anyio.run(window.ainvoke, "question")

    assert [doc.page_content for doc in found] == ["Titre."]


def test_the_window_is_the_last_step_of_the_retrieval() -> None:
    factory = RetrieverFactory(
        FakeVectorStore(), k=8, max_distance=0.6, engine=UNREACHABLE, context_window=True
    )

    retriever = factory.for_owner("alice")

    assert isinstance(retriever, ContextWindowRetriever)
    assert isinstance(retriever.retriever, HybridRetriever)
    assert retriever.owner_id == "alice"


def test_the_window_can_be_turned_off() -> None:
    factory = RetrieverFactory(FakeVectorStore(), k=8, max_distance=0.6, engine=UNREACHABLE)

    assert isinstance(factory.for_owner("alice"), HybridRetriever)
