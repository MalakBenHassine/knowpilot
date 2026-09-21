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

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    current_session,
    get_answer_chain,
    get_quota_tracker,
    get_vector_store,
    require_csrf,
)
from app.core.quota import QuotaTracker
from app.main import app
from app.rag.generation import REFUSAL
from app.rag.llm import LanguageModelUnavailableError, QuotaExhaustedError
from app.schemas.chat import SNIPPET_CHARACTERS
from tests.fakes import FakeVectorStore, SpyChatModel, answer_chain, passage, spy
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
async def client(
    model: SpyChatModel, store: FakeVectorStore, quota: QuotaTracker
) -> AsyncIterator[AsyncClient]:
    """The real application with its edges replaced, and nothing else."""
    app.dependency_overrides[current_session] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[require_csrf] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[get_vector_store] = lambda: store
    app.dependency_overrides[get_answer_chain] = lambda: answer_chain(model)
    app.dependency_overrides[get_quota_tracker] = lambda: quota

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


async def test_the_retrieval_policy_comes_from_the_settings(
    client: AsyncClient, store: FakeVectorStore
) -> None:
    await client.post("/api/chat", **ask())

    assert store.k == [8]  # KP_TOP_K default
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
    assert response.json() == {"answer": "", "citations": [], "is_grounded": False}
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
    """Our counter was wrong about the real budget; the user is not punished."""
    store.hits = [(passage(CONGES), 0.2)]
    app.dependency_overrides[get_answer_chain] = lambda: FailingChain(
        QuotaExhaustedError(retry_after=1800)
    )

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 429
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
