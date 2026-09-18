from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration comes from the environment only (12-factor).

    Nothing is hard-coded and nothing is read from a file in production: the
    same image runs locally, in CI and in Kubernetes, with different variables.
    """

    model_config = SettingsConfigDict(env_file="../.env", env_prefix="KP_", extra="ignore")

    environment: Literal["local", "ci", "production"] = "local"
    # Public name of the service; never includes a version (see contract.md).
    service_name: str = "knowpilot-api"

    # --- OIDC (Keycloak) ---
    oidc_issuer: str = "http://localhost:8080/realms/knowpilot"
    oidc_client_id: str = "knowpilot-bff"
    # No default: the application must not start without it in production.
    oidc_client_secret: str = ""

    # --- Session (BFF) ---
    redis_url: str = "redis://localhost:6379/0"
    # Sliding window: every request extends it.
    session_idle_seconds: int = 30 * 60
    # Hard limit: never extended, so a stolen cookie eventually dies.
    session_absolute_seconds: int = 8 * 60 * 60

    # Where the browser is sent back after login and logout.
    frontend_url: str = "http://localhost:5173"

    @property
    def cookie_secure(self) -> bool:
        """`Secure` is always on: browsers accept it on http://localhost."""
        return True


@lru_cache
def get_settings() -> Settings:
    """Settings are read once: they never change while the process runs."""
    return Settings()
