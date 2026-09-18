"""The documents repository.

Needs a real PostgreSQL:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_documents.py -v
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.documents import (
    create_document,
    find_by_content_hash,
    get_document,
    list_documents,
    mark_failed,
    mark_ready,
    set_stage,
)
from tests.conftest import requires_database

ALICE = "alice-sub-0001"
BOB = "bob-sub-0002"

pytestmark = [pytest.mark.anyio, requires_database]


async def add(
    session: AsyncSession, owner_id: str, *, filename: str = "contract.pdf", digest: str = ""
) -> uuid.UUID:
    document_id = uuid.uuid4()
    await create_document(
        session,
        document_id=document_id,
        owner_id=owner_id,
        filename=filename,
        mime_type="application/pdf",
        size_bytes=2048,
        content_hash=digest or uuid.uuid4().hex,
        storage_path=f"/data/{document_id}",
    )
    return document_id


# --- Isolation -------------------------------------------------------------


async def test_a_listing_only_shows_the_documents_of_one_owner(
    session: AsyncSession,
) -> None:
    await add(session, ALICE, filename="mine.pdf")
    await add(session, BOB, filename="not-mine.pdf")

    names = [document.filename for document in await list_documents(session, owner_id=ALICE)]

    assert names == ["mine.pdf"]


async def test_fetching_the_document_of_another_owner_returns_nothing(
    session: AsyncSession,
) -> None:
    document_id = await add(session, BOB)

    # None here, 404 in the API. A 403 would confirm the identifier is real.
    assert await get_document(session, owner_id=ALICE, document_id=document_id) is None
    assert await get_document(session, owner_id=BOB, document_id=document_id) is not None


async def test_the_same_file_uploaded_by_two_users_stays_two_documents(
    session: AsyncSession,
) -> None:
    digest = uuid.uuid4().hex
    await add(session, ALICE, digest=digest)
    await add(session, BOB, digest=digest)

    # Deduplication is per user. Answering across users would turn the hash
    # into an oracle: upload a file, learn whether someone else holds it.
    assert await find_by_content_hash(session, owner_id=ALICE, content_hash=digest) is not None
    assert await find_by_content_hash(session, owner_id=BOB, content_hash=digest) is not None


async def test_an_identical_upload_is_recognised(session: AsyncSession) -> None:
    digest = uuid.uuid4().hex
    document_id = await add(session, ALICE, digest=digest)

    found = await find_by_content_hash(session, owner_id=ALICE, content_hash=digest)

    assert found is not None
    assert found.id == document_id


async def test_an_empty_library_is_an_empty_list_not_an_error(
    session: AsyncSession,
) -> None:
    assert await list_documents(session, owner_id="nobody-at-all") == []


# --- The lifecycle of one document -----------------------------------------


async def test_a_new_document_starts_processing_at_the_parsing_stage(
    session: AsyncSession,
) -> None:
    document_id = await add(session, ALICE)

    document = await get_document(session, owner_id=ALICE, document_id=document_id)

    assert document is not None
    assert (document.status, document.stage) == ("processing", "parsing")


async def test_the_stage_follows_the_real_pipeline(session: AsyncSession) -> None:
    document_id = await add(session, ALICE)

    await set_stage(session, document_id=document_id, stage="embedding")

    document = await get_document(session, owner_id=ALICE, document_id=document_id)
    assert document is not None
    # A real step, never an invented percentage.
    assert document.stage == "embedding"


async def test_a_ready_document_carries_no_stage_and_no_failure(
    session: AsyncSession,
) -> None:
    document_id = await add(session, ALICE)
    await set_stage(session, document_id=document_id, stage="indexing")

    await mark_ready(session, document_id=document_id, page_count=12, chunk_count=24)

    document = await get_document(session, owner_id=ALICE, document_id=document_id)
    assert document is not None
    # The frontend union type says ready documents have no stage. The database
    # must not be able to produce a state the interface cannot render.
    assert (document.status, document.stage, document.failure_reason) == (
        "ready",
        None,
        None,
    )
    assert (document.page_count, document.chunk_count) == (12, 24)


async def test_a_failure_is_recorded_as_a_code(session: AsyncSession) -> None:
    document_id = await add(session, ALICE)

    await mark_failed(session, document_id=document_id, reason="no_text_found", retryable=False)

    document = await get_document(session, owner_id=ALICE, document_id=document_id)
    assert document is not None
    # A code, not a message: the wording belongs to the frontend, and a
    # technical message would leak internals to the client.
    assert document.failure_reason == "no_text_found"
    # A scanned page will not become readable on a second attempt.
    assert document.retryable is False
