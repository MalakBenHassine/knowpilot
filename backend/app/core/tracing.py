"""Refuse to start if LangChain would send user documents to a tracing service.

LangSmith is LangChain's observability platform, and a good one: it records
every step of every chain. That is exactly the problem. A trace of a question
contains the retrieved passages, and the passages are the private documents of
a user. Enabling it is one environment variable - LANGSMITH_TRACING=true, or
its older name LANGCHAIN_TRACING_V2 - which can be set by a copied .env file, a
CI template or a well-meaning colleague.

So the default is fail-closed: the process refuses to start with tracing on
unless KP_LANGSMITH_TRACING_ALLOWED says that someone decided it deliberately.
A privacy decision should be an explicit act, not the absence of one.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
_TRUTHY = {"1", "true", "yes", "on"}


def tracing_requested(environ: dict[str, str] | None = None) -> bool:
    environ = dict(os.environ) if environ is None else environ
    return any(environ.get(name, "").strip().lower() in _TRUTHY for name in TRACING_VARIABLES)


def ensure_tracing_is_deliberate(*, allowed: bool, environ: dict[str, str] | None = None) -> None:
    """Raise at startup when tracing is on without an explicit decision."""
    if not tracing_requested(environ):
        return
    if not allowed:
        raise RuntimeError(
            "LangSmith tracing is enabled but KP_LANGSMITH_TRACING_ALLOWED is not: "
            "traces would contain users' private documents. Unset LANGSMITH_TRACING, "
            "or set KP_LANGSMITH_TRACING_ALLOWED=true if this is intended."
        )
    logger.warning("LangSmith tracing is ON: prompts and passages leave this process")
