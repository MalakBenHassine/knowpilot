"""The Groq client.

No network, no API key, no quota: `httpx.MockTransport` answers every request
with whatever the test needs, including failures a real provider would only
hand us at three in the morning.

The sleep between retries is patched out in the one test that triggers it. A
suite that waits five real seconds is a suite people stop running, and a suite
people stop running protects nothing.
"""

from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from app.rag.generation import LanguageModel
from app.rag.groq import (
    MAX_OUTPUT_TOKENS,
    GroqLanguageModel,
    LanguageModelUnavailableError,
    QuotaExhaustedError,
)

KEY = "gsk_" + "x" * 52
MODEL = "openai/gpt-oss-120b"

if TYPE_CHECKING:
    # Proven by mypy, not at runtime: the class satisfies the protocol by
    # structure alone, with no import of it and no base class.
    def _satisfies_the_protocol(model: GroqLanguageModel) -> LanguageModel:
        return model


def answer(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": text}}]}


def build(handler: object, key: str = KEY) -> GroqLanguageModel:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return GroqLanguageModel(key, MODEL, httpx.AsyncClient(transport=transport))


def complete(model: GroqLanguageModel, system: str = "rules", user: str = "question") -> str:
    return anyio.run(lambda: model.complete(system, user))


# --- The happy path ------------------------------------------------------


def test_the_answer_is_returned() -> None:
    model = build(lambda request: httpx.Response(200, json=answer("Docker [1].")))

    assert complete(model) == "Docker [1]."


def test_the_request_carries_what_the_answer_depends_on() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json=answer("ok [1]"))

    complete(build(handler))
    sent = seen[0]

    assert sent["model"] == MODEL
    # Zero, so the same question over the same passages gives the same answer.
    # A RAG assistant has no use for creativity, and a deterministic one can be
    # reasoned about when it is wrong.
    assert sent["temperature"] == 0.0
    # Output tokens come out of the same daily budget as input tokens: an
    # unbounded answer is taken from every other user of the day.
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
    ]


def test_the_key_travels_as_a_bearer_token() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=answer("ok [1]"))

    complete(build(handler))

    assert seen[0] == f"Bearer {KEY}"


# --- Configuration, caught at construction -------------------------------


def test_an_empty_key_is_refused_before_the_application_starts() -> None:
    with pytest.raises(ValueError):
        build(lambda request: httpx.Response(200), key="")


def test_a_blank_key_is_refused_too() -> None:
    """The bug that cost us an afternoon.

    Sixteen pasted spaces are not an empty string, so `if not key` lets them
    through: the application boots believing it is configured and the first
    user meets a 401 about authorisation, hours after the failed paste.
    """
    with pytest.raises(ValueError):
        build(lambda request: httpx.Response(200), key="    ")


# --- 429, which is really two different failures -------------------------


def test_a_short_delay_is_waited_out_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.rag.groq.anyio.sleep", fake_sleep)
    replies = iter(
        [
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(200, json=answer("Docker [1].")),
        ]
    )

    assert complete(build(lambda request: next(replies))) == "Docker [1]."
    # Exactly what Groq asked for, not a number of our own invention.
    assert slept == [3.0]


def test_a_long_delay_is_the_daily_budget_and_is_not_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the whole retry policy rests on.

    A per-minute burst clears in seconds. The daily token budget does not clear
    until midnight, so waiting for it burns a request to learn what the header
    already said.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "3600"})

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError("waited for a daily quota")

    monkeypatch.setattr("app.rag.groq.anyio.sleep", fail_sleep)

    with pytest.raises(QuotaExhaustedError) as caught:
        complete(build(handler))

    assert calls == 1
    # Carried through so the endpoint can put it in a Retry-After header: the
    # user is told when to come back, not merely that they cannot proceed.
    assert caught.value.retry_after == 3600.0


def test_a_second_refusal_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "2"})

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("app.rag.groq.anyio.sleep", fake_sleep)

    with pytest.raises(QuotaExhaustedError):
        complete(build(handler))

    # One retry, never more: a human is waiting, and past ten seconds they have
    # already closed the tab.
    assert calls == 2


def test_a_missing_or_absurd_delay_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The header is provider input, so it is parsed like any other."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.rag.groq.anyio.sleep", fake_sleep)
    replies = iter(
        [
            httpx.Response(429, headers={"retry-after": "soon"}),
            httpx.Response(200, json=answer("ok [1]")),
        ]
    )

    complete(build(lambda request: next(replies)))

    assert slept == [5.0]


# --- Everything else the provider can do to us ---------------------------


def test_a_rejected_key_is_a_503_not_a_crash() -> None:
    model = build(lambda request: httpx.Response(401, json={"error": "invalid api key"}))

    with pytest.raises(LanguageModelUnavailableError):
        complete(model)


def test_a_server_error_is_translated() -> None:
    model = build(lambda request: httpx.Response(500, text="upstream exploded"))

    with pytest.raises(LanguageModelUnavailableError):
        complete(model)


def test_a_timeout_is_translated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    # The caller must never have to catch an httpx type to learn that no answer
    # is coming: the provider is an implementation detail of this module.
    with pytest.raises(LanguageModelUnavailableError):
        complete(build(handler))


def test_an_unreachable_host_is_translated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(LanguageModelUnavailableError):
        complete(build(handler))


def test_an_unexpected_payload_shape_is_a_failure_not_an_answer() -> None:
    model = build(lambda request: httpx.Response(200, json={"unexpected": True}))

    with pytest.raises(LanguageModelUnavailableError):
        complete(model)


def test_an_empty_answer_is_a_failure_not_a_refusal() -> None:
    """A 200 holding nothing is a malfunction, and must not look like a refusal.

    Returning "" would flow into the guards in `generation` and reach the user
    as "the passages do not answer that" - a sentence that sounds honest about
    something that never happened. The user is told the service failed.
    """
    model = build(lambda request: httpx.Response(200, json=answer("   ")))

    with pytest.raises(LanguageModelUnavailableError):
        complete(model)
