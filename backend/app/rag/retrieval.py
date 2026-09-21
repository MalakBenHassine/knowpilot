"""Retrieval: LangChain retrievers that cannot be built without an owner.

Four `BaseRetriever`s, composed:

    SemanticRetriever       meaning      pgvector cosine distance, via PGVectorStore
    KeywordRetriever        exact words  PostgreSQL full-text search, via SQL
    HybridRetriever         both         reciprocal rank fusion of the two
    ContextWindowRetriever  context      each passage read with the chunk before it

Each is a standard LangChain retriever - `ainvoke` works, callbacks trace it,
it composes in LCEL - and each takes `owner_id` as a REQUIRED field. Pydantic
refuses to construct one without a tenant, so the unsafe retriever is not a
mistake someone can make: it does not exist. `vector_store.as_retriever()`
would have been the textbook answer, and its filter is optional.

Why not the ready-made hybrid options (ADR-0015, both measured):

- `PGVectorStore`'s hybrid search builds its keyword query with
  `plainto_tsquery`, which requires EVERY word. "Qui est le syndic et comment
  le joindre ?" demands a passage containing "comment", and none does: zero
  keyword hits on natural questions. It also overwrites the cosine distance
  with a fused score - which our distance guard reads - and writes the
  question into a configuration object that may be shared between requests.
- `EnsembleRetriever` fuses documents but keeps one copy of each, dropping the
  metadata of the other side. The admission rule below needs both signals.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import FULL_TEXT_CONFIG
from app.db.vector_store import owner_filter

# The constant of reciprocal rank fusion. 60 is the value of the original paper
# (Cormack et al., 2009) and the default almost everywhere: it flattens the
# difference between rank 1 and rank 2 enough that one leg cannot drown the
# other, which is the whole point of fusing ranks rather than scores. Scores
# are not comparable anyway - a cosine distance and a ts_rank live on
# different scales, and normalising them is guesswork.
RRF_K = 60


class _AsyncOnly(BaseRetriever):
    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # The API is fully asynchronous. A synchronous path would block the
        # event loop for an embedding and a query, so it is refused rather than
        # implemented and forgotten.
        raise NotImplementedError("use ainvoke: the retriever is async-only")


class SemanticRetriever(_AsyncOnly):
    """Top-k passages of ONE owner, closer in meaning than a distance ceiling."""

    vector_store: VectorStore
    # min_length=1: an empty string is refused at construction, not at query.
    owner_id: str = Field(min_length=1)
    k: int = Field(ge=1, le=20)
    # Cosine distance: 0 is identical, 1 unrelated, 2 opposite. A library always
    # has a least-bad match; without a ceiling, a question about cooking would
    # be answered from a passage about Kubernetes, with a real citation.
    max_distance: float = Field(gt=0, le=2)

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


# One statement, parameterised: the question travels as a bound value and is
# parsed by PostgreSQL, never interpolated into SQL.
#
# The question's lexemes are taken from PostgreSQL's own parser, then filtered:
# an alphabetic lexeme shorter than three letters is dropped, a number of any
# length is kept. The French stop-word list misses words such as "les", which
# the stemmer turns into "le" - found by a test, where "le la les ?" matched
# any passage containing "les". Digits are kept because "04 72 55" is exactly
# what this search exists for.
#
# The keyword query is an OR of those lexemes - not the AND of plainto_tsquery -
# each quoted by PostgreSQL itself (quote_literal). Candidates are then ranked
# by COVERAGE: the share of the question's lexemes present in the passage.
# Coverage, unlike ts_rank, is on a fixed 0-1 scale, so a threshold on it means
# the same thing for every question.
#
# S608 is silenced knowingly: the only value formatted into this string is
# FULL_TEXT_CONFIG, a module constant that is part of the schema. Everything a
# user controls - the question, the owner - is a bound parameter.
_KEYWORD_SEARCH = text(
    f"""
    WITH lexemes AS (
        SELECT array(
            SELECT DISTINCT lexeme
            FROM unnest(tsvector_to_array(to_tsvector('{FULL_TEXT_CONFIG}', :question))) AS lexeme
            WHERE lexeme ~ '[0-9]' OR length(lexeme) >= 3
        ) AS list
    ),
    question AS (
        SELECT
            list AS lexemes,
            array_to_string(
                array(SELECT quote_literal(lexeme) FROM unnest(list) AS lexeme), ' | '
            ) AS any_word
        FROM lexemes
    )
    SELECT
        chunk.id, chunk.text, chunk.document_id, chunk.owner_id, chunk.filename,
        chunk.chunk_index, chunk.page_number, chunk.embedding_model,
        cardinality(array(
            SELECT unnest(tsvector_to_array(chunk.text_tsv))
            INTERSECT
            SELECT unnest(question.lexemes)
        ))::float / cardinality(question.lexemes) AS coverage
    FROM document_chunks AS chunk, question
    WHERE chunk.owner_id = :owner_id
      AND cardinality(question.lexemes) > 0
      AND chunk.text_tsv @@ question.any_word::tsquery
    ORDER BY coverage DESC, ts_rank_cd(chunk.text_tsv, question.any_word::tsquery) DESC
    LIMIT :k
    """  # noqa: S608
)


class KeywordRetriever(_AsyncOnly):
    """Passages of ONE owner that share words with the question.

    What embeddings cannot do: a phone number, a contract reference or a
    surname carries almost no meaning, so its vector is close to every other
    number or name. Full-text search matches the token itself.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    engine: AsyncEngine
    owner_id: str = Field(min_length=1)
    k: int = Field(ge=1, le=20)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    _KEYWORD_SEARCH, {"question": query, "owner_id": self.owner_id, "k": self.k}
                )
            ).all()
        return [
            Document(
                page_content=row.text,
                id=str(row.id),
                # The same metadata shape as PGVectorStore returns, so fusion and
                # citation code never need to know which leg found a passage.
                metadata={
                    "document_id": row.document_id,
                    "owner_id": row.owner_id,
                    "filename": row.filename,
                    "chunk_index": row.chunk_index,
                    "page_number": row.page_number,
                    "embedding_model": row.embedding_model,
                    "keyword_coverage": float(row.coverage),
                },
            )
            for row in rows
        ]


