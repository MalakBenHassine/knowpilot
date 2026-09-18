"""Shared dependencies: session lookup, authentication and CSRF."""

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from app.core.session import SessionData, SessionStore


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


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
