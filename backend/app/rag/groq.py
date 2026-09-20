"""The GroqCloud implementation of the LanguageModel protocol.

Nothing above this file knows that Groq exists: `answer_question` takes a
`LanguageModel`, and this class happens to satisfy it. Swapping providers means
changing one line in the lifespan, not touching the generation logic or a
single one of its fourteen tests.

Two rules shape the whole file. Every failure is translated into one of our own
exceptions, because a caller should never have to catch `httpx.ReadTimeout` to
know that an answer is not coming. And every wait is bounded, because an
unbounded wait is a leaked resource.
"""

from __future__ import annotations

import logging

import anyio
import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.groq.com/openai/v1/chat/completions"

# A server that cannot accept a TCP connection in five seconds is down; waiting
# longer only holds a slot. Reading is given ten times the normal generation
# time instead, because "slow today" and "unreachable" are different failures
# and deserve different limits.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0

# Groq returns 429 for two unrelated reasons. A per-minute burst clears in
# seconds and is worth one retry; the daily token budget does not clear until
# midnight, and retrying it spends a request to learn what we already know.
# `Retry-After` is what tells the two apart.
MAX_RETRY_AFTER_SECONDS = 30.0
FALLBACK_RETRY_AFTER_SECONDS = 5.0

# Only one retry: a human is waiting. Exponential backoff belongs to background
# work, where nobody is watching the screen. Past roughly ten seconds the user
# has given up, and we would be generating an answer for an abandoned tab.
MAX_ATTEMPTS = 2

# Output tokens are billed against the same daily budget as input tokens, so a
# runaway answer is taken out of every other user of the day.
MAX_OUTPUT_TOKENS = 700

# Zero, not the default: the same question over the same passages must produce
# the same answer. Creativity is the failure mode this project exists to avoid,
# and a deterministic model is also one we can reason about when it is wrong.
TEMPERATURE = 0.0


class LanguageModelError(Exception):
    """Base class, so a caller can catch every provider failure at once."""


class QuotaExhaustedError(LanguageModelError):
    """The budget is spent. Maps to 429, carrying the delay we were given."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"quota exhausted, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class LanguageModelUnavailableError(LanguageModelError):
    """The provider failed us. Maps to 503: the user did nothing wrong."""


def _retry_after(response: httpx.Response) -> float:
    """Read the delay Groq asks for, defensively.

    The header is provider-controlled input, so it is parsed like any other
    untrusted value: a missing, malformed or absurd number falls back to a
    constant rather than becoming a sleep of several hours.
    """
    raw = response.headers.get("retry-after", "")
    try:
        seconds = float(raw)
    except ValueError:
        return FALLBACK_RETRY_AFTER_SECONDS
    return seconds if seconds > 0 else FALLBACK_RETRY_AFTER_SECONDS


def _content(response: httpx.Response) -> str:
    """Pull the answer out, treating the response as untrusted input.

    A 200 with a shape we did not expect is a provider malfunction, so it
    becomes a 503. Returning an empty string instead would reach the guards in
    `generation` and surface to the user as "the passages do not answer that" -
    an honest-sounding sentence about something that never happened.
    """
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, LookupError, TypeError) as exc:
        logger.warning("groq returned an unexpected payload shape")
        raise LanguageModelUnavailableError("unexpected response shape") from exc

    if not isinstance(content, str) or not content.strip():
        logger.warning("groq returned an empty answer")
        raise LanguageModelUnavailableError("empty answer")

    return content


class GroqLanguageModel:
    """Satisfies `LanguageModel` by structure, without inheriting anything."""

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient) -> None:
        if not api_key.strip():
            # Fail at construction, in the lifespan, where the message is read
            # by whoever is deploying - not on the first question, hours later,
            # in front of a user.
            raise ValueError("a Groq API key is required")
        self._api_key = api_key.strip()
        self._model = model
        # Injected rather than created here: one client for the whole process
        # reuses connections and TLS handshakes, and the tests replace it with
        # a transport that never touches the network.
        self._client = client

    async def complete(self, system: str, user: str) -> str:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await self._post(system, user)

            if response.status_code == 429:
                delay = _retry_after(response)
                # A delay longer than a minute window is the daily budget
                # talking. No amount of patience fixes that before midnight.
                if delay > MAX_RETRY_AFTER_SECONDS or attempt == MAX_ATTEMPTS:
                    logger.info("groq quota exhausted, retry after %.0fs", delay)
                    raise QuotaExhaustedError(delay)
                logger.info("groq rate limited, waiting %.0fs before one retry", delay)
                # `anyio.sleep`, never `time.sleep`: the latter freezes the
                # event loop, so this one slow request would stop serving every
                # other user, health probe included.
                await anyio.sleep(delay)
                continue

            if response.status_code == 401:
                # Our configuration is wrong, not their service. Loud, because
                # nobody will find this by staring at the frontend.
                logger.error("groq rejected the api key; check KP_GROQ_API_KEY")
                raise LanguageModelUnavailableError("invalid api key")

            if response.status_code >= 400:
                # The body may quote the prompt, and the prompt carries the
                # private documents of a user. Only the status code is logged.
                logger.warning("groq returned %s", response.status_code)
                raise LanguageModelUnavailableError(f"groq returned {response.status_code}")

            return _content(response)

        raise LanguageModelUnavailableError("exhausted every attempt")

    async def _post(self, system: str, user: str) -> httpx.Response:
        """One request, with every network failure translated on the way out."""
        try:
            return await self._client.post(
                API_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "temperature": TEMPERATURE,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            )
        except httpx.TimeoutException as exc:
            logger.warning("groq timed out after %.0fs", READ_TIMEOUT_SECONDS)
            raise LanguageModelUnavailableError("groq timed out") from exc
        except httpx.HTTPError as exc:
            # The exception is logged by type, never by message: httpx puts the
            # request inside it, and the request carries the passages.
            logger.warning("groq is unreachable (%s)", type(exc).__name__)
            raise LanguageModelUnavailableError("groq is unreachable") from exc
