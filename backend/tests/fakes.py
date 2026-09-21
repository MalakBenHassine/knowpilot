"""Test doubles built on LangChain's own fakes.

LangChain ships fake models precisely so chains can be tested without a
network: `FakeListChatModel` answers from a list, `DeterministicFakeEmbedding`
hashes text into a stable vector. Building on them rather than on hand-rolled
mocks means the REAL prompt template, the REAL LCEL composition and the REAL
output parser run in every test - only the provider is fake.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import BaseMessage
from langchain_core.vectorstores import VectorStore
from pydantic import Field

from app.db.models import EMBEDDING_DIMENSIONS
from app.rag.generation import ANSWER_PROMPT
from app.rag.llm import build_answer_chain


class SpyChatModel(FakeListChatModel):
    """A fake chat model that also records every conversation it was sent."""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def _call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> str:
        self.received.append(list(messages))
        return super()._call(messages, stop, run_manager, **kwargs)

    @property
    def calls(self) -> int:
        return len(self.received)

    def last(self) -> tuple[str, str]:
        """(system, human) text of the last conversation."""
        system, human = self.received[-1]
        return str(system.content), str(human.content)


def spy(*replies: str) -> SpyChatModel:
    return SpyChatModel(responses=list(replies) or ["Docker et Jenkins [1]."])


def answer_chain(model: SpyChatModel) -> Any:
    """The production chain, with only the provider replaced."""
    return build_answer_chain(ANSWER_PROMPT, model)


def fake_embeddings() -> Embeddings:
    return DeterministicFakeEmbedding(size=EMBEDDING_DIMENSIONS)


def passage(
    text: str = "Contenu du passage.",
    *,
    filename: str = "document.pdf",
    page_number: int = 1,
    document_id: uuid.UUID | None = None,
    distance: float = 0.3,
) -> Document:
    """A retrieved passage, shaped exactly like what PGVectorStore returns."""
    return Document(
        page_content=text,
        metadata={
            "document_id": document_id or uuid.uuid4(),
            "owner_id": "owner",
            "filename": filename,
            "chunk_index": 0,
            "page_number": page_number,
            "embedding_model": "fake",
            "distance": distance,
        },
    )


def passages(count: int = 2) -> list[Document]:
    return [
        passage(
            f"Contenu du passage numero {number}.",
            filename=f"document-{number}.pdf",
            page_number=number,
        )
        for number in range(1, count + 1)
    ]


class FakeSubjectCheck:
    """Stands in for SubjectCheck: the extraction and the SQL are tested elsewhere.

    `subjects` is what the question is about; `absent` is which of them no
    passage names. Records what it was asked, so a test can prove the presence
    check never runs when there is nothing to check.
    """

    def __init__(self, subjects: Sequence[str] = (), absent: Sequence[str] = ()) -> None:
        self.subjects = list(subjects)
        self.absent = list(absent)
        self.questions: list[str] = []
        self.checked: list[tuple[list[str], int]] = []

    async def subjects_of(self, question: str) -> list[str]:
        self.questions.append(question)
        return list(self.subjects)

    async def unnamed(self, subjects: Sequence[str], passages: Sequence[Document]) -> list[str]:
        self.checked.append((list(subjects), len(passages)))
        return [subject for subject in subjects if subject in self.absent]


class FakeVectorStore(VectorStore):
    """Returns preset (document, distance) pairs and records every filter.

    Recording the filter is the point: the tenant boundary is asserted on what
    actually reaches the store, not on what the code intended to send.
    """

    def __init__(self, hits: Sequence[tuple[Document, float]] = (), fail: bool = False) -> None:
        self.hits = list(hits)
        self.fail = fail
        self.filters: list[dict[str, Any] | None] = []
        self.queries: list[str] = []
        self.k: list[int] = []

    async def asimilarity_search_with_score(
        self, query: str, k: int = 4, filter: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[tuple[Document, float]]:
        if self.fail:
            raise RuntimeError("the database is down")
        self.queries.append(query)
        self.filters.append(filter)
        self.k.append(k)
        # Copies, so a retriever writing metadata cannot alter the next call.
        return [
            (Document(doc.page_content, metadata=dict(doc.metadata)), distance)
            for doc, distance in self.hits
        ]

    # The abstract surface of VectorStore, unused by the code under test.
    def add_texts(
        self, texts: Iterable[str], metadatas: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> list[str]:
        raise NotImplementedError

    def similarity_search(self, query: str, k: int = 4, **kwargs: Any) -> list[Document]:
        raise NotImplementedError

    @classmethod
    def from_texts(
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> FakeVectorStore:
        raise NotImplementedError
