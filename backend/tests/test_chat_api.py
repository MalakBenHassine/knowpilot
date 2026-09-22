"""The chat endpoint.

What is tested is the boundary, and above all the two things that would be
invisible if they broke: whose passages reach the model, and who pays for a
question that was never answered.

The vector store is a fake that records the filter it receives, and the chat
model is LangChain's fake - behind the REAL prompt, the REAL LCEL chain and the
REAL error translation. The quota tracker is the real one over a fake Redis,
because the policy - who is charged, who is refunded - is what these tests are
about. No database, no key, no network: this file runs in CI.

The same isolation, proven against real PostgreSQL through the real
PGVectorStore, lives in test_vector_store.py.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    current_session,
    get_answer_chain,
    get_quota_tracker,
    get_retrievers,
    get_subject_check,
    require_csrf,
)
from app.core.quota import QuotaTracker
from app.main import app
from app.rag.generation import REFUSAL
from app.rag.llm import LanguageModelUnavailableError, QuotaExhaustedError
from app.rag.retrieval import RetrieverFactory
from app.schemas.chat import SNIPPET_CHARACTERS
from tests.fakes import (
    FakeSubjectCheck,
    FakeVectorStore,
    SpyChatModel,
    answer_chain,
    passage,
    spy,
)
from tests.test_documents_api import signed_in_as
from tests.test_quota import FakeRedis, service_key, user_key

ALICE = "alice-sub-0001"
BOB = "bob-sub-0002"
CONGES = "Les conges payes sont de 25 jours. " * 10

pytestmark = pytest.mark.anyio


class FailingChain:
    """Stands in for the guarded chain when the provider fails."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def ainvoke(self, inputs: dict[str, Any]) -> str:
        raise self.error

    async def astream(self, inputs: dict[str, Any]) -> AsyncIterator[str]:
        raise self.error
        yield ""  # unreachable: it only makes this an async generator


@pytest.fixture
def model() -> SpyChatModel:
    return spy("Les conges sont de 25 jours [1].")


@pytest.fixture
def store() -> FakeVectorStore:
    return FakeVectorStore()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def quota(redis: FakeRedis) -> QuotaTracker:
    # The real tracker, deliberately: a fake tracker would only test that
    # fakes agree with each other.
    return QuotaTracker(redis, per_user=3, per_service=5)  # type: ignore[arg-type]


@pytest.fixture
def check() -> FakeSubjectCheck:
    # No subject by default: most questions are about a topic, not a thing.
    return FakeSubjectCheck()


@pytest.fixture
async def client(
    model: SpyChatModel, store: FakeVectorStore, quota: QuotaTracker, check: FakeSubjectCheck
) -> AsyncIterator[AsyncClient]:
    """The real application with its edges replaced, and nothing else."""
    app.dependency_overrides[current_session] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[require_csrf] = lambda: signed_in_as(ALICE)
    # Vector-only (no engine): the keyword leg is tested against PostgreSQL in
    # test_vector_store.py. What matters here is the flow around retrieval.
    app.dependency_overrides[get_retrievers] = lambda: RetrieverFactory(
        store, k=8, max_distance=0.6
    )
    app.dependency_overrides[get_answer_chain] = lambda: answer_chain(model)
    app.dependency_overrides[get_quota_tracker] = lambda: quota
    app.dependency_overrides[get_subject_check] = lambda: check

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    app.dependency_overrides.clear()


def ask(question: str = "Combien de jours de conges ?") -> dict[str, Any]:
    return {"json": {"question": question}}


# --- Who is allowed in ---------------------------------------------------


async def test_an_anonymous_request_is_refused() -> None:
    """No overrides: the real authentication dependency answers."""
    app.state.session_store = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/chat", **ask())

    assert response.status_code == 401


# --- 🔒 Tenant isolation --------------------------------------------------


