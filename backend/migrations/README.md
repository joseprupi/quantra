# Migrations

A single Alembic environment manages two schemas inside one Postgres
instance — `app.*` (user data) and `md.*` (market data).

```
migrations/
├── alembic/
│   ├── env.py               # one env, dual-schema, async
│   ├── script.py.mako       # revision template
│   └── versions/
│       ├── app/             # app.* schema migrations (alembic_version_app)
│       │   ├── 0001_init.py
│       │   ├── 0002_users_and_api_keys.py
│       │   ├── 0003_curves_and_indices.py
│       │   ├── 0004_snapshots_and_quote_book.py
│       │   ├── 0005_vol_surfaces_and_models.py
│       │   ├── 0006_products.py
│       │   ├── 0007_pricing_history.py
│       │   ├── 0008_role_connection_limits.py
│       │   ├── 0009_pricing_traces.py
│       │   ├── 0010_pricing_traces_product.py
│       │   ├── 0011_pricing_traces_summary.py
│       │   └── 0012_drop_quotes_saved.py
│       └── md/              # md.* schema migrations (alembic_version_md)
│           ├── 0001_init.py
│           ├── 0002_catalog.py
│           ├── 0003_quote_points.py
│           ├── 0004_snapshots.py
│           ├── 0005_ingestion_log.py
│           ├── 0006_role_connection_limits.py
│           ├── 0007_quotes_timeseries_view.py
│           ├── 0008_canonical_instruments_view.py
│           └── 0009_drop_md_drift_views.py
└── scripts/
    ├── roundtrip_app_schema.py  # ad-hoc app.* end-to-end verification
    ├── roundtrip_md_schema.py   # ad-hoc md.* end-to-end verification
    └── saturate_md_rw.py        # pool isolation acceptance runbook
```

## How the dual-schema env works

`alembic.ini` (at the repo root) declares two named sections, `[app]`
and `[md]`. Each fixes the section's `version_locations` (one of the
two `versions/<schema>/` directories) and `version_table`
(`alembic_version_app` / `alembic_version_md`). Both version tables
live in the `public` schema so neither runtime role needs to see them.

The schema is selected per invocation:

```bash
uv run alembic -n app upgrade head
uv run alembic -n md  upgrade head
```

`env.py`:

- Reads `config.config_ini_section` to learn which schema this run
  targets, and refuses to proceed on the default `[alembic]` section
  (so a forgotten `-n` flag never silently writes to the wrong
  version table).
- Pulls the connection URL from `Settings.require_postgres_dsn_admin()`
  — the admin DSN, since migrations need superuser/owner privileges
  (`CREATE ROLE`, `CREATE EXTENSION`, `CREATE SCHEMA`, `ALTER ROLE
  ... SET search_path`).
- Runs migrations through SQLAlchemy's async engine
  (`async_engine_from_config` + `connection.run_sync`), so the only
  Postgres driver the workspace ships is asyncpg.

## Naming convention

Per-schema sequential, zero-padded: `0001_init.py`, `0002_<slug>.py`,
…. The two schemas keep independent counters because they are two
independent DAGs. Timestamped revision IDs were considered and
rejected — sequential numbers read more cleanly when the two
sequences interleave in PR diffs.

When generating a new revision, scope it explicitly to one schema:

```bash
uv run alembic -n app revision -m "add users table"
# writes migrations/alembic/versions/app/000N_add_users_table.py
```

## Authoring rules

1. **Use schema-qualified DDL.** Migrations run as the admin role,
   whose default `search_path` is `public`. Always write
   `CREATE TABLE app.foo (…)`, never `SET search_path = app; CREATE
   TABLE foo (…)`.
2. **Idempotent guards.** Use `CREATE … IF NOT EXISTS` for schemas,
   tables, indexes, and extensions. Wrap `CREATE ROLE` in a
   `DO $$ … pg_roles … $$` block. Repeating `ALTER DEFAULT PRIVILEGES`
   with the same target is a no-op, so it's safe by construction.
3. **Schema-qualified extension calls.** Roles' `search_path` is
   pinned to their own schema (no `public`). Call extension
   functions as `public.gen_random_uuid()`, `public.digest(...)`,
   etc. — never bare.
