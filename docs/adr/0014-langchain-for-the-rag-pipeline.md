# ADR-0014: LangChain for the RAG pipeline, on our own table and our own guards

- **Status:** Accepted
- **Date:** 2026-09-21
- **Amends:** [ADR-0012](0012-hosted-llm-behind-an-interface.md) — the
  `LanguageModel` protocol is replaced by LangChain's `BaseChatModel`; the
  provider choice, the model and the threat model are unchanged.

## Context

The pipeline was first written by hand behind six small protocols (loader,
embeddings, store, language model...). It worked - 11/11 on the evaluation
harness - but every piece was bespoke. The project brief names LangChain, and
it is the framework most teams building on LLMs use: its interfaces
(`BaseLoader`, `Embeddings`, `VectorStore`, `BaseRetriever`, `BaseChatModel`,
`Runnable`) are the shared vocabulary a new colleague already knows.

The question was never "LangChain or not" but **where** it goes, because two
properties of the hand-written version are not negotiable: a passage of one
user must never reach another user's prompt, and a question with no evidence
must never reach the model.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| A. LangChain end to end: default `PGVector` tables, `as_retriever()`, a `retriever \| prompt \| llm` chain | The tutorial shape; least code | Its own tables: the owner becomes a JSON metadata field, the foreign key and `ON DELETE CASCADE` are lost, the schema escapes Alembic. `as_retriever()` has an **optional** filter - a forgotten one searches every tenant. Nowhere to put "skip the model when nothing was found" or the quota reservation |
| **B. LangChain components on our foundations** | Standard interfaces everywhere; our schema, isolation and guards unchanged | More wiring than A; a custom retriever and loader to maintain |
| C. LangChain for the LLM call only | Smallest change | Cosmetic: one import in one file |

## Decision

**Option B.** Each stage of the pipeline is a standard LangChain component:

| Stage | Component |
| --- | --- |
| Load | `UploadedFileLoader(BaseLoader)` - our parser, limits and OCR fallback behind the standard interface; one `Document` per page with `page_number`, `total_pages`, `source` |
| Split | `RecursiveCharacterTextSplitter` (600 / 100, `keep_separator="end"`), `split_documents` keeps page metadata |
| Embed | `HuggingFaceEmbeddings` (BGE-M3, normalised); `DeterministicFakeEmbedding` in tests |
| Store | `PGVectorStore` from `langchain-postgres`, **pointed at the Alembic-owned `document_chunks` table** with an explicit column mapping and no JSON catch-all column |
| Retrieve | `OwnerScopedRetriever(BaseRetriever)` - `owner_id` is a **required field**, so a retriever without a tenant cannot be constructed; applies the distance ceiling |
| Prompt | `ChatPromptTemplate` (system + human); passages and question are values, never re-parsed as template syntax |
| Generate | LCEL `prompt \| ChatGroq \| StrOutputParser`, wrapped in a `RunnableLambda` that translates provider errors into ours |

What stays plain Python, deliberately: the three guards (no passage → no
call; refusal token; citation validation) and the quota reservation. They sit
**between** two runnables in the route, because a policy that decides whether
to call a paid model is clearer as code than as a branch inside a chain.

Supporting decisions:

- **Deterministic chunk ids** (`uuid5(document_id, chunk_index)`) plus a
  delete-by-owner-and-document before insert: a retried job converges, and a
  shorter re-cut leaves no stale chunk.
- **`filename` denormalised onto chunks** (migration 0002), so a retrieved
  `Document` carries everything a citation needs without a join.
- **LangSmith tracing fails closed.** A trace contains the retrieved passages -
  private documents. The API and the worker refuse to start with
  `LANGSMITH_TRACING` on unless `KP_LANGSMITH_TRACING_ALLOWED=true`.
- **Only the packages needed:** `langchain-core`, `-text-splitters`,
  `-huggingface`, `-groq`, `-postgres`. Not the `langchain` meta-package (it
  pulls LangGraph, unused) nor `langchain-community`.

## Consequences

- The evaluation harness scores **11/11 after the migration**, the same as
  before, on the same fixtures - including the diluted-chunk case the chunk
  size was tuned on and every injection and isolation case.
- Tests use LangChain's own fakes (`FakeListChatModel`,
  `DeterministicFakeEmbedding`), so the real prompt, chain and parser run in
  every test. The chat API tests no longer need PostgreSQL and now run in CI;
  isolation is proven separately against the real `PGVectorStore`.
- Testing through the real `ChatGroq` surfaced one surprise: it sends
  `temperature=1e-8` when asked for `0`.
- **Costs:**
  - `langchain-postgres` 0.0.x pins `pgvector<0.4`; we moved from 0.5 to 0.3.6
    and added a scoped mypy override (that release has no type hints).
  - `PGVectorStore` commits **each row separately**, so writing chunks is no
    longer atomic with the `ready` status. Mitigated by ordering (status
    written last) and idempotent retries; a crash mid-write leaves a document
    `processing`, never `ready` with half its passages. Chunks of a
    re-indexing document are briefly searchable while it is `processing`.
  - Retrying 429s is now the Groq SDK's policy: on an exhausted daily budget
    it spends one extra refused request before we surface the delay.
  - `langchain-core` brings `langsmith` as a dependency even though tracing is off.

## Revisit when

- `langchain-postgres` reaches 1.0 or lifts the `pgvector` pin.
- Answers need multi-step reasoning or tools: move the orchestration to
  LangGraph - and re-open ADR-0012's threat model first, because tools turn a
  poisoned document into an action.
- Documents become shareable between users: the owner filter becomes an
  access-control query, and the retriever is where it goes.
