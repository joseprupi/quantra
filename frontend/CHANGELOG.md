# Changelog

All notable changes to the Quantra portal (web client) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The portal ships in lockstep with the backend as a single **platform release**;
version numbers track the platform, not the portal in isolation.

Versions 0.2.x and earlier predate this monorepo — they are the
pre-open-source release history of the portal, kept for reference. 0.3.0 is
the first release cut from the monorepo.

## [0.4.2] - 2026-07-29

### Added
- **SABR swaption smile calibration** works end to end in the Volatility
  Workbench: fill an expiry x tenor x strike-spread vol grid, calibrate, and
  read per-node alpha/beta/rho/nu with fit diagnostics.

### Changed
- Value-curve rows now use explicit tenor and date controls (a per-row
  Tenor/Date toggle with a number-plus-unit selector or a date picker)
  replacing the free-text field, with shorter anchor labels.

### Fixed
- An unfilled vol cube could reach the engine as zeros; empty cells are now
  rejected client-side with the exact cell named.

## [0.4.1] - 2026-07-27

### Removed
- Removed a stale hint claiming discount-factor and forward-rate curves need
  a newer engine (the platform ships engine 0.5.0).

## [0.4.0] - 2026-07-27

Platform release **v0.4.0**, cut against pricing engine 0.5.0.

### Added
- **Value-based curve construction.** The Curve Builder can build curves from
  given values instead of instruments: zero rates, discount factors, or
  forward rates, with inline values or market-data quote references resolved
  at the As-Of date, paste-a-table entry, and per-row validation.

### Fixed
- Product pages previously dropped the interpolator and bootstrap trait on
  inline curves, so non-default traits were silently mistranslated; the full
  curve specification now reaches the backend.

## [0.3.0] - 2026-07-20

First release cut from the open-source monorepo
([github.com/joseprupi/quantra](https://github.com/joseprupi/quantra)).

### Added
- **Open-sourced under AGPL-3.0-only** as part of the platform monorepo; the
  portal image (`ghcr.io/joseprupi/quantra-portal`) is now published to GHCR
  on every release tag.
- **History panel (audit trail).** Every product page and the Curve Builder
  gain a History tab: a version timeline (who / when / change type / reason /
  request id), full snapshot view of any version, a dotted-path diff between
  versions, and restore-as-new-version. The save bar accepts an optional
  change reason recorded in the audit trail.
- **Full-coverage e2e journey suite** (`e2e/full/`, `npm run test:e2e:full`):
  45 real-browser journeys across all six products, market data, curves,
  vols and audit, with a UI-vs-API pricing parity oracle. Run nightly in CI.

### Changed
- **Real-data-only.** The portal defaults the pricing As-Of to the latest
  real ingested business day and labels live public feeds honestly; no
  synthetic quote data is shipped.
- Product saves no longer clobber same-named user curves: wire curves get
  unique names on create and are updated by id on re-save.

## [0.2.0] - 2026-07-10

Platform release **v0.2.0** — portal + backend cut together and tested against
**pricing engine 0.2.0**. The web-client version is now surfaced in-app: the
header ⓘ **About** panel reads `__APP_VERSION__` (from `package.json`) and this
release shows `0.2.0` on the "Web client" row.

### Added
- **About panel.** A header ⓘ button opens an "About Quantra" panel showing the
  three versions that make up a running stack: Web client (build-time
  `__APP_VERSION__`), Backend (orchestrator version + build SHA from
  `GET /v1/version`), and Pricing engine (version from `GET /v1/version`).
  Backend/engine rows show a subtle loading state and degrade gracefully to
  "Unavailable" on fetch error; the web-client row always renders.
- **Market-data Import screen.** Market Data → Import supports manual quote entry
  and CSV upload, wired to `POST /v1/market-data/import`, with a per-row import
  report and a D54 provenance banner (imports land in the shared `md.*` catalog,
  tagged by source).
- **Quote Book series CRUD.** Create, edit, and delete quote series
  (`md.canonical_ids` definitions) directly in the Quote Book; the Import screen's
  canonical_id field is now a dropdown populated from `listSeries()`.
- **Unified Quote Book.** A single master-detail table replaces the old two-pane
  split: series + latest value + source, inline edit/delete, and a row-expand
  view showing value history (with a chart) plus inline add-value.

### Changed
- **Legacy client retired.** Five features that still called the legacy
  `quantra-api.ts` (old cloud `api.quantra.io` + Firebase auth, which broke under
  dev-bypass with "Not authenticated") are rewired onto the orchestrator
  (`/v1/...`). `quantra-api.ts` is deleted; shared types moved to
  `quantra-types.ts`. (O57)
- **Same-origin market data.** The Quote Book reads the bundle-local MD catalog
  via a same-origin `/_md` proxy instead of a hard-coded cloud host, so the
  self-hosted bundle shows local series and imported quotes with no cross-origin
  calls. (O58)
- **Quote Book readability.** Expanded/selected rows are now readable on the
  yellow highlight (dark text, sufficient contrast), including the latest value
  and the expanded details card.

### Fixed
- **Stale market-data URL / CORS.** Runtime-injected `marketDataUrl` now wins over
  a stale `localStorage['quantra_md_backend_url']` value left by an older portal
  build (e.g. a cross-origin `https://market.quantra.io`), which had shadowed the
  same-origin `/_md` config and caused dead cross-origin (CORS) fetches on quote
  resolution. The stale key self-heals to the runtime value; dev/hosted overrides
  are preserved when no runtime config is injected.
- **About panel positioning.** The About overlay is rendered through a portal to
  `document.body` so it escapes the header's `backdrop-filter` containing block,
  which had trapped the `position: fixed` overlay at the top of the header instead
  of covering the viewport.
- **Time Series Lab.** Repointed off the retired `localStorage['quantra_quote_book']`
  key (dead since the market-data unification) onto the live data path used by the
  unified Quote Book: series universe from the orchestrator catalog, latest value +
  provenance from the MD read path, and per-series history via `getSeriesPoints`.
  The "My Data vs Quantra Data" split is now a real provenance split
  (manual/CSV vs synthetic), with proper empty and error states.
- **Quote Book value chart.** Replaced the stretched `preserveAspectRatio="none"`
  SVG (which produced a soft, variable-width "3D-ish" stroke and no axes) with a
  crisp analytical 2D line chart with reference axes.
- **Docker/nginx IPv6.** nginx now also listens on `[::]:80` so `localhost` resolves
  on macOS hosts.
