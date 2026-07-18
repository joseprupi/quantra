# quantra-md-ingester

Scheduled worker + CLI that writes market data into the `md.*` schema
(`md.canonical_ids`, `md.vendor_mappings`, `md.quote_points`,
`md.snapshots`, `md.snapshot_quotes`). It is the only writer on that
schema's request-free rail — the read path belongs to
`services/market_data`, and it is never on any request path itself.

## Sources

| Source | Feed | Key needed | What it ingests |
|---|---|---|---|
| `boe_ois` | Bank of England OIS spot curve | no | Real GBP SONIA OIS par rates, `GBP.RATES.BOE.OIS.{6M..25Y}.PAR` (10 tenors; zero → discount factor → par conversion) |
| `boe` | Bank of England | no | SONIA fixing + gilt government curve (6 tenors) |
| `treasury` | US Treasury | no | Nominal par curve (10 tenors), real curve (5), bill rates (4) — `USD.RATES.UST.OFFICIAL.*` |
| `ecb` | ECB data portal | no | EUR FX reference rates (4) + HICP inflation series (2) |
| `fred` | FRED (St. Louis Fed) | **`FRED_API_KEY`** (free) | ~31 US series: UST DGS yields, SOFR/EFFR, CPI/PCE, inflation breakevens, credit OAS, VIX/MOVE |
| `synthetic` | generated | no | Deterministic demo standing data for the full portal canonical-id vocabulary (23 series) — **opt-in only** |

A bare `--source`-less invocation defaults to the four real-vendor
sources (`fred,ecb,boe,treasury`). `boe_ois` and `synthetic` must be
named explicitly: `boe_ois` because its natural window differs (see
`--since-month-start` below), `synthetic` so demo data can never
silently mix into a real-vendor run.

Any subcommand touching FRED without `FRED_API_KEY` fails that source
and skips it; every keyless source still works.

## CLI

```bash
uv run quantra-md-ingester <subcommand> [flags]
```

| Subcommand | Purpose |
|---|---|
| `ingest` | Fetch + upsert quotes for the window. No snapshot. `--start-date` defaults to `--as-of` (one-day incremental). |
| `backfill` | Like `ingest` but requires an explicit `--start-date` (intentional historical pull). |
| `build-snapshot` | Rebuild the named snapshot (default `PUBLIC_USD_EOD`) from data already in `quote_points`. `--as-of` defaults to today UTC. |
| `run-pipeline` | `ingest` then `build-snapshot` in two transactions. |
| `run` | Alias for `run-pipeline` with today's UTC date and default sources. |
| `roll-curve-dates` | Bump the seeded MD-backed demo curves' `reference_date` (in `app.curves`) to the latest ingested business day for the series they reference. Marker- and owner-scoped; idempotent; run after the real ingests. |

Window flags: `--as-of` (end date, default today UTC), `--start-date`
(explicit window start), and `--since-month-start` (anchor the window
at the first of `--as-of`'s month when no `--start-date` is given —
used for the real curve feeds like `boe_ois`, where a current-month
window pulls the fresher "latest" workbook instead of an empty one-day
window on a date the feed has no data for).

Every subcommand prints a JSON summary; `build-snapshot` includes
`version_etag_before` / `version_etag_after` so the snapshot etag
trigger's behavior is visible without a `psql` session.

## Quickstart

```bash
# 1. Bring up dev Postgres and apply migrations (once, from the repo root).
docker compose up -d postgres
uv run alembic -n app upgrade head
uv run alembic -n md  upgrade head

# 2. Ingest the keyless real feeds for the current month.
uv run quantra-md-ingester ingest --source boe_ois,treasury --since-month-start

# 3. (Optional) FRED too — free signup at https://fred.stlouisfed.org.
export FRED_API_KEY=...
uv run quantra-md-ingester ingest --source fred --since-month-start

# 4. Rebuild the default snapshot.
uv run quantra-md-ingester build-snapshot
```

### Synthetic demo data

`--source synthetic` generates economically-plausible, deterministic
demo market data covering the full portal vocabulary so all six
products price end-to-end with no vendor subscription:

```bash
uv run quantra-md-ingester ingest --source synthetic
uv run quantra-md-ingester build-snapshot
```

- **Not real data.** Every row carries `source="synthetic"`,
  `meta.synthetic = true`, and a human-readable disclaimer; the tag
  also flows into `md.snapshot_quotes.source`, so provenance is always
  visible.
- **Economic model.** Rate curves (`USD.IRS`, `USD.SOFR`, `EUR.IRS`)
  are upward-sloping, strictly monotone
  (`base + slope·(1 − e^{−t/decay})`) and span to 30Y so the engine
  bootstraps past the longest product maturity. A per-curve, per-day
  parallel shift (seeded by `as_of`) keeps values positive and monotone
  while drifting day to day. Swaption vol ≈ 22% Black, HICP inflation
  ≈ 2.1%, dividend yield ≈ 1.2%.
