"""Vector storage and, above all, isolation between accounts.

These tests need a real PostgreSQL with pgvector, so they are opt-in:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_vector_store.py -v

They run inside a transaction that is rolled back at the end, so they can be
run against the development database without leaving anything behind.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EMBEDDING_DIMENSIONS, Document
from app.db.vector_store import (
    delete_document,
    replace_document_chunks,
    search_chunks,
)
from app.rag.chunking import Chunk
from app.rag.embeddings import EmbeddedChunk
from tests.conftest import requires_database

ALICE = "alice-sub-0001"
BOB = "bob-sub-0002"
MODEL = "BAAI/bge-m3"

pytestmark = [pytest.mark.anyio, requires_database]


def unit_vector(axis: int) -> list[float]:
    """A vector of length 1 pointing along one axis.

    Two different axes are exactly as far apart as cosine distance allows for
    positive vectors, which makes the assertions unambiguous.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[axis] = 1.0
    return vector


async def make_document(session: AsyncSession, owner_id: str) -> uuid.UUID:
    document = Document(
        owner_id=owner_id,
        filename="contract.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        # Unique per run so re-running the tests never trips the
        # (owner_id, content_hash) constraint.
        content_hash=uuid.uuid4().hex,
        storage_path=f"/data/{uuid.uuid4()}",
        status="ready",
    )
    session.add(document)
    await session.flush()
    return document.id


def embedded(texts_and_axes: list[tuple[str, int]]) -> list[EmbeddedChunk]:
    return [
        EmbeddedChunk(
            chunk=Chunk(index=index, text=text, page_number=index + 1),
            vector=unit_vector(axis),
        )
        for index, (text, axis) in enumerate(texts_and_axes)
    ]


# --- Writing and reading back ----------------------------------------------


async def test_chunks_are_written_and_found_by_their_owner(
    session: AsyncSession,
) -> None:
    document_id = await make_document(session, ALICE)
    await replace_document_chunks(
        session,
        owner_id=ALICE,
        document_id=document_id,
        embedded=embedded([("les conges payes sont de 25 jours", 0)]),
        embedding_model=MODEL,
    )

    found = await search_chunks(session, owner_id=ALICE, query_vector=unit_vector(0))

    assert len(found) == 1
    assert found[0].page_number == 1
    assert found[0].distance == pytest.approx(0.0, abs=1e-6)


async def test_results_come_back_closest_first(session: AsyncSession) -> None:
    document_id = await make_document(session, ALICE)
    await replace_document_chunks(
        session,
        owner_id=ALICE,
        document_id=document_id,
        embedded=embedded([("loin", 5), ("proche", 0), ("moyen", 3)]),
        embedding_model=MODEL,
    )

    found = await search_chunks(session, owner_id=ALICE, query_vector=unit_vector(0))

    assert found[0].text == "proche"
    assert [item.distance for item in found] == sorted(item.distance for item in found)


async def test_a_weak_match_can_be_rejected(session: AsyncSession) -> None:
    document_id = await make_document(session, ALICE)
    await replace_document_chunks(
        session,
        owner_id=ALICE,
        document_id=document_id,
        embedded=embedded([("sans rapport", 7)]),
        embedding_model=MODEL,
    )

    # Without a ceiling, retrieval always returns *something*, and the model
    # answers confidently from the least bad passage in the library.
    found = await search_chunks(
        session, owner_id=ALICE, query_vector=unit_vector(0), max_distance=0.5
    )

    assert found == []


# --- The test this whole module exists for ---------------------------------


async def test_a_search_never_reaches_another_owner(session: AsyncSession) -> None:
    """The passage of another account must not surface, whatever its score."""
    alice_document = await make_document(session, ALICE)
    bob_document = await make_document(session, BOB)

    # Alice owns a mediocre match for the query.
    await replace_document_chunks(
        session,
        owner_id=ALICE,
        document_id=alice_document,
        embedded=embedded([("note de service de alice", 4)]),
        embedding_model=MODEL,
    )
    # Bob owns a PERFECT match: same vector as the query.
    await replace_document_chunks(
        session,
        owner_id=BOB,
        document_id=bob_document,
        embedded=embedded([("le salaire confidentiel de bob est de 4200 euros", 0)]),
        embedding_model=MODEL,
    )

    found = await search_chunks(session, owner_id=ALICE, query_vector=unit_vector(0))

    # Ranking would have put Bob first. The owner filter runs before ranking.
    assert [item.document_id for item in found] == [alice_document]
    assert all("bob" not in item.text for item in found)


# --- Idempotence and deletion ----------------------------------------------


async def test_indexing_the_same_document_twice_does_not_duplicate(
    session: AsyncSession,
) -> None:
    document_id = await make_document(session, ALICE)
    chunks = embedded([("un", 0), ("deux", 1)])

    for _ in range(2):
        await replace_document_chunks(
            session,
            owner_id=ALICE,
            document_id=document_id,
            embedded=chunks,
            embedding_model=MODEL,
        )

    # A background task that crashes and is retried must converge, not double.
    found = await search_chunks(session, owner_id=ALICE, query_vector=unit_vector(0), limit=100)
    assert len(found) == 2


async def test_deleting_a_document_removes_its_chunks(session: AsyncSession) -> None:
    document_id = await make_document(session, ALICE)
    await replace_document_chunks(
        session,
        owner_id=ALICE,
        document_id=document_id,
        embedded=embedded([("a supprimer", 0)]),
        embedding_model=MODEL,
    )

    assert await delete_document(session, owner_id=ALICE, document_id=document_id)
    await session.flush()

    # The cascade is the database doing it, not the application remembering to.
    # This is what pgvector buys over a separate vector store.
    assert await search_chunks(session, owner_id=ALICE, query_vector=unit_vector(0)) == []


async def test_deleting_the_document_of_another_owner_does_nothing(
    session: AsyncSession,
) -> None:
    document_id = await make_document(session, BOB)
    await replace_document_chunks(
        session,
        owner_id=BOB,
        document_id=document_id,
        embedded=embedded([("document de bob", 0)]),
        embedding_model=MODEL,
    )

    deleted = await delete_document(session, owner_id=ALICE, document_id=document_id)

    # False here, 404 in the API: never 403, which would confirm the id exists.
    assert deleted is False
    assert await search_chunks(session, owner_id=BOB, query_vector=unit_vector(0)) != []
