"""Shared dependencies: session lookup, authentication, CSRF and database."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.session import SessionData, SessionStore
from app.core.storage import FileStorage
from app.db.ingestion import DatabaseIngestionStore
from app.rag.embeddings import EmbeddingModel
from app.rag.pipeline import IngestionStore


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


def get_file_storage(request: Request) -> FileStorage:
    storage: FileStorage = request.app.state.file_storage
    return storage


Storage = Annotated[FileStorage, Depends(get_file_storage)]


def get_embedding_model(request: Request) -> EmbeddingModel:
    """The loaded model, or 503.

    Refusing the upload is more honest than accepting a job we cannot run: the
    user keeps their file and can try again, instead of watching a document
    fail a minute later for a reason that has nothing to do with it.
    """
    model: EmbeddingModel | None = request.app.state.embedding_model
    if model is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Indexing is unavailable")
    return model


Model = Annotated[EmbeddingModel, Depends(get_embedding_model)]


def get_ingestion_store(request: Request) -> IngestionStore:
    """Persistence for the background pipeline.

    Built from the factory rather than from the request session: by the time
    the task runs, the request session is closed and committed.
    """
    return DatabaseIngestionStore(request.app.state.session_factory)


Ingestion = Annotated[IngestionStore, Depends(get_ingestion_store)]


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
