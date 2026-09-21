"""The LangChain PGVectorStore over our table, and above all tenant isolation.

These tests need a real PostgreSQL with pgvector, so they are opt-in:

    KP_RUN_DB_TESTS=1 uv run pytest tests/test_vector_store.py -v

Unlike the rest of the database tests, they cannot run inside a rolled-back
transaction: PGVectorStore takes its own connections from the pool and commits
each write, so it would never see a row written by an uncommitted session.
Each test therefore uses owners that are unique to the run, and deletes every
document it created at the end - the cascade removes the chunks.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_postgres import PGVectorStore
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import EMBEDDING_DIMENSIONS, DocumentChunk
from app.db.models import Document as DocumentRow
from app.db.session import create_engine
from app.db.vector_store import (
    chunk_id,
    create_vector_store,
    delete_document,
    replace_document_chunks,
)
from app.rag.retrieval import ContextWindowRetriever, KeywordRetriever, SemanticRetriever
from tests.conftest import requires_database

MODEL = "BAAI/bge-m3"

pytestmark = [pytest.mark.anyio, requires_database]


def unit_vector(axis: int) -> list[float]:
    """Length 1 along one axis: two axes are at cosine distance exactly 1."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[axis] = 1.0
    return vector


class AxisEmbeddings(Embeddings):
    """Every question is embedded along axis 0: the query of these tests."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [unit_vector(0) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return unit_vector(0)


@dataclass
class Db:
    engine: AsyncEngine
    store: PGVectorStore
    alice: str
    bob: str

    async def document(self, owner_id: str, filename: str = "contract.pdf") -> uuid.UUID:
        # expire_on_commit=False: the id is read after the commit, and an
        # expired attribute would trigger a lazy load outside any greenlet.
        factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with factory() as session:
            row = DocumentRow(
                owner_id=owner_id,
                filename=filename,
                mime_type="application/pdf",
                size_bytes=1024,
                content_hash=uuid.uuid4().hex,
                storage_path=f"/data/{uuid.uuid4()}",
                status="ready",
            )
            session.add(row)
            await session.commit()
            return row.id

    async def index(
        self,
        owner_id: str,
        document_id: uuid.UUID,
        texts_and_axes: list[tuple[str, int]],
        pages: list[int] | None = None,
    ) -> int:
        """One chunk per page unless `pages` says otherwise."""
        chunks = [
            Document(
                text,
                metadata={
                    "chunk_index": index,
                    "page_number": pages[index] if pages else index + 1,
                },
            )
            for index, (text, _) in enumerate(texts_and_axes)
        ]
        return await replace_document_chunks(
            self.store,
            owner_id=owner_id,
            document_id=document_id,
            filename="contract.pdf",
            chunks=chunks,
            vectors=[unit_vector(axis) for _, axis in texts_and_axes],
            embedding_model=MODEL,
        )

    async def count(self, document_id: uuid.UUID) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            )
            return int(result.scalar_one())

    def retriever(self, owner_id: str, max_distance: float = 0.6) -> SemanticRetriever:
        return SemanticRetriever(
            vector_store=self.store, owner_id=owner_id, k=8, max_distance=max_distance
        )


@pytest.fixture
async def db() -> AsyncIterator[Db]:
    engine = create_engine(get_settings().database_url)
    suffix = uuid.uuid4().hex[:8]
    handle = Db(
        engine=engine,
        store=await create_vector_store(engine, AxisEmbeddings()),
        alice=f"test-alice-{suffix}",
        bob=f"test-bob-{suffix}",
    )
    yield handle
    async with engine.begin() as connection:
        await connection.execute(
            delete(DocumentRow).where(DocumentRow.owner_id.in_([handle.alice, handle.bob]))
        )
    await engine.dispose()


# --- Writing and reading back ----------------------------------------------


async def test_chunks_are_written_and_found_by_their_owner(db: Db) -> None:
    document_id = await db.document(db.alice, "conges.pdf")
    await db.index(db.alice, document_id, [("les conges payes sont de 25 jours", 0)])

    found = await db.retriever(db.alice).ainvoke("combien de conges ?")

    assert len(found) == 1
    metadata = found[0].metadata
    # Every field a citation needs comes back from the row itself.
    assert metadata["document_id"] == document_id
    assert metadata["filename"] == "contract.pdf"
    assert metadata["page_number"] == 1
    assert metadata["distance"] == pytest.approx(0.0, abs=1e-6)


async def test_results_come_back_closest_first(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("loin", 5), ("proche", 0), ("moyen", 3)])

    found = await db.retriever(db.alice, max_distance=2.0).ainvoke("question")

    assert found[0].page_content == "proche"
    distances = [doc.metadata["distance"] for doc in found]
    assert distances == sorted(distances)


async def test_a_weak_match_is_rejected(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("sans rapport", 7)])

    # Without a ceiling, retrieval always returns *something*, and the model
    # answers confidently from the least bad passage in the library.
    assert await db.retriever(db.alice, max_distance=0.5).ainvoke("question") == []


# --- 🔒 The test this whole module exists for ------------------------------


async def test_a_search_never_reaches_another_owner(db: Db) -> None:
    """The passage of another account must not surface, whatever its score."""
    alice_document = await db.document(db.alice)
    bob_document = await db.document(db.bob)
    # Alice owns a mediocre match; Bob owns a PERFECT one.
    await db.index(db.alice, alice_document, [("note de service de alice", 4)])
    await db.index(db.bob, bob_document, [("le salaire confidentiel de bob", 0)])

    found = await db.retriever(db.alice, max_distance=2.0).ainvoke("salaire")

    # Ranking would have put Bob first. The owner filter is a WHERE clause
    # PostgreSQL applies before ranking.
    assert [doc.metadata["document_id"] for doc in found] == [alice_document]
    assert all("bob" not in doc.page_content for doc in found)


async def test_a_retriever_without_an_owner_cannot_be_built(db: Db) -> None:
    # The unsafe retriever is not a mistake someone can make: it does not exist.
    with pytest.raises(ValueError):
        db.retriever("")


# --- Idempotence and deletion ----------------------------------------------


async def test_indexing_the_same_document_twice_does_not_duplicate(db: Db) -> None:
    document_id = await db.document(db.alice)

    for _ in range(2):
        await db.index(db.alice, document_id, [("un", 0), ("deux", 1)])

    # A job that crashes and is retried must converge, not double.
    assert await db.count(document_id) == 2


async def test_a_shorter_reindex_leaves_no_stale_chunk(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("un", 0), ("deux", 1), ("trois", 2)])

    await db.index(db.alice, document_id, [("seul", 0)])

    # Deterministic ids alone would overwrite chunk 0 and leave 1 and 2 behind
    # - passages of the old cut, answering questions forever. The delete
    # before the insert is what removes them.
    assert await db.count(document_id) == 1


async def test_chunk_ids_are_deterministic() -> None:
    document_id = uuid.uuid4()

    assert chunk_id(document_id, 3) == chunk_id(document_id, 3)
    assert chunk_id(document_id, 3) != chunk_id(document_id, 4)


async def test_deleting_a_document_removes_its_chunks(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("a supprimer", 0)])

    async with async_sessionmaker(db.engine)() as session:
        assert await delete_document(session, owner_id=db.alice, document_id=document_id)
        await session.commit()

    # The cascade is the database doing it, not the application remembering
    # to - the reason the LangChain store points at OUR table.
    assert await db.count(document_id) == 0


async def test_reindexing_cannot_touch_another_owners_chunks(db: Db) -> None:
    bob_document = await db.document(db.bob)
    await db.index(db.bob, bob_document, [("document de bob", 0)])

    # Alice's pipeline, pointed at Bob's document by a wrong identifier: the
    # delete is scoped by owner as well as by document, so nothing happens.
    await db.index(db.alice, bob_document, [])

    assert await db.count(bob_document) == 1


# --- KeywordRetriever: full-text search on the same table ------------------


def keywords(db: Db, owner_id: str) -> KeywordRetriever:
    return KeywordRetriever(engine=db.engine, owner_id=owner_id, k=8)


async def test_a_phone_number_is_found_by_its_digits(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(
        db.alice,
        document_id,
        [("Le syndic est le Cabinet Marchand, joignable au 04 72 55 18 90.", 0), ("Autre.", 1)],
    )

    found = await keywords(db, db.alice).ainvoke("A qui correspond le 04 72 55 18 90 ?")

    assert "Marchand" in found[0].page_content
    assert found[0].metadata["document_id"] == document_id
    assert found[0].metadata["keyword_coverage"] >= 0.5


async def test_accents_do_not_matter_either_way(db: Db) -> None:
    # Users type "numero" and "conges"; documents say "numéro" and "congés".
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("Le numéro des congés payés.", 0)])

    found = await keywords(db, db.alice).ainvoke("numero conges payes")

    assert found
    assert found[0].metadata["keyword_coverage"] == 1.0


async def test_keyword_search_never_reaches_another_owner(db: Db) -> None:
    bob_document = await db.document(db.bob)
    await db.index(db.bob, bob_document, [("Le salaire de Bob Martin est de 68000 euros.", 0)])

    # A perfect lexical match - in somebody else's library.
    assert await keywords(db, db.alice).ainvoke("salaire Bob Martin 68000") == []


async def test_a_question_of_stop_words_matches_nothing(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("Le la les de du des.", 0)])

    # Nothing meaningful left once the stop words are removed: no division by
    # zero, and no match on everything.
    assert await keywords(db, db.alice).ainvoke("le la les ?") == []


async def test_sql_in_the_question_is_just_words(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [("Texte ordinaire.", 0)])

    # The question is a bound parameter, parsed by PostgreSQL as text.
    found = await keywords(db, db.alice).ainvoke("x'); DROP TABLE document_chunks; --")

    assert found == []
    assert await db.count(document_id) == 1


# --- The context window, on real chunks (ADR-0017) -----------------------------

AMENDMENT = (
    "Avenant signé le 15 septembre, applicable au 1er octobre. La cotisation passe à 59 euros."
)
VALUE = "La cotisation passe à 59 euros. Les autres clauses demeurent inchangées."


def window(db: Db, owner_id: str) -> ContextWindowRetriever:
    return ContextWindowRetriever(
        retriever=db.retriever(owner_id), engine=db.engine, owner_id=owner_id
    )


async def test_a_passage_is_read_with_the_chunk_before_it(db: Db) -> None:
    """The defect of ADR-0017: the value was found, its date was not."""
    document_id = await db.document(db.alice)
    # Only the second chunk is close to the question (axis 0).
    await db.index(db.alice, document_id, [(AMENDMENT, 1), (VALUE, 0)], pages=[4, 4])

    found = await window(db, db.alice).ainvoke("combien je paie")

    assert len(found) == 1
    text = found[0].page_content
    assert "applicable au 1er octobre" in text
    # The overlap of the splitter is read once, not twice.
    assert text.count("59 euros") == 1
    # Still the passage that was found: its page and its position are cited.
    assert found[0].metadata["chunk_index"] == 1
    assert found[0].metadata["page_number"] == 4
    assert found[0].metadata["context_from"] == 0


async def test_the_window_never_crosses_a_page(db: Db) -> None:
    """A citation says page 4; text from page 3 would make it lie."""
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [(AMENDMENT, 1), (VALUE, 0)], pages=[3, 4])

    found = await window(db, db.alice).ainvoke("combien je paie")

    assert found[0].page_content == VALUE
    assert "context_from" not in found[0].metadata


async def test_a_chunk_already_retrieved_is_not_read_twice(db: Db) -> None:
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [(AMENDMENT, 0), (VALUE, 0)], pages=[4, 4])

    found = await window(db, db.alice).ainvoke("combien je paie")

    assert sorted(doc.page_content for doc in found) == sorted([AMENDMENT, VALUE])


async def test_the_window_never_reads_another_owners_chunk(db: Db) -> None:
    """Defence in depth: even handed a passage that points into Alice's
    document - a bug upstream, a forged metadata - Bob's window reads nothing
    of hers. The owner is filtered in the query itself."""
    document_id = await db.document(db.alice)
    await db.index(db.alice, document_id, [(AMENDMENT, 1), (VALUE, 0)], pages=[4, 4])
    alices_passage = (await db.retriever(db.alice).ainvoke("combien je paie"))[0]

    bobs_window = ContextWindowRetriever(
        retriever=db.retriever(db.bob), engine=db.engine, owner_id=db.bob
    )
    widened = await bobs_window._widen([alices_passage])

    assert widened[0].page_content == VALUE
