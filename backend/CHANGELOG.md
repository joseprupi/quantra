# Changelog

All notable changes to the Quantra backend (orchestrator / market_data /
md_ingester) are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

Backend and portal are released together at the same platform `vX.Y.Z` tag.
Versions **0.2.x and earlier are historical release tags from before this
monorepo was assembled**; 0.3.0 is the first release cut from the monorepo.

## [0.3.0] - 2026-07-20

First release cut from the open-source monorepo
([github.com/joseprupi/quantra](https://github.com/joseprupi/quantra)).

### Added
- **Open-sourced under AGPL-3.0-only.** The whole platform (orchestrator,
  market-data service, md-ingester, portal) now lives in one public monorepo
  with a fresh history, a CLA, and CI gates on every push.
- **Entity versioning / audit trail.** Append-only `app.entity_versions`
  snapshots every create/amend/delete/restore of every mutable `app.*` entity
  (full JSON snapshot, actor, optional `X-Change-Reason`, request grouping),
  enforced immutable at the database level (INSERT+SELECT-only grants).
  Generic `GET /v1/{entity}/{id}/versions[/{n}]` routes; `pricing_history`
  records the exact trade version each by-reference pricing used.
- **Published images + release automation.** A pushed `v*` tag builds and
  publishes the four platform images to GHCR
  (`ghcr.io/joseprupi/quantra-{orchestrator,market-data,md-ingester,portal}`)
  and attaches an offline install tarball plus the pull-and-run bundle
  (`docker-compose.release.yml` + `install.sh`) to the GitHub Release.
  `backend/deploy/install.sh` is now a real pull-and-run installer.
- **Nightly end-to-end run.** A scheduled workflow boots the full stack from
  source (real BoE/Treasury/ECB ingests) and drives the 45-journey browser
  suite against it, uploading the Playwright report and failure traces.

### Changed
- **Real-data-only platform.** No synthetic market data anywhere: the seeded
  curves resolve against genuine daily public feeds (BoE SONIA OIS,
  US Treasury, ECB; FRED with a free key), ingested on boot and refreshed by
  the daily cron.
- Save-graph identity model: product saves create uniquely named curves and
  re-save by id, so saving can never clobber an unrelated user curve.
- Curve preview resolves term IBOR indices exactly like pricing does
  (EURIBOR-referencing curves preview real grids).
- Pricing an As-Of earlier than a curve's reference date returns a typed,
  actionable `422 pricing_as_of_before_curve_date` instead of an opaque
  engine error.

## [0.2.0] - 2026-07-10

Platform release pinned to the OSS pricing engine **0.2.0**.

> **Engine pin:** this release requires `ghcr.io/joseprupi/quantra-server:0.2.0`.
> The self-hosted bundle now defaults to that public GHCR engine and its own
> entrypoint. Engine 0.2.0 is a **breaking wire change** vs 0.1.x; do not run
> this orchestrator against a 0.1.x engine (and vice-versa).

### Added
- **`GET /v1/version`** — reports the orchestrator and engine versions.
- **Quote-Book series CRUD** — `POST/GET/PATCH/DELETE /v1/market-data/series`
  to create, edit and delete quote **series definitions** (`md.canonical_ids`).
  Deleting a series cascades its own quote points.
- **Market-data import** — `POST /v1/market-data/import` accepts CSV multipart
  (`source=csv`) or manual JSON (`source=manual`) into the shared `md.*`
  schema, with per-row soft errors and provenance tagging. The `md_ingester`
  gains a CSV connector and a shared upsert writer. The importer **rejects
  values for a non-existent series** (create it first via the series CRUD).
- **Calendar + vol-tools `/v1` routes** — `/v1/calendar/business-days`,
  `/v1/calendar/holidays`, `/v1/calendar/advance`, `/v1/vol-surfaces/sample`,
  `/v1/calibrate-swaption-vol`, `/v1/calibrate-swaption-model` over engine RPCs
  (retires the legacy cloud pricing client on the portal side).
- **License-gate seam** — a signed-license (Ed25519) check runs at
  orchestrator startup inside the production boot guard. Ships free-tier by default:
  no license file ⇒ boots and prices with zero friction; a valid license sets
  the tier; a tampered/expired license refuses boot. Nothing is gated yet.

### Changed
- **Engine 0.2.0 compatibility** — the orchestrator now speaks the OSS
  engine's 0.2.0 wire contract (explicit conventions). No priced numbers
  changed for existing inputs.
- **Self-hosted release bundle defaults to the public GHCR engine 0.2.0** with
  the image's own entrypoint. `deploy/docker-compose.release.yml` no longer
  overrides the engine `command` by default (an empty default ⇒ the image
  entrypoint), so the bundle works out-of-the-box against the pullable engine.
  Repointing `QUANTRA_ENGINE_IMAGE` at a local-build engine still works by
  setting `QUANTRA_ENGINE_COMMAND=/src/build/server/sync_server 50051`.
- `QUANTRA_VERSION` / bundle defaults bumped `0.1.0` → `0.2.0`.

## [0.1.0] - 2026-07-08

First Tier-1 release: the installable self-hosted bundle. One command
(`docker compose --profile self-hosted up` / `deploy/install.sh`) brings up a
self-contained Quantra — postgres, the OSS pricing engine, market_data, the
orchestrator, the first-boot init chain (migrate → synthetic-ingest →
init-seed) and the browser portal — with dev-auth bypass, synthetic market
data, and same-origin portal proxy. Prices out of the box (inline and
MD-backed). Published as version-pinned GHCR images via
`.github/workflows/release.yml`.
