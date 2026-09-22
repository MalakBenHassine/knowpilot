"""Prometheus metrics: what the service does, in numbers (ADR-0019).

Three families, each answering a question an operator actually asks:

- HTTP - is the API up, how fast, how often does it fail?
- Answers - how many questions are answered, refused, found nothing, or hit
  a limit? A rise in refusals after a deployment is a regression the error
  rate will never show.
- Tokens - how fast is the provider's daily budget going? The free tier has
  one, and running out of it is an outage that looks like success until 4 pm.

Rules, because metrics are an API too:

- Label values are bounded sets: a route TEMPLATE, never a raw path; an
  outcome from a fixed list; never an owner, a question or a document id. A
  label per user is one time series per user, and a Prometheus that falls
  over under cardinality is the classic self-inflicted outage.
- Nothing personal is exported. Counts, durations and token totals - never
  the text of a question or an answer.
- Names follow the Prometheus conventions: a `knowpilot_` prefix, `_total`
  for counters, base units (`_seconds`).
"""

from __future__ import annotations

import time
from typing import Any, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import ChatGeneration, LLMResult
from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_REQUESTS = Counter(
    "knowpilot_http_requests_total",
    "HTTP requests, by route template and status code.",
    ["method", "route", "status"],
)

# Time to the response HEADERS, not to the last byte: a streamed answer stays
# open for seconds by design, and counting that as latency would bury every
# slow endpoint under the chat stream.
HTTP_LATENCY = Histogram(
    "knowpilot_http_request_duration_seconds",
    "Time until the response starts, by route template.",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

Outcome = Literal[
    "answered",  # grounded, with citations
    "refused",  # the model was asked and declined (or was not grounded)
    "nothing_found",  # no passage close enough: the model was never called
    "busy",  # the provider's own limit (503)
    "unavailable",  # the provider failed (503)
]

QUESTIONS = Counter(
    "knowpilot_questions_total",
    "Questions, by route and outcome.",
    ["route", "outcome"],
)

# Answers carrying the "your documents never mention X" notice (ADR-0018).
# Its share of answers is the measure of how often users ask about things
# their documents only cover by category.
NOT_IN_DOCUMENTS = Counter(
    "knowpilot_answers_with_unnamed_subject_total",
    "Grounded answers shown with a notice that a subject is named in no passage.",
)

RETRIEVED_PASSAGES = Histogram(
    "knowpilot_retrieved_passages",
    "Passages admitted by retrieval per question.",
    buckets=(0, 1, 2, 3, 4, 6, 8, 12, 20),
)

GENERATION_LATENCY = Histogram(
    "knowpilot_generation_duration_seconds",
    "Time from calling the model to the final verdict, by route.",
    ["route"],
    buckets=(0.5, 1, 2, 3, 5, 8, 13, 20, 30),
)

LIMITED = Counter(
    "knowpilot_requests_limited_total",
    "Requests refused by our own limits, before any work.",
    ["limit"],  # chat_rate, upload_rate, user_quota, service_quota
)

TOKENS = Counter(
    "knowpilot_llm_tokens_total",
    "Tokens reported by the provider, by model and direction.",
    ["model", "direction"],  # direction: input, output
)


class TokenMetrics(BaseCallbackHandler):
    """Counts the tokens of every chat model call it is attached to.

    A LangChain callback, attached to the model itself, so every call is
    counted - the answering chain, the subject check, streamed or not - with
    no code at the call sites. Groq reports usage on the final chunk of a
    stream, and LangChain aggregates it into the message `on_llm_end` sees.

    Stateless apart from the global counters, so one instance is shared by
    every concurrent request.
    """

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                # The model that ANSWERED, as the provider named it: the
                # answering model and the check model are counted apart.
                model = str(message.response_metadata.get("model_name", "unknown"))
                TOKENS.labels(model=model, direction="input").inc(usage.get("input_tokens", 0))
                TOKENS.labels(model=model, direction="output").inc(usage.get("output_tokens", 0))


class HttpMetricsMiddleware:
    """Counts requests and measures latency, labelled by route TEMPLATE.

    A pure ASGI middleware rather than Starlette's BaseHTTPMiddleware, which
    wraps the response body and is known to interfere with streaming - the
    one thing the chat stream cannot afford.

    The route is read after the router has run, from the matched route FastAPI
    records in the scope: a TEMPLATE - "/api/documents/{document_id}", one
    series for every document instead of one each (see _route). A request no
    route matched is labelled "unmatched", for the same reason.
    """

    def __init__(self, app: ASGIApp, *, excluded: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.excluded = excluded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.excluded:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500  # an exception before the response starts is a 500

        async def observed(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                HTTP_LATENCY.labels(method=scope["method"], route=_route(scope)).observe(
                    time.perf_counter() - started
                )
            await send(message)

        try:
            await self.app(scope, receive, observed)
        finally:
            HTTP_REQUESTS.labels(
                method=scope["method"], route=_route(scope), status=str(status)
            ).inc()


def _route(scope: Scope) -> str:
    """The full route template of a request: "/api/documents/{document_id}".

    FastAPI 0.141 dispatches included routers lazily: the route in the scope
    knows its own path ("/documents/{document_id}") but not the prefix it was
    included under ("/api"), and the full template lives only in a private
    FastAPI object. Found by reading the real /metrics after the tests passed
    on an app with no prefix.

    So the prefix is recovered from public values: the template filled with
    the request's path parameters is the tail of the real path, and whatever
    precedes it is the prefix. It stays a bounded label - a route only
    matches under its real prefix - and when the tail cannot be rebuilt, the
    route's own path is still a template, never a raw path.
    """
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(template, str):
        return "unmatched"
    try:
        concrete = template.format(**scope.get("path_params", {}))
    except (KeyError, IndexError, ValueError):
        return template
    path = str(scope["path"])
    if concrete and path.endswith(concrete):
        return path[: len(path) - len(concrete)] + template
    return template
