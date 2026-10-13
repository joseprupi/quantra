# Changelog

All notable changes to the Quantra backend (orchestrator / market_data /
md_ingester) are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

Backend and portal are released together at the same platform `vX.Y.Z` tag.
Versions **0.2.x and earlier are historical release tags from before this
monorepo was assembled**; 0.3.0 is the first release cut from the monorepo.

## [Unreleased]

Platform pinned to the OSS pricing engine **0.11.0** (from 0.6.0).

### Added
- **FX spot market data** (engine 0.9.0 `pricing.fx.spots[]`): a new
  `app.fx_spots` entity with CRUD at `/v1/fx-spots` (versioned in the audit
  trail) — a currency pair, an inline `rate` OR an MD `quote_id` (resolved
  server-side at `as_of`; the ECB reference-rate ingest writes
  `EUR.FX.ECB.<PAIR>.1D.SPOT`, e.g. `EUR.FX.ECB.EURUSD.1D.SPOT`), the spot lag
  (`spot_days`), the joint `settlement_calendars` and the
  `business_day_convention`. Pricing requests carry `fx_spots: [id-ref |
  inline]`; the translator emits `Pricing.fx` only when a spot is given, so
  every existing product's wire is byte-identical (golden-hex tests unchanged).
- **FX-implied curve helpers** `FxSwapHelper` (`fx_points` | `quote_id`,
  `tenor`, `fx_spot`, `end_of_month`, `collateral_currency`,
  `collateral_curve: {id}`) and `CrossCcyBasisHelper` (`spread` | `quote_id`,
  `tenor`, `fx_spot`, `base_index` / `quote_index`, `base_forwarding_curve` /
  `quote_forwarding_curve`, exactly one of `base_discount_curve` /
  `quote_discount_curve`, `spread_on_base_leg`, `payment_frequency`,
  `payment_lag`, `calendar`, `business_day_convention`, `end_of_month`,
  `mtm_reset`) accepted as curve points on the pricing paths and on
  `POST /v1/curve-preview`; the preview also accepts `pricing.fx.spots[]` and
  an explicit `pricing.indices[]` (engine `IndexDef` shape, registered
  verbatim). Collateral-adjusted discount curves (FX swaps to 1Y, basis swaps
  beyond) now bootstrap through the orchestrator.
- **`POST /v1/price/fx-forward`** (engine `PriceFxForward`): trade =
  `{base_currency, quote_currency, direction: BuyBase|SellBase, base_notional,
  forward_rate, maturity_date, fx_spot, base_discounting_curve,
  quote_discounting_curve, reporting_currency?}` priced by reference
  (`fx_forward_id`, new `app.fx_forwards` product table with CRUD at
  `/v1/fx-forwards` + versions) or inline, with `curves` / `fx_spots` /
  `indices` ref-or-inline lists; result = `{npv, reporting_currency,
  fair_forward_rate, forward_points, base_leg_pv, quote_leg_pv, fx_spot_used,
  spot_date, maturity_date}`. Typed 422s (`fx_forward_*`), D159 trace stages
  incl. the exact engine wire, `pricing_history` rows (`product_kind`
  `fx_forwards`, CHECK extended by migration 0015).
- `POST /v1/import` reports `fx_forwards[]` trades and `pricing.fx.spots[]`
  as unsupported items (never dropped); mapping FX spots to `app.fx_spots` is
  a follow-up.

