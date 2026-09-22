"""Prometheus metrics (ADR-0019).

Counters are process-global, so every test reads a DELTA: the value after
minus the value before. That keeps the tests independent of their order and
of whatever the rest of the suite already counted.
"""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import httpx
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

from app.core.metrics import HttpMetricsMiddleware, TokenMetrics
from app.main import app as knowpilot
from app.rag.generation import ANSWER_PROMPT
from app.rag.llm import build_answer_chain, create_chat_model
from tests.test_llm import INPUTS, KEY, Recorder, completion

pytestmark = pytest.mark.anyio


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def delta(name: str, action: Callable[[], object], **labels: str) -> float:
    before = value(name, **labels)
    action()
    return value(name, **labels) - before


# --- Tokens: a real ChatGroq against a fake server -----------------------------


def ask_groq(server: Recorder, streamed: bool = False) -> None:
    async def call() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            model = create_chat_model(
                KEY, "openai/gpt-oss-120b", client, callbacks=[TokenMetrics()]
            )
            chain = build_answer_chain(ANSWER_PROMPT, model)
            if streamed:
                async for _ in chain.astream(INPUTS):
                    pass
            else:
                await chain.ainvoke(INPUTS)

    anyio.run(call)


TOKENS = "knowpilot_llm_tokens_total"
MODEL = {"model": "openai/gpt-oss-120b"}


def test_the_tokens_of_a_call_are_counted_by_model_and_direction() -> None:
    server = Recorder(completion("Docker [1]."))  # usage: 10 in, 5 out

    spent_in = delta(TOKENS, lambda: ask_groq(server), direction="input", **MODEL)

    assert spent_in == 10


def test_a_streamed_answer_is_counted_too() -> None:
    """Groq reports usage on the LAST chunk of a stream (x_groq.usage)."""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"content": "Docker [1]."}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "x_groq": {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        },
    ]
    body = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "openai/gpt-oss-120b",
                    **chunk,
                }
            )
            + "\n\n"
            for chunk in chunks
        )
        + "data: [DONE]\n\n"
    )
    server = Recorder(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
    )

    spent_out = delta(TOKENS, lambda: ask_groq(server, streamed=True), direction="output", **MODEL)

    assert spent_out == 3


# --- HTTP: route templates, never raw paths ------------------------------------


def tiny_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    # Shaped like the real application: a router with its own prefix,
    # included under another. The first version of the label lost the outer
    # one - found on the real /metrics, after these tests passed without it.
    router = APIRouter(prefix="/documents")

    @router.get("/{document_id}")
    def document(document_id: str) -> dict[str, str]:
        return {"id": document_id}

    app.include_router(router, prefix="/api")

    @app.get("/metrics")
    def scrape() -> dict[str, str]:
        return {}

    app.add_middleware(HttpMetricsMiddleware, excluded=frozenset({"/metrics"}))
    return app


async def get(app: FastAPI, path: str) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        return (await http.get(path)).status_code


REQUESTS = "knowpilot_http_requests_total"


async def test_requests_are_labelled_by_route_template() -> None:
    """One series for every item, not one series per item.

    A label per document id is a time series per document - the classic way
    to take down the Prometheus that was supposed to watch the service.
    """
    app = tiny_app()
    labels = {"method": "GET", "route": "/items/{item_id}", "status": "200"}
    before = value(REQUESTS, **labels)

    for item_id in ("a", "b", "c"):
        await get(app, f"/items/{item_id}")

    assert value(REQUESTS, **labels) - before == 3
    assert value(REQUESTS, method="GET", route="/items/a", status="200") == 0


async def test_a_route_included_under_a_prefix_keeps_the_whole_template() -> None:
    app = tiny_app()
    labels = {"method": "GET", "route": "/api/documents/{document_id}", "status": "200"}
    before = value(REQUESTS, **labels)

    await get(app, "/api/documents/8c1e2d4a")
    await get(app, "/api/documents/0f9b7c3e")

    assert value(REQUESTS, **labels) - before == 2


async def test_an_unknown_path_is_one_bounded_label() -> None:
    app = tiny_app()
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = value(REQUESTS, **labels)

    await get(app, "/wp-admin/../../etc/passwd")
    await get(app, "/random-scanner-path")

    # A scanner trying a thousand paths adds nothing but this one counter.
    assert value(REQUESTS, **labels) - before == 2


async def test_the_scrape_itself_is_not_counted() -> None:
    app = tiny_app()
    before = value(REQUESTS, method="GET", route="/metrics", status="200")

    await get(app, "/metrics")

    assert value(REQUESTS, method="GET", route="/metrics", status="200") == before


# --- The real endpoint ----------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=knowpilot), base_url="http://test") as http:
        yield http


async def test_the_endpoint_serves_the_prometheus_format(client: AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "knowpilot_questions_total" in response.text
    assert "knowpilot_llm_tokens_total" in response.text


async def test_the_endpoint_lives_outside_api(client: AsyncClient) -> None:
    """The proxy forwards /api only: /metrics must never be under it."""
    assert (await client.get("/api/metrics")).status_code == 404
