"""Fills document_chunks with a synthetic corpus, to measure the database.

    uv run python -m perf.seed 10000

Why synthetic vectors rather than real ones: embedding 100 000 passages with
BGE-M3 on a laptop CPU takes hours and would measure the MODEL. What scales
badly as a corpus grows is the SEARCH - the HNSW index, the GIN index, the
tenant filter - and that is what this fills. The cost of embedding one
question is measured separately, in perf/retrieval.py, where it belongs.

The vectors are random and normalised, which is the honest worst case: real
embeddings of related passages cluster, and clustered data is easier for HNSW
than uniform noise. A number measured here is a floor, not a flattering one.

Everything it writes belongs to one owner, PERF_OWNER, and `--clean` removes
exactly that. It never touches another owner's rows.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
import uuid

from sqlalchemy import text

from app.core.config import get_settings
from app.db.models import EMBEDDING_DIMENSIONS
from app.db.session import create_engine

PERF_OWNER = "perf-benchmark-owner"

# A small vocabulary, in French, so the tsvector and the keyword search have
# something realistic to chew on. Real documents repeat their vocabulary; a
# corpus of unique random words would make the GIN index look far better than
# it is.
WORDS = (
    "contrat resiliation preavis abonnement facture mensuelle engagement "
    "penalite remboursement garantie livraison delai conditions tarif "
    "assurance franchise sinistre declaration indemnisation vehicule "
    "conducteur constat expertise reparation vetuste plafond exclusion "
    "salarie conges anciennete remuneration prime mutuelle teletravail"
).split()

BATCH = 500


def vocabulary(size: int) -> list[str]:
    """The words a chunk is built from.

    Vocabulary size is not decoration: the keyword search filters with the
    GIN index and then computes a coverage score for every row that matched.
    With 36 words, every question word is in nearly every chunk, so the whole
    corpus matches and the score is computed 50 000 times. With 20 000 words -
    the order of a real document set - a question word is in a fraction of a
    percent of them. The same query, two entirely different costs, and only
    measuring both tells you which one you are looking at.
    """
    if size <= len(WORDS):
        return WORDS[:size]
    extra = [f"terme{index:05d}" for index in range(size - len(WORDS))]
    return WORDS + extra


def _chunk_text(rng: random.Random, words: list[str]) -> str:
    return " ".join(rng.choice(words) for _ in range(120))


def _vector(rng: random.Random) -> str:
    values = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIMENSIONS)]
    norm = sum(v * v for v in values) ** 0.5
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


async def seed(count: int, documents: int, vocabulary_size: int) -> None:
    rng = random.Random(20260924)
    words = vocabulary(vocabulary_size)
    engine = create_engine(get_settings().database_url)
    started = time.perf_counter()

    async with engine.begin() as connection:
        document_ids = []
        for number in range(documents):
            document_id = uuid.uuid4()
            document_ids.append(document_id)
            await connection.execute(
                text(
                    "INSERT INTO documents (id, owner_id, filename, mime_type, "
                    "size_bytes, content_hash, storage_path, status, retryable, page_count, "
                    "chunk_count) VALUES (:id, :owner, :filename, 'text/plain', "
                    "1024, :digest, :path, 'ready', false, 10, :chunks)"
                ),
                {
                    "id": document_id,
                    "owner": PERF_OWNER,
                    "filename": f"corpus-{number:04d}.txt",
                    "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                    "path": f"/tmp/perf/corpus-{number:04d}.txt",
                    "chunks": count // documents,
                },
            )

        rows = []
        for index in range(count):
            document_id = document_ids[index % documents]
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "document_id": document_id,
                    "owner": PERF_OWNER,
                    "filename": "corpus.txt",
                    "chunk_index": index // documents,
                    "page": (index % 10) + 1,
                    "content": _chunk_text(rng, words),
                    "embedding": _vector(rng),
                }
            )
            if len(rows) == BATCH or index == count - 1:
                await connection.execute(
                    text(
                        "INSERT INTO document_chunks (id, document_id, owner_id, "
                        "filename, chunk_index, page_number, text, embedding, "
                        "embedding_model) VALUES (:id, :document_id, :owner, "
                        ":filename, :chunk_index, :page, :content, "
                        "CAST(:embedding AS vector), 'perf-synthetic')"
                    ),
                    rows,
                )
                rows = []
                print(f"\r  {index + 1}/{count} chunks", end="", flush=True)

    print()
    async with engine.begin() as connection:
        # Without this the planner still holds the statistics of an empty
        # table, and the first queries are planned for a corpus that no longer
        # exists. Measuring that would measure a mistake, not the system.
        await connection.execute(text("ANALYZE document_chunks"))

    await engine.dispose()
    print(
        f"seeded {count} chunks from a {len(words)}-word vocabulary "
        f"in {time.perf_counter() - started:.1f}s"
    )


async def clean() -> None:
    engine = create_engine(get_settings().database_url)
    async with engine.begin() as connection:
        chunks = await connection.execute(
            text("DELETE FROM document_chunks WHERE owner_id = :owner RETURNING 1"),
            {"owner": PERF_OWNER},
        )
        await connection.execute(
            text("DELETE FROM documents WHERE owner_id = :owner"), {"owner": PERF_OWNER}
        )
    await engine.dispose()
    print(f"removed {len(chunks.fetchall())} chunks belonging to {PERF_OWNER}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, nargs="?", default=10_000)
    parser.add_argument("--documents", type=int, default=20)
    parser.add_argument(
        "--vocabulary",
        type=int,
        default=20_000,
        help="distinct words; small values make the keyword search its own worst case",
    )
    parser.add_argument("--clean", action="store_true")
    arguments = parser.parse_args()

    if arguments.clean:
        asyncio.run(clean())
    else:
        asyncio.run(seed(arguments.count, arguments.documents, arguments.vocabulary))


if __name__ == "__main__":
    main()