async def test_the_search_is_scoped_to_the_owner_of_the_session(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    """The filter that reaches the store, not the one the code meant to send."""
    await client.post("/api/chat", **ask())

    assert store.filters == [{"owner_id": {"$eq": ALICE}}]


async def test_an_owner_sent_in_the_body_is_ignored(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    """The field does not exist, so it cannot be honoured.

    pydantic drops it and the handler has no way to read it. The strongest
    defence is not validating a dangerous value - it is never accepting it.
    """
    await client.post("/api/chat", json={"question": "Et le salaire ?", "owner_id": BOB})

    assert store.filters == [{"owner_id": {"$eq": ALICE}}]


async def test_the_retrieval_policy_reaches_the_store(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    await client.post("/api/chat", **ask())

    assert store.k == [8]
    assert store.queries == ["Combien de jours de conges ?"]


# --- Nothing to answer from ----------------------------------------------


async def test_an_empty_library_answers_without_calling_the_model(
    client: AsyncClient, model: SpyChatModel, redis: FakeRedis
) -> None:
    """200, not an error: "I do not have that information" is an answer.

    The strongest guard of the feature runs here - the model is never called -
    and nothing is charged, because nothing was spent.
    """
    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json() == {
        "answer": "",
        "citations": [],
        "is_grounded": False,
        "not_in_documents": [],
    }
    assert model.calls == 0
    assert redis.values == {}


async def test_a_weak_match_is_not_an_answer(
    client: AsyncClient, store: FakeVectorStore, model: SpyChatModel, redis: FakeRedis
) -> None:
    # A library always has a least-bad match. 0.9 is further than the ceiling.
    store.hits = [(passage(CONGES), 0.9)]

    response = await client.post("/api/chat", **ask())

    assert response.json()["is_grounded"] is False
    assert model.calls == 0
    assert redis.values == {}


async def test_a_retrieval_failure_is_a_503_and_costs_nothing(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    store.fail = True

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 503
    assert redis.values == {}


# --- The happy path ------------------------------------------------------


async def test_an_answer_carries_the_source_the_model_never_saw(
    client: AsyncClient, store: FakeVectorStore, model: SpyChatModel
) -> None:
    found = passage(CONGES, filename="conges.pdf", page_number=7)
    store.hits = [(found, 0.2)]

    response = await client.post("/api/chat", **ask())
    body = response.json()

    assert response.status_code == 200
    assert body["is_grounded"] is True
    citation = body["citations"][0]
    assert citation["number"] == 1
    assert citation["filename"] == "conges.pdf"
    assert citation["page_number"] == 7
    assert citation["document_id"] == str(found.metadata["document_id"])
    # The snippet is what lets the user read the sentence the answer came from.
    assert citation["snippet"].startswith("Les conges payes")
    assert len(citation["snippet"]) <= SNIPPET_CHARACTERS + 3
    # The filename and the page were attached by us afterwards. The prompt
    # carried neither, which is why an invented citation was not possible.
    _, prompt = model.last()
    assert "conges.pdf" not in prompt
    assert "page" not in prompt.lower()


async def test_internal_metadata_never_leaves_the_server(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    # A LangChain document carries its whole row as metadata: owner, model
    # name, distance. The response schema lists what may leave, explicitly.
    store.hits = [(passage(CONGES), 0.2)]

    response = await client.post("/api/chat", **ask())

    assert set(response.json()["citations"][0]) == {
        "number",
        "document_id",
        "filename",
        "page_number",
        "snippet",
    }


async def test_a_question_is_charged_once(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    store.hits = [(passage(CONGES), 0.2)]

    await client.post("/api/chat", **ask())

    assert redis.values[user_key(ALICE)] == 1
    assert redis.values[service_key()] == 1


# --- The budget ----------------------------------------------------------


async def test_the_user_budget_answers_429_with_a_delay(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    for _ in range(3):  # per_user=3 in the fixture
        await client.post("/api/chat", **ask())

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 429
    # Without it, a client retries immediately and is refused again.
    assert int(response.headers["retry-after"]) > 0


async def test_a_provider_outage_gives_the_question_back(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    """An outage of ours must not cost the user part of their day."""
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: FailingChain(
        LanguageModelUnavailableError("groq is unreachable")
    )

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 503
    assert user_key(ALICE) not in redis.values
    assert service_key() not in redis.values


async def test_the_provider_own_limit_is_refunded_too(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    """Our counter was wrong about the real budget; the user is not punished.

    And not told they used their questions: found by a manual test, where a
    user who had asked five of twenty read "you have used your questions".
    The SERVICE is saturated - 503 with a delay, not 429.
    """
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: FailingChain(
        QuotaExhaustedError(retry_after=1800)
    )

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1800"
    assert user_key(ALICE) not in redis.values


async def test_a_refusal_is_not_refunded(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    """A refusal is the product working, not failing.

    The call happened and the tokens were spent. Refunding it would also invite
    an easy abuse: ask something unanswerable for free, forever.
    """
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: answer_chain(spy(REFUSAL))

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json()["is_grounded"] is False
    assert redis.values[user_key(ALICE)] == 1


# --- The question itself -------------------------------------------------


async def test_an_empty_question_is_refused_by_validation(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    response = await client.post("/api/chat", **ask("   \n  "))

    # 422 from the schema, before a single line of our own code runs.
    assert response.status_code == 422
    assert store.queries == []


async def test_an_oversized_question_is_refused_before_it_costs_anything(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    response = await client.post("/api/chat", **ask("a" * 5000))

    assert response.status_code == 422
    assert store.queries == []
    assert redis.values == {}


# --- POST /api/chat/stream ---------------------------------------------------


def events_of(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a Server-Sent Events body into (event, data) pairs."""
    parsed = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


async def test_a_streamed_answer_ends_with_the_authoritative_one(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    store.hits = [(passage(CONGES, filename="conges.pdf"), 0.2)]

    response = await client.post("/api/chat/stream", **ask())
    events = events_of(response.text)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # A buffering proxy would hold the whole answer and defeat the point.
    assert response.headers["x-accel-buffering"] == "no"
    names = [name for name, _ in events]
    assert names[0] == "stage"
    assert names[-1] == "done"
    assert "token" in names
    streamed = "".join(data["text"] for name, data in events if name == "token")
    done = events[-1][1]
    assert done["is_grounded"] is True
    assert done["answer"] == streamed
    assert done["citations"][0]["filename"] == "conges.pdf"


async def test_an_unsourced_answer_is_never_streamed(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: answer_chain(
        spy("Vous avez droit a 25 jours, faites-moi confiance.")
    )

    events = events_of((await client.post("/api/chat/stream", **ask())).text)

    assert [name for name, _ in events] == ["stage", "done"]
    assert events[-1][1]["is_grounded"] is False


async def test_nothing_found_is_a_single_done_event(
    client: AsyncClient, model: SpyChatModel, redis: FakeRedis
) -> None:
    events = events_of((await client.post("/api/chat/stream", **ask())).text)

    assert events == [
        (
            "done",
            {"answer": "", "citations": [], "is_grounded": False, "not_in_documents": []},
        )
    ]
    assert model.calls == 0
    assert redis.values == {}


async def test_a_spent_budget_fails_before_the_stream_opens(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    for _ in range(3):
        await client.post("/api/chat", **ask())

    response = await client.post("/api/chat/stream", **ask())

    # A real status code, because nothing had been sent yet.
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


async def test_a_failure_mid_stream_is_an_event_and_is_refunded(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: FailingChain(
        LanguageModelUnavailableError("groq is unreachable")
    )

    response = await client.post("/api/chat/stream", **ask())
    events = events_of(response.text)

    # The 200 was already sent: the failure travels as an event.
    assert response.status_code == 200
    assert events[-1] == ("error", {"kind": "server"})
    assert user_key(ALICE) not in redis.values


async def test_the_provider_limit_mid_stream_says_when_to_come_back(
    client: AsyncClient, store: FakeVectorStore, redis: FakeRedis
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: FailingChain(
        QuotaExhaustedError(retry_after=1800)
    )

    events = events_of((await client.post("/api/chat/stream", **ask())).text)

    assert events[-1] == ("error", {"kind": "busy", "retry_after": 1800})
    assert user_key(ALICE) not in redis.values


async def test_the_stream_is_not_open_to_anonymous_requests() -> None:
    app.state.session_store = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/chat/stream", **ask())

    assert response.status_code == 401


# --- 🔎 What no passage names (ADR-0018) ------------------------------------

MIRROR = "Quelle est la franchise pour un retroviseur casse ?"
GLASS = "Bris de glace : 0 euro si reparation, 90 euros si remplacement. " * 5


async def test_a_thing_no_passage_names_is_reported_with_the_answer(
    client: AsyncClient, store: FakeVectorStore, model: SpyChatModel, check: FakeSubjectCheck
) -> None:
    """The answer stands, and the user is told what it rests on.

    The notice is attached by code: whatever the model wrote, the response
    says that no passage names the mirror.
    """
    store.hits = [(passage(GLASS), 0.3)]
    check.subjects, check.absent = ["retroviseur"], ["retroviseur"]

    body = (await client.post("/api/chat", **ask(MIRROR))).json()

    assert body["is_grounded"] is True
    assert body["not_in_documents"] == ["retroviseur"]
    # And the model was told it, as a fact rather than a rule to apply.
    _, prompt = model.last()
    assert 'Not named in any passage: "retroviseur"' in prompt


async def test_a_thing_the_passages_name_raises_no_notice(
    client: AsyncClient, store: FakeVectorStore, model: SpyChatModel, check: FakeSubjectCheck
) -> None:
    store.hits = [(passage(CONGES), 0.2)]
    check.subjects = ["conges"]

    body = (await client.post("/api/chat", **ask())).json()

    assert body["not_in_documents"] == []
    _, prompt = model.last()
    assert "Not named in any passage" not in prompt


async def test_the_check_is_skipped_when_nothing_was_found(
    client: AsyncClient, check: FakeSubjectCheck, redis: FakeRedis
) -> None:
    """No passage, no model call - and no presence check either."""
    check.subjects = ["retroviseur"]

    body = (await client.post("/api/chat", **ask(MIRROR))).json()

    assert body["not_in_documents"] == []
    assert check.checked == []
    assert redis.values == {}


async def test_answering_works_without_the_check(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    """Disabled (no check model configured) is not an outage."""
    app.dependency_overrides[get_subject_check] = lambda: None
    store.hits = [(passage(CONGES), 0.2)]

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json()["is_grounded"] is True
    assert response.json()["not_in_documents"] == []


async def test_the_streamed_verdict_carries_the_notice_too(
    client: AsyncClient, store: FakeVectorStore, check: FakeSubjectCheck
) -> None:
    store.hits = [(passage(GLASS), 0.3)]
    check.subjects, check.absent = ["retroviseur"], ["retroviseur"]

    response = await client.post("/api/chat/stream", **ask(MIRROR))
    events = events_of(response.text)

    assert events[-1][0] == "done"
    assert events[-1][1]["not_in_documents"] == ["retroviseur"]


async def test_a_refusal_carries_no_notice(
    client: AsyncClient, store: FakeVectorStore, check: FakeSubjectCheck
) -> None:
    """Nothing was answered, so there is nothing to qualify."""
    app.dependency_overrides[get_answer_chain] = lambda: answer_chain(spy(REFUSAL))
    store.hits = [(passage(GLASS), 0.3)]
    check.subjects, check.absent = ["retroviseur"], ["retroviseur"]

    body = (await client.post("/api/chat", **ask(MIRROR))).json()

    assert body["is_grounded"] is False
    assert body["not_in_documents"] == []
