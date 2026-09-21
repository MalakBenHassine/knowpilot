"""Shared dependencies: session lookup, authentication, CSRF and database."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from langchain_core.runnables import Runnable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.queue import JobQueue
from app.core.quota import QuotaTracker
from app.core.session import SessionData, SessionStore
from app.core.storage import FileStorage
from app.rag.retrieval import RetrieverFactory
from app.rag.subjects import SubjectCheck


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


def get_file_storage(request: Request) -> FileStorage:
    storage: FileStorage = request.app.state.file_storage
    return storage


Storage = Annotated[FileStorage, Depends(get_file_storage)]


def get_retrievers(request: Request) -> RetrieverFactory:
    """Builds the owner-scoped retriever of a request, or 503.

    None means the embedding model is disabled in this process: a question
    cannot be embedded, so it cannot be searched.
    """
    factory: RetrieverFactory | None = request.app.state.retrievers
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable")
    return factory


Retrievers = Annotated[RetrieverFactory, Depends(get_retrievers)]


def get_job_queue(request: Request) -> JobQueue:
    """Publishes ingestion jobs. The route never learns who consumes them."""
    queue: JobQueue = request.app.state.job_queue
    return queue


Queue = Annotated[JobQueue, Depends(get_job_queue)]


def get_answer_chain(request: Request) -> Runnable[dict[str, Any], str]:
    """The LCEL answer chain (prompt | ChatGroq | parser), or 503.

    None means no API key was configured. Answering 503 keeps the rest of
    the application alive: uploads, listings and deletions have nothing to
    do with generation, and an optional external service must never be able
    to take the whole product down with it.
    """
    chain: Runnable[dict[str, Any], str] | None = request.app.state.answer_chain
    if chain is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Answering is unavailable")
    return chain


AnswerChain = Annotated[Runnable[dict[str, Any], str], Depends(get_answer_chain)]


def get_subject_check(request: Request) -> SubjectCheck | None:
    """The subject check of ADR-0018, or None when it is disabled.

    None is NOT a 503, unlike the two dependencies above: the check only adds
    a notice to an answer, and its absence must never stop one.
    """
    check: SubjectCheck | None = request.app.state.subject_check
    return check


SubjectChecker = Annotated[SubjectCheck | None, Depends(get_subject_check)]


def get_quota_tracker(request: Request) -> QuotaTracker:
    """Shared through Redis, so every worker counts into the same budget."""
    tracker: QuotaTracker = request.app.state.quota
    return tracker


Quota = Annotated[QuotaTracker, Depends(get_quota_tracker)]


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request, committed only if the handler returns.

    The commit happens here rather than inside the repository functions, so a
    handler that writes several tables either succeeds entirely or leaves
    nothing behind. An exception on the way out rolls everything back before
    the error response is built.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


Db = Annotated[AsyncSession, Depends(db_session)]


async def current_session(
    store: Annotated[SessionStore, Depends(get_session_store)],
    session_cookie: Annotated[str | None, Cookie(alias="__Host-session")] = None,
) -> SessionData:
    """The authenticated session, or 401.

    401 means "not authenticated" — the frontend turns it into "anonymous", not
    into an error banner.
    """
    if not session_cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    session = await store.get(session_cookie)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return session


CurrentSession = Annotated[SessionData, Depends(current_session)]


async def require_csrf(
    session: CurrentSession,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SessionData:
    """Protects state-changing requests.

    The session cookie travels automatically, so a third-party site could
    trigger a POST. It cannot read this token (same-origin policy), so
    requiring it proves the request came from our own page.

    Safe methods (GET, HEAD) are exempt: they must never change state.
    """
    if not csrf_header or csrf_header != session.csrf_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid CSRF token")
    return session


CsrfProtected = Annotated[SessionData, Depends(require_csrf)]
