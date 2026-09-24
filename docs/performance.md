# Performance

Measured, not estimated. Every number below was produced by a command in this
document, on the machine named in it, and can be produced again.

> **Read this first.** A latency without its hardware, its data and its
> concurrency is a decoration. So is a single total: this service is a model,
> a database and somebody else's API, and they grow in different directions.
> The point of this page is to say which part costs what, and which one breaks
> first.

## The machine

| | |
| --- | --- |
| CPU | Intel Core i5-14450HX, 8 cores available to WSL2 |
| Memory | 11 GiB available to WSL2 (host: 15.7 GiB) |
| Kernel | 6.6.114.1-microsoft-standard-WSL2 |
| PostgreSQL | 17.11 with pgvector, in Docker, published on 127.0.0.1 |
| Embedding model | BAAI/bge-m3, 1024 dimensions, **on CPU** |
| Date | 24 September 2026 |

A laptop, not a server. Everything here is a floor: a machine with a GPU, or
with the database on its own disk, does better. Nothing here is a best case.

## The read path: asking a question

```bash
cd backend
uv run python -m perf.seed 50000 --documents 200 --vocabulary 20000
uv run python -m perf.retrieval --corpus 50000
```

Each line is the one above it plus one more layer, so the cost of a layer is
the difference between two lines. Twenty questions, two discarded warm-ups.

| Corpus | Embed the question | + vector search | + keyword search | + context window |
| ---: | ---: | ---: | ---: | ---: |
| 1 000 chunks | 77 ms | 83 ms | 93 ms | **107 ms** |
| 10 000 chunks | 74 ms | 79 ms | 180 ms | **189 ms** |
| 50 000 chunks | 86 ms | 89 ms | 110 ms | **109 ms** |

Three things fall out of that table.

**Embedding the question is the floor, and it does not move.** About 80 ms,
whatever the corpus size, because it is the model running on a CPU and has
nothing to do with the data. It is also the single largest item in the budget
for a small corpus - more than the entire database.

**The vector search is flat.** Three to six milliseconds at 1 000 chunks and
at 50 000. That is the HNSW index doing exactly what it exists for
(`ix_document_chunks_embedding_hnsw`, built in migration 0001). Multiplying
the corpus by fifty did not move it.

**The keyword search is the one that moves**, and the 10 000-chunk row is
not a typo. That row was measured on a different corpus, and the difference
between the rows is the finding.

### Why the keyword search cost changes by a factor of ten

The SQL filters with the GIN index (`text_tsv @@ any_word`) and then computes
a coverage score **for every row that matched**. So its cost is not
"how many chunks exist" but "how many chunks contain any word of the
question". Two corpora of exactly 50 000 chunks, differing only in vocabulary:

| Corpus of 50 000 chunks | Rows scored | Keyword query | Retrieval, p50 |
| --- | ---: | ---: | ---: |
| 36-word vocabulary | **49 964** | 1157 ms | 771 ms |
| 20 000-word vocabulary | **534** | 73 ms | 110 ms |

`EXPLAIN (ANALYZE, BUFFERS)` shows it directly: `loops=49964` against the
first corpus, `loops=534` against the second. Same query, same index, same row
count - twenty times the latency, because a question word was in every
document instead of one in a hundred.

The 36-word corpus is not a realistic document set, but it is not a fantasy
either: two hundred revisions of the same contract template, or a year of
invoices from one supplier, look like that. **The number to remember is not
110 ms; it is that the keyword search costs one scored row per matching
chunk, and that a repetitive corpus makes every chunk match.**

## The HTTP layer under concurrency

```bash
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000   # in another shell
k6 run perf/api.js
```

Ramped to 100 virtual users against one uvicorn worker. 283 746 requests,
**zero failed**.

| Scenario | What it exercises | p50 | p95 | max |
| --- | --- | ---: | ---: | ---: |
| `/api/health/live` | routing, middleware, metrics - nothing else | 5.7 ms | **14.4 ms** | 103 ms |
| `/api/health/ready` | the same, plus PostgreSQL, Redis and the model check | 28.6 ms | **141.9 ms** | 603 ms |

The floor is the honest cost of the framework: under fifteen milliseconds at
the 95th percentile with a hundred clients in flight. The difference between
the two rows - about 125 ms at p95 - is what touching the dependencies costs
under that load, and most of it is connection pool contention: one uvicorn
worker, a pool sized for a laptop, a hundred callers.

