"""The provider boundary: a REAL ChatGroq, talking to a fake server.

`httpx.MockTransport` answers in place of api.groq.com, injected through the
`http_async_client` ChatGroq accepts. So everything between our chain and the
network is the production code - LangChain's message conversion, the Groq
SDK's request building and retry policy - and only the server is simulated.
No key, no quota, no network.
"""

import json
from collections.abc import Callable

import anyio
import httpx
import pytest

from app.rag.generation import ANSWER_PROMPT
from app.rag.llm import (
    MAX_OUTPUT_TOKENS,
    REASONING_EFFORT,
    LanguageModelUnavailableError,
    QuotaExhaustedError,
    build_answer_chain,
    create_chat_model,
)

KEY = "gsk_test_not_a_real_key"
INPUTS = {
    "passages": "[1] (document A) Docker est utilise.",
    "question": "Quels outils ?",
    "today": "2026-09-21",
    "unnamed": "",
}

Handler = Callable[[httpx.Request], httpx.Response]


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def run(handler: Handler) -> str:
    async def call() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            chain = build_answer_chain(
                ANSWER_PROMPT, create_chat_model(KEY, "openai/gpt-oss-120b", client)
            )
            return await chain.ainvoke(INPUTS)

    return anyio.run(call)


class Recorder:
    """A fake server that replays responses in order and keeps the requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[min(len(self.requests), len(self.responses)) - 1]


# --- The happy path ------------------------------------------------------


def test_the_answer_is_returned() -> None:
    assert run(Recorder(completion("Docker [1]."))) == "Docker [1]."


def test_the_request_carries_what_the_answer_depends_on() -> None:
    server = Recorder(completion("Docker [1]."))

    run(server)

    body = json.loads(server.requests[0].content)
    assert body["model"] == "openai/gpt-oss-120b"
    # Deterministic, and bounded: a runaway answer spends everybody's budget.
    # Not exactly 0: langchain-groq sends 1e-8 when asked for 0, because the
    # Groq API has treated a literal 0 as "unset". Found by this very test -
    # which is the argument for testing through the real client rather than
    # asserting on the arguments we passed to it.
    assert body["temperature"] <= 1e-6
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS
    # gpt-oss reasons inside the output budget; low effort keeps it from
    # spending the whole budget thinking and answering nothing.
    assert body["reasoning_effort"] == REASONING_EFFORT
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert "Quels outils ?" in body["messages"][1]["content"]


def test_the_key_travels_as_a_bearer_token() -> None:
    server = Recorder(completion("Docker [1]."))

    run(server)

    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_an_empty_key_is_refused_before_the_application_starts() -> None:
    with pytest.raises(ValueError):
        create_chat_model("", "openai/gpt-oss-120b")


def test_a_blank_key_is_refused_too() -> None:
    # What a failed paste into .env actually produces.
    with pytest.raises(ValueError):
        create_chat_model("   \n", "openai/gpt-oss-120b")


def test_the_key_never_appears_in_a_repr() -> None:
    # SecretStr: a model printed in a log or a traceback shows asterisks.
    assert KEY not in repr(create_chat_model(KEY, "openai/gpt-oss-120b"))


# --- Rate limits and quota ----------------------------------------------


def rate_limited(retry_after: str, milliseconds: str | None = None) -> httpx.Response:
    headers = {"retry-after": retry_after}
    if milliseconds is not None:
        headers["retry-after-ms"] = milliseconds
    return httpx.Response(429, headers=headers, json={"error": {"message": "rate limited"}})


def test_a_short_burst_is_retried_once_and_answered() -> None:
    # retry-after-ms keeps the test fast; the SDK reads it before retry-after.
    server = Recorder(rate_limited("1", milliseconds="10"), completion("Docker [1]."))

    assert run(server) == "Docker [1]."
    assert len(server.requests) == 2


def test_a_second_refusal_gives_up_with_the_providers_delay() -> None:
    server = Recorder(rate_limited("1", milliseconds="10"), rate_limited("7", milliseconds="10"))

    with pytest.raises(QuotaExhaustedError) as caught:
        run(server)

    # One retry, never more: a human is waiting.
    assert len(server.requests) == 2
    assert caught.value.retry_after == 7


def test_a_missing_or_absurd_delay_falls_back() -> None:
    server = Recorder(rate_limited("soon", milliseconds="10"))

    with pytest.raises(QuotaExhaustedError) as caught:
        run(server)

    assert caught.value.retry_after == 5.0


# --- Failures become our exceptions -------------------------------------


def test_a_rejected_key_is_unavailable_not_a_crash() -> None:
    server = Recorder(httpx.Response(401, json={"error": {"message": "invalid key"}}))

    with pytest.raises(LanguageModelUnavailableError):
        run(server)
    # A wrong key does not become right by asking again.
    assert len(server.requests) == 1


def test_a_server_error_is_translated_after_one_retry() -> None:
    server = Recorder(httpx.Response(503, headers={"retry-after-ms": "10"}, json={}))

    with pytest.raises(LanguageModelUnavailableError):
        run(server)
    assert len(server.requests) == 2


def test_an_unreachable_host_is_translated() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LanguageModelUnavailableError):
        run(refuse)


def test_an_empty_answer_is_a_failure_not_a_refusal() -> None:
    # Surfacing "" would reach the guards and read as "the passages do not
    # answer that" - an honest-sounding sentence about something that never
    # happened.
    with pytest.raises(LanguageModelUnavailableError):
        run(Recorder(completion("   ")))


def test_the_passages_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    server = Recorder(httpx.Response(500, headers={"retry-after-ms": "10"}, json={}))

    with caplog.at_level("DEBUG", logger="app"), pytest.raises(LanguageModelUnavailableError):
        run(server)

    # The request carries a user's private document. Only status codes and
    # exception types may be logged by our code.
    assert "Docker est utilise" not in caplog.text


# --- Streaming through the real ChatGroq ------------------------------------


def sse(*contents: str) -> httpx.Response:
    """A streamed completion, in the OpenAI-compatible format Groq sends."""
    lines = []
    for content in contents:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(lines).encode()
    )


def stream(handler: Handler) -> list[str]:
    async def call() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            chain = build_answer_chain(
                ANSWER_PROMPT, create_chat_model(KEY, "openai/gpt-oss-120b", client)
            )
            return [chunk async for chunk in chain.astream(INPUTS)]

    return anyio.run(call)


def test_the_answer_arrives_in_pieces() -> None:
    server = Recorder(sse("Docker", " est", " utilise [1]."))

    chunks = stream(server)

    assert "".join(chunks) == "Docker est utilise [1]."
    assert len(chunks) >= 3
    # The streaming API, not a blocking request replayed in one piece.
    assert json.loads(server.requests[0].content)["stream"] is True


def test_a_streamed_quota_refusal_is_translated() -> None:
    server = Recorder(rate_limited("1", milliseconds="10"), rate_limited("9", milliseconds="10"))

    with pytest.raises(QuotaExhaustedError) as caught:
        stream(server)

    assert caught.value.retry_after == 9


def test_a_stream_that_says_nothing_is_a_failure() -> None:
    with pytest.raises(LanguageModelUnavailableError):
        stream(Recorder(sse("", "  ")))


def test_the_synchronous_path_is_refused() -> None:
    chain = build_answer_chain(ANSWER_PROMPT, create_chat_model(KEY, "openai/gpt-oss-120b"))

    with pytest.raises(NotImplementedError):
        chain.invoke(INPUTS)
