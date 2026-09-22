"""The language model: `ChatGroq`, wrapped so its failures speak our language.

Nothing above this file imports `groq` or `langchain_groq`. The generation
code receives a `Runnable` that takes the prompt variables and returns text;
which provider runs behind it is decided here and only here. Changing provider
is replacing `ChatGroq` with `ChatOpenAI` or `ChatMistralAI` in one function -
the reason LangChain's chat-model interface exists.

Two rules shape the file, and both survive the move to LangChain. Every
failure is translated into one of our own exceptions, because a route should
never have to catch `groq.RateLimitError` to know an answer is not coming.
And every wait is bounded, because an unbounded wait is a leaked resource.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing
from typing import Any, cast

import groq
import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_groq import ChatGroq
from pydantic import BaseModel, SecretStr, ValidationError

logger = logging.getLogger(__name__)

# A server that cannot accept a TCP connection in five seconds is down; waiting
# longer only holds a slot. Reading gets ten times the normal generation time,
# because "slow today" and "unreachable" are different failures.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0

# One retry, made by the Groq SDK on a 429, a 5xx or a dropped connection. On a
# per-minute burst it waits exactly the Retry-After it was given; beyond a
# minute - the daily budget talking - it ignores the header and retries after
# a short backoff, is refused again, and we surface the provider's delay. That
# costs one wasted request per exhausted day, which is cheaper than writing
# our own retry loop around a client that already has one.
#
# One, because a human is waiting: the retries multiply the worst-case latency
# (READ_TIMEOUT_SECONDS x attempts), and past ten seconds the tab is abandoned.
MAX_RETRIES = 1

# Groq sends Retry-After on a 429. Parsed like any provider-controlled input: a
# missing or absurd value falls back to a constant rather than becoming a wait
# of several hours shown to the user.
FALLBACK_RETRY_AFTER_SECONDS = 5.0

# gpt-oss REASONS before it answers, and the reasoning is drawn from the same
# output budget as the answer. Found by the first real streamed question: asked
# for a detailed answer, the model spent all 700 tokens thinking and returned
# nothing - a 503 on a perfectly answerable question, in both the streamed and
# the blocking path. Measured on that question:
#
#   effort   max_tokens   finish   answer        tokens used   reasoning
#   medium      700       length   0 chars           700       2977 chars
#   medium     1200       length   1440 chars cut   1200       3240 chars
#   low         700       stop     2060 chars        680        436 chars
#   low        1200       stop     2048 chars        670        417 chars
#
# "low": answering from passages already chosen by retrieval needs little
# deliberation - the hard part, finding the evidence, is done - and the
# reasoning shrinks sevenfold. The evaluation harness confirmed the quality.
REASONING_EFFORT = "low"

# Output tokens are billed against the same daily budget as input tokens, so a
# runaway answer is taken out of every other user of the day. 1000 rather than
# the 680 a detailed answer measured: headroom, so a long answer is not cut
# mid-sentence - and mid-citation.
MAX_OUTPUT_TOKENS = 1000

# Zero: the same question over the same passages must produce the same answer.
# Creativity is the failure mode this project exists to avoid.
TEMPERATURE = 0.0


class LanguageModelError(Exception):
    """Base class, so a caller can catch every provider failure at once."""


class QuotaExhaustedError(LanguageModelError):
    """The provider's budget is spent. Maps to 429, with the delay we were given."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"quota exhausted, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class LanguageModelUnavailableError(LanguageModelError):
    """The provider failed us. Maps to 503: the user did nothing wrong."""


