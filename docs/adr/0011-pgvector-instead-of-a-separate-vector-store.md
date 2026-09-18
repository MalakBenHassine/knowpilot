# ADR-0011: pgvector in the application database, not a separate vector store

- **Status:** Accepted
- **Date:** 2026-09-18
- **Supersedes:** the passing mention of Chroma in [ADR-0006](0006-local-embeddings.md)

## Context

ADR-0006 chose BGE-M3 and mentioned Chroma in one line, as a remark about
configuring cosine distance. That was never a decision: it was an assumption
carried along from the reference guide, and it was never weighed.

By the time the indexing step arrived, the stack already ran PostgreSQL for
Keycloak and for the application. Retrieval quality does not depend on the
engine — the same vectors and the same metric return the same neighbours in
any of the candidates, and at this scale the search is exact anyway. What
differs is everything around the search: consistency, isolation and operation.

The deployment target is one free VM already running Postgres, Redis, Keycloak
and a 2.2 GB embedding model.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **pgvector in the existing database** | A document and its chunks live in one transaction, so a delete cascades and no orphaned vector can survive; the owner filter is an ordinary `WHERE`, and Row-Level Security remains possible; one backup, one service to secure; hybrid search later with `tsvector` in the same query | SQL to write by hand; HNSW parameters to understand; the Postgres image must carry the extension |
| Chroma as a service | The simplest API, built for RAG | A second data store: consistency between the `documents` table and the index becomes application code, and a crash between the two writes leaves vectors answering questions about a deleted file. One more container, volume, backup and attack surface |
| Qdrant as a service | The strongest dedicated engine: fast filtering, quantisation, real scaling | Same split-brain problem as Chroma, with a higher memory footprint on a VM that is already full |

## Decision

Vectors live in `document_chunks`, in the application database, as
`vector(1024)` with an HNSW index using `vector_cosine_ops`. The Postgres image
becomes `pgvector/pgvector:pg17`. The extension is created by the first Alembic
migration, so every environment gets it, not only a freshly initialised
container.

`owner_id` is denormalised onto `document_chunks` although it could be reached
through `documents`.

## Consequences

- Deleting a document deletes its vectors through `ON DELETE CASCADE`, in the
  same transaction. The class of bug where an index and a table disagree simply
  does not exist here.
- Search filters on the owner before ranking. A test stores a *perfect* match
  owned by another account and asserts it never surfaces; removing the filter
  turns it red.
- The denormalised `owner_id` also means the isolation can later be enforced by
  PostgreSQL itself with Row-Level Security, which is impossible without the
  column on the table.
- Alembic migrations, already needed for the schema, now also carry the index
  definition — there is no second system with its own idea of the truth.
- Moving from Alpine to the Debian-based pgvector image changed the C library
  underneath an existing data directory. Indexes had to be rebuilt with
  `REINDEX`. Worth remembering: changing the base image of a database is a
  migration, not a version bump.
- We give up the tuning that a dedicated engine offers (quantisation, payload
  indexes, sharding). At tens of thousands of chunks this costs nothing.

## Revisit when

The corpus grows past a few million chunks, or filtered search becomes slow
enough to show up in the p95, or the embedding pipeline needs to scale
independently of the database. Any of those is the moment to measure pgvector
against Qdrant on our own data rather than on benchmarks.
