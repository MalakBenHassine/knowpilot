"""Measure the assistant against the real stack.

    uv run python -m evals.run

Real embeddings, real pgvector, real GroqCloud. Nothing is faked, because the
question being asked is not "does the code work" - two hundred tests answer
that with a fake model - but "does the system behave well", and no fake can
answer it.

This is an EVALUATION, not a test suite, and the difference matters. A test
asserts a behaviour the code must have, and a failure blocks a commit. An
evaluation measures a quality that can move without any code changing: a
provider ships a new model, a threshold is tuned, a document is worded
differently. A regression here is a conversation, not a build failure.

It costs about ten questions of a daily budget of eighty-five, and it cleans
up after itself.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.storage import FileStorage
from app.db import documents as repository
from app.db import vector_store
from app.db.ingestion import DatabaseIngestionStore
from app.db.session import create_engine, create_session_factory
from app.rag.embeddings import embed_query
from app.rag.generation import Answer, Passage, answer_question
from app.rag.groq import GroqLanguageModel
from app.rag.model import LocalEmbeddingModel
from app.rag.pipeline import ingest_document
from evals.dataset import ALL_OWNERS, CASES, FIXTURES, Case

GREEN, RED, YELLOW, GREY, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"


@dataclass
class Outcome:
    case: Case
    answer: Answer
    passages: int
    seconds: float
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def judge(case: Case, answer: Answer) -> list[str]:
    """Check an answer against what a correct system would have done.

    Deliberately lenient about wording and strict about substance. Asserting
    an exact sentence would measure the phrasing of one model version and
    break on the next, while teaching nothing: what matters is whether the
    answer is grounded, whether the facts are in it, and whether the planted
    ones are not.
    """
    failures: list[str] = []
    if answer.is_grounded != case.expect_grounded:
        expected = "an answer" if case.expect_grounded else "a refusal"
        failures.append(f"expected {expected}")

    text = answer.text.lower()
    for needle in case.must_contain:
        if needle.lower() not in text:
            failures.append(f"missing {needle!r}")
    for needle in case.must_not_contain:
        if needle.lower() in text:
            # The only class of failure that is a security incident rather
            # than a quality problem.
            failures.append(f"LEAKED {needle!r}")

    if case.must_cite and not any(c.filename == case.must_cite for c in answer.citations):
        failures.append(f"did not cite {case.must_cite}")
    if case.expect_grounded and not answer.citations:
        failures.append("grounded but uncited")
    return failures


async def ingest_fixtures(factory, model, storage: FileStorage) -> list[uuid.UUID]:  # type: ignore[no-untyped-def]
    """Put the fixtures through the real pipeline, in process.

    Not through the queue: the worker is another deployment concern, and an
    evaluation that needs a second process running is an evaluation people
    skip. The pipeline code is identical either way.
    """
    store = DatabaseIngestionStore(factory)
    created: list[uuid.UUID] = []

    for fixture in FIXTURES:
        document_id = uuid.uuid4()

        async def body(text: str = fixture.text):  # type: ignore[no-untyped-def]
            yield text.encode("utf-8")

        stored = await storage.save(body(), document_id)
        async with factory() as session:
            await repository.create_document(
                session,
                document_id=document_id,
                owner_id=fixture.owner_id,
                filename=fixture.filename,
                mime_type="text/plain",
                size_bytes=stored.size_bytes,
                content_hash=stored.content_hash,
                storage_path=stored.path,
            )
            await session.commit()

        await ingest_document(store, model, document_id=document_id, owner_id=fixture.owner_id)
        created.append(document_id)
        print(f"{GREY}  indexed {fixture.filename} for {fixture.owner_id}{RESET}")

    return created


async def run_case(case: Case, factory, model, llm, settings) -> Outcome:  # type: ignore[no-untyped-def]
    """The exact chain POST /api/chat runs, minus HTTP and the quota."""
    started = time.monotonic()
    vector = embed_query(case.question, model)

    async with factory() as session:
        found = await vector_store.search_chunks(
            session,
            owner_id=case.asked_by,
            query_vector=vector,
            limit=settings.top_k,
            max_distance=settings.max_distance,
        )

    passages = [
        Passage(text=chunk.text, filename=chunk.filename, page_number=chunk.page_number)
        for chunk in found
    ]
    answer = await answer_question(case.question, passages, llm)
    return Outcome(
        case=case,
        answer=answer,
        passages=len(passages),
        seconds=time.monotonic() - started,
        failures=judge(case, answer),
    )


async def cleanup(factory, storage: FileStorage) -> None:  # type: ignore[no-untyped-def]
    """Leave the database exactly as it was found.

    An evaluation that pollutes the data it measures stops being repeatable
    after the first run.
    """
    async with factory() as session:
        for owner_id in ALL_OWNERS:
            for document in await repository.list_documents(session, owner_id=owner_id):
                await vector_store.delete_document(
                    session, owner_id=owner_id, document_id=document.id
                )
                storage.delete(document.id)
        await session.commit()


def report(outcomes: list[Outcome]) -> int:
    print()
    print("=" * 78)
    for outcome in outcomes:
        mark = f"{GREEN}PASS{RESET}" if outcome.passed else f"{RED}FAIL{RESET}"
        print(
            f"{mark}  {outcome.case.name:<28} {outcome.seconds:5.2f}s  "
            f"{outcome.passages} passage(s)"
        )
        print(f"{GREY}      Q: {outcome.case.question}{RESET}")
        body = outcome.answer.text or "(refus)"
        print(f"{GREY}      A: {body[:150]}{RESET}")
        if outcome.answer.citations:
            sources = ", ".join(
                f"[{c.number}] {c.filename} p.{c.page_number}" for c in outcome.answer.citations
            )
            print(f"{GREY}      S: {sources}{RESET}")
        for failure in outcome.failures:
            colour = RED if failure.startswith("LEAKED") else YELLOW
            print(f"      {colour}-> {failure}{RESET}")
        print()

    passed = sum(1 for outcome in outcomes if outcome.passed)
    leaks = sum(1 for o in outcomes for f in o.failures if f.startswith("LEAKED"))
    print("=" * 78)
    print(f"{passed}/{len(outcomes)} passed", end="")
    if leaks:
        # Never reported as one failure among others: a leak is a different
        # kind of event from a mediocre answer.
        print(f"   {RED}{leaks} LEAK(S) - this is a security failure{RESET}", end="")
    print()
    return 0 if passed == len(outcomes) else 1


async def main() -> int:
    settings = get_settings()
    if not settings.generation_enabled:
        print(f"{RED}KP_GROQ_API_KEY is not set: nothing to evaluate.{RESET}")
        return 2

    print(
        f"model={settings.groq_model}  embeddings={settings.embedding_model}  "
        f"max_distance={settings.max_distance}  top_k={settings.top_k}"
    )
    print(f"{GREY}loading the embedding model...{RESET}")
    model = LocalEmbeddingModel.load(settings.embedding_model, settings.embedding_cache_dir)

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    storage = FileStorage(Path(settings.upload_root))

    async with httpx.AsyncClient() as client:
        llm = GroqLanguageModel(settings.groq_api_key, settings.groq_model, client)
        try:
            await cleanup(factory, storage)  # in case a previous run was interrupted
            await ingest_fixtures(factory, model, storage)
            outcomes = [await run_case(case, factory, model, llm, settings) for case in CASES]
            code = report(outcomes)
        finally:
            await cleanup(factory, storage)
            await engine.dispose()

    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
