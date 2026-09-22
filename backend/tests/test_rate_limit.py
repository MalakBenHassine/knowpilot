"""Per-minute request limits.

Redis is the FakeRedis of test_quota.py and the clock is injected, so a test
can stand exactly on a window boundary instead of sleeping until one comes.
"""

import anyio
import pytest

from app.core.rate_limit import WINDOW_SECONDS, RateLimitedError, RateLimiter
from tests.test_quota import FakeRedis

ALICE = "alice-sub"
BOB = "bob-sub"

# A moment 15 seconds into a window.
START = 1_000_000 * WINDOW_SECONDS + 15.0


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def build(limit: int = 3, clock: Clock | None = None) -> tuple[RateLimiter, FakeRedis]:
    fake = FakeRedis()
    limiter = RateLimiter(
        fake,  # type: ignore[arg-type]
        limits={"chat": limit, "upload": limit},
        clock=clock or Clock(),
    )
    return limiter, fake


def hit(limiter: RateLimiter, owner_id: str = ALICE, action: str = "chat") -> None:
    anyio.run(lambda: limiter.hit(action, owner_id=owner_id))  # type: ignore[arg-type]


def test_requests_under_the_limit_pass() -> None:
    limiter, _ = build(limit=3)

    for _ in range(3):
        hit(limiter)


def test_the_request_over_the_limit_is_refused_with_the_time_left() -> None:
    limiter, _ = build(limit=3)
    for _ in range(3):
        hit(limiter)

    with pytest.raises(RateLimitedError) as refused:
        hit(limiter)

    # 15 seconds into the minute: 45 left, which is what Retry-After says.
    assert refused.value.retry_after == WINDOW_SECONDS - 15
    assert refused.value.action == "chat"


def test_a_refused_request_is_still_counted() -> None:
    """Unlike the quota: a client that keeps hammering stays refused."""
    limiter, fake = build(limit=1)
    hit(limiter)
    for _ in range(3):
        with pytest.raises(RateLimitedError):
            hit(limiter)

    assert max(fake.values.values()) == 4


def test_a_new_window_starts_a_new_count() -> None:
    clock = Clock()
    limiter, _ = build(limit=1, clock=clock)
    hit(limiter)

    clock.now += WINDOW_SECONDS

    hit(limiter)  # no exception: a new minute, a new counter


def test_users_never_share_a_counter() -> None:
    limiter, _ = build(limit=1)
    hit(limiter, ALICE)

    hit(limiter, BOB)  # Alice's flood is not Bob's problem


def test_chat_and_uploads_are_counted_apart() -> None:
    # Uploading a batch of files must not stop the user from asking a question.
    limiter, _ = build(limit=1)
    hit(limiter, action="upload")

    hit(limiter, action="chat")


def test_every_counter_expires() -> None:
    """A key without a TTL would stay in Redis forever, one per user per minute."""
    limiter, fake = build()
    hit(limiter)

    assert fake.ttls and all(ttl == 2 * WINDOW_SECONDS for ttl in fake.ttls.values())


def test_the_delay_is_never_zero() -> None:
    # On the last instant of a window, "retry in 0 seconds" would invite an
    # immediate retry into the same window.
    limiter, _ = build(limit=1, clock=Clock(1_000_001 * WINDOW_SECONDS - 0.2))
    hit(limiter)

    with pytest.raises(RateLimitedError) as refused:
        hit(limiter)

    assert refused.value.retry_after >= 1
