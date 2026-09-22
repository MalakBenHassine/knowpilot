"""Per-user request rate limits, counted in Redis.

The daily quota (quota.py) counts what reaches the language model. It does not
count what happens BEFORE: a question that retrieves nothing is free, and so is
its embedding - a second of CPU in the API process. A user sending questions
in a loop would therefore spend none of their quota and still take the API
away from everybody else. Uploads are the same: each one is up to 20 MB of
disk and a queued job of parsing and embedding.

So both are limited per minute, per user, before any work is done.

A FIXED one-minute window: one INCR and one EXPIRE, atomic, shared by every
process through Redis - the same machinery as the quota. Its known cost is a
burst of up to twice the limit across the boundary of two windows. That is
accepted: the purpose is to stop a flood, not to meter requests exactly, and
the exact alternative (a sliding log in a sorted set) costs a set per user and
four commands per request to remove a difference no user will notice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Literal

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

Action = Literal["chat", "upload"]

WINDOW_SECONDS = 60


class RateLimitedError(Exception):
    """Maps to 429 with Retry-After: the seconds left in the current window."""

    def __init__(self, action: Action, retry_after: int) -> None:
        super().__init__(f"{action} rate limit reached, retry in {retry_after}s")
        self.action = action
        self.retry_after = retry_after


class RateLimiter:
    """At most `limits[action]` requests per user per minute."""

    def __init__(
        self,
        redis: Redis,
        *,
        limits: Mapping[Action, int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        # The clock is injected so a test can stand on a window boundary
        # instead of sleeping until one comes.
        self._redis = redis
        self._limits = dict(limits)
        self._clock = clock

    async def hit(self, action: Action, *, owner_id: str) -> None:
        """Count one request, or raise if the user is over the limit.

        Unlike the quota, a refused request is NOT given back. The quota counts
        questions served; this counts requests made - and a client hammering
        the endpoint is exactly what it must keep refusing until the window
        ends.
        """
        now = self._clock()
        window = int(now // WINDOW_SECONDS)
        # The window is part of the key, so a new minute is a new counter and
        # nothing ever has to be reset. `owner_id` is the Keycloak subject.
        key = f"rate:{action}:{owner_id}:{window}"

        count = int(await self._redis.incr(key))
        # Refreshed every time, as in quota.py: a process dying between INCR
        # and EXPIRE must not leave a counter that never expires. Two windows,
        # so a key is never deleted while its window is still running.
        await self._redis.expire(key, 2 * WINDOW_SECONDS)

        if count > self._limits[action]:
            retry_after = max(1, WINDOW_SECONDS - int(now % WINDOW_SECONDS))
            # The count only, never the owner: a log line is not the place to
            # build a list of who was throttled when.
            logger.info("%s rate limit reached (%s/%s)", action, count, self._limits[action])
            raise RateLimitedError(action, retry_after)
