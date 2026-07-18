# Quantra backend

The backend half of the **Quantra** monorepo — a self-hostable
derivatives-pricing application. This Python workspace contains the three
services that sit between the browser portal and the open-source
[quantraserver](https://github.com/joseprupi/quantraserver) pricing
engine (C++ / QuantLib, gRPC):

- **`services/orchestrator`** — the public REST API (FastAPI). Owns
  users' reference data and saved products, resolves every market-data
  reference server-side, and drives the pricing engine over gRPC.
- **`services/market_data`** — a small internal read-only service over
  the `md.*` market-data schema (catalog, series ranges, batch quote
  resolution).
- **`services/md_ingester`** — a scheduled worker + CLI that ingests
  public market data (Bank of England, US Treasury, ECB, FRED) and a
  deterministic synthetic demo dataset into `md.*`.

The browser frontend lives in the sibling
[`../frontend`](../frontend) directory of this monorepo; the
pricing engine is a separate open-source project and is consumed here
only as a Docker image / gRPC endpoint.

Licensed under **AGPL-3.0** (see [License](#license)).

## Architecture

```
                              browser portal (React)           ../frontend
                                        │  REST /v1/*
                                        │  (same-origin nginx proxy)
                                        ▼
 ┌────────────────┐    gRPC     ┌────────────────┐   HTTP   ┌────────────────┐
 │ quantraserver  │◀────────────│  orchestrator  │─────────▶│  market_data   │
 │ (C++/QuantLib) │(FlatBuffers)│   (FastAPI)    │          │  (read-only)   │
 │  stateless —   │             └───────┬────────┘          └───────┬────────┘
 │  never touches │                     │ app.*                     │ md.*
 │  the database  │                     │ read/write                │ read-only
 └────────────────┘                     ▼                           ▼
                        ┌─────────────────────────────────────────────────────┐
                        │                      Postgres                       │
                        │  app.*  user data, saved products, pricing traces   │
                        │  md.*   market-data catalog + quote history         │
                        └─────────────────────────────────────────────────────┘
                                                            ▲
                                                            │ md.* write
                                                    ┌───────┴────────┐
     BoE / US Treasury / ECB / FRED ───────────────▶│  md_ingester   │
     (public data)                                  │ (worker + CLI) │
                                                    └────────────────┘
```

Key invariants:

- The frontend only ever talks to the orchestrator. Nothing else is
  public.
- Market data referenced by quote id is resolved **server-side**; the
  engine only ever receives fully self-contained requests (it never
  sees quote ids or database references).
- One Postgres, two schemas (`app.*` for user data, `md.*` for market
  data), four roles (`app_rw`/`app_ro`/`md_rw`/`md_ro`) with
  schema-scoped grants and per-role connection pools.

## Quickstart — run the whole app

The fastest way to see everything working — Postgres + engine +
market-data service + orchestrator + browser portal, pre-seeded and
pricing out of the box — is the **repo-root compose file** (one
directory above this one):

```bash
# from the repo root
docker compose up -d
# then open http://localhost:5173
```

It builds everything from this tree; no prebuilt Quantra images are
needed (none are published yet — `deploy/` holds the release tooling
kept for that future, see [`deploy/README.md`](deploy/README.md)).

The stack prices with **real, daily-refreshed public market data**: a
genuine Bank of England SONIA OIS curve for GBP swaps, US Treasury /
BoE gilt government curves, ECB FX + inflation series, and (with a free
`FRED_API_KEY`) ~31 FRED series. A cron sidecar keeps the feeds
current — see [`deploy/README.md`](deploy/README.md).

Optional smoke test once the stack is up:

```bash
uv run python scripts/self_hosted_smoke.py
```

## Development setup

The workspace tool is [uv](https://docs.astral.sh/uv/) (>= 0.11);
Python 3.12+ is pinned via `.python-version` (uv fetches an interpreter
automatically if needed). One root `.venv`, one lockfile
(`uv.lock`, checked in; CI installs with `uv sync --frozen`).

```bash
# 1. Install uv.
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Sync the workspace from the repo root.
uv sync

# 3. Run the gates (same three CI runs).
uv run ruff check .
uv run mypy packages services
uv run pytest
```

`.github/workflows/ci.yml` runs those three gates plus a frozen-lockfile
check on every push and pull request.

### Local Postgres

```bash
docker compose up -d postgres      # Postgres 16 on ${POSTGRES_HOST_PORT:-5432}
cp .env.example .env               # one-time
uv run alembic -n app upgrade head # provision app.* schema + roles
uv run alembic -n md  upgrade head # provision md.* schema + roles
```

This provisions the two schemas, the four per-schema roles (each with
`search_path` pinned to its own schema and no cross-schema grants), the
`pgcrypto`/`pg_trgm`/`btree_gist` extensions in `public`, and per-schema
Alembic version tables. Full authoring rules, naming conventions, and
backup/restore notes: [`migrations/README.md`](migrations/README.md).

Connection pools are sized **per role** (SQLAlchemy `QueuePool` bounded
by `PG_POOL_SIZE_<ROLE>` / `PG_POOL_MAX_OVERFLOW_<ROLE>`) and capped
cluster-side by `ALTER ROLE ... CONNECTION LIMIT` migrations, so e.g. an
ingestion spike on `md_rw` cannot starve orchestrator traffic. Defaults
fit within Postgres' stock `max_connections=100`; the constants live in
`packages/common/src/quantra_common/settings/base.py` and the two
`*_role_connection_limits.py` migrations.

## Repo layout

```
backend/
├── pyproject.toml          # uv workspace root + shared tool config (ruff, mypy, pytest)
├── uv.lock                 # checked in; CI runs --frozen
├── docker-compose.yml      # backend DEV stack: postgres + opt-in profiles
├── packages/
│   └── common/             # shared lib: settings, db engines, auth, logging,
│                           #   MD client, engine gRPC client + vendored bindings
├── services/
│   ├── orchestrator/       # public FastAPI API (see its README)
│   ├── market_data/        # internal read-only MD service
│   └── md_ingester/        # market-data ingest worker + CLI
├── migrations/alembic/     # single Alembic env, two schemas (app.* + md.*)
├── deploy/                 # maintainer release tooling (offline tarball, upgrade)
├── runbooks/               # operational walkthroughs
└── scripts/                # entity seed, smoke test, engine-binding regen
```

Each `packages/<x>/` and `services/<x>/` directory is its own
[uv workspace member](https://docs.astral.sh/uv/concepts/projects/workspaces/)
with its own `pyproject.toml`; all share the root `.venv`, lockfile,
and tool config.

### Working in the monorepo

- Add a runtime dep to one service:
  `uv add --package quantra-orchestrator <pkg>`.
- Add a shared dev dep: `uv add --group dev <pkg>`.
- Run a service's entry point:
  `uv run --package quantra-orchestrator python -m quantra_orchestrator`.

## Related code

- **Pricing engine:** <https://github.com/joseprupi/quantraserver> —
  open-source C++ / QuantLib pricing server (gRPC + JSON API). This
  tree consumes its published Docker image.
- **Portal:** [`../frontend`](../frontend) — the React/Vite browser
  frontend (same monorepo).

## License

This project is licensed under the **GNU Affero General Public License
v3.0** — see [`LICENSE`](LICENSE). In short: you can use, modify, and
self-host it freely; if you run a modified version as a network service
you must make your modified source available to its users.
