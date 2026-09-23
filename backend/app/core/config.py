from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration comes from the environment only (12-factor).

    Nothing is hard-coded and nothing is read from a file in production: the
    same image runs locally, in CI and in Kubernetes, with different variables.
    """

    # env_ignore_empty: a variable set to "" means "not set", and the default
    # below applies. docker-compose.prod.yml passes every tunable as
    # ${KP_X:-}, so the defaults live HERE only - copying them into the
    # compose file would give each one two sources of truth that drift apart.
    model_config = SettingsConfigDict(
        env_file="../.env", env_prefix="KP_", extra="ignore", env_ignore_empty=True
    )

    environment: Literal["local", "ci", "production"] = "local"
    # Applies to our own loggers only; libraries stay at WARNING.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Public name of the service; never includes a version (see contract.md).
    service_name: str = "knowpilot-api"
    # The calendar the users live in. It decides which day "today" is when an
    # answer depends on a date - an amendment in force from the 1st, a
    # deadline. Validated at startup: a typo would otherwise surface as a
    # crash on the first question.
    timezone: str = "Europe/Paris"

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
    # The documentation lists models this account cannot reach, so this value
    # was chosen from the models endpoint queried with the real key.
    groq_model: str = "openai/gpt-oss-120b"
    # A second, smaller model for one narrow job: naming the thing a question
    # is about, so the code can check whether any passage names it (ADR-0018).
    # Groq budgets each model separately, so this call does not spend the
    # answering model's daily tokens. Empty disables the check; answering
    # still works, without the "not in your documents" notice.
    groq_check_model: str = "openai/gpt-oss-20b"
    # An explicit switch rather than "an empty model name disables it": with
    # empty values ignored (see model_config), emptiness can no longer carry
    # a meaning - and it was a poor carrier of one anyway.
    subject_check_enabled: bool = True

    # --- LangSmith tracing (ADR-0014) ---
    # LangChain sends every run - prompt, passages, answer - to LangSmith when
    # LANGSMITH_TRACING is set. The passages are the private documents of our
    # users, so tracing is refused at startup unless this flag says someone
    # decided it on purpose (a self-hosted LangSmith, or a test account).
    langsmith_tracing_allowed: bool = False

    # --- Retrieval policy ---
    # Cosine distance above which a passage is treated as not about the
    # question: 0 is identical, 1 is unrelated. The strongest guard of the
    # whole feature, because it runs BEFORE the provider is called - there is
    # nothing to hallucinate from if nothing was sent, and nothing to pay for.
    #
    # Configuration rather than a constant, for the same reason as the budgets:
    # the right value depends on the documents. A library of short, single-
    # topic notes tolerates a tighter ceiling than one of long mixed pages,
    # where a chunk covering three subjects has an averaged vector that matches
    # none of them closely.
    #
    # Change it with the evaluation harness open. Loosening it does not only
    # find more answers: it also lets weaker passages reach the model, and the
    # measure of success is that the refusals still refuse.
    max_distance: float = Field(default=0.6, gt=0.0, le=2.0)

    # How many passages reach the model. Tied to the chunk size rather than
    # chosen alone: eight chunks of six hundred characters carry about the same
    # context - and therefore about the same token cost - as the five chunks of
    # a thousand this replaced. Raising it without shrinking chunks would spend
    # budget for context the question did not need.
    top_k: int = Field(default=8, ge=1, le=20)

    # Hybrid retrieval (ADR-0015): full-text search beside the embeddings, for
    # what embeddings cannot see - phone numbers, references, surnames. Off
    # means vector-only, the behaviour the gain was measured against.
    keyword_search_enabled: bool = True

    # The share of the question's words a passage must contain to be admitted
    # on keywords alone. 0.5 was measured: exact-value questions reach 0.7 to
    # 1.0 on the passage that answers them, while the noise - a passage sharing
    # "jour" or "quel" with the question - stays at 0.2 or below. It never
    # affects passages admitted on meaning; it only decides which extra ones
    # the keyword leg may add.
    min_keyword_coverage: float = Field(default=0.5, gt=0.0, le=1.0)

    # Each passage is read together with the chunk before it on the same page
    # (ADR-0017). Contracts state the condition first and the value after -
    # "amendment applicable from 1 October ... the premium is raised to 59
    # euros" - and a chunk holding only the value was answered as if it were
    # already in force. Costs input tokens; off restores bare chunks.
    passage_context_enabled: bool = True

    # --- Daily question budgets (ADR-0012) ---
    # Policy, not logic, which is why it lives here rather than as a constant
    # in the module that enforces it. The same code runs on a laptop with one
    # user, on a demo with a handful, and in production; only these two numbers
    # differ, and recompiling to change a number is not a deployment strategy.
    #
    # Defaults are derived: 200K provider tokens a day over roughly 2300 per
    # question leaves about 85, and the service stops at 80 so the last few are
    # spare. Four users can then spend their whole allowance on the same day.
    #
    # Found by using the product: on a single-user instance the per-user limit
    # binds at 20 while 60 questions of the service budget sit unreachable.
    # Fairness that protects nobody is only a smaller budget.
    daily_questions_per_user: int = Field(default=20, gt=0)
    daily_questions_per_service: int = Field(default=80, gt=0)

    # --- Per-minute request limits (app/core/rate_limit.py) ---
    # The daily budget counts what reaches the model; these count every
    # request, before any work - retrieval and uploads cost CPU and disk even
    # when they cost no tokens. Ten a minute is far above a person reading
    # answers and far below a script.
    chat_requests_per_minute: int = Field(default=10, gt=0)
    upload_requests_per_minute: int = Field(default=10, gt=0)

    @model_validator(mode="after")
    def _budgets_are_coherent(self) -> "Settings":
        """A per-user limit above the service limit can never be reached.

        Nothing would break: the service counter would simply refuse first, and
        every user would read "the service has used its questions" instead of
        "you have used yours". The misconfiguration would be invisible except
        as a confusing message, which is exactly the kind of thing that must
        fail at startup, in front of whoever is deploying.
        """
        if self.daily_questions_per_user > self.daily_questions_per_service:
            raise ValueError(
                f"KP_DAILY_QUESTIONS_PER_USER ({self.daily_questions_per_user}) exceeds "
                f"KP_DAILY_QUESTIONS_PER_SERVICE ({self.daily_questions_per_service}): "
                "the per-user limit could never be reached"
            )
        return self

    @model_validator(mode="after")
    def _production_is_complete(self) -> "Settings":
        """Refuse to run in production on a development configuration.

        Every default in this file is a LOCAL default: http URLs, localhost, an
        empty password. Each of them would start a production container that
        seems to work and is wrong - a login redirected to localhost, a
        database without a password, a cookie flow over plain HTTP. Failing
        here, at startup and in the deployment log, is the difference between
        a deployment that does not start and one that starts broken.
        """
        if self.environment != "production":
            return self
        problems = []
        if not self.oidc_client_secret:
            problems.append("KP_OIDC_CLIENT_SECRET is empty")
        if not self.postgres_password:
            problems.append("KP_POSTGRES_PASSWORD is empty")
        for name, url in (
            ("KP_OIDC_ISSUER", self.oidc_issuer),
            ("KP_FRONTEND_URL", self.frontend_url),
        ):
            if not url.startswith("https://"):
                problems.append(f"{name} must be an https:// URL, got {url!r}")
        if "localhost" in self.redis_url or "@" not in self.redis_url:
            problems.append("KP_REDIS_URL must point at the production Redis, with a password")
        if problems:
            raise ValueError("production configuration incomplete: " + "; ".join(problems))
        return self

    @field_validator("groq_api_key", "groq_model", "groq_check_model", mode="after")
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

    @field_validator("timezone", mode="after")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}, expected e.g. Europe/Paris") from exc
        return value

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
