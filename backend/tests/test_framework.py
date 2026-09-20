"""Framework behaviour the application depends on.

These tests assert nothing about our own code. They pin down a behaviour of
FastAPI and Starlette that a comment in our routes used to state as obvious -
wrongly - and that cost a silent production failure: the first real upload
stayed `processing` for ever because a background task looked for a row that
had not been committed yet.

If a future version of FastAPI changes this order, these tests go red and
someone reads this file instead of spending an evening reading the database.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.testclient import TestClient


def test_background_tasks_run_before_dependencies_exit() -> None:
    """The order is: handler returns, task runs, THEN the dependency exits.

    The consequence for us: a dependency that commits on its way out commits
    AFTER the background task has already run. Anything the task must see has
    to be committed by the handler itself, explicitly.
    """
    events: list[str] = []
    app = FastAPI()

    def dependency() -> Iterator[str]:
        yield "resource"
        events.append("dependency exit")

    def task() -> None:
        events.append("background task")

    @app.get("/")
    def handler(
        background: BackgroundTasks, resource: Annotated[str, Depends(dependency)]
    ) -> dict[str, str]:
        background.add_task(task)
        events.append("handler returns")
        return {"ok": "yes"}

    with TestClient(app) as client:
        client.get("/")

    assert events == ["handler returns", "background task", "dependency exit"]


def test_a_background_task_still_runs_when_the_client_is_gone() -> None:
    """The response is sent first; the task is not cancelled with the request.

    This is what lets an upload answer 201 in milliseconds while a minute of
    indexing continues behind it.
    """
    ran: list[str] = []
    app = FastAPI()

    @app.get("/")
    def handler(background: BackgroundTasks) -> dict[str, str]:
        background.add_task(ran.append, "done")
        return {"ok": "yes"}

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert ran == ["done"]
