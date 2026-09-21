# ADR-0015: Hybrid retrieval - full-text search beside the embeddings

- **Status:** Accepted
- **Date:** 2026-09-21

## Context

Embeddings capture meaning. A phone number, a contract reference or a surname
carries almost none: its vector is close to every other string of digits or
every other name. Two evaluation cases were written **before** any code, to
measure this instead of assuming it:

| Case | Vector-only (ADR-0014) |
| --- | --- |
| "A qui correspond le numero 04 72 55 18 90 ?" | 0 passages - refused |
| "Cabinet Marchand" | 0 passages - refused |

Vector-only scored **12/14**. The answer was in the database both times.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| `PGVectorStore` hybrid search (`HybridSearchConfig`) | Built into the store we use | **Measured useless** on natural questions: it uses `plainto_tsquery`, an AND of every word, so "Qui est le syndic et comment le joindre ?" requires a passage containing "comment". It also replaces the cosine distance with a fused score (which our distance guard reads) and writes the question into its config object - a race if that object is shared |
| `EnsembleRetriever` (langchain-classic) | Standard LangChain fusion | Keeps one copy of each document and drops the other side's metadata; our admission rule needs both the distance and the keyword coverage. Adds a package |
| Lower the distance ceiling | One number | Admits every weak semantic match, not only exact-value ones; the refusal cases would pay for it |
| **Our own `KeywordRetriever` + `HybridRetriever` (both `BaseRetriever`)** | Both signals kept; admission rule explicit and tested | ~150 lines to own |

## Decision

Three LangChain retrievers, each requiring an `owner_id`:

- `SemanticRetriever` - PGVectorStore, cosine distance, unchanged ceiling.
- `KeywordRetriever` - PostgreSQL full-text search on a **generated** `tsvector`
  column with a GIN index (migration 0003). The text search configuration
  `french_unaccent` folds accents before the French stemmer, because users type
  "numero" and documents say "numéro". The query is an **OR** of the question's
  lexemes; alphabetic lexemes under three letters are dropped (the French stop
  list misses "les"), numbers of any length are kept.
- `HybridRetriever` - runs both concurrently and fuses them by **reciprocal
  rank fusion** (k = 60): ranks, not scores, because a cosine distance and a
  `ts_rank` are not on comparable scales.

**Admission rule.** A passage close enough in meaning is admitted exactly as
before. A passage found only by keywords is admitted when it contains at least
`KP_MIN_KEYWORD_COVERAGE` (0.5) of the question's lexemes. Keywords can add an
exact-value passage; they never lower the bar for meaning. Both legs are
validated to serve the same owner.

## Consequences

- Evaluation **14/14** (from 12/14). The out-of-domain, the plausible-but-absent
  and both tenant cases still retrieve nothing or refuse.
- A test found the stop-word gap ("le la les ?" matched anything with "les")
  before any user did.
- The keyword configuration is part of the schema: changing the language is a
  migration, like changing the embedding model.
- The SQL is written by us, so it is reviewed like any SQL: every user value is
  a bound parameter; the one formatted value is a module constant (ruff S608
  silenced with that justification).
- `KP_KEYWORD_SEARCH_ENABLED=false` returns to vector-only retrieval - the
  switch the gain was measured with.

## Revisit when

- Documents in other languages become common: one configuration per document
  language, stored with the chunk.
- The corpus grows enough for `coverage` to need corpus statistics (IDF-like
  weighting) to tell a rare word from a common one.
