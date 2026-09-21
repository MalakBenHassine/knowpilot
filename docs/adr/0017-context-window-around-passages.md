# ADR-0017: Read each passage with the chunk before it

- **Status:** Accepted
- **Date:** 2026-09-21

## Context

A manual test on a real insurance contract, on 21 September 2026. Asked
"Combien je paie chaque mois", the assistant answered **59 €**. The right
answer was **57 €**, and then 59 € from 1 October: an amendment signed on
15 September raises the premium from that date.

Retrieval returned the chunk that begins "La cotisation mensuelle est portée
à 59 euros". The date of application was in the chunk before it, and
retrieval did not return that chunk. The model was not wrong about what it
read. It was never shown the condition.

Contracts, leases and policies put the condition first and the value after,
so a chunk boundary between the two is common.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| Bigger chunks | One setting | Every vector averages more subjects, and precise questions get worse. The chunk size was chosen against the evaluation (ADR-0014) |
| More passages (`top_k`) | One setting | Adds passages that were ranked lower, not the text next to the one that matched. The cost grows with every question |
| Contextual chunk headers (an LLM summary written before each chunk at ingestion) | Improves retrieval too | One model call per chunk at upload, on a free daily budget; must be re-run when the prompt changes |
| `ParentDocumentRetriever` (LangChain) | The standard answer: search on small chunks, read the larger parent | Needs a separate docstore of parents, and our chunks already live in PostgreSQL |
| **Previous-chunk window (`ContextWindowRetriever`)** | Same idea as the parent retriever, built on the rows we already have; one SQL query per question | Costs input tokens; we own about 80 lines |

## Decision

`ContextWindowRetriever` is a LangChain `BaseRetriever` that wraps the hybrid
retriever and runs **last**. The chunks it adds are used for reading only;
they never take part in choosing the passages. For each passage it selects,
it adds the chunk just before it:

- The chunk must be in the **same document** and on the **same page**. A
  citation that says page 4 must not rest on text from page 3.
- The query filters on the **same owner** again. The passages already belong
  to that owner, but a query that reads chunks by identifier does not rely on
  its caller for tenancy.
- The text is joined **without repeating the splitter's overlap**. A sentence
  read twice looks like two statements. An overlap shorter than 10 characters
  is treated as a coincidence and ignored, because merging on it would delete
  real text.
- If the previous chunk was already retrieved, nothing is added: the model
  already reads it as its own passage.

The cited page and chunk index stay those of the passage that was found.
`context_from` records the chunk that was added.

`KP_PASSAGE_CONTEXT_ENABLED=false` returns to bare chunks.

## Consequences

Measured on the real library, with the question from the manual test, two
runs per setting:

| Window | Answers containing 57 € | Input tokens |
| --- | --- | --- |
| off | 0/2 (both said "59 €") | 1 394 |
| on | 2/2 ("57 €, 59 € from 1 October 2026") | 1 725 (+24 %) |

- The evaluation passes **21/21** with the window on. The mean is 1 096 input
  tokens per answered question and the maximum is 1 682. This fits the
  budget derivation in `config.py`, which assumes about 2 300 tokens per
  question including output.
- The evaluation's fixture corpus does **not** reproduce the ranking of the
  real library: there, both chunks of the amendment are retrieved anyway. We
  kept the case `a-value-read-with-its-date` as a regression check. The
  deterministic proof is the database test
  `test_a_passage_is_read_with_the_chunk_before_it`. The real-library
  measurement above is the evidence that the window fixes the defect.
- A condition written **two** chunks earlier is still lost. We have not yet
  seen a real document where this happens.

## Revisit when

- A real document places a condition two chunks away from its value. Widen
  the window to N chunks, or group chunks by section heading.
- Input tokens start to bind on the daily budget. Add the previous chunk only
  when it ends mid-paragraph.
