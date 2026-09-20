"""The chat endpoint.

What is tested is the boundary, and above all the two things that would be
invisible if they broke: whose passages reach the model, and who pays for a
question that was never answered.

The language model is a fake, so no key, no network and no quota are needed.
The quota tracker is the real one over a fake Redis, because the policy - who
is charged, who is refunded - is exactly what these tests are about.

Needs PostgreSQL:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_chat_api.py -v
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_session,
    db_session,
    get_embedding_model,
    get_language_model,
    get_quota_tracker,
    require_csrf,
)
from app.core.quota import QuotaTracker
from app.db.documents import create_document
from app.db.models import EMBEDDING_DIMENSIONS
from app.db.vector_store import replace_document_chunks
from app.main import app
from app.rag.chunking import Chunk
from app.rag.embeddings import EmbeddedChunk
from app.rag.generation import REFUSAL
from app.rag.groq import LanguageModelUnavailableError, QuotaExhaustedError
from tests.conftest import requires_database
from tests.test_documents_api import signed_in_as
from tests.test_pipeline import FakeModel
from tests.test_quota import FakeRedis, service_key, user_key

ALICE = "alice-sub-0001"
BOB = "bob-sub-0002"

pytestmark = [pytest.mark.anyio, requires_database]


class FakeLlm:
    """Replies with whatever the test needs, and records every call."""

    def __init__(self, reply: str = "Les conges sont de 25 jours [1].") -> None:
        self.reply = reply
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def quota(redis: FakeRedis) -> QuotaTracker:
    # The real tracker, deliberately: the refund policy is the subject of half
    # this file, and a fake tracker would only test that fakes agree.
    return QuotaTracker(redis, per_user=3, per_service=5)  # type: ignore[arg-type]


@pytest.fixture
async def client(
    session: AsyncSession, llm: FakeLlm, quota: QuotaTracker
) -> AsyncIterator[AsyncClient]:
    """The real application with its edges replaced, and nothing else.

    Retrieval, ordering, the owner filter, error translation and the quota
    policy are all the shipped code. Only what needs a key, a GPU or Keycloak
    is stubbed.
    """
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[current_session] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[require_csrf] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[get_embedding_model] = FakeModel
    app.dependency_overrides[get_language_model] = lambda: llm
    app.dependency_overrides[get_quota_tracker] = lambda: quota

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    app.dependency_overrides.clear()


async def add_passage(session: AsyncSession, owner_id: str, filename: str, text: str) -> uuid.UUID:
    """One indexed document with one chunk, for the given owner."""
    document_id = uuid.uuid4()
    await create_document(
        session,
        document_id=document_id,
        owner_id=owner_id,
        filename=filename,
        mime_type="application/pdf",
        size_bytes=1024,
        content_hash=uuid.uuid4().hex,
        storage_path=f"/data/{document_id}",
    )
    await replace_document_chunks(
        session,
        owner_id=owner_id,
        document_id=document_id,
        embedded=[
            EmbeddedChunk(
                chunk=Chunk(index=0, text=text, page_number=7),
                # The same vector the fake encoder returns for the question, so
                # the distance is zero and retrieval is not what is under test.
                vector=[0.1] * EMBEDDING_DIMENSIONS,
            )
        ],
        embedding_model="fake-model",
    )
    return document_id


def ask(question: str = "Combien de jours de conges ?") -> dict[str, Any]:
    return {"json": {"question": question}}


# --- Who is allowed in ---------------------------------------------------


async def test_an_anonymous_request_is_refused(session: AsyncSession) -> None:
    """No overrides: the real authentication dependency answers."""
    app.dependency_overrides[db_session] = lambda: session
    transport = ASGITransport(app=app)
    app.state.session_store = None
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/chat", **ask())
    app.dependency_overrides.clear()

    assert response.status_code == 401


# --- Nothing to answer from ----------------------------------------------


async def test_an_empty_library_answers_without_calling_the_model(
    client: AsyncClient, llm: FakeLlm, redis: FakeRedis
) -> None:
    """200, not an error: "I do not have that information" is an answer.

    The strongest guard of the feature runs here - the model is never called -
    and nothing is charged, because nothing was spent.
    """
    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json() == {"answer": "", "citations": [], "is_grounded": False}
    assert llm.calls == []
    assert redis.values == {}


# --- The happy path ------------------------------------------------------


async def test_an_answer_carries_the_source_the_model_never_saw(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm
) -> None:
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)

    response = await client.post("/api/chat", **ask())
    body = response.json()

    assert response.status_code == 200
    assert body["is_grounded"] is True
    assert body["citations"] == [{"number": 1, "filename": "conges.pdf", "page_number": 7}]
    # The filename and the page were attached by us afterwards. The prompt
    # carried neither, which is why an invented citation was not possible.
    _, prompt = llm.calls[0]
    assert "conges.pdf" not in prompt
    assert "page" not in prompt.lower()


async def test_a_question_is_charged_once(
    client: AsyncClient, session: AsyncSession, redis: FakeRedis
) -> None:
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)

    await client.post("/api/chat", **ask())

    assert redis.values[user_key(ALICE)] == 1
    assert redis.values[service_key()] == 1


# --- 🔒 Tenant isolation --------------------------------------------------


async def test_the_documents_of_another_user_never_reach_the_model(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm
) -> None:
    """The leak that would never look like a bug.

    Bob has the only document that matches, and it matches perfectly. The
    assistant would answer fluently, cite a real page of a real file, and
    nothing in the interface would suggest the file was not Alice.

    Nothing here relies on remembering to filter: the owner comes from the
    session, `ChatRequest` has no owner field for a handler to take by mistake,
    and `search_chunks` takes it keyword-only so omitting it is a TypeError.
    """
    await add_passage(session, BOB, "salaires-bob.pdf", "Le salaire de Bob est confidentiel. " * 10)

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json()["is_grounded"] is False
    # Not "the answer did not mention Bob": the passage never left the database.
    assert llm.calls == []


async def test_only_the_documents_of_the_caller_are_retrieved(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm
) -> None:
    await add_passage(session, ALICE, "alice.pdf", "Le document de Alice parle de conges. " * 10)
    await add_passage(session, BOB, "bob.pdf", "Le document de Bob parle de salaires. " * 10)

    await client.post("/api/chat", **ask())

    _, prompt = llm.calls[0]
    assert "Alice" in prompt
    assert "Bob" not in prompt


# --- The budget ----------------------------------------------------------


async def test_the_user_budget_answers_429_with_a_delay(
    client: AsyncClient, session: AsyncSession
) -> None:
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)
    for _ in range(3):  # per_user=3 in the fixture
        await client.post("/api/chat", **ask())

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 429
    # Without it, a client retries immediately and is refused again.
    assert int(response.headers["retry-after"]) > 0


async def test_a_provider_outage_gives_the_question_back(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm, redis: FakeRedis
) -> None:
    """An outage of ours must not cost the user part of their day.

    This is the reason reserving before the call is acceptable at all: the
    defect it introduces is the one that can be undone.
    """
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)
    llm.error = LanguageModelUnavailableError("groq is unreachable")

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 503
    assert user_key(ALICE) not in redis.values
    assert service_key() not in redis.values


async def test_the_provider_own_limit_is_refunded_too(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm, redis: FakeRedis
) -> None:
    """Our counter was wrong about the real budget; the user is not punished."""
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)
    llm.error = QuotaExhaustedError(retry_after=1800)

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1800"
    assert user_key(ALICE) not in redis.values


async def test_a_refusal_is_not_refunded(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm, redis: FakeRedis
) -> None:
    """A refusal is the product working, not failing.

    The call happened and the tokens were spent, so the question is charged.
    Refunding it would also invite an easy abuse: ask something unanswerable
    for free, forever.
    """
    await add_passage(session, ALICE, "conges.pdf", "Les conges payes sont de 25 jours. " * 10)
    llm.reply = REFUSAL

    response = await client.post("/api/chat", **ask())

    assert response.status_code == 200
    assert response.json()["is_grounded"] is False
    assert redis.values[user_key(ALICE)] == 1


# --- The question itself -------------------------------------------------


async def test_an_empty_question_is_refused_by_validation(
    client: AsyncClient, llm: FakeLlm
) -> None:
    response = await client.post("/api/chat", **ask("   \n  "))

    # 422 from the schema, before a single line of our own code runs.
    assert response.status_code == 422
    assert llm.calls == []


async def test_an_oversized_question_is_refused_before_it_costs_anything(
    client: AsyncClient, llm: FakeLlm, redis: FakeRedis
) -> None:
    response = await client.post("/api/chat", **ask("a" * 5000))

    assert response.status_code == 422
    assert llm.calls == []
    assert redis.values == {}


async def test_an_owner_sent_in_the_body_is_ignored(
    client: AsyncClient, session: AsyncSession, llm: FakeLlm
) -> None:
    """The field does not exist, so it cannot be honoured.

    A client is free to send one; pydantic drops it and the handler has no way
    to read it. The strongest defence is not validating a dangerous value - it
    is never accepting it into scope.
    """
    await add_passage(session, BOB, "bob.pdf", "Le document de Bob parle de salaires. " * 10)

    response = await client.post("/api/chat", json={"question": "Et le salaire ?", "owner_id": BOB})

    assert response.status_code == 200
    assert response.json()["is_grounded"] is False
    assert llm.calls == []
