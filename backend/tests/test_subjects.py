"""The subject check of ADR-0018: what a question is about, and whether it is named.

Three layers, tested where each one lives:

- `kept_subjects`: the model's output is untrusted input, bounded in code;
- the advisory chain: a REAL ChatGroq with structured output, against a fake
  server - including every way it must fail open;
- the presence check: real SQL on real PostgreSQL (opt-in, like every
  database test), because stemming and accent folding are PostgreSQL's.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.db.session import create_engine
from app.rag.llm import build_advisory_chain, create_chat_model
from app.rag.subjects import (
    MAX_SUBJECT_CHARACTERS,
    MAX_SUBJECTS,
    SUBJECT_PROMPT,
    SubjectCheck,
    Subjects,
    kept_subjects,
)
from tests.conftest import requires_database
from tests.fakes import passage
from tests.test_llm import KEY, Recorder, completion

pytestmark = pytest.mark.anyio

MIRROR = "Quelle est la franchise pour un rétroviseur cassé ?"


# --- The model's output, bounded -------------------------------------------


def test_a_subject_copied_from_the_question_is_kept() -> None:
    assert kept_subjects(MIRROR, ["rétroviseur"]) == ["rétroviseur"]


def test_a_subject_the_user_never_wrote_is_dropped() -> None:
    """The model was asked to COPY. A word it invented would become a false
    notice shown to the user - "your documents never mention X" about an X
    nobody asked about."""
    assert kept_subjects(MIRROR, ["pare-brise"]) == []


def test_accents_and_case_do_not_hide_a_copied_subject() -> None:
    # The model may restore an accent the user did not type, or the reverse.
    assert kept_subjects("franchise du retroviseur", ["Rétroviseur"]) == ["Rétroviseur"]


def test_duplicates_and_blanks_are_removed() -> None:
    assert kept_subjects(MIRROR, ["rétroviseur", "Retroviseur", "  ", ""]) == ["rétroviseur"]


def test_the_number_and_length_of_subjects_are_bounded() -> None:
    question = " ".join(f"objet{n}" for n in range(10)) + " " + "x" * 100
    proposed = [f"objet{n}" for n in range(10)] + ["x" * (MAX_SUBJECT_CHARACTERS + 1)]

    kept = kept_subjects(question, proposed)

    assert kept == [f"objet{n}" for n in range(MAX_SUBJECTS)]


async def test_no_answer_from_the_model_means_no_subject() -> None:
    """The advisory chain returns None when it failed open."""
    check = SubjectCheck(RunnableLambda(lambda _: None), engine=None)  # type: ignore[arg-type]

    assert await check.subjects_of(MIRROR) == []


async def test_nothing_is_queried_without_subjects_or_passages() -> None:
    # engine=None: any query would raise, so passing proves none was made.
    check = SubjectCheck(RunnableLambda(lambda _: None), engine=None)  # type: ignore[arg-type]

    assert await check.unnamed([], [passage("texte")]) == []
    assert await check.unnamed(["rétroviseur"], []) == []


# --- The chain: real ChatGroq, fake server ---------------------------------


def run_chain(server: Recorder) -> Subjects | None:
    async def call() -> Subjects | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            chain = build_advisory_chain(
                SUBJECT_PROMPT,
                create_chat_model(KEY, "openai/gpt-oss-20b", client, max_tokens=300),
                Subjects,
            )
            return await chain.ainvoke({"question": MIRROR})

    return anyio.run(call)


def test_the_reply_is_parsed_into_the_schema() -> None:
    server = Recorder(completion(json.dumps({"things": ["rétroviseur"]})))

    assert run_chain(server) == Subjects(things=["rétroviseur"])


def test_the_schema_and_the_budget_are_sent_to_the_provider() -> None:
    server = Recorder(completion(json.dumps({"things": []})))

    run_chain(server)

    body = json.loads(server.requests[0].content)
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["max_tokens"] == 300
    # Structured output: the provider is held to the schema, and the reply is
    # validated against it - nothing is parsed by hand.
    assert body["response_format"]["type"] == "json_schema"
    assert "things" in json.dumps(body["response_format"])


def test_a_provider_outage_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """The check improves an answer; it must never be able to stop one."""
    server = Recorder(httpx.Response(503, json={"error": {"message": "the question"}}))

    with caplog.at_level(logging.WARNING, logger="app.rag.llm"):
        assert run_chain(server) is None

    # The type of the failure is logged, never the message: it carries the
    # request, and the request carries the question.
    assert "InternalServerError" in caplog.text or "APIStatusError" in caplog.text
    assert "the question" not in caplog.text


def test_a_reply_outside_the_schema_fails_open() -> None:
    server = Recorder(completion(json.dumps({"subjects": "rétroviseur"})))

    assert run_chain(server) is None


def test_a_reply_that_is_not_json_fails_open() -> None:
    server = Recorder(completion("rétroviseur"))

    assert run_chain(server) is None


# --- The presence check: real PostgreSQL -----------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings().database_url)
    yield engine
    await engine.dispose()


GLASS_TABLE = (
    "Garantie Franchise. Bris de glace : 0 € si réparation / 90 € si remplacement. "
    "Vol et tentative de vol : 350 €. Une garantie couvre le câble de recharge."
)


async def unnamed(engine: AsyncEngine, *subjects: str) -> list[str]:
    check = SubjectCheck(RunnableLambda(lambda _: None), engine)
    return await check.unnamed(list(subjects), [passage(GLASS_TABLE)])


@requires_database
async def test_a_thing_the_passages_never_name_is_reported(engine: AsyncEngine) -> None:
    """The defect of ADR-0018: glass breakage covers the windscreen and maybe
    the mirror - but no passage NAMES the mirror, and that fact is checked."""
    assert await unnamed(engine, "rétroviseur") == ["rétroviseur"]


@requires_database
async def test_a_named_thing_is_not_reported_whatever_its_form(engine: AsyncEngine) -> None:
    # Plural, missing accents, other case: the same stemming and folding as
    # the keyword search, so what matches there matches here.
    assert await unnamed(engine, "cables de recharge", "VOL", "Bris de glace") == []


@requires_database
async def test_every_word_of_a_subject_must_be_named(engine: AsyncEngine) -> None:
    """ "Câble" alone is named; "câble HDMI" is not - the passage says nothing
    about HDMI, and a notice is exactly what the user needs to read."""
    assert await unnamed(engine, "câble HDMI") == ["câble HDMI"]


@requires_database
async def test_a_subject_of_stop_words_is_never_reported(engine: AsyncEngine) -> None:
    # It names nothing that could be checked: reporting it would be noise.
    assert await unnamed(engine, "le la les") == []


@requires_database
async def test_the_order_of_the_question_is_kept(engine: AsyncEngine) -> None:
    assert await unnamed(engine, "vélo", "vol", "trottinette") == ["vélo", "trottinette"]


@requires_database
async def test_sql_in_a_subject_is_just_words(engine: AsyncEngine) -> None:
    hostile = f"x'); DROP TABLE document_chunks; -- {uuid.uuid4().hex[:6]}"

    assert await unnamed(engine, hostile) == [hostile]
