# ADR-0013: Ingestion in a worker process, behind a queue

- **Status:** Accepted
- **Date:** 2026-09-20
- **Supersedes:** the use of Starlette background tasks for ingestion, introduced with the upload endpoint

## Context

Uploading returned `201` immediately and indexed the document in a Starlette
background task: in the process that serves requests, on the event loop that
answers questions. It worked, and it was the right first version - a queue
before the first document was indexed would have been machinery in search of a
problem.

Three defects then became real rather than theoretical.

**Contention.** Parsing and embedding are CPU-bound. The pipeline already
pushes them into a worker thread, which keeps the event loop responsive, but
the CPU is still the same CPU: a thirty-page PDF being embedded is thirty
seconds of a shared vCPU that somebody waiting for an answer does not get.

**Loss.** A background task exists only in memory. A deployment, a crash or an
`OOM` during ingestion loses the work with no trace: the document row says
`processing` for ever, and nothing anywhere records that indexing was owed.
There is no mechanism that could ever notice.

**Coupling.** The web tier is bound by connections and the ingestion tier by
CPU and memory. Running them in one process means scaling one is scaling both,
and each API replica also carries a 2.2 GB model it may never use for indexing.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **arq (Redis)** | Redis is already in the stack for sessions and quotas, so no new service, volume, backup or attack surface; asyncio-native, so the pipeline moves unchanged; ~200 lines total | A second process to run and supervise; a smaller ecosystem than Celery |
| Celery + Redis | The reference in Python, huge ecosystem | Built around threads and processes rather than asyncio; heavier configuration than this project needs |
| A `jobs` table polled by a worker | No new dependency at all; the job and the document commit in one transaction | Polling to write, polling to reason about, polling to tune - reimplementing a queue badly |
| Keep background tasks | Nothing to do | Accepts losing work silently, which is the defect that cannot be monitored |

## Decision

**arq, with `app/worker.py` as a separate process**, reached through a
`JobQueue` protocol so no route knows what consumes its jobs.

`POST /api/documents` and the retry endpoint now commit, then publish. The
commit still comes first, and the reason is sharper than before: a worker in
another process can claim the job microseconds later and will read the database
through its own connection, which cannot see an uncommitted row.

The worker runs **one job at a time**. The work is CPU-bound, so four parallel
ingestions on a shared vCPU finish later than four sequential ones and hold
four documents worth of tensors while doing it. Concurrency pays when tasks
wait; these do not.

## What did NOT change, and why that is the point

`app/rag/pipeline.py` is untouched. It already took an `IngestionStore` and an
`EmbeddingModel` as arguments rather than reaching for a database or a model of
its own, so moving it into another process meant building those two objects
somewhere else and nothing more. The nine pipeline tests still pass against
their fakes, unaware that a process boundary appeared underneath them.

That is what dependency inversion buys, stated concretely: **an architectural
change with no change to the code being re-architected.**

## Consequences

- **A second process must run.** Without it, uploads are stored and queued but
  never indexed - visibly, as documents that stay `processing`. That is a
  better failure than the one it replaces: the work is written down in Redis
  and runs when a worker appears.
- **Ingestion survives a restart.** arq re-delivers a job whose worker died
  mid-flight, capped at two tries.
- **The API no longer needs the embedding model for uploads** - but still loads
  it, because `POST /api/chat` embeds the question. Splitting that out would
  mean an embedding service of its own, which is not warranted yet.
- **The endpoint tests got better by losing something.** They can no longer
  watch the pipeline run, so they assert what the endpoint actually owes its
  caller: that the job was published, with the owner, after the commit.
- **Job payloads are strings.** A UUID that survives only because both
  processes use the same serialiser is an undeclared dependency that breaks the
  day one side is upgraded alone.
- **One monitoring gap is accepted:** nothing yet alerts when the queue grows
  faster than it drains. The document list makes it visible to a user; it is
  not visible to an operator. That belongs with the rest of observability.