- **Deterministic.** The same `as_of` reproduces the same values. See
  `connectors/synthetic.py` and `configs/synthetic/standing_demo.json`.
- The short portal ids it writes (`USD.IRS.5Y`,
  `USD.SWPTN.ATM.5Y10Y.VOL`, `AAPL.DIV.1Y`, …) are accepted by the
  write path via the short-id grammar in `canonical.py`.

## Units — honest per series

- Rate connectors normalize vendor **percent to decimals**
  (`units='decimal_rate'`; the raw percent is kept in `raw_value` /
  `meta.raw_value_percent`).
- Series flagged `vendor_unit: "level"` (CPI/PCE indices, VIX, MOVE,
  SOFR index) are stored verbatim with `units='level'`.
- ECB FX reference rates are stored verbatim with `units='spot_rate'`.
- `md.canonical_ids.units` is set on first sight of a series and never
  clobbered afterwards.

## Architecture: connectors + configs

Each source is a connector module in `connectors/` (fetch + parse)
driven by declarative JSON configs in `configs/<source>/` that map
vendor series to canonical ids (with per-series unit flags). The
pipeline (`pipeline.py`) fans out over the requested sources and
upserts through the shared `writer.py`. Connectors log-and-skip
per-series failures; a hard per-source failure (e.g. a missing
`FRED_API_KEY`) fails the invocation with a structured error before
any write transaction opens — which is why the schedulers run each
source as its own job.

To add a connector: write `connectors/<source>.py` returning
`QuoteRecord`s, add a `configs/<source>/*.json` mapping, register the
source in `pipeline.py` (`ALL_SOURCES` / the dispatch block), and
decide whether it belongs in `DEFAULT_SOURCES`. A vendor mapping ships
as config only when it is a real, verified series — placeholder
mappings that "roughly match" have historically written junk fixings
and are deliberately not accepted.

Two write-path rules enforced by the shared writer:

- **Imports only add values to existing series.** The user-facing
  import route (orchestrator `POST /v1/market-data/import`, which
  reuses this service's writer) rejects values for canonical ids that
  don't exist yet — series are created explicitly, never as bare
  auto-created rows.
- **Nightly ingests never clobber user metadata.** The canonical-ids
  upsert only fills `description` when it is empty, so a user's series
  description edit survives the next scheduled tick.

## Connection routing

The worker builds its engine via
`quantra_common.db.make_md_engine(role="rw")`, so the `md_rw` pool
isolation applies. DSN comes from `POSTGRES_DSN_MD_RW`;
`roll-curve-dates` additionally needs `POSTGRES_DSN_APP_RW` (it updates
`app.curves`). See the repo-root `.env.example`.

## Scheduled runner

The self-hosted release bundle
([`deploy/docker-compose.release.yml`](../../deploy/docker-compose.release.yml))
ships an `ofelia` cron sidecar that runs the ingester image once per
tick, in UTC:

| Job | Time (UTC) | Command |
|---|---|---|
| `md-ingest-boe-ois` | 12:30 | `ingest --source boe_ois --since-month-start` |
| `md-ingest-boe` | 17:00 | `ingest --source boe` |
| `md-ingest-ecb` | 17:30 | `ingest --source ecb` |
| `md-ingest-treasury` | 21:00 | `ingest --source treasury --since-month-start` |
| `md-roll-curve-dates` | 21:30 | `roll-curve-dates` (after the ingests it consumes) |
| `md-ingest-fred` | 22:00 | `ingest --source fred` — skipped without `FRED_API_KEY` |

The dev `docker-compose.yml` has a similar profile-gated sidecar
(`docker compose --profile scheduled up -d ofelia`) with an additional
daily synthetic refresh + snapshot rebuild.

## Tests

`uv run pytest services/md_ingester` runs the fast hermetic suite
(specs, parsers, CLI, deterministic pipeline). DB-backed tests are
gated behind the `md_ingester_db` marker:

```bash
export QUANTRA_MD_INGESTER_TEST_DSN=postgresql+asyncpg://md_rw:md_rw@localhost:5432/quantra
export QUANTRA_MD_INGESTER_TEST_ADMIN_DSN=postgresql+asyncpg://quantra:quantra@localhost:5432/quantra
uv run pytest -m md_ingester_db services/md_ingester
```

The DB-backed fixtures truncate only the ingester-owned `md.*` tables
on teardown and never touch `app.*`.

## Operational notes

- On a clean database the *first* ingest can take longer than later
  runs (each connector pulls the archive window its configs declare);
  bound it explicitly with `--start-date`.
- Snapshot rebuilds fire the `md.bump_snapshot_etag` statement-level
  trigger; the CLI summary reports the etag before/after.
- Public feeds publish on business days only — an ingest on a weekend
  or holiday finds (and writes) nothing new, which is normal.
