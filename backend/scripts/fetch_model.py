"""Download the embedding model into its volume, and prove it is the right one.

    python -m scripts.fetch_model

The one-shot `model` service of docker-compose.prod.yml. It runs BEFORE the
API and the worker, which mount the same volume read-only and run with
HF_HUB_OFFLINE=1: they never reach Hugging Face, so a restart never waits on
a 2.2 GB download, and an outage of the model hub cannot take the service
down with it.

Idempotent: with the weights already in the volume, it loads them from disk
and exits. It loads through `load_checked_embeddings`, the same function the
services use, so a model whose vectors do not match the database column fails
HERE - in a job whose failure stops the deployment - rather than on the first
upload.

It reads its two settings directly instead of building `Settings`: the full
settings object refuses to exist in production without the database password,
the OIDC secret and the Redis URL, and a job that only downloads weights has
no business holding any of them. Least privilege, applied to configuration.
"""

from __future__ import annotations

import logging
import os

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.vector_store import load_checked_embeddings

logger = logging.getLogger("app.fetch_model")


def main() -> None:
    configure_logging("INFO")
    default = str(Settings.model_fields["embedding_model"].default)
    name = os.environ.get("KP_EMBEDDING_MODEL", default)
    cache_dir = os.environ.get("KP_EMBEDDING_CACHE_DIR")
    load_checked_embeddings(name, cache_dir)
    logger.info("embedding model %s ready in %s", name, cache_dir)


if __name__ == "__main__":
    main()
