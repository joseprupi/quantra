# syntax=docker/dockerfile:1.7
###############################################################################
# Build image for `services/md_ingester`.
#
# Two stages, mirroring services/orchestrator/Dockerfile and
# services/market_data/Dockerfile:
#   1. `builder`  syncs the workspace into a vendored /opt/venv from the
#      lockfile pin (D18 — `uv sync --frozen --no-dev`).
#   2. `runtime`  slim Python 3.12 image with the editable .py source.
#
# Build context is the monorepo root so the lockfile and packages/common are
# in scope:
#
#   docker build -f services/md_ingester/Dockerfile -t quantra-md-ingester .
#
# Runtime contract (unchanged): the console entry point takes a subcommand +
# args, e.g. compose invokes `ingest --source synthetic --as-of 2025-01-15`.
###############################################################################

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5

# ----------------------------- builder stage ---------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Workspace metadata first so the dependency layer caches across source-only
# edits. README.md files travel alongside their pyproject.toml because
# hatchling validates the `readme = "README.md"` field during the editable
# install; missing the file fails the build with
# `OSError: Readme file does not exist`.
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/README.md packages/common/
COPY services/market_data/pyproject.toml services/market_data/README.md services/market_data/
COPY services/orchestrator/pyproject.toml services/orchestrator/README.md services/orchestrator/
COPY services/md_ingester/pyproject.toml services/md_ingester/README.md services/md_ingester/

# Deps-only layer: install just the third-party dependency closure and defer
# EVERY workspace member to the second sync below, once the source is copied.
# ``--no-install-workspace`` (not ``--no-install-project``) is required because
# quantra-md-ingester force-includes a ``configs`` dir at wheel-build time; an
# editable build of it in this source-less layer would fail on the missing
# forced-include path (same reasoning as the orchestrator Dockerfile).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-md-ingester --no-install-workspace

# Members the ingester links against: itself + the shared library.
COPY packages/common packages/common
COPY services/md_ingester services/md_ingester

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-md-ingester

# ----------------------------- runtime stage ---------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN groupadd --system --gid 10001 quantra \
    && useradd --system --uid 10001 --gid quantra --create-home --home-dir /home/quantra quantra

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/packages/common /app/packages/common
COPY --from=builder /app/services/md_ingester /app/services/md_ingester

RUN chmod -R a+rX /app/packages /app/services

WORKDIR /app
USER quantra

# The console script (registered in pyproject.toml) is on PATH via /opt/venv/bin.
# Compose passes a subcommand + args as the container command, e.g.
# ``ingest --source synthetic --as-of 2025-01-15``, which appends to this
# ENTRYPOINT. ``--help`` is the safe default with no command.
ENTRYPOINT ["quantra-md-ingester"]
CMD ["--help"]
