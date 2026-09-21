# syntax=docker/dockerfile:1.10
#
# One image, two roles: the API and the ingestion worker run the same code and
# differ only by the command. Two images would let them drift apart, and they
# share the pipeline - a worker built from a different commit would write
# chunks the API cannot explain.
#
#   docker build -t knowpilot-backend:dev ./backend
#   docker run --rm -p 8000:8000 --env-file .env knowpilot-backend:dev
#
# The embedding model is NOT baked in: 2.2 GB of weights that change on their
# own schedule belong in a volume, not in a layer that every deploy re-pushes.


# ============================================================================
# Stage 1 - builder: resolve and install dependencies, and nothing else
# ============================================================================
# Pinned to a minor version, never `latest`: an image that rebuilds differently
# next month is not reproducible, and "it worked yesterday" is not a diagnosis.
# `slim` carries a real Debian userland without the 300 MB of tooling of the
# full image; `alpine` would mean musl, and PyTorch ships no musl wheels.
FROM python:3.12-slim-bookworm AS builder

# uv as a binary copied from its own published image, rather than installed
# with pip. It is one static file, the version is pinned, and no Python
# packages are added to the layer that builds the environment.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The most important five lines of the file.
#
# Only the manifest and the lock file are mounted, so this layer is rebuilt
# ONLY when a dependency changes. Editing application code - which happens a
# hundred times a day - reuses it, and a 1.4 GB install is skipped.
#
# `--mount=type=bind` rather than COPY: the files are needed during the
# command and never afterwards, so they never become part of a layer.
# `--mount=type=cache` keeps uv downloads between builds without storing them
# in the image either.
#
# `--frozen` fails if uv.lock disagrees with pyproject.toml, rather than
# silently resolving something new: the lock file is the build, and a build
# that quietly picks a different version is not the build that was tested.
# `--no-dev` leaves out pytest, mypy, ruff and reportlab - a test runner in a
# production image is dead weight and extra attack surface.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev


# ============================================================================
# Stage 2 - runtime: what actually ships
# ============================================================================
# A second FROM starts an empty image. Everything installed above - uv, the
# caches, the build tooling - is left behind unless it is copied explicitly.
# That is the whole point of a multi-stage build: the tools that build an
# artefact have no business running in production.
FROM python:3.12-slim-bookworm AS runtime

# Machine-readable provenance. A scanner, a registry and a colleague can all
# answer "where does this image come from" without asking anybody.
LABEL org.opencontainers.image.title="knowpilot-backend" \
      org.opencontainers.image.description="KnowPilot API and ingestion worker" \
      org.opencontainers.image.licenses="MIT"

# A dedicated unprivileged account, created before anything is copied.
#
# Containers run as root unless told otherwise, and root inside a container is
# root on the host kernel: a bug that writes outside the container writes as
# uid 0. The fixed uid matters too - a volume written as 1001 must stay
# readable when the image is rebuilt.
RUN groupadd --system --gid 1001 knowpilot \
 && useradd --system --uid 1001 --gid knowpilot --home-dir /home/knowpilot \
    --create-home knowpilot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# The finished environment, and nothing that built it.
# `--chown` at copy time rather than a later `RUN chown`: changing ownership
# afterwards rewrites every file into a new layer, doubling a 1.4 GB directory
# inside the image.
COPY --from=builder --chown=knowpilot:knowpilot /app/.venv /app/.venv

# Application code last, because it changes most often. Docker invalidates
# every layer after the first one that changed, so the order of a Dockerfile is
# "rarest to most frequent" - it is a caching decision before it is a
# readability one.
COPY --chown=knowpilot:knowpilot alembic.ini ./
COPY --chown=knowpilot:knowpilot alembic ./alembic
COPY --chown=knowpilot:knowpilot app ./app
COPY --chown=knowpilot:knowpilot scripts ./scripts

USER knowpilot

# Documentation, not a firewall rule: EXPOSE publishes nothing by itself. It
# tells a reader, and tooling, which port the process listens on.
EXPOSE 8000

# Readiness rather than liveness: the question a healthcheck answers is "may
# this container receive traffic", and the answer is no while PostgreSQL,
# Redis or the model are not ready. `depends_on: service_healthy` in Compose
# reads exactly this.
#
# `start-period` is generous because loading 2.2 GB of weights takes about
# twenty seconds, and a container killed during startup never starts at all.
#
# Written with urllib rather than curl: the slim image has no curl, and adding
# a package to a production image only to check on itself is a poor trade.
#
# NOTE - this healthcheck describes the API, which is the default role of this
# image. The worker has no HTTP port and overrides it in Compose. An image
# that serves two roles cannot carry one healthcheck for both.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4).status == 200 else 1)"

# Exec form, never the shell form. `CMD uvicorn ...` would run under /bin/sh,
# which does not forward signals: `docker stop` would time out and kill the
# process, cutting requests in flight instead of draining them.
#
# --host 0.0.0.0 because the container has its own network namespace: binding
# to 127.0.0.1 would make the service reachable only from inside itself.
#
# No --reload, no --workers: reload watches the filesystem for a code that
# never changes, and scaling is the job of the orchestrator, which can move
# replicas between machines - a second process on one machine cannot.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
