from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration comes from the environment only (12-factor).

    Nothing is hard-coded and nothing is read from a file in production: the
    same image runs locally, in CI and in Kubernetes, with different variables.
    """

    model_config = SettingsConfigDict(env_file="../.env", env_prefix="KP_", extra="ignore")

    environment: Literal["local", "ci", "production"] = "local"
    # Applies to our own loggers only; libraries stay at WARNING.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
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

    # --- PostgreSQL ---
    # Split into parts rather than one URL: the same values already feed the
    # Docker Compose service, so a single source avoids the two drifting apart.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "knowpilot"
    postgres_password: str = ""
    postgres_db: str = "knowpilot"

    # --- File storage ---
    # Relative to the backend directory; .data/ is git-ignored at the repo
    # root. In Docker this points at a mounted volume, because a container
    # filesystem disappears on the next deploy.
    upload_root: str = "../.data/uploads"

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

    # --- Generation (GroqCloud, ADR-0012) ---
    # Empty disables generation entirely: the chat endpoint answers 503 and
    # the rest of the application still starts. An optional external service
    # must never be able to prevent the process from booting.
    groq_api_key: str = ""
    # One line, because LanguageModel is a Protocol. The documentation lists
    # models this account cannot reach, so this value was chosen from the
    # models endpoint queried with the real key.
    groq_model: str = "openai/gpt-oss-120b"

    @field_validator("groq_api_key", "groq_model", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        """Whitespace pasted around a value is invisible and never harmless.

        A key pasted as sixteen spaces is not empty, so `if not key` lets it
        through, the application boots believing it is configured, and the
        first user sees a 401 about authorisation rather than the truth: a
        failed copy and paste. Empty and blank are different, and blank is
        the one that slips through.
        """
        return value.strip()

    @property
    def generation_enabled(self) -> bool:
        """Reads after the strip above, so blank counts as absent."""
        return bool(self.groq_api_key)

    @property
    def database_url(self) -> str:
        """The asyncpg URL, built from the parts.

        The password is percent-encoded: a perfectly valid password containing
        an at sign or a slash would otherwise be parsed as part of the host,
        and the failure would look like a network problem rather than a
        quoting one.
        """
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cookie_secure(self) -> bool:
        """`Secure` is always on: browsers accept it on http://localhost."""
        return True


@lru_cache
def get_settings() -> Settings:
    """Settings are read once: they never change while the process runs."""
    return Settings()
