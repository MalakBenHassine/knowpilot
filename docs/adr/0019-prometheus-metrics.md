# ADR-0019: Prometheus metrics, on an endpoint the internet cannot reach

- **Status:** Accepted
- **Date:** 2026-09-22

## Context

The service had logs and nothing else. Three failures seen during manual
testing were invisible without reading log lines one by one:

- the provider's daily token budget ran out in the evening;
- refusals varied from one run to the next;
- limits were hit.

In production, a rise in refusals after a deployment is a quality regression
that no error rate shows. An empty token budget is an outage that looks like
success until the moment it happens.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| `prometheus-fastapi-instrumentator` | A few lines to set up | Another dependency, and its HTTP middleware sees nothing of our domain: answers, refusals and tokens would still have to be written by hand |
| OpenTelemetry (traces + metrics) | The standard for distributed tracing | Needs a collector to run, which is heavy for one API and one worker. Traces of a RAG request also carry its text, the privacy problem LangSmith already raised (ADR-0014) |
| **`prometheus-client` + our own ASGI middleware + a LangChain callback** | The official library; domain metrics are first-class; about 150 lines we own | We own the middleware |

## Decision

A `/metrics` endpoint in the Prometheus text format, exposing three families
of metrics:

| Metric | Answers the question |
| --- | --- |
| `knowpilot_http_requests_total{method,route,status}` | Is the API failing? |
| `knowpilot_http_request_duration_seconds{method,route}` | Is it slow? (time until the response **starts**, so a stream of several seconds does not hide every slow endpoint) |
| `knowpilot_questions_total{route,outcome}` | Answered, refused, nothing_found, busy, unavailable |
| `knowpilot_answers_with_unnamed_subject_total` | How often the notice of ADR-0018 is shown |
| `knowpilot_retrieved_passages` | How many passages retrieval admits per question |
| `knowpilot_generation_duration_seconds{route}` | How long the model takes |
| `knowpilot_requests_limited_total{limit}` | How often each limit refuses: chat_rate, upload_rate, user_quota, service_quota |
| `knowpilot_llm_tokens_total{model,direction}` | How fast each model's daily token budget is spent |

It also exposes the default process collector, including resident memory
(2.1 GB with the embedding model loaded).

**Implementation choices:**

- **Tokens come from a LangChain callback (`on_llm_end`) attached to the
  model.** Every call is counted, including the answering model and the
  subject check, streamed or not, with no code at the call sites. Groq
  reports usage on the last chunk of a stream, and LangChain adds it to the
  message the callback sees.
- **The HTTP middleware is pure ASGI.** Starlette's `BaseHTTPMiddleware`
  wraps the response body and is known to interfere with streaming.
- **Labels are bounded sets.** The route label is a **template**, never a
  raw path. A path no route matches is labelled `unmatched`. No label ever
  holds an owner, a question or a document id. The cardinality and privacy
  rules are both tested.
- **`/metrics` is outside `/api`.** The reverse proxy forwards only `/api`
  and serves the SPA for every other path, so the endpoint answers only on
  the internal network, where Prometheus runs. The scrape and the health
  probes are not counted.

## Consequences

- Reading the real `/metrics` after the tests passed revealed that FastAPI
  0.141 no longer puts the `include_router` prefix in the matched route: the
  label read `/documents` instead of `/api/documents`. The prefix is now
  recovered from public values of the ASGI scope, and a test uses an app
  shaped like the real one: a prefixed router included under another prefix.
- Counters live in each process. With several uvicorn workers,
  `prometheus-client` needs its multiprocess mode, and that has to be
  configured before switching to multiple workers.
- The ingestion worker exports nothing yet: it is a separate process with no
  HTTP server.

## Revisit when

- There are several API workers: configure `PROMETHEUS_MULTIPROC_DIR`.
- Ingestion needs to be watched: have the worker expose its own port, with
  the time per document and failures by reason.
- A Grafana dashboard exists: commit it as JSON next to this ADR.
