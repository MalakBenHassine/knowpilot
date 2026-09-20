"""Logging configuration.

Uvicorn configures its own loggers and leaves the root logger untouched, so
`logger.info(...)` written by application code goes nowhere at all.

That is not a cosmetic detail. The ingestion pipeline logs every stage it
enters and every failure it catches, and none of it was ever printed - so the
first real upload failed silently and had to be diagnosed by reading the
database instead of the logs. A system you cannot observe is a system you
debug by guessing.
"""

from logging.config import dictConfig


def configure_logging(level: str = "INFO") -> None:
    """Send application logs to stderr, and keep libraries quiet.

    Only the `app` logger is turned up. Third-party libraries stay at WARNING:
    at INFO, a single model load prints forty lines of HTTP chatter from the
    Hugging Face downloader, and the one line that matters drowns in it.
    """
    dictConfig(
        {
            "version": 1,
            # Uvicorn has already configured its own loggers by this point.
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    "datefmt": "%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    # stderr, not stdout: logs are diagnostics, not output. A
                    # container runtime collects both, but keeping them apart
                    # means piping the output of a command never mixes them.
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                "app": {"handlers": ["console"], "level": level, "propagate": False},
            },
            "root": {"handlers": ["console"], "level": "WARNING"},
        }
    )