class HybridRetriever(_AsyncOnly):
    """Semantic and keyword retrieval, fused by rank.

    The admission rule is the part that matters, because it decides what the
    model is allowed to read:

    - a passage close enough in MEANING is admitted, exactly as before - the
      distance ceiling is untouched;
    - a passage that only shares WORDS is admitted when it contains at least
      `min_keyword_coverage` of the question's words. "Cabinet Marchand" in a
      passage that names the Cabinet Marchand is evidence; a passage that
      shares the word "jour" with a question about holidays is not.

    Keyword search can therefore ADD a passage the embeddings missed, but only
    one that literally contains most of what was asked. It never lowers the
    bar for meaning, and the out-of-domain and tenant cases of the evaluation
    still find nothing.
    """

    semantic: SemanticRetriever
    keyword: KeywordRetriever
    k: int = Field(ge=1, le=20)
    min_keyword_coverage: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _one_owner(self) -> HybridRetriever:
        # Two legs built for two different owners would fuse two tenants'
        # passages into one prompt. Impossible by construction, not by care.
        if self.semantic.owner_id != self.keyword.owner_id:
            raise ValueError("both retrievers must serve the same owner")
        return self

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        # Concurrently: the embedding runs in a thread while the full-text
        # query runs in PostgreSQL. Sequential, the latencies would add up.
        semantic, keyword = await asyncio.gather(
            self.semantic.ainvoke(query, config={"callbacks": run_manager.get_child()}),
            self.keyword.ainvoke(query, config={"callbacks": run_manager.get_child()}),
        )
        lexical = [
            doc for doc in keyword if doc.metadata["keyword_coverage"] >= self.min_keyword_coverage
        ]
        return fuse(semantic, lexical, k=self.k)


