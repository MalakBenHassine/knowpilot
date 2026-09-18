"""Authentication endpoints (Backend-for-Frontend).

/login and /callback are reached by BROWSER REDIRECTS: the user must see
Keycloak's page, and the application must never handle the password.
/me and /logout are called with fetch by the SPA.
"""

import logging
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import CsrfProtected, CurrentSession, get_session_store
from app.core.config import Settings, get_settings
from app.core.oidc import OidcClient, OidcError, pkce_challenge
from app.core.session import (
    LOGIN_TX_COOKIE,
    SESSION_COOKIE,
    LoginTransaction,
    SessionData,
    SessionStore,
    new_token,
)
from app.schemas.auth import CurrentUser, LogoutResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def get_oidc(request: Request) -> OidcClient:
    client: OidcClient = request.app.state.oidc
    return client


def _set_cookie(
    response: Response, name: str, value: str, max_age: int, settings: Settings
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,  # unreachable from JavaScript: an XSS cannot read it
        secure=settings.cookie_secure,  # required by the __Host- prefix
        samesite="lax",  # blocks most cross-site requests
        path="/",  # required by the __Host- prefix
    )


async def _start_oidc_flow(
    store: SessionStore,
    oidc: OidcClient,
    settings: Settings,
    *,
    register: bool,
) -> RedirectResponse:
    """Login and sign-up share one flow; only the first Keycloak page differs."""
    state = new_token(16)
    nonce = new_token(16)
    code_verifier = new_token(48)

    # The transaction lives server side; the browser only carries its id.
    tx_id = await store.start_login(
        LoginTransaction(state=state, nonce=nonce, code_verifier=code_verifier)
    )
    url = await oidc.authorization_url(
        state, nonce, pkce_challenge(code_verifier), register=register
    )

    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    _set_cookie(response, LOGIN_TX_COOKIE, tx_id, 300, settings)
    return response


@router.get("/login", summary="Start the OIDC login")
async def login(
    store: Annotated[SessionStore, Depends(get_session_store)],
    oidc: Annotated[OidcClient, Depends(get_oidc)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    return await _start_oidc_flow(store, oidc, settings, register=False)


@router.get("/register", summary="Start the sign-up")
async def register(
    store: Annotated[SessionStore, Depends(get_session_store)],
    oidc: Annotated[OidcClient, Depends(get_oidc)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Sends the browser to Keycloak's registration form.

    Account creation, password policy and duplicate checks are Keycloak's job:
    the application writes no sign-up code at all. A successful registration
    ends on the same callback as a login, already authenticated.
    """
    return await _start_oidc_flow(store, oidc, settings, register=True)


@router.get("/callback", summary="Finish the OIDC login")
async def callback(
    request: Request,
    store: Annotated[SessionStore, Depends(get_session_store)],
    oidc: Annotated[OidcClient, Depends(get_oidc)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    failure = RedirectResponse(
        f"{settings.frontend_url}/login?error=auth", status.HTTP_303_SEE_OTHER
    )
    failure.delete_cookie(LOGIN_TX_COOKIE, path="/")

    # The user cancelled, or Keycloak refused. Not an application error.
    if error or not code or not state:
        logger.info("login aborted: %s", error or "missing code/state")
        return failure

    tx_id = request.cookies.get(LOGIN_TX_COOKIE)
    transaction = await store.pop_login(tx_id) if tx_id else None
    if transaction is None:
        logger.warning("login callback without a known transaction")
        return failure

    # Binds the callback to the login THIS browser started: blocks CSRF on the
    # OAuth flow itself.
    if not secrets.compare_digest(transaction.state, state):
        logger.warning("state mismatch on login callback")
        return failure

    try:
        tokens = await oidc.exchange_code(code, transaction.code_verifier)
        claims = await oidc.verify_id_token(tokens["id_token"], transaction.nonce)
    except (OidcError, KeyError):
        logger.exception("login failed during token exchange")
        return failure

    session_id = await store.create(
        SessionData(
            # `sub` is the stable identifier; an email can change.
            sub=str(claims["sub"]),
            email=str(claims.get("email", "")),
            display_name=str(claims.get("name") or claims.get("preferred_username") or ""),
            access_token=str(tokens.get("access_token", "")),
            refresh_token=str(tokens.get("refresh_token", "")),
            id_token=str(tokens["id_token"]),
            csrf_token=new_token(32),
            created_at=time.time(),
        )
    )

    response = RedirectResponse(f"{settings.frontend_url}/documents", status.HTTP_303_SEE_OTHER)
    _set_cookie(response, SESSION_COOKIE, session_id, settings.session_idle_seconds, settings)
    response.delete_cookie(LOGIN_TX_COOKIE, path="/")
    return response


@router.get("/me", summary="Current user", responses={401: {"description": "No session"}})
async def me(session: CurrentSession) -> CurrentUser:
    return CurrentUser(
        id=session.sub,
        email=session.email,
        display_name=session.display_name,
        csrf_token=session.csrf_token,
    )


@router.post("/logout", summary="End the session")
async def logout(
    request: Request,
    response: Response,
    session: CsrfProtected,
    store: Annotated[SessionStore, Depends(get_session_store)],
    oidc: Annotated[OidcClient, Depends(get_oidc)],
) -> LogoutResponse:
    """Destroys our session, then tells the browser where to end Keycloak's."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        await store.delete(session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")

    try:
        url = await oidc.end_session_url(session.id_token)
    except OidcError:
        # Our session is already gone; failing here would be worse than a
        # local-only logout.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Logout provider unavailable") from None
    return LogoutResponse(logout_url=url)