4. **Same admin role across runs.** `ALTER DEFAULT PRIVILEGES` only
   takes effect for objects created by the role that ran the alter.
   All migrations must run under the same `POSTGRES_DSN_ADMIN` role
   (the dev container's superuser is `quantra`).
5. **Don't grant `CREATE` on the schema to runtime roles.** Tables
   are created by the migration; runtime roles only need DML.

## Connection / role cheat sheet

| Role     | Schema | Privileges               | Search path | DSN env var               |
|----------|--------|--------------------------|-------------|---------------------------|
| `app_rw` | `app`  | SELECT/INSERT/UPDATE/DELETE | `app`    | `POSTGRES_DSN_APP_RW`     |
| `app_ro` | `app`  | SELECT                   | `app`       | `POSTGRES_DSN_APP_RO`     |
| `md_rw`  | `md`   | SELECT/INSERT/UPDATE/DELETE | `md`     | `POSTGRES_DSN_MD_RW`      |
| `md_ro`  | `md`   | SELECT                   | `md`        | `POSTGRES_DSN_MD_RO`      |
| `quantra` (dev superuser) | n/a | superuser, migrations only | (default) | `POSTGRES_DSN_ADMIN` |

The four runtime DSNs go to the orchestrator and the MD service /
ingester. The admin DSN is read **only** by `migrations/alembic/env.py`.

## `app.*` entity tables

The tables provisioned by `0002`–`0012` cover every entity the portal
persists (plus the pricing history / trace logs). Each "named entity" row
follows the same template — `id UUID PK`, `owner_uid` (FK to
`app.users(uid) ON DELETE RESTRICT`), `name`, JSONB body, `created_at`,
`updated_at` (maintained by the shared `app.set_updated_at()` trigger),
`deleted_at` (soft delete), `(owner_uid)` index, and a partial unique
index on `(owner_uid, name) WHERE deleted_at IS NULL`. Cross-entity
references inside JSONB bodies are **soft references**: the linked row
may legitimately be deleted or replaced by an inline definition.

| Table | Purpose |
|---|---|
| `users` | Firebase-authenticated user. `uid` is the natural PK; every other table FKs here. |
| `api_keys` | Programmatic credentials for non-portal clients. `key_hash` is the SHA-256 of the raw key. |
| `indices` | Saved IBOR / Overnight / Inflation index definitions. |
| `curves` | Saved discount / forward / inflation curves. `points` JSONB stays separate from the rest of the body. |
| `curve_sets` | Named bundles of curve / credit-curve / quote IDs (soft refs). |
| `credit_curves` | Saved credit curves (flat / manual / quote-book-backed). |
| `snapshots` | A named, dated bundle of quote references. |
| `quote_book` | One row per user — the time-series quote book (`UNIQUE (owner_uid)`). |
| `vol_surfaces` | Saved swaption / equity / optionlet vol surface specs (`kind` discriminator). |
| `swaption_models` | Calibrated short-rate models (Hull-White today). |
| `swaps_ir` | Saved interest-rate swaps (full request JSONB). |
| `swaps_inflation` | Saved inflation swaps. |
| `swaptions` | Saved swaptions. |
| `bonds_fixed` | Saved fixed-rate bonds. |
| `bonds_floating` | Saved floating-rate bonds. |
| `cds` | Saved credit default swaps. |
| `equity_options` | Saved equity options. |
| `pricing_history` | Append-only log of every pricing call. |
| `pricing_traces` | Per-request pricing pipeline traces (the portal's Investigate page). |

### Verifying the schema end-to-end

`scripts/roundtrip_app_schema.py` is an ad-hoc verification script: it
inserts a representative row into every entity table via `app_rw`,
reads it back via `app_ro`, asserts byte-for-byte equality on first-class
and JSONB columns, and confirms that `app_ro` is denied INSERT / UPDATE /
DELETE. Run it after a fresh `alembic -n app upgrade head`:

```bash
uv run python migrations/scripts/roundtrip_app_schema.py
```

It is deliberately **not** part of `pytest`; CI doesn't talk to a live
Postgres yet (`docker compose` is only available in the dev workflow).

## Per-role `CONNECTION LIMIT`

Each schema's role pair gets a Postgres-side cap on simultaneous
connections, set by an additive migration that lives next to the role's
`0001_init.py`:

| Role     | Migration | Default `rolconnlimit` |
|----------|-----------|:----------------------:|
| `app_rw` | `app/0008_role_connection_limits.py` | 30 |
| `app_ro` | `app/0008_role_connection_limits.py` | 15 |
| `md_rw`  | `md/0006_role_connection_limits.py`  | 8  |
| `md_ro`  | `md/0006_role_connection_limits.py`  | 25 |

The values are the per-process pool maximum
(`pool_size + max_overflow`) for that role; both must move together
when the matching `Settings` defaults change, keeping the
`Σ rolconnlimit + admin_headroom ≤ max_connections` invariant (see the
pool-sizing notes in the backend [`README.md`][repo-readme] and
`packages/common/src/quantra_common/settings/base.py`).

[repo-readme]: ../README.md

The acceptance runbook is `scripts/saturate_md_rw.py` — it pins every
`md_rw` slot, confirms one extra checkout raises a client-side
`TimeoutError`, then runs five `app_rw` queries to prove isolation.

```bash
uv run python migrations/scripts/saturate_md_rw.py
```

## Operational notes

- **Backup / restore (dev):**

  ```bash
  # container name for the repo-root compose stack (the backend dev
  # compose uses quantra-backend-postgres instead):
  docker exec quantra-oss-postgres pg_dump -U quantra -d quantra \
    --schema=app --schema=md > quantra-backup.sql
  docker exec -i quantra-oss-postgres psql -U quantra -d quantra \
    < quantra-backup.sql
  ```

  For the safe production-style upgrade/backup flow, see
  [`../deploy/UPGRADE.md`](../deploy/UPGRADE.md).

- **Resetting a dev database from scratch:**

  ```bash
  docker compose down -v   # nukes the postgres_data volume
  docker compose up -d postgres
  uv run alembic -n app upgrade head
  uv run alembic -n md  upgrade head
  ```
