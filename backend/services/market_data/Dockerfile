# syntax=docker/dockerfile:1.7
###############################################################################
# Quantra MD read service.
#
# Two stages:
#   1. `builder`  installs uv, syncs the workspace into a vendored .venv (the
#      lockfile pin from D18 — `uv sync --frozen --no-dev`).
#   2. `runtime`  is a slim Python 3.12 image that copies the .venv and the
#      service source. No build tools, no caches, no uv binary.
#
# Build context is the monorepo root so the lockfile and packages/common are
# in scope:
#
#   docker build -f services/market_data/Dockerfile -t quantra-market-data .
#
# Runtime contract:
#   - Reads `POSTGRES_DSN_MD_RO` for the MD pool (matches D6 + D42).
#   - Optional `MD_SERVICE_PORT` (default 8082) and `DEFAULT_SNAPSHOT_NAME`.
#   - The service is read-only on the request path; write the legacy DSN
#     here while plan `market_data/03_md_schema_migration.md` is pending.
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

# Copy the workspace metadata first so the dependency layer caches across
# source-only edits. README.md files travel alongside their pyproject.toml
# because hatchling validates the configured `readme = "README.md"` field
# during the editable install in the next layer; missing the file fails
# the build with `OSError: Readme file does not exist`.
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/README.md packages/common/
COPY services/market_data/pyproject.toml services/market_data/README.md services/market_data/
COPY services/orchestrator/pyproject.toml services/orchestrator/README.md services/orchestrator/
COPY services/md_ingester/pyproject.toml services/md_ingester/README.md services/md_ingester/

# Install third-party deps + the workspace members in one frozen sync. The
# packages themselves are still source-less at this point, so uv only
# materialises namespace shims; the real source lands in the next stage.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-market-data --no-install-project

# Copy the source tree and finalise the workspace install.
COPY packages/common packages/common
COPY services/market_data services/market_data

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package quantra-market-data

# ----------------------------- runtime stage ---------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN groupadd --system --gid 10001 quantra \
    && useradd --system --uid 10001 --gid quantra --create-home --home-dir /home/quantra quantra

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/packages/common /app/packages/common
COPY --from=builder /app/services/market_data /app/services/market_data

WORKDIR /app
USER quantra

EXPOSE 8082

# Healthcheck mirrors the route — defense in depth against a partial boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, os; \
url = f'http://127.0.0.1:{os.getenv(\"MD_SERVICE_PORT\", \"8082\")}/health'; \
urllib.request.urlopen(url, timeout=3).read()" || exit 1

CMD ["python", "-m", "quantra_market_data"]
