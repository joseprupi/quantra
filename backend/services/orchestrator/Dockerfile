# syntax=docker/dockerfile:1.7
###############################################################################
# Quantra orchestrator (Quantra API).
#
# Two stages, mirroring services/market_data/Dockerfile:
#   1. `builder`  installs uv, syncs the workspace into a vendored .venv from
#      the lockfile pin (D18 — `uv sync --frozen --no-dev`).
#   2. `runtime`  is a slim Python 3.12 image that copies the .venv and the
#      orchestrator source. No build tools, no caches, no uv binary.
#
# Build context is the monorepo root so the lockfile and packages/common are
# in scope:
#
#   docker build -f services/orchestrator/Dockerfile -t quantra-orchestrator .
#
# Runtime contract:
#   - Reads BUILD_SHA at image build time and threads it into the runtime env
#     so `GET /health` reports the SHA the image was cut from.
#   - Optional ORCHESTRATOR_PORT (default 8080).
#   - Subsequent plans (auth, DB engines, MD client, engine client) layer
#     additional env vars on top of the shared `quantra_common.settings` model;
#     this Dockerfile does not need to enumerate them.
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
# install in the next layer; missing the file fails the build with
# `OSError: Readme file does not exist`.
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/README.md packages/common/
COPY services/market_data/pyproject.toml services/market_data/README.md services/market_data/
COPY services/orchestrator/pyproject.toml services/orchestrator/README.md services/orchestrator/
COPY services/md_ingester/pyproject.toml services/md_ingester/README.md services/md_ingester/

# Deps-only layer: install just the third-party dependency closure and defer
# EVERY workspace member (quantra-common, quantra-orchestrator, and — since
# D164 — quantra-md-ingester) to the second sync below, once their source
# trees are copied. ``--no-install-workspace`` (not ``--no-install-project``)
# is required because quantra-md-ingester force-includes a ``configs`` dir at
# wheel-build time; an editable build of it in this source-less layer would
# fail on the missing forced-include path.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-orchestrator --no-install-workspace

COPY packages/common packages/common
COPY services/orchestrator services/orchestrator
# D164 — the orchestrator now depends on ``quantra-md-ingester`` for the
# SHARED md.* writer/CSV-import logic (the import route reuses it rather
# than duplicating SQL). The workspace install needs the ingester source
# tree present to build its editable install.
COPY services/md_ingester services/md_ingester

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-orchestrator

# ----------------------------- runtime stage ---------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# BUILD_SHA is injected at `docker build --build-arg BUILD_SHA=...` time and
# baked into the image's environment so `GET /health` reports the SHA the
# image was cut from. Defaults to "dev" so `docker build` works without args.
ARG BUILD_SHA=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    BUILD_SHA=${BUILD_SHA}

RUN groupadd --system --gid 10001 quantra \
    && useradd --system --uid 10001 --gid quantra --create-home --home-dir /home/quantra quantra

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/packages/common /app/packages/common
COPY --from=builder /app/services/orchestrator /app/services/orchestrator
# D164 — the editable ``quantra-md-ingester`` install (SHARED md.* writer +
# CSV import used by the market-data import route) needs its source tree at
# runtime, same as packages/common and services/orchestrator above.
COPY --from=builder /app/services/md_ingester /app/services/md_ingester

# First-boot init assets vendored into the image so the release (image-only)
# bundle needs NO repo checkout beside the compose file. Two one-shot init
# services reuse this orchestrator image and previously bind-mounted these
# from the repo via ``../``:
#   * ``migrate`` runs ``alembic -n {app,md} upgrade head`` from WORKDIR /app,
#     which needs ``alembic.ini`` (``script_location``/``version_locations``
#     are repo-root-relative) + the whole ``migrations/`` tree.
#   * ``init-seed`` runs ``scripts/seed_demo_entities.py``, which seeds the
#     real reference entities programmatically (no fixture file).
# Only the seed script is vendored from ``scripts/`` (the other scripts are
# dev-only and would bloat the image). alembic itself ships in the venv via
# ``quantra-common``.
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY scripts/seed_demo_entities.py /app/scripts/seed_demo_entities.py

# The editable-install source tree is imported at runtime by the non-root
# ``quantra`` user. Some vendored subtrees (e.g. the flatbuffers-generated
# ``quantra_common.engine_client._generated.quantra`` package) carry
# restrictive ``0750 root:root`` perms from the generator's umask; preserved
# through COPY they become unreadable to uid 10001 and imports fail with a
# spurious ``ModuleNotFoundError``. Normalize to read+traverse-for-all so the
# runtime user can import every module regardless of source-tree perms — the
# vendored init assets above are included so migrate/seed can read them too.
RUN chmod -R a+rX /app/packages /app/services /app/alembic.ini /app/migrations /app/scripts

WORKDIR /app
USER quantra

EXPOSE 8080

# Healthcheck mirrors the route — defense in depth against a partial boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, os; \
url = f'http://127.0.0.1:{os.getenv(\"ORCHESTRATOR_PORT\", \"8080\")}/health'; \
urllib.request.urlopen(url, timeout=3).read()" || exit 1

CMD ["python", "-m", "quantra_orchestrator"]