def create_chat_model(
    api_key: str,
    model: str,
    http_client: httpx.AsyncClient | None = None,
    *,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> ChatGroq:
    """Build the provider client once, at startup.

    The shared `httpx.AsyncClient` is injected so one connection pool - and one
    TLS session - serves every question of the process, instead of a handshake
    per request.

    `max_tokens` is lowered for the subject check, whose whole answer is a
    short JSON list: a budget sized for a detailed answer would only be room
    for a runaway.
    """
    if not api_key.strip():
        # Fail at construction, in the lifespan, where the message is read by
        # whoever is deploying - not on the first question, in front of a user.
        raise ValueError("a Groq API key is required")
    return ChatGroq(
        model=model,
        # SecretStr: the key is masked in reprs, logs and tracebacks. A plain
        # string in a pydantic model is one `print(model)` away from a leak.
        api_key=SecretStr(api_key.strip()),
        temperature=TEMPERATURE,
        max_tokens=max_tokens,
        reasoning_effort=REASONING_EFFORT,
        timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        max_retries=MAX_RETRIES,
        http_async_client=http_client,
        # On the model rather than on each call: every call is seen - blocking,
        # streamed, answering or checking - with nothing to forget at the call
        # sites. The token metrics of ADR-0019 are one of these.
        callbacks=list(callbacks) or None,
    )


def _retry_after(error: groq.APIStatusError) -> float:
    raw = error.response.headers.get("retry-after", "")
    try:
        seconds = float(raw)
    except ValueError:
        return FALLBACK_RETRY_AFTER_SECONDS
    return seconds if seconds > 0 else FALLBACK_RETRY_AFTER_SECONDS


def _translate(error: Exception) -> LanguageModelError:
    """Map a provider exception onto ours.

    Only the status code or the exception type is logged, never the message:
    the SDK puts the request inside it, and the request carries the passages -
    the private documents of a user. A log is a file that travels.
    """
    if isinstance(error, groq.RateLimitError):
        delay = _retry_after(error)
        logger.info("groq quota exhausted, retry after %.0fs", delay)
        return QuotaExhaustedError(delay)
    if isinstance(error, groq.AuthenticationError):
        # Our configuration is wrong, not their service. Loud, because nobody
        # will find this by staring at the frontend.
        logger.error("groq rejected the api key; check KP_GROQ_API_KEY")
        return LanguageModelUnavailableError("invalid api key")
    if isinstance(error, groq.APIStatusError):
        logger.warning("groq returned %s", error.status_code)
        return LanguageModelUnavailableError(f"groq returned {error.status_code}")
    if isinstance(error, groq.APIConnectionError):
        # Includes APITimeoutError, its subclass.
        logger.warning("groq is unreachable (%s)", type(error).__name__)
        return LanguageModelUnavailableError("groq is unreachable")
    logger.warning("generation failed (%s)", type(error).__name__)
    return LanguageModelUnavailableError(type(error).__name__)


# The provider exceptions we translate. httpx errors are listed too: a stream
# that breaks halfway can surface the transport's exception rather than the
# SDK's, and it must not escape as a 500.
_PROVIDER_ERRORS = (groq.GroqError, httpx.HTTPError)


def _unavailable_if_empty() -> LanguageModelUnavailableError:
    # A 200 with nothing in it is a provider malfunction. Returning "" would
    # reach the guards in `generation` and surface as "the passages do not
    # answer that" - an honest-sounding sentence about something that never
    # happened.
    logger.warning("groq returned an empty answer")
    return LanguageModelUnavailableError("empty answer")


class GuardedChain(Runnable[dict[str, Any], str]):
    """A chain that raises only `LanguageModelError`, whether invoked or streamed.

    A Runnable subclass rather than a RunnableLambda: a lambda wrapping
    `ainvoke` cannot stream - it would wait for the whole answer and hand it
    over in one piece, silently turning `astream` into `ainvoke`. Implementing
    both methods keeps each path native: `ainvoke` makes one ordinary request,
    `astream` consumes the provider's stream token by token.

    Async only, like everything that talks to the network in this service.
    """

    def __init__(self, chain: Runnable[dict[str, Any], str]) -> None:
        self.chain = chain

    def invoke(
        self, input: dict[str, Any], config: RunnableConfig | None = None, **kwargs: Any
    ) -> str:
        raise NotImplementedError("use ainvoke or astream: generation is async-only")

    async def ainvoke(
        self, input: dict[str, Any], config: RunnableConfig | None = None, **kwargs: Any
    ) -> str:
        try:
            reply = await self.chain.ainvoke(input, config, **kwargs)
        except _PROVIDER_ERRORS as error:
            raise _translate(error) from error
        if not reply.strip():
            raise _unavailable_if_empty()
        return reply

    async def astream(
        self, input: dict[str, Any], config: RunnableConfig | None = None, **kwargs: Any | None
    ) -> AsyncIterator[str]:
        produced = False
        stream = cast("AsyncGenerator[str, None]", self.chain.astream(input, config, **kwargs))
        try:
            # Closed explicitly, so that a consumer closing THIS generator
            # closes the provider's stream too, instead of leaving it open
            # until garbage collection.
            async with aclosing(stream):
                async for chunk in stream:
                    produced = produced or bool(chunk.strip())
                    yield chunk
        except _PROVIDER_ERRORS as error:
            raise _translate(error) from error
        if not produced:
            raise _unavailable_if_empty()


def build_answer_chain(prompt: ChatPromptTemplate, model: BaseChatModel) -> GuardedChain:
    """prompt | model | parser, the canonical LCEL chain, with our guard on top."""
    return GuardedChain(prompt | model | StrOutputParser())


def build_advisory_chain[SchemaT: BaseModel](
    prompt: ChatPromptTemplate, model: BaseChatModel, schema: type[SchemaT]
) -> Runnable[dict[str, Any], SchemaT | None]:
    """prompt | model.with_structured_output(schema), failing OPEN to None.

    For calls that improve an answer without deciding whether there is one -
    the subject check of ADR-0018. If the provider refuses, times out or
    returns JSON that does not fit the schema, the question is still answered
    exactly as it was before the check existed. An advisory step that could
    take answering down would turn an improvement into a new outage.

    `with_structured_output` is LangChain's standard way to get typed output:
    the schema is sent to the provider as a JSON schema and the reply is
    validated into the pydantic model, so no reply is ever parsed by hand.
    The fallback is `with_fallbacks`, restricted to the failures listed below:
    a bug of ours - a TypeError, a KeyError - still raises, loudly.
    """

    def unavailable(inputs: dict[str, Any]) -> SchemaT | None:
        # The type only: the message of a provider error carries the request.
        logger.warning(
            "advisory call failed (%s); answering without it", type(inputs["error"]).__name__
        )
        return None

    structured = cast(
        "Runnable[dict[str, Any], SchemaT | None]",
        prompt | model.with_structured_output(schema, method="json_schema"),
    )
    fallback: Runnable[dict[str, Any], SchemaT | None] = RunnableLambda(unavailable)
    return structured.with_fallbacks(
        [fallback],
        exceptions_to_handle=(*_PROVIDER_ERRORS, OutputParserException, ValidationError),
        # Hands the exception to the fallback, which logs what went wrong.
        exception_key="error",
    )
