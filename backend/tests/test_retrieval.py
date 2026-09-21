"""OwnerScopedRetriever, without a database.

The real-PostgreSQL version of these guarantees is in test_vector_store.py;
this file pins down the retriever's own logic in milliseconds.
"""

import anyio
import pytest
from pydantic import ValidationError

from app.rag.retrieval import OwnerScopedRetriever
from tests.fakes import FakeVectorStore, passage


def retriever(
    store: FakeVectorStore, owner_id: str = "alice", **policy: float
) -> OwnerScopedRetriever:
    return OwnerScopedRetriever(
        vector_store=store,
        owner_id=owner_id,
        k=int(policy.get("k", 8)),
        max_distance=policy.get("max_distance", 0.6),
    )


def test_the_owner_is_part_of_every_search() -> None:
    store = FakeVectorStore()

    anyio.run(retriever(store, "alice").ainvoke, "question")

    assert store.filters == [{"owner_id": {"$eq": "alice"}}]


@pytest.mark.parametrize("owner", ["", None])
def test_a_retriever_without_an_owner_does_not_exist(owner: str | None) -> None:
    with pytest.raises(ValidationError):
        OwnerScopedRetriever(
            vector_store=FakeVectorStore(),
            owner_id=owner,  # type: ignore[arg-type]
            k=8,
            max_distance=0.6,
        )


@pytest.mark.parametrize(("k", "max_distance"), [(0, 0.6), (21, 0.6), (8, 0.0), (8, 2.5)])
def test_an_absurd_policy_is_refused(k: int, max_distance: float) -> None:
    with pytest.raises(ValidationError):
        OwnerScopedRetriever(
            vector_store=FakeVectorStore(), owner_id="alice", k=k, max_distance=max_distance
        )


def test_passages_beyond_the_ceiling_are_dropped() -> None:
    store = FakeVectorStore(
        [(passage("proche"), 0.2), (passage("limite"), 0.6), (passage("loin"), 0.61)]
    )

    found = anyio.run(retriever(store).ainvoke, "question")

    assert [doc.page_content for doc in found] == ["proche", "limite"]


def test_the_distance_travels_with_the_passage() -> None:
    store = FakeVectorStore([(passage("proche"), 0.25)])

    found = anyio.run(retriever(store).ainvoke, "question")

    # Kept for the debugging panel and the evaluation report - never sent to
    # the model, never returned to the browser (see test_chat_api).
    assert found[0].metadata["distance"] == 0.25


def test_the_synchronous_path_is_refused() -> None:
    # A sync call would block the event loop for an embedding and a query.
    with pytest.raises(NotImplementedError):
        retriever(FakeVectorStore()).invoke("question")
