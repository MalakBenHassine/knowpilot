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

    # --- Embeddings (ADR-0006) ---
    # The name is part of the data contract with the vector index: changing it
    # invalidates every vector ever written, so it must be configuration, not a
    # constant buried in the code.
    embedding_model: str = "BAAI/bge-m3"
    # Loading costs ~20 s and 2.2 GB of RAM. Turned off while working on
    # anything other than ingestion, and always off in tests.
    embeddings_enabled: bool = True
    # Where the weights are cached. None uses the Hugging Face default
    # (~/.cache/huggingface). In Docker this points at a mounted volume, so a
    # restart does not download two gigabytes again.
    # Named `embedding_cache_dir`, not `model_cache_dir`: pydantic reserves the
    # `model_` prefix for its own API and would warn about the collision.
    embedding_cache_dir: str | None = None

    @property
    def cookie_secure(self) -> bool:
        """`Secure` is always on: browsers accept it on http://localhost."""
        return True


@lru_cache
def get_settings() -> Settings:
    """Settings are read once: they never change while the process runs."""
    return Settings()
