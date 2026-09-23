"""A production container must refuse to start on a development configuration.

These tests describe the settings object itself, so they must not depend on
where they run: `_env_file=None` silences the developer's .env, and the
fixture below silences the process environment.
"""

import os
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings

PRODUCTION = {
    "environment": "production",
    "oidc_client_secret": "a-real-secret",
    "postgres_password": "a-real-password",
    "oidc_issuer": "https://knowpilot.example.org/auth/realms/knowpilot",
    "frontend_url": "https://knowpilot.example.org",
    "redis_url": "redis://:a-real-password@redis:6379/0",
}


@pytest.fixture(autouse=True)
def without_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every KP_ variable the environment happens to carry.

    pydantic-settings reads the process environment as well as the file, and
    the CI exports KP_ENVIRONMENT and KP_POSTGRES_PASSWORD so the database
    tests can reach PostgreSQL. Without this, these tests assert on the
    runner's configuration instead of the code's defaults - and they did:
    the database job was red while the same tests passed on a laptop.
    """
    for name in list(os.environ):
        if name.startswith("KP_"):
            monkeypatch.delenv(name, raising=False)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_a_complete_production_configuration_starts() -> None:
    assert settings(**PRODUCTION).environment == "production"


def test_the_development_defaults_are_refused_in_production() -> None:
    """Each default is a LOCAL default, and each would start broken."""
    with pytest.raises(ValidationError) as refused:
        settings(environment="production")

    message = str(refused.value)
    for problem in (
        "KP_OIDC_CLIENT_SECRET",
        "KP_POSTGRES_PASSWORD",
        "KP_OIDC_ISSUER",
        "KP_FRONTEND_URL",
        "KP_REDIS_URL",
    ):
        assert problem in message
    # Every problem at once: fixing a deployment one error per restart is how
    # an hour disappears.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frontend_url", "http://knowpilot.example.org"),
        ("oidc_issuer", "http://keycloak:8080/realms/knowpilot"),
        ("redis_url", "redis://redis:6379/0"),
        ("redis_url", "redis://:secret@localhost:6379/0"),
    ],
)
def test_one_wrong_value_is_enough_to_refuse(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        settings(**{**PRODUCTION, field: value})


def test_local_development_keeps_its_defaults() -> None:
    # The check applies to production only: a laptop runs on http and
    # localhost, and that is correct there.
    assert settings().environment == "local"


def test_an_empty_variable_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """How docker-compose.prod.yml passes a tunable nobody set: KP_X="".

    Without env_ignore_empty, "" would be validated as a value - an invalid
    timezone, a question budget of "" - and the container would not start.
    """
    monkeypatch.setenv("KP_TIMEZONE", "")
    monkeypatch.setenv("KP_DAILY_QUESTIONS_PER_USER", "")

    loaded = settings()

    assert loaded.timezone == "Europe/Paris"
    assert loaded.daily_questions_per_user == 20


def test_the_subject_check_is_switched_off_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KP_SUBJECT_CHECK_ENABLED", "false")

    assert settings().subject_check_enabled is False
