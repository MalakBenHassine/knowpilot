"""LangSmith tracing must be a decision, never an accident."""

import pytest

from app.core.tracing import ensure_tracing_is_deliberate, tracing_requested


@pytest.mark.parametrize(
    "environ",
    [
        {"LANGSMITH_TRACING": "true"},
        {"LANGCHAIN_TRACING_V2": "true"},
        {"LANGSMITH_TRACING": " TRUE "},
        {"LANGSMITH_TRACING": "1"},
    ],
)
def test_every_spelling_of_tracing_is_detected(environ: dict[str, str]) -> None:
    assert tracing_requested(environ)


def test_tracing_off_is_the_default() -> None:
    assert not tracing_requested({})
    assert not tracing_requested({"LANGSMITH_TRACING": "false"})


def test_tracing_without_a_decision_stops_the_startup() -> None:
    # A trace of a question contains the retrieved passages: the private
    # documents of a user, sent to a third-party service.
    with pytest.raises(RuntimeError, match="KP_LANGSMITH_TRACING_ALLOWED"):
        ensure_tracing_is_deliberate(allowed=False, environ={"LANGSMITH_TRACING": "true"})


def test_a_deliberate_decision_is_respected(caplog: pytest.LogCaptureFixture) -> None:
    ensure_tracing_is_deliberate(allowed=True, environ={"LANGSMITH_TRACING": "true"})

    # Allowed, but never silent.
    assert "tracing is ON" in caplog.text


def test_no_tracing_needs_no_decision() -> None:
    ensure_tracing_is_deliberate(allowed=False, environ={})
