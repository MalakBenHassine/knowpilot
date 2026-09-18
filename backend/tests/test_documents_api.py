"""The documents endpoints.

What is tested is the boundary: who is refused, what the envelope looks like,
what never appears in a response, and which upload is rejected before a single
byte is written.

Needs PostgreSQL:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_documents_api.py -v
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_session,
    db_session,
    get_embedding_model,
    get_file_storage,
    get_ingestion_store,
    require_csrf,
)
from app.core.session import SessionData
from app.core.storage import FileStorage
from app.db.documents import create_document, mark_ready
from app.main import app
from tests.conftest import requires_database
from tests.test_pipeline import FakeModel, FakeStore

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
def indexing() -> FakeStore:
    """Stands in for the background pipeline, and records that it ran."""
    return FakeStore(None)


@pytest.fixture
async def client(
    session: AsyncSession, tmp_path: Path, indexing: FakeStore
) -> AsyncIterator[AsyncClient]:
    """The real application, with its edges replaced.

    Overridden on purpose, and nothing else: the database hands over the test
    session so every write is rolled back; files land in a temporary directory
    rather than in the project; authentication and indexing are stubbed because
    Keycloak and a 2.2 GB model have nothing to do with what this file proves.

    Everything in between - validation, ordering, error translation - is the
    real code.
    """
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[current_session] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[require_csrf] = lambda: signed_in_as(ALICE)
    app.dependency_overrides[get_file_storage] = lambda: FileStorage(tmp_path)
    app.dependency_overrides[get_embedding_model] = FakeModel
    app.dependency_overrides[get_ingestion_store] = lambda: indexing

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


def upload(content: bytes, filename: str = "notes.txt") -> dict[str, object]:
    return {"files": {"file": (filename, content, "application/octet-stream")}}


TEXT = b"Les conges payes sont de 25 jours par annee complete. " * 20


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


# --- POST /api/documents ---------------------------------------------------


async def test_an_upload_is_accepted_immediately_as_processing(
    client: AsyncClient, indexing: FakeStore
) -> None:
    response = await client.post("/api/documents", **upload(TEXT))

    # 201 without waiting for indexing: embedding a long document takes a
    # minute, and a request held open that long is killed by every proxy
    # between here and the browser.
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "processing"
    assert body["stage"] == "parsing"
    assert body["mime_type"] == "text/plain"
    assert body["size_bytes"] == len(TEXT)
    # And the background task really was scheduled, and really ran - after the
    # response, with its own persistence rather than the request session.
    assert indexing.load_calls == 1


async def test_a_type_we_do_not_read_is_refused_before_anything_is_written(
    client: AsyncClient, tmp_path: Path
) -> None:
    response = await client.post("/api/documents", **upload(b"PK\x03\x04binary", "invoice.pdf"))

    # 415, not 400: the request is well formed, we simply do not read ZIP -
    # whatever the extension claims.
    assert response.status_code == 415
    # Nothing reached the disk: a refused upload must not cost a single write.
    # ASYNC240 is about blocking IO starving the event loop; in a test there is
    # no concurrency to starve, and looking at the disk is the whole point.
    assert list(tmp_path.rglob("*")) == []  # noqa: ASYNC240


async def test_an_empty_upload_is_refused(client: AsyncClient) -> None:
    assert (await client.post("/api/documents", **upload(b""))).status_code == 400


async def test_the_same_file_twice_is_a_conflict(client: AsyncClient) -> None:
    assert (await client.post("/api/documents", **upload(TEXT))).status_code == 201

    second = await client.post("/api/documents", **upload(TEXT, "copy.txt"))

    # Recognised by content, not by name: renaming a file does not make it new,
    # and re-indexing it would pay for the same embeddings twice.
    assert second.status_code == 409


async def test_a_hostile_filename_is_cleaned_before_it_is_stored(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/documents", **upload(TEXT, "../../etc/passwd\nFAKE LOG LINE")
    )

    stored = response.json()["filename"]
    # The directory part is gone: what is stored and displayed is a name, never
    # a path.
    assert "/" not in stored
    assert stored.isprintable()
    assert stored.startswith("passwd")
    # httpx percent-encodes the newline before the request even leaves the
    # client, so what arrives here is already tame. A raw socket would not be
    # so polite - which is exactly why safe_filename is tested on its own, on
    # input no HTTP client ever sanitised.


async def test_the_upload_response_hides_the_same_internal_fields(
    client: AsyncClient,
) -> None:
    body = (await client.post("/api/documents", **upload(TEXT))).json()

    assert "storage_path" not in body
    assert "content_hash" not in body
    assert "owner_id" not in body
