from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration comes from the environment only (12-factor).

    Nothing is hard-coded and nothing is read from a file in production: the
    same image runs locally, in CI and in Kubernetes, with different variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="KP_", extra="ignore")

    environment: Literal["local", "ci", "production"] = "local"
    # Public name of the service; never includes a version (see contract.md).
    service_name: str = "knowpilot-api"


@lru_cache
def get_settings() -> Settings:
    """Settings are read once: they never change while the process runs."""
    return Settings()
