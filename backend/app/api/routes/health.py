from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response, status

router = APIRouter(prefix="/health", tags=["health"])

# Readiness checks are registered here as the dependencies arrive
# (PostgreSQL, Redis, Chroma). Empty today, so the service is ready as soon as
# it answers.
type Check = Callable[[], Awaitable[bool]]
READINESS_CHECKS: dict[str, Check] = {}


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process alive?

    Checks nothing external on purpose: a failing database must never cause a
    restart, because restarting would not fix it.
    """
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, object]:
    """Can this instance serve traffic?

    Returns 503 when a dependency is down, so the orchestrator stops routing
    traffic here without restarting the container.
    """
    checks: dict[str, str] = {}
    for name, check in READINESS_CHECKS.items():
        try:
            checks[name] = "ok" if await check() else "error"
        except Exception:  # noqa: BLE001 - the cause belongs in the logs, not in the response
            checks[name] = "error"

    is_ready = all(result == "ok" for result in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Booleans only: a version, a hostname or an exception would map the
    # internal infrastructure for an attacker.
    return {"status": "ready" if is_ready else "not_ready", "checks": checks}
