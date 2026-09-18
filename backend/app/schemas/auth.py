from pydantic import BaseModel


class CurrentUser(BaseModel):
    """What GET /api/auth/me exposes.

    Built explicitly from the session: the tokens and the refresh token stay
    server side, and a field added to the session is never leaked by accident.
    """

    id: str
    email: str
    display_name: str
    csrf_token: str


class LogoutResponse(BaseModel):
    """Where the browser must go to end the Keycloak session as well."""

    logout_url: str