### Changed
- **Pricing engine updated to quantra-server 0.11.0** (engine 0.10.0 is
  wire-breaking for swaps and swaptions; 0.11.0 is 0.10.0 rebuilt on
  QuantLib 1.44). The orchestrator now emits the engine's `SwapLeg` model on
  `/v1/price/swap/ir` and `/v1/price/swaption`: each underlying leg carries
  `pay_receive` (from the request's `swap_type`: Payer = pay fixed / receive
  floating), `currency` (the forwarding index's currency, both legs), its
  notional / schedule / day count / payment convention, a typed `coupons`
  union (`FixedCoupons{rate}` / `IborCoupons{index, spread}`) and its own
  curves (the discounting curve on both legs, the forwarding curve on the
  floating leg). The `/v1` request contract is unchanged (`swap_type`,
  `discounting_curve`, `forwarding_curve` ids as before).
- **IR swap result:** per-leg NPVs and flows are read from the engine's
  `legs[]` (`leg_npvs` roles `fixed` / `floating` unchanged); every flow now
  carries `kind` (`Coupon` | `NotionalExchange`); `extras.fair_rate` /
  `extras.fair_spread` are `null` when the engine omits them (a zero-annuity
  leg) instead of `0.0`.
- Vendored engine FlatBuffers bindings regenerated from engine tag `v0.11.0`
  (241 modules; 18 tables changed, 5 removed, 19 added; the orchestrator's
  hand-patched `CDS` / `CreditCurveSpec` emission is natively presence-based
  upstream, so no local patch is carried).
- `GET /v1/version` reports engine `0.11.0`; both compose files and the
  release `.env.example` default `QUANTRA_ENGINE_IMAGE` to
  `ghcr.io/joseprupi/quantra-server:0.11.0`.

### Removed
- **IR swap result `extras.fixed_leg_bps` / `extras.floating_leg_bps`**: the
  engine's per-leg BPS fields were removed in 0.10.0 and are no longer
  surfaced (the portal's BPS lines must go).

### Notes
- Results that move on 0.11.0 because of QuantLib 1.41 -> 1.44 are
  documented in the engine's `docs/quantlib-upgrades/1.41-to-1.44.md`. For
  this platform the affected product is **CDS on a bootstrapped credit
  curve** (the orchestrator's helper conventions use `settlement_days: 0`
  with the `TwentiethIMM` date rule, the documented case: up to ~1.4% of
  NPV); swaps, swaptions, bonds, equity options and inflation swaps priced
  through `/v1` are unchanged.

## [0.5.0] - 2026-08-03

Platform release pinned to the OSS pricing engine **0.6.0**.

### Added
- **Importer service.** `POST /v1/import` accepts a JSON document in the
  pricing engine's request format and creates the corresponding entities:
  indices, curves (instrument helpers and value points), credit curves,
  volatility surfaces and swaption models, with a per-item ok/skipped/error
  report. Supports `dry_run` and `on_conflict=error|skip`; document quote
  values are substituted where possible, and unsupported sections (trades,
  swap indices, inflation, equity) are reported, never silently dropped.

### Changed
- **Pricing engine updated to quantra-server 0.6.0.** OIS curve helpers now
  always emit the overnight-coupon parameters the 0.6.0 contract requires —
  `payment_lag`, `averaging_method`, `lookback_days`, `lockout_days`,
  `apply_observation_shift` — and `DatedOISHelper` gains
  `fixed_leg_frequency` (previously hardcoded to Annual engine-side).
  Stored curves whose OIS points predate these fields translate with the
  exact pre-0.6 behavior (payment lag 0, Compound, no lookback/lockout, no
  observation shift). **BREAKING for self-hosted installs that pin an older
  engine image:** curves using OIS helpers require pricing engine >= 0.6.0.

## [0.4.2] - 2026-07-29

### Added
- `SwaptionSabrCalibrateSpec` surfaces are now translated to the engine
  (SABR smile calibration).

### Fixed
- `/v1/calibrate-swaption-vol` returns the full diagnostics including
  per-node calibrated parameters, which were previously dropped in decoding.

## [0.4.1] - 2026-07-27

### Fixed
- Report engine version 0.5.0 in `/v1/version`, matching the pinned engine
  image (the value is still a documented stopgap until the engine exposes
  its version over gRPC). The release env example now also pins the engine
  image to 0.5.0.

## [0.4.0] - 2026-07-27

Platform release pinned to the OSS pricing engine **0.5.0**.

### Added
- **Value-based yield curves.** Curves can now be built from given values
  instead of instruments: zero rates, discount factors, or forward rates
  (QuantLib `InterpolatedZeroCurve` / `InterpolatedDiscountCurve` /
  `InterpolatedForwardCurve`). Values can be entered inline or reference
  market-data quotes, resolved at the As-Of date.

### Changed
- **Pricing engine updated to quantra-server 0.5.0.** The 0.5.0 wire contract
  makes required fields explicit, and the bootstrap trait is now a validated
  curve-family selector. FlatBuffers bindings regenerated from the 0.5.0
  schema; all products verified byte-identical NPVs across the engine upgrade.

### Fixed
- CDS response decode could crash when the engine omits `fair_spread`.
- Duplicate curve ids in assembled requests are now deduplicated.

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
