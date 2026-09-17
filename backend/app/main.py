from fastapi import FastAPI

from app.api.routes import health
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="KnowPilot API",
    # The interactive docs are useful while developing and are a needless
    # disclosure in production, where they are disabled.
    docs_url="/api/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.environment != "production" else None,
)

# Every route lives under /api: the reverse proxy serves the SPA at / and
# forwards /api here, so both share one origin (no CORS, first-party cookie).
app.include_router(health.router, prefix="/api")
