"""GET /api/documents.

What is tested is the boundary: who is refused, what the envelope looks like,
and - above all - what never appears in a response.

Needs PostgreSQL:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_documents_api.py -v
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_session, db_session
from app.core.session import SessionData
from app.db.documents import create_document, mark_ready
from app.main import app
from tests.conftest import requires_database

ALICE = "alice-sub-0001"
BOB = "bob-sub-0002"

pytestmark = [pytest.mark.anyio, requires_database]


def signed_in_as(sub: str) -> SessionData:
    return SessionData(
        sub=sub,
        email=f"{sub}@knowpilot.dev",
        display_name="Test User",
        access_token="",
        refresh_token="",
        id_token="",
        # ruff flags any literal assigned to a token-looking argument (S106).
        # Here it is a fixture value in a test, not a credential in shipped
        # code - which is exactly the distinction the rule cannot make.
        csrf_token="csrf-token",  # noqa: S106
        created_at=0.0,
    )


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real application, with two dependencies replaced.

    The database dependency hands over the test session, so everything written
    by a request is rolled back with it. Authentication is replaced because
    Keycloak has nothing to do with what this file tests - dragging a real
    login in would make these tests slow and fragile without proving more.
    """
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[current_session] = lambda: signed_in_as(ALICE)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def add(session: AsyncSession, owner_id: str, filename: str) -> uuid.UUID:
    document_id = uuid.uuid4()
    await create_document(
        session,
        document_id=document_id,
        owner_id=owner_id,
        filename=filename,
        mime_type="application/pdf",
        size_bytes=2411724,
        content_hash=uuid.uuid4().hex,
        storage_path=f"/data/{document_id}",
    )
    return document_id


async def test_an_empty_library_returns_an_empty_envelope(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/documents")

    assert response.status_code == 200
    # An envelope, not a bare array: `total` can be added later without
    # breaking a client that already reads `items`.
    assert response.json() == {"items": []}


async def test_only_the_documents_of_the_caller_are_listed(
    client: AsyncClient, session: AsyncSession
) -> None:
    await add(session, ALICE, "mine.pdf")
    await add(session, BOB, "not-mine.pdf")

    body = (await client.get("/api/documents")).json()

    assert [item["filename"] for item in body["items"]] == ["mine.pdf"]


async def test_the_response_never_exposes_internal_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    document_id = await add(session, ALICE, "contract.pdf")
    await mark_ready(session, document_id=document_id, page_count=12, chunk_count=24)

    item = (await client.get("/api/documents")).json()["items"][0]

    # Built from an explicit schema, never by serialising the model. These
    # three exist on the row and must never travel: the storage path invites
    # path games, and the hash lets an attacker test whether we hold a file.
    assert "owner_id" not in item
    assert "storage_path" not in item
    assert "content_hash" not in item
    assert item["page_count"] == 12
    assert item["stage"] is None


async def test_an_anonymous_caller_is_refused(session: AsyncSession) -> None:
    # Authentication is NOT overridden here: the real current_session runs, so
    # this exercises the actual 401 path rather than a stub.
    app.dependency_overrides[db_session] = lambda: session
    # The lifespan does not run under ASGITransport, so app.state is empty. The
    # store is never used: with no cookie, current_session raises 401 before
    # looking anything up. It only has to exist for the dependency to resolve.
    app.state.session_store = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/documents")

    app.dependency_overrides.clear()
    assert response.status_code == 401