def _key(document: Document) -> str:
    # The chunk id: both legs return the same row under the same identifier.
    return document.id or f"{document.metadata['document_id']}:{document.metadata['chunk_index']}"


def fuse(semantic: Sequence[Document], lexical: Sequence[Document], *, k: int) -> list[Document]:
    """Reciprocal rank fusion, keeping the metadata of both legs.

    Each list contributes 1 / (RRF_K + rank) to every passage it contains, so a
    passage found by both legs rises above one found by a single leg, and the
    order inside each leg still counts. `match` records which leg found it,
    for the debugging panel and the evaluation report.
    """
    scores: dict[str, float] = {}
    merged: dict[str, Document] = {}
    found_by: dict[str, set[str]] = {}

    for leg, documents in (("semantic", semantic), ("keyword", lexical)):
        for rank, document in enumerate(documents):
            key = _key(document)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            found_by.setdefault(key, set()).add(leg)
            if key in merged:
                # Same row from the other leg: add what that leg knows.
                known = merged[key].metadata
                known.update(
                    {name: value for name, value in document.metadata.items() if name not in known}
                )
            else:
                merged[key] = Document(
                    page_content=document.page_content,
                    id=document.id,
                    metadata=dict(document.metadata),
                )

    ranked = sorted(scores, key=lambda key: scores[key], reverse=True)[:k]
    for key in ranked:
        legs = found_by[key]
        merged[key].metadata["match"] = "both" if len(legs) == 2 else next(iter(legs))
    return [merged[key] for key in ranked]


# The chunk BEFORE each passage, same document, same page, same owner. The
# owner is filtered on again although the passages already belong to them:
# a query that reads chunks by identifier must not depend on its caller for
# tenancy. The page is part of the join so a citation never points at page 4
# for text that came from page 3.
_PREVIOUS_CHUNKS = text(
    """
    SELECT chunk.document_id, chunk.chunk_index, chunk.text
    FROM document_chunks AS chunk
    JOIN unnest(
        CAST(:documents AS uuid[]), CAST(:indexes AS integer[]), CAST(:pages AS integer[])
    ) AS wanted(document_id, chunk_index, page_number)
      ON chunk.document_id = wanted.document_id
     AND chunk.chunk_index = wanted.chunk_index
     AND chunk.page_number = wanted.page_number
    WHERE chunk.owner_id = :owner_id
    """
)

# Below this, a match between the end of one chunk and the start of the next
# is a coincidence - a shared "e" or "de " - not the splitter's overlap, and
# merging on it would delete real characters.
MIN_OVERLAP_CHARACTERS = 10


def join_overlapping(before: str, after: str) -> str:
    """Join two consecutive chunks without repeating their overlap.

    The splitter repeats up to CHUNK_OVERLAP characters of one chunk at the
    start of the next. Concatenated as they are, the model would read that
    sentence twice - and a sentence read twice looks like two statements.
    """
    for size in range(min(len(before), len(after)), MIN_OVERLAP_CHARACTERS - 1, -1):
        if before.endswith(after[:size]):
            return before + after[size:]
    return f"{before}\n{after}"


