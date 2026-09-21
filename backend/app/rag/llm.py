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
from typing import Any

import groq
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_groq import ChatGroq
from pydantic import SecretStr

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

# Output tokens are billed against the same daily budget as input tokens, so a
# runaway answer is taken out of every other user of the day.
MAX_OUTPUT_TOKENS = 700

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
    api_key: str, model: str, http_client: httpx.AsyncClient | None = None
) -> ChatGroq:
    """Build the provider client once, at startup.

    The shared `httpx.AsyncClient` is injected so one connection pool - and one
    TLS session - serves every question of the process, instead of a handshake
    per request.
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
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        max_retries=MAX_RETRIES,
        http_async_client=http_client,
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


def guarded(chain: Runnable[dict[str, Any], str]) -> Runnable[dict[str, Any], str]:
    """Wrap a chain so it raises only `LanguageModelError`.

    A `RunnableLambda` rather than a try/except at the call site: the result is
    still a Runnable, so it composes, streams and traces like the chain it
    wraps, and every caller gets the translation without writing it.
    """

    async def invoke(inputs: dict[str, Any]) -> str:
        try:
            reply = await chain.ainvoke(inputs)
        except groq.GroqError as error:
            raise _translate(error) from error
        if not reply.strip():
            # A 200 with nothing in it is a provider malfunction. Returning ""
            # would reach the guards in `generation` and surface as "the
            # passages do not answer that" - an honest-sounding sentence about
            # something that never happened.
            logger.warning("groq returned an empty answer")
            raise LanguageModelUnavailableError("empty answer")
        return reply

    return RunnableLambda(invoke, name="guarded_generation")


def build_answer_chain(
    prompt: ChatPromptTemplate, model: BaseChatModel
) -> Runnable[dict[str, Any], str]:
    """prompt | model | parser, the canonical LCEL chain, with our guard on top."""
    return guarded(prompt | model | StrOutputParser())
