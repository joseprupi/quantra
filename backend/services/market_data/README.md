# quantra-market-data

Internal **read-only** service over the `md.*` market-data schema.
The orchestrator (and, in the self-hosted bundle, the portal's Quote
Book via the portal's same-origin proxy) reads market data through it;
all writes belong to `services/md_ingester`.

The surface is deliberately small — four routes:

| Route | Purpose |
|---|---|
| `GET /health` | Liveness probe. |
| `GET /catalog/series` | List the series catalog (`md.canonical_ids`), optionally filtered by `?source=` (active vendor mappings), `?limit=`. |
| `GET /series/{canonical_id}` | Point range for one series from `md.quote_points` (`start`/`end` datetimes, `limit`, optional `max_points` downsampling). |
| `POST /quotes/resolved` | Batch resolver: given `canonical_ids` + `as_of` (+ optional `snapshot_version` etag pin), return one value per id. Unpinned lookups resolve the latest point at-or-before `as_of`; pinned lookups are exact against `md.snapshot_quotes`. Missing ids come back `found=false` — the caller decides how to fail. |

This is the resolver behind the orchestrator's server-side quote
resolution: pricing requests reference quote ids, the orchestrator
calls `POST /quotes/resolved`, and the engine only ever sees resolved
numeric values.

A middleware enforces the read-only posture: any `POST` outside the
explicit allow-list (the resolver) is rejected with 403, and
`PUT`/`PATCH`/`DELETE` are rejected outright.

## Layout

```
services/market_data/
├── Dockerfile                   # multi-stage uv-based build
├── pyproject.toml               # quantra-market-data workspace member
├── src/quantra_market_data/
│   ├── __main__.py              # `python -m quantra_market_data`
│   ├── app.py                   # FastAPI factory + middleware wiring
│   ├── settings.py              # MdServiceSettings (extends quantra_common.Settings)
│   ├── db.py                    # md_ro engine lifespan + dependency
│   ├── middleware.py            # read-only enforcement
│   ├── schemas.py               # response models
│   ├── _helpers.py              # iso() / downsample_rows()
│   └── routes/                  # health, quotes (series range + resolver), public (catalog)
└── tests/
```

Like the other services it builds its engine via
`quantra_common.db.make_md_engine(role="ro")` in the app lifespan (no
import-time side effects), logs through `structlog`, and echoes
`X-Request-Id` via the shared `RequestIdMiddleware`.

## Settings

| Env var | Default | Purpose |
|---|---|---|
| `POSTGRES_DSN_MD_RO` | unset | The `md.*` read DSN (required to serve data). |
| `MD_SERVICE_PORT` | `8082` | Bind port for `python -m quantra_market_data`. |
| `DEFAULT_SNAPSHOT_NAME` | `PUBLIC_USD_EOD` | Default snapshot name when a caller does not pin one. |

Pool sizing (`PG_POOL_SIZE_MD_RO` / `PG_POOL_MAX_OVERFLOW_MD_RO`) and
the rest of the shared config come from `quantra_common.Settings` —
see the repo-root `.env.example`.

## Run locally (no Docker)

```bash
# 1. Postgres with the md.* schema + some data (from the repo root):
docker compose up -d postgres
uv run alembic -n app upgrade head
uv run alembic -n md  upgrade head
uv run quantra-md-ingester ingest --source synthetic   # or a real source

# 2. Point the service at it and start it.
echo 'POSTGRES_DSN_MD_RO=postgresql+asyncpg://md_ro:md_ro@localhost:5432/quantra' >> .env
uv run --package quantra-market-data python -m quantra_market_data

# 3. Smoke it.
curl -fsS http://localhost:8082/health
curl -fsS 'http://localhost:8082/catalog/series?limit=5'
```

## Run locally (Docker Compose)

The repo-root `docker-compose.yml` (one directory above `backend/`) builds
and runs this service as part of the whole app:

```bash
# from the repo root
docker compose up -d --build market_data
curl -fsS http://localhost:8082/health
```

## Tests

The fast suite is hermetic:

```bash
uv run pytest services/market_data
```

DB-backed integration tests (marker `md_db`) are skipped by default;
to run them against the dev Postgres:

```bash
export QUANTRA_MD_TEST_DSN=postgresql+asyncpg://md_ro:md_ro@localhost:5432/quantra
export QUANTRA_MD_TEST_ADMIN_DSN=postgresql+asyncpg://quantra:quantra@localhost:5432/quantra
uv run pytest -m md_db services/market_data
```

The fixture creates a temporary schema mirroring the `md.*` layout,
points `md_ro`'s `search_path` at it for the duration, and tears
everything down on exit.

## Build the image

The Dockerfile uses the monorepo root as its build context so
`uv.lock` and `packages/common` are in scope:

```bash
docker build -f services/market_data/Dockerfile -t quantra-market-data .
docker run --rm -p 8082:8082 \
    -e POSTGRES_DSN_MD_RO=postgresql+asyncpg://md_ro:secret@host.docker.internal:5432/quantra \
    quantra-market-data
```
