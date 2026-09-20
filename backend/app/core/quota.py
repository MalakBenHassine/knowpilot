"""Daily question budgets, counted in Redis.

The free GroqCloud tier gives 200K tokens a day, and a grounded question costs
roughly 2300 of them: about 85 questions for every user of this service put
together. Without a budget, one curious person empties the day before anyone
else logs in, so quotas are part of the chat endpoint rather than a later
refinement.

Two counters, because they defend against different things. The per-user one
protects users from each other; the service one protects the service from all
of them at once, including from a day where four people each stay under their
own limit.

Redis rather than a dictionary: four uvicorn workers would hold four separate
dictionaries and therefore allow four times the budget, which is the failure
this module exists to prevent. Redis rather than PostgreSQL: it is already in
the stack for sessions, INCR is atomic, and a TTL expires the counter without a
scheduled job. Counters are lost if Redis restarts, and that is accepted: the
worst case is a few extra questions, which is not the same class of problem as
losing a balance.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# 200K tokens a day over roughly 2300 tokens a question leaves about 85. The
# service stops at 80 so that the last few are spare: hitting our own limit
# produces a clear message, hitting the provider limit produces a 429 we have
# to interpret.
DAILY_QUESTIONS_PER_SERVICE = 80

# Four users can spend their whole allowance on the same day without exhausting
# the service. Deliberately generous: a quota that bites during normal use
# teaches people to distrust the product.
DAILY_QUESTIONS_PER_USER = 20

# Cleanup only. The day is already part of the key name, so yesterday counters
# are never read again; the TTL simply stops them accumulating in memory.
KEY_TTL_SECONDS = 2 * 24 * 60 * 60

Scope = Literal["user", "service"]


class QuotaExceededError(Exception):
    """Maps to 429. `scope` decides which sentence the user reads."""

    def __init__(self, scope: Scope, retry_after: int) -> None:
        super().__init__(f"{scope} quota exhausted, retry in {retry_after}s")
        self.scope = scope
        self.retry_after = retry_after


def seconds_until_reset(now: datetime | None = None) -> int:
    """How long until the counters start again, in seconds.

    UTC, not local time: a server whose timezone observes daylight saving would
    otherwise gain or lose an hour of budget twice a year, and the bug would
    appear exactly once every six months.
    """
    moment = now or datetime.now(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (moment - midnight).total_seconds()
    return max(1, int(24 * 60 * 60 - elapsed))


class QuotaTracker:
    """Reserve before spending, refund when the spending did not happen.

    Incrementing after a successful call would let two simultaneous requests
    both pass the check before either had counted, and the tokens they spend at
    Groq cannot be taken back. Incrementing first can instead charge a user for
    a question the provider failed to answer - and that one is undone with a
    DECR. Of two defects, take the reversible one.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        per_user: int = DAILY_QUESTIONS_PER_USER,
        per_service: int = DAILY_QUESTIONS_PER_SERVICE,
    ) -> None:
        self._redis = redis
        self._per_user = per_user
        self._per_service = per_service

    async def reserve(self, *, owner_id: str) -> None:
        """Take one question from both budgets, or raise.

        `owner_id` is keyword-only and has no default, exactly like every other
        tenant-scoped call in this codebase: forgetting it must be a TypeError
        at import time, never a counter silently shared between accounts.
        """
        retry_after = seconds_until_reset()
        user_key, service_key = self._keys(owner_id)

        used = await self._bump(user_key)
        if used > self._per_user:
            # Give it back: the counter must mean "questions served", not
            # "attempts made", or a user hammering the endpoint would inflate
            # their own number and confuse tomorrow reading of the logs.
            await self._undo(user_key)
            logger.info("user quota reached (%s/%s)", used - 1, self._per_user)
            raise QuotaExceededError("user", retry_after)

        total = await self._bump(service_key)
        if total > self._per_service:
            await self._undo(service_key)
            await self._undo(user_key)
            logger.warning("service quota reached (%s/%s)", total - 1, self._per_service)
            raise QuotaExceededError("service", retry_after)

    async def refund(self, *, owner_id: str) -> None:
        """Hand the question back after a failure that spent nothing.

        Called when the provider was unreachable, timed out, or refused us: the
        user asked, nothing was generated, and charging them would make an
        outage cost them their day.

        A refund that crosses midnight lands on the new day counter and is
        deleted rather than left negative. The inaccuracy is one question on one
        boundary, and correcting it would mean storing which day each in-flight
        request belonged to - more machinery than the error it removes.
        """
        user_key, service_key = self._keys(owner_id)
        await self._undo(user_key)
        await self._undo(service_key)

    def _keys(self, owner_id: str) -> tuple[str, str]:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return f"quota:user:{owner_id}:{day}", f"quota:service:{day}"

    async def _bump(self, key: str) -> int:
        """Increment, and make sure the key is still set to expire.

        INCR returns the new value, so the count is never read before being
        written: check-then-act would let two requests read the same number and
        both decide they were under the limit.

        The TTL is refreshed on every call rather than only on the first. A
        process dying between INCR and EXPIRE would otherwise leave a counter
        that never expires, and refreshing costs one cheap command while making
        the next request repair it. Resetting it is harmless because the day is
        part of the key name.
        """
        count = int(await self._redis.incr(key))
        await self._redis.expire(key, KEY_TTL_SECONDS)
        return count

    async def _undo(self, key: str) -> None:
        """Give one back, without ever leaving a counter below zero.

        DECR on a missing key creates it at -1 with no expiry, which would both
        be meaningless and never be cleaned up. Zero and absent mean the same
        thing here, so the key is simply removed.
        """
        remaining = int(await self._redis.decr(key))
        if remaining <= 0:
            await self._redis.delete(key)
