"""A LangChain retriever that cannot be built without an owner.

`vector_store.as_retriever()` would work, and would be the textbook answer.
It is not used, because its filter is an optional keyword: a retriever built
without one searches every tenant, returns the best passage in the whole
database, and the answer cites a real page of somebody else's contract. A
leak here would not look like a bug - it would look like a good answer.

`OwnerScopedRetriever` is a `BaseRetriever` like any other - it composes in
LCEL, it is traced by callbacks, `ainvoke` works - but `owner_id` is a
required field. Pydantic refuses to construct it without one, so the unsafe
retriever is not a mistake someone can make: it does not exist.

It also applies the distance ceiling, which `as_retriever` expresses as a
"relevance score" (1 - cosine distance). We keep the distance itself, the
number the whole evaluation harness was tuned in, and expose it on each
document for the debugging panel the guide asks for.
"""

from __future__ import annotations

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore
from pydantic import Field

from app.db.vector_store import owner_filter


class OwnerScopedRetriever(BaseRetriever):
    """Top-k passages of ONE owner, closer than a distance ceiling."""

    vector_store: VectorStore
    # min_length=1: an empty string is refused at construction, not at query.
    owner_id: str = Field(min_length=1)
    k: int = Field(ge=1, le=20)
    # Cosine distance: 0 is identical, 1 unrelated, 2 opposite. A library always
    # has a least-bad match; without a ceiling, a question about cooking would
    # be answered from a passage about Kubernetes, with a real citation.
    max_distance: float = Field(gt=0, le=2)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # The API is fully asynchronous. A synchronous path would block the
        # event loop for the length of an embedding and a query, so it is
        # refused rather than implemented and forgotten.
        raise NotImplementedError("use ainvoke: the retriever is async-only")

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        # The embedding of the question runs in a thread pool inside the store
        # (Embeddings.aembed_query), so the event loop stays free.
        found = await self.vector_store.asimilarity_search_with_score(
            query, k=self.k, filter=owner_filter(self.owner_id)
        )
        passages: list[Document] = []
        for document, distance in found:
            if distance > self.max_distance:
                # Ordered by distance: everything after this one is further.
                break
            document.metadata["distance"] = float(distance)
            passages.append(document)
        return passages
