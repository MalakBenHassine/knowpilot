"""Daily question budgets.

Redis is faked with a dictionary. The four commands this module uses - INCR,
DECR, EXPIRE, DEL - are simple enough that a fake cannot drift from the real
thing in any way that matters, and the tests run in milliseconds without a
server.

What is exercised is the policy: who is refused, who is charged, and above all
who is given their question back.
"""

from datetime import UTC, datetime

import anyio
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.quota import (
    KEY_TTL_SECONDS,
    QuotaExceededError,
    QuotaTracker,
    seconds_until_reset,
)

ALICE = "alice-sub"
BOB = "bob-sub"


class FakeRedis:
    """Only the four commands QuotaTracker uses, and they count themselves."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def delete(self, key: str) -> int:
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def user_key(owner_id: str) -> str:
    return f"quota:user:{owner_id}:{today()}"


def service_key() -> str:
    return f"quota:service:{today()}"


def build(per_user: int = 3, per_service: int = 10) -> tuple[QuotaTracker, FakeRedis]:
    fake = FakeRedis()
    return QuotaTracker(fake, per_user=per_user, per_service=per_service), fake  # type: ignore[arg-type]


def reserve(tracker: QuotaTracker, owner_id: str) -> None:
    anyio.run(lambda: tracker.reserve(owner_id=owner_id))


def refund(tracker: QuotaTracker, owner_id: str) -> None:
    anyio.run(lambda: tracker.refund(owner_id=owner_id))


# --- The per-user budget -------------------------------------------------


def test_a_question_within_the_budget_is_allowed() -> None:
    tracker, fake = build()

    reserve(tracker, ALICE)

    assert fake.values[user_key(ALICE)] == 1
    assert fake.values[service_key()] == 1


def test_the_question_after_the_limit_is_refused() -> None:
    tracker, _ = build(per_user=3)
    for _ in range(3):
        reserve(tracker, ALICE)

    with pytest.raises(QuotaExceededError) as caught:
        reserve(tracker, ALICE)

    # The scope decides which sentence the user reads: "you have used your
    # twenty questions" is actionable, "the service is busy" is not the same
    # message at all.
    assert caught.value.scope == "user"
    assert caught.value.retry_after > 0


def test_a_refused_attempt_does_not_inflate_the_counter() -> None:
    """The counter means questions served, never attempts made.

    Without the refund on refusal, a user retrying ten times would read 30/20
    in tomorrow logs, and the number nobody trusts is the number nobody uses.
    """
    tracker, fake = build(per_user=3)
    for _ in range(3):
        reserve(tracker, ALICE)

    for _ in range(5):
        with pytest.raises(QuotaExceededError):
            reserve(tracker, ALICE)

    assert fake.values[user_key(ALICE)] == 3


def test_two_users_have_separate_budgets() -> None:
    tracker, fake = build(per_user=3)
    for _ in range(3):
        reserve(tracker, ALICE)

    # Alice is finished; Bob has not started. The whole point of the per-user
    # counter is that one curious person cannot end the day for everyone.
    reserve(tracker, BOB)

    assert fake.values[user_key(BOB)] == 1


# --- The service budget --------------------------------------------------


def test_the_service_budget_refuses_a_user_who_is_still_under_their_own() -> None:
    tracker, _ = build(per_user=10, per_service=2)
    reserve(tracker, ALICE)
    reserve(tracker, ALICE)

    with pytest.raises(QuotaExceededError) as caught:
        reserve(tracker, BOB)

    # Four users each staying under their own limit still add up past the
    # provider budget, which is exactly what this second counter is for.
    assert caught.value.scope == "service"


def test_a_user_refused_by_the_service_budget_is_not_charged() -> None:
    """Both increments happen before either limit is known, so both come back.

    Charging Bob for a question the service refused would mean an outage costs
    him part of his own allowance - punishing a user for our shortage.
    """
    tracker, fake = build(per_user=10, per_service=2)
    reserve(tracker, ALICE)
    reserve(tracker, ALICE)

    with pytest.raises(QuotaExceededError):
        reserve(tracker, BOB)

    assert user_key(BOB) not in fake.values
    assert fake.values[service_key()] == 2


def test_the_service_counter_is_shared_between_users() -> None:
    tracker, fake = build(per_user=10, per_service=10)

    reserve(tracker, ALICE)
    reserve(tracker, BOB)

    assert fake.values[service_key()] == 2


# --- Refunding -----------------------------------------------------------


def test_a_refund_gives_the_question_back() -> None:
    """The reason reserving first is acceptable at all.

    The provider failed, nothing was generated, and the user is not charged for
    our outage. Of the two possible defects this is the reversible one, which
    is why it is the one we chose.
    """
    tracker, fake = build(per_user=3)
    for _ in range(3):
        reserve(tracker, ALICE)
    with pytest.raises(QuotaExceededError):
        reserve(tracker, ALICE)

    refund(tracker, ALICE)

    reserve(tracker, ALICE)
    assert fake.values[user_key(ALICE)] == 3


def test_a_refund_refunds_the_service_budget_too() -> None:
    tracker, fake = build(per_user=10, per_service=10)
    reserve(tracker, ALICE)

    refund(tracker, ALICE)

    assert service_key() not in fake.values


def test_a_refund_never_leaves_a_negative_counter() -> None:
    """DECR on a missing key creates it at -1, with no expiry.

    That counter would be meaningless and would also never be cleaned up, so
    zero and absent are collapsed into absent.
    """
    tracker, fake = build()

    refund(tracker, ALICE)

    assert user_key(ALICE) not in fake.values
    assert service_key() not in fake.values


# --- Housekeeping --------------------------------------------------------


def test_every_counter_is_given_an_expiry() -> None:
    """A counter without a TTL is a memory leak with a date in its name."""
    tracker, fake = build()

    reserve(tracker, ALICE)

    assert fake.ttls[user_key(ALICE)] == KEY_TTL_SECONDS
    assert fake.ttls[service_key()] == KEY_TTL_SECONDS


def test_the_expiry_is_refreshed_rather_than_set_once() -> None:
    """Repairs the key a crash between INCR and EXPIRE would have left behind."""
    tracker, fake = build()
    reserve(tracker, ALICE)
    fake.ttls.clear()

    reserve(tracker, ALICE)

    assert fake.ttls[user_key(ALICE)] == KEY_TTL_SECONDS


def test_the_reset_delay_counts_to_midnight_utc() -> None:
    at_six = datetime(2026, 9, 20, 6, 0, 0, tzinfo=UTC)

    # UTC, not local time: a server observing daylight saving would gain or
    # lose an hour of budget twice a year, and the bug would surface exactly
    # once every six months.
    assert seconds_until_reset(at_six) == 18 * 60 * 60


def test_the_reset_delay_is_never_zero() -> None:
    at_midnight = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)

    # A Retry-After of zero invites an immediate retry, which is refused again.
    assert seconds_until_reset(at_midnight) > 0


# --- The tenant-isolation convention -------------------------------------


def test_the_limits_have_no_default() -> None:
    """The tracker must be told the policy, never assume one.

    A default here would be a policy decision hidden inside a library: a caller
    that forgot to pass the configured value would silently enforce somebody
    else numbers, and nothing would ever say so.
    """
    with pytest.raises(TypeError):
        QuotaTracker(FakeRedis())  # type: ignore[call-arg,arg-type]


def test_a_per_user_limit_above_the_service_one_is_refused_at_startup() -> None:
    """Nothing would break - which is precisely the problem.

    The service counter would simply refuse first, and every user would read
    "the service has used its questions" instead of "you have used yours". An
    invisible misconfiguration is one that fails in front of whoever is
    deploying, not at three in the morning.
    """
    with pytest.raises(ValidationError):
        Settings(daily_questions_per_user=100, daily_questions_per_service=80)


def test_a_zero_budget_is_refused() -> None:
    # Answering is disabled by leaving the API key empty, which produces an
    # honest 503. A budget of zero would instead look like a quota anybody can
    # hit on their first question of the day.
    with pytest.raises(ValidationError):
        Settings(daily_questions_per_user=0)


def test_the_owner_cannot_be_passed_positionally() -> None:
    """Same rule as every tenant-scoped call here: keyword-only, no default.

    Forgetting the owner must be a TypeError the moment the code runs, never a
    counter quietly shared between two accounts.
    """
    tracker, _ = build()

    with pytest.raises(TypeError):
        anyio.run(lambda: tracker.reserve(ALICE))  # type: ignore[call-arg]
