# Quantra Portal

Web frontend for the [Quantra](https://quantra.io) derivatives pricing platform,
built with React + TypeScript + Vite.

The portal is a browser client for the Quantra orchestrator API. It provides:

- **Pricing** for six product families: interest-rate swaps, fixed & floating
  rate bonds, credit default swaps, swaptions, equity options, and inflation
  swaps.
- **Curve tooling**: yield / credit / inflation curve builders with live
  bootstrap previews, curve sets, and custom indices.
- **Market data**: a Quote Book over the market-data catalog (series CRUD,
  value history), CSV/manual import, and a Time Series Lab.
- **Vol tools**: swaption vol surface sampling and model calibration.
- **Investigate**: a per-request pricing trace inspector (inputs, resolved
  entities, the exact engine request, and the engine response).

All pricing goes through the orchestrator's REST API — the portal never talks
to the pricing engine directly.

## Running it

### Option 1 — the self-hosted bundle (recommended)

The easiest way to run the full stack (portal + orchestrator + market data +
Postgres + pricing engine) is `docker compose up -d` from the repository
root, which builds this portal (as an nginx image) together with the backend
services in [`../backend`](../backend). A prebuilt-images flavor also lives
in `../backend/deploy/`.

### Option 2 — local development against a running orchestrator

Prerequisites: Node.js 18+, and a Quantra orchestrator reachable over HTTP
(e.g. from the self-hosted bundle, or run from [`../backend`](../backend)).

```bash
npm install
npm run dev        # http://localhost:5173
```

By default the dev server expects an orchestrator at `http://localhost:8080`.

#### Environment variables (build/dev time)

Set these in `.env.local` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_ORCHESTRATOR_URL` | `http://localhost:8080` | Base URL of the orchestrator API. |
| `VITE_DEV_AUTH_BYPASS` | unset | `true` enables the single-user dev-auth bypass (no login). The orchestrator must run with its matching bypass flag. |
| `VITE_MARKET_DATA_API_URL` | `http://localhost:8082` (dev) / same-origin `/_md` (production build) | Base URL of the market-data catalog service. |
| `VITE_DEFAULT_AS_OF` | unset | Optional ISO `YYYY-MM-DD` initial As-Of date for the pricing forms (otherwise "today"). |

Hosted authentication (Firebase login) exists in the code but is unused in
self-hosted mode; with the dev-auth bypass on, no Firebase configuration is
needed.

#### Runtime configuration (the container)

Vite inlines `VITE_*` values at build time, so the shipped Docker image is
instead re-pointable at **run** time: `docker/entrypoint.sh` writes
`/config.js` (loaded before the app bundle) from container env vars —
`ORCHESTRATOR_URL`, `ORCHESTRATOR_PROXY_TARGET`, `MD_PROXY_TARGET`,
`DEV_AUTH_BYPASS`, `DEFAULT_AS_OF`. nginx reverse-proxies `/v1/` and `/auth/`
to the orchestrator and `/_md/` to the market-data service, so the browser
only ever makes same-origin requests (no CORS). See `docker/entrypoint.sh`
for the full reference.

## Tests

```bash
npx vitest run       # unit / component suite
npm run lint         # ESLint (zero warnings allowed)
npx tsc --noEmit     # type check
npm run build        # production build (runs tsc first)
```

End-to-end specs (`e2e/`, Playwright) come in two projects, neither part of
the default gate:

**Full journey suite** (`e2e/full/` + `e2e/lib/`) — real browser, real
clicks, REAL backend. It covers every user-facing area (market data, indices,
curves + preview, curve sets, calendar, vol workbench, models, all seven
products, traces, settings/chrome), creates every entity it needs through the
UI, and cross-checks every displayed NPV against a direct
`POST /v1/price/*` replay (parity oracle, 1e-6 relative). Known defects and
capability gaps are pinned as explicit tests (annotated `defect` /
`known-gap`) so the suite doubles as a truth map. Boot the root compose
stack, wait for the boot ingests, then:

```bash
docker compose up -d --build          # from the repo root (or an isolated project)
E2E_PORTAL_URL=http://localhost:5173 npm run test:e2e:full
```

`E2E_PORTAL_URL` points at the portal (all API traffic rides its same-origin
proxy). The suite waits for real market data (`/v1/market-data/latest-date`)
before pricing journeys run. ~3 minutes at 4 workers.

**Legacy hermetic smoke set** (`e2e/*.spec.ts`) — route-intercepted specs
against the Vite dev server (`npm run test:e2e`; set `E2E_DEV_PORT` if 5173
is taken). NOTE: currently stale — they pre-date the backend-backed entity
stores (they seed `localStorage['quantra_curves']`, which the app no longer
reads) and are not run in CI; the full suite supersedes them.

## Project structure

```
src/
├── pages/            # Route-level screens (products, curves, market data, …)
├── components/       # Shared UI (curve editors, product forms, charts, …)
├── lib/
│   ├── api/          # Orchestrator API client, per-product pricing services,
│   │   └── _generated/orchestrator.d.ts   # types generated from the
│   │                                      # orchestrator's /openapi.json
│   │                                      # (scripts/regen_orchestrator_types.sh)
│   ├── migration/    # One-time importers for data saved by older versions
│   ├── storage/      # Backend-backed entity stores (curves, indices, …)
│   └── runtimeConfig.ts   # runtime /config.js + VITE_* fallbacks
├── hooks/            # React hooks (auth, entity data, …)
└── test/             # Test setup and helpers
docker/               # nginx config + entrypoint for the shipped image
e2e/                  # Playwright end-to-end specs
```

## License

AGPL-3.0-only — see [LICENSE](LICENSE).
