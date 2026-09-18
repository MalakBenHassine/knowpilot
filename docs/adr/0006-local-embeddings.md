# ADR-0006: Local BGE-M3 embeddings instead of a hosted API

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

Retrieval quality depends on the embedding model. The project must cost nothing
to run, the documents are private, and they will often be in French as well as
English. The target server is a free ARM VM with CPU only — no GPU. The
reference guide names no specific model, so this is our decision to make and to
justify.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| OpenAI `text-embedding-3-small` | Excellent quality; the name most seen in job postings | **Paid**; documents leave our infrastructure for indexing |
| Gemini embeddings (free tier) | Excellent quality; free | On the free tier the provider may use submitted data to improve its products — unacceptable for private documents |
| `multilingual-e5-small` (local) | Very light and fast | Lower ceiling; requires `query:` / `passage:` prefixes |
| **BGE-M3 (local, 568M)** | Multilingual (100+ languages); MIT licence; 8K context; dense **and** sparse vectors, enabling hybrid search later | Slower to index on CPU; adds a few gigabytes to the image or volume |
| Qwen3-Embedding (large variants) | Tops multilingual benchmarks | Too heavy for a CPU-only free VM |

## Decision

BGE-M3, running locally, behind an `EmbeddingProvider` interface so the model is
a configuration choice rather than a hard dependency.

## Consequences

- **Documents never leave the server for indexing.** Only the retrieved
  passages are sent to the LLM for generation, and the UI says so.
- Indexing is slower on CPU, which is acceptable because it is asynchronous and
  the UI reports the real pipeline stage.
- The embedding model and its version are stored with the collection: changing
  the model means **re-indexing everything**, and we must be able to detect it.
- Chroma must be configured with cosine distance; its default is L2.
- Phase 7 compares BGE-M3 with `multilingual-e5-small` on a gold question set,
  measuring recall and latency. That comparison is the real justification.

## Revisit when

The evaluation shows another model is clearly better on our own data, or the
deployment gains a GPU, or a paid API becomes acceptable.
