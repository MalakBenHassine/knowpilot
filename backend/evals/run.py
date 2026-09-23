"""Measure the assistant against the real stack.

    uv run python -m evals.run                 every case
    uv run python -m evals.run NAME [NAME...]  only these, to check one fix cheaply

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
from langchain_core.callbacks import get_usage_metadata_callback

from app.core.config import get_settings
from app.core.storage import FileStorage
from app.db import documents as repository
from app.db import vector_store
from app.db.ingestion import DatabaseIngestionStore
from app.db.session import create_engine, create_session_factory
from app.rag.generation import ANSWER_PROMPT, Answer, answer_question
from app.rag.llm import (
    QuotaExhaustedError,
    build_advisory_chain,
    build_answer_chain,
    create_chat_model,
)
from app.rag.pipeline import ingest_document
from app.rag.retrieval import RetrieverFactory
from app.rag.subjects import SUBJECT_PROMPT, SubjectCheck, Subjects
from evals.dataset import ALL_OWNERS, CASES, EVAL_TODAY, FIXTURES, Case

GREEN, RED, YELLOW, GREY, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"


@dataclass
class Outcome:
    case: Case
    answer: Answer
    passages: int
    seconds: float
    failures: list[str]
    # Input tokens of the answering model: what a change of retrieval costs.
    input_tokens: int = 0

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
    if case.expect_grounded is not None and answer.is_grounded != case.expect_grounded:
        expected = "an answer" if case.expect_grounded else "a refusal"
        failures.append(f"expected {expected}")

    text = answer.text.lower()
    # An allowed refusal has no text to check; an answer always does.
    for needle in case.must_contain if answer.is_grounded else ():
        if needle.lower() not in text:
            failures.append(f"missing {needle!r}")
    for needle in case.must_not_contain:
        if needle.lower() in text:
            # The only class of failure that is a security incident rather
            # than a quality problem.
            failures.append(f"LEAKED {needle!r}")

    if (
        answer.is_grounded
        and case.must_cite
        and not any(c.filename == case.must_cite for c in answer.citations)
    ):
        failures.append(f"did not cite {case.must_cite}")
    if case.expect_grounded and not answer.citations:
        failures.append("grounded but uncited")

    # The notice of ADR-0018, in both directions: missing where the document
    # never names the subject, or shown where it does.
    if answer.is_grounded:
        shown = {subject.lower() for subject in answer.not_in_documents}
        wanted = {subject.lower() for subject in case.expect_not_in_documents}
        for subject in wanted - shown:
            failures.append(f"no notice for {subject!r}")
        for subject in shown - wanted:
            failures.append(f"false notice for {subject!r}")
    return failures


async def ingest_fixtures(  # type: ignore[no-untyped-def]
    factory, vectors, embeddings, settings, storage: FileStorage
) -> list[uuid.UUID]:
    """Put the fixtures through the real pipeline, in process.

    Not through the queue: the worker is another deployment concern, and an
    evaluation that needs a second process running is an evaluation people
    skip. The pipeline code is identical either way.
    """
    store = DatabaseIngestionStore(factory, vectors)
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

        await ingest_document(
            store,
            embeddings,
            embedding_model=settings.embedding_model,
            document_id=document_id,
            owner_id=fixture.owner_id,
        )
        created.append(document_id)
        print(f"{GREY}  indexed {fixture.filename} for {fixture.owner_id}{RESET}")

    return created


async def _patiently(ask, attempts: int = 4):  # type: ignore[no-untyped-def]
    """Wait out the provider's per-minute limit instead of dying on it.

    The free tier allows 8000 tokens a minute and a question costs about 2700
    with its output budget, so twenty questions in a row are bound to hit it.
    The API surfaces that to the user as a 429 with a delay; an evaluation can
    afford to wait the delay it was given.
    """
    for attempt in range(attempts):
        try:
            return await ask()
        except QuotaExhaustedError as exc:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(exc.retry_after + 1)
    raise AssertionError("unreachable")


async def run_case(  # type: ignore[no-untyped-def]
    case: Case, retrievers: RetrieverFactory, chain, check: SubjectCheck | None, model: str
) -> Outcome:
    """The exact steps POST /api/chat runs, minus HTTP and quota."""
    started = time.monotonic()
    # LangChain's usage callback: every chat model call inside the block
    # reports its tokens here, per model - so the answering model's cost is
    # read apart from the check model's.
    with get_usage_metadata_callback() as usage:
        passages, subjects = await asyncio.gather(
            retrievers.for_owner(case.asked_by).ainvoke(case.question),
            check.subjects_of(case.question) if check else asyncio.sleep(0, result=[]),
        )
        unnamed = await check.unnamed(subjects, passages) if check and passages else []
        answer = await _patiently(
            lambda: answer_question(
                case.question, passages, chain, today=EVAL_TODAY, unnamed=unnamed
            )
        )
    return Outcome(
        case=case,
        answer=answer,
        passages=len(passages),
        seconds=time.monotonic() - started,
        failures=judge(case, answer),
        input_tokens=(
            usage.usage_metadata[model]["input_tokens"] if model in usage.usage_metadata else 0
        ),
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
            f"{mark}  {outcome.case.name:<36} {outcome.seconds:5.2f}s  "
            f"{outcome.passages} passage(s)  {outcome.input_tokens:>5} tokens in"
        )
        print(f"{GREY}      Q: {outcome.case.question}{RESET}")
        body = outcome.answer.text or "(refus)"
        print(f"{GREY}      A: {body[:150]}{RESET}")
        if outcome.answer.citations:
            sources = ", ".join(
                f"[{c.number}] {c.filename} p.{c.page_number}" for c in outcome.answer.citations
            )
            print(f"{GREY}      S: {sources}{RESET}")
        if outcome.answer.not_in_documents:
            print(f"{GREY}      N: not in documents: {outcome.answer.not_in_documents}{RESET}")
        for failure in outcome.failures:
            colour = RED if failure.startswith("LEAKED") else YELLOW
            print(f"      {colour}-> {failure}{RESET}")
        print()

    passed = sum(1 for outcome in outcomes if outcome.passed)
    leaks = sum(1 for o in outcomes for f in o.failures if f.startswith("LEAKED"))
    answered = [o.input_tokens for o in outcomes if o.input_tokens]
    print("=" * 78)
    if answered:
        print(
            f"input tokens per answered question: mean {sum(answered) // len(answered)}, "
            f"max {max(answered)}"
        )
    print(f"{passed}/{len(outcomes)} passed", end="")
    if leaks:
        # Never reported as one failure among others: a leak is a different
        # kind of event from a mediocre answer.
        print(f"   {RED}{leaks} LEAK(S) - this is a security failure{RESET}", end="")
    print()
    return 0 if passed == len(outcomes) else 1


async def main(names: list[str]) -> int:
    settings = get_settings()
    if not settings.generation_enabled:
        print(f"{RED}KP_GROQ_API_KEY is not set: nothing to evaluate.{RESET}")
        return 2

    check_model = settings.groq_check_model if settings.subject_check_enabled else "off"
    print(
        f"model={settings.groq_model}  embeddings={settings.embedding_model}  "
        f"max_distance={settings.max_distance}  top_k={settings.top_k}  "
        f"keywords={settings.keyword_search_enabled}  coverage={settings.min_keyword_coverage}  "
        f"context={settings.passage_context_enabled}  check={check_model}  "
        f"today={EVAL_TODAY}"
    )
    print(f"{GREY}loading the embedding model...{RESET}")
    embeddings = vector_store.load_checked_embeddings(
        settings.embedding_model, settings.embedding_cache_dir
    )

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    storage = FileStorage(Path(settings.upload_root))
    vectors = await vector_store.create_vector_store(engine, embeddings)

    async with httpx.AsyncClient() as client:
        chain = build_answer_chain(
            ANSWER_PROMPT, create_chat_model(settings.groq_api_key, settings.groq_model, client)
        )
        check = (
            SubjectCheck(
                build_advisory_chain(
                    SUBJECT_PROMPT,
                    create_chat_model(
                        settings.groq_api_key, settings.groq_check_model, client, max_tokens=300
                    ),
                    Subjects,
                ),
                engine,
            )
            if settings.subject_check_enabled
            else None
        )
        try:
            await cleanup(factory, storage)  # in case a previous run was interrupted
            await ingest_fixtures(factory, vectors, embeddings, settings, storage)
            retrievers = RetrieverFactory(
                vectors,
                k=settings.top_k,
                max_distance=settings.max_distance,
                engine=engine,
                keyword_search=settings.keyword_search_enabled,
                min_keyword_coverage=settings.min_keyword_coverage,
                context_window=settings.passage_context_enabled,
            )
            outcomes = [
                await run_case(case, retrievers, chain, check, settings.groq_model)
                for case in CASES
                if not names or case.name in names
            ]
            code = report(outcomes)
        finally:
            await cleanup(factory, storage)
            await engine.dispose()

    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