`perf/api.js` carries thresholds (`p(95)<50` for the floor, `p(95)<200` with
dependencies, under 1% failures), so it exits non-zero when it regresses. A
benchmark without a threshold is a graph nobody looks at twice.

**`/api/chat` is deliberately not load-tested.** Every question there reaches
Groq, on a free tier with a daily budget: a load test would measure somebody
else's server and spend the product's quota to do it. Generation latency is
read from `knowpilot_generation_duration_seconds` instead (ADR-0019), where
real traffic has already measured it.

## The write path: indexing a document

```bash
cd backend
uv run python -m perf.ingestion --pages 20
```

The real pipeline - parse, split, embed - against a store that keeps nothing,
so the number is the CPU and not the disk.

| Document | Passages | Median | Throughput |
| --- | ---: | ---: | --- |
| 20 pages, 87 KiB | 200 | **80.9 s** | 0.25 pages/s, 2.5 passages/s |

Embedding dominates: 200 passages through BGE-M3 on a CPU. This is the number
that decides what the product feels like, because it is the one a user waits
for - the upload returns in milliseconds, but the document answers no question
until this finishes.

### A limit the code sets and this machine cannot meet

`MAX_PAGES = 500` (`app/rag/parsing.py`) accepts a 500-page document.
`JOB_TIMEOUT_SECONDS = 600` (`app/worker.py`) kills a job after ten minutes.
At 0.25 pages/s, 500 pages needs about **2 000 seconds** - more than three
times the timeout.

So on hardware like this, a document somewhere above roughly **150 pages** is
accepted by the parser, indexed for ten minutes, killed by the timeout,
retried once by `max_tries = 2`, and killed again. The user sees
`processing_error`, marked retryable, for ever.

The two constants were each reasonable on their own and were never measured
against each other.

### How it was fixed

The budget is now derived from the measurement, and spent **before** the work:

```python
MAX_INGESTION_SECONDS = 1800   # what this deployment will spend on one document
SECONDS_PER_PAGE = 6           # measured 4 s/page; margin for a slower machine
```

Once parsing has produced the page count, `ingest_document` refuses anything
that would not fit - `too_large`, not retryable, in seconds - and nothing is
embedded. `JOB_TIMEOUT_SECONDS` is now `MAX_INGESTION_SECONDS + 300`, so the
two can no longer disagree: the inner decision always wins, and a job that
reaches arq's limit is stuck somewhere unforeseen rather than merely slow. A
test asserts that ordering, so the pair cannot drift apart again.

**Why a check before the work rather than a deadline around it.**
`aembed_documents` hands one blocking call to a worker thread, and a cancel
scope cannot interrupt a thread already inside torch: a deadline would not
fire until the work it guards had finished. Once the page count is known the
cost is predictable, so the decision is taken while it can still be acted on.
Refusing in two seconds is a better answer than killing after thirty minutes.

**What it costs.** On this hardware the ceiling is 300 pages rather than the
500 the parser allows, and `MAX_PAGES` goes back to being what it always
was - a guard on memory, checked while the file is read, not a promise about
time. A deployment on faster hardware lowers `SECONDS_PER_PAGE` and gets its
pages back; there is one number to calibrate, and this page is how to
calibrate it.

## What is not measured here

- **Generation.** Groq's latency is theirs, and measuring it would spend the
  daily budget. The metric exists in production.
- **OCR.** Designed in ADR-0010, not wired; there is nothing to time.
- **More than one node.** Everything above is one process against one
  database. The numbers say nothing about what a second replica does to the
  connection pool.
- **Cold starts.** The model takes about twenty seconds to load, which is why
  the worker has a `startupProbe` and not just a liveness probe.

## What breaks first

In the order it would actually happen:

1. ~~Indexing long documents~~ - fixed above; the ceiling is now derived from
   the measurement rather than guessed.
2. **The keyword search on a repetitive corpus**, as soon as one user uploads
   a few hundred near-identical documents.
3. **The connection pool**, under real concurrency on one uvicorn worker - the
   125 ms the dependencies add at 100 clients is the first sign of it.
4. **The vector search**, last, and not soon: flat from 1 000 to 50 000 chunks.

Nothing here argues for rewriting anything. It argues for knowing, before
somebody else finds out, which of the four it is.