class ContextWindowRetriever(_AsyncOnly):
    """Each passage, read together with the chunk before it on the same page.

    The defect this fixes, found by a manual test: "how much do I pay each
    month?" was answered 59 euros, the amount of an amendment applicable from
    the first of NEXT month. The chunk sent to the model began with "the
    monthly premium is raised to 59 euros"; the date of application was two
    sentences earlier, in the previous chunk, which retrieval did not return.
    Contracts, leases and policies all write the condition first and the value
    after, so a chunk boundary between them is common, not bad luck.

    The standard remedy (LangChain's ParentDocumentRetriever, LlamaIndex's
    sentence window) is to SEARCH on small chunks and READ larger text around
    them: small chunks keep the vectors precise, the surrounding text keeps
    the meaning. Here the window is the previous chunk, fetched in one query
    for all passages. A passage whose previous chunk was retrieved anyway is
    left alone: the model already reads it, as its own passage.

    The price is input tokens - up to twice the passage text - measured by the
    evaluation harness before being accepted.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    retriever: BaseRetriever
    engine: AsyncEngine
    owner_id: str = Field(min_length=1)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        passages = await self.retriever.ainvoke(
            query, config={"callbacks": run_manager.get_child()}
        )
        return await self._widen(passages)

    async def _widen(self, passages: list[Document]) -> list[Document]:
        present = {_position(passage) for passage in passages}
        wanted = [
            passage
            for passage in passages
            if (position := _position(passage))[1] > 0
            and (position[0], position[1] - 1) not in present
        ]
        if not wanted:
            return passages

        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    _PREVIOUS_CHUNKS,
                    {
                        "documents": [uuid.UUID(_position(p)[0]) for p in wanted],
                        "indexes": [_position(p)[1] - 1 for p in wanted],
                        "pages": [int(p.metadata["page_number"]) for p in wanted],
                        "owner_id": self.owner_id,
                    },
                )
            ).all()
        previous = {(str(row.document_id), row.chunk_index): row.text for row in rows}

        widened = []
        for passage in passages:
            document_id, index = _position(passage)
            before = previous.get((document_id, index - 1))
            if before is None:
                widened.append(passage)
                continue
            widened.append(
                Document(
                    page_content=join_overlapping(before, passage.page_content),
                    id=passage.id,
                    # The metadata of the passage that was FOUND: its page is
                    # the one cited, and `context_from` says the text now also
                    # holds the chunk before it.
                    metadata={**passage.metadata, "context_from": index - 1},
                )
            )
        return widened


def _position(passage: Document) -> tuple[str, int]:
    return str(passage.metadata["document_id"]), int(passage.metadata["chunk_index"])


class RetrieverFactory:
    """Builds the retriever for one owner, per request.

    Built once at startup with the shared store, engine and policy; called per
    request with the owner from the session. The owner is part of the object,
    so there is no shared retriever that could serve the wrong tenant.

    Without an engine the retriever is vector-only and reads bare chunks:
    both other steps are SQL.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        k: int,
        max_distance: float,
        engine: AsyncEngine | None = None,
        min_keyword_coverage: float = 0.5,
        keyword_search: bool = True,
        context_window: bool = False,
    ) -> None:
        self._store = vector_store
        self._engine = engine
        self._k = k
        self._max_distance = max_distance
        self._min_keyword_coverage = min_keyword_coverage
        self._keyword_search = keyword_search
        self._context_window = context_window

    def for_owner(self, owner_id: str) -> BaseRetriever:
        semantic = SemanticRetriever(
            vector_store=self._store,
            owner_id=owner_id,
            k=self._k,
            max_distance=self._max_distance,
        )
        if self._engine is None:
            # Vector-only, exactly the behaviour before ADR-0015 - which is
            # how the gain of each later step was measured.
            return semantic
        retriever: BaseRetriever = semantic
        if self._keyword_search:
            retriever = HybridRetriever(
                semantic=semantic,
                keyword=KeywordRetriever(engine=self._engine, owner_id=owner_id, k=self._k),
                k=self._k,
                min_keyword_coverage=self._min_keyword_coverage,
            )
        if self._context_window:
            # Last: the window widens what was SELECTED, it never takes part
            # in selecting. Admission rules and ranking are unchanged.
            retriever = ContextWindowRetriever(
                retriever=retriever, engine=self._engine, owner_id=owner_id
            )
        return retriever
