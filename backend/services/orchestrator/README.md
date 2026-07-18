# quantra-orchestrator

The single public API of the Quantra backend (FastAPI). Every external
client — the browser portal, scripts, external integrations — talks to
this one process; nothing else in the stack is exposed.

Responsibilities:

- **Own user data.** CRUD over the `app.*` schema for reference data
  (curves, curve sets, indices, credit curves, vol surfaces, swaption
  models, snapshots, quote book) and saved products (IR swaps,
  swaptions, fixed/floating bonds, CDS, equity options, inflation
  swaps). Owner-scoped, soft-delete + restore.
- **Price.** Per-product pricing endpoints that load/collapse
  references, resolve every quote id server-side against the
  market-data service, translate the request into the engine's
  FlatBuffers wire format, and call the
  [quantraserver](https://github.com/joseprupi/quantraserver) engine
  over gRPC. The engine only ever sees fully self-contained requests.
- **Observe.** Prometheus `/metrics`, structured logs with request ids,
  and optional per-request pricing traces (`/v1/traces/{request_id}`)
  that record each pipeline stage including the exact engine wire bytes.

## Route groups

| Group | Routes |
|---|---|
| Meta | `GET /health`, `GET /ready`, `GET /metrics`, `GET /v1/version` |
| Auth | `GET /auth/whoami`, `POST /auth/provision` |
| Pricing | `POST /v1/price/swap/ir`, `/v1/price/swaption`, `/v1/price/bonds/fixed`, `/v1/price/bonds/floating`, `/v1/price/cds`, `/v1/price/equity-option`, `/v1/price/swaps/inflation` |
| Entity CRUD | `/v1/{curves,curve-sets,indices,credit-curves,vol-surfaces,swaption-models,snapshots}` + the seven product tables (`/v1/swaps/ir`, `/v1/swaptions`, `/v1/bonds/fixed`, `/v1/bonds/floating`, `/v1/cds`, `/v1/equity-options`, `/v1/swaps/inflation`), each with list/create/get/patch/delete/`:restore`; plus the `GET`/`PUT` singleton `/v1/quote-book` |
| Pricing history | `GET /v1/pricing-history`, `GET /v1/pricing-history/{id}` (immutable log of priced calls) |
| Curve tools | `POST /v1/curve-preview` (bootstrap a yield or inflation curve through the engine and return the grid) |
| Vol tools | `POST /v1/vol-surfaces/sample`, `POST /v1/calibrate-swaption-vol`, `POST /v1/calibrate-swaption-model` |
| Calendar | `POST /v1/calendar/business-days`, `/v1/calendar/holidays`, `/v1/calendar/advance` |
| Market data | `POST /v1/market-data/import` (CSV / manual quote upload), `/v1/market-data/series` CRUD, `GET /v1/market-data/latest-date` |
| Traces | `GET /v1/traces/{request_id}` |
| Debug | `/debug/md/*`, `/debug/engine/*` — verification-only, hidden from the OpenAPI schema |

Errors use one JSON envelope everywhere:
`{ "error": ..., "code": <machine token>, "request_id": ..., "details": [...] }`.

## Running locally

From the monorepo root:

```bash
# Live-reload dev server.
uv run --package quantra-orchestrator \
    uvicorn quantra_orchestrator.app:create_app --factory --reload --port 8080

# Or the production entry point (same boot path the Dockerfile uses).
uv run --package quantra-orchestrator python -m quantra_orchestrator

curl -s localhost:8080/health
open http://localhost:8080/docs
```

With no configuration the app boots in a degraded-but-serviceable mode:
no DSNs → no data layer, no `ENGINE_GRPC_TARGET` → a stub engine client
that answers every pricing call with a 502 `engine_unavailable`
envelope. Point it at real backends via the env vars below (the
repo-root `.env.example` documents the full set).

## Settings

`OrchestratorSettings` extends the shared `quantra_common.Settings`.
The important env vars:

| Env var | Default | Purpose |
|---|---|---|
| `POSTGRES_DSN_APP_RW` / `POSTGRES_DSN_APP_RO` | unset | `app.*` engines (user data). Unset → data routes unavailable. |
| `POSTGRES_DSN_MD_RO` | unset | Optional direct `md.*` read engine so `/ready` can probe the MD database independently of the MD service. |
| `POSTGRES_DSN_MD_RW` | unset | Optional `md.*` write engine for the market-data import/series/latest-date routes. Unset → those routes return 503. |
| `MD_SERVICE_URL` | unset | Base URL of the market-data read service; gates the MD client (one HTTP pool per process, bounded TTL-LRU quote cache). |
| `ENGINE_GRPC_TARGET` | unset | `host:port` of the pricing engine. Set → real gRPC channel (with retries); unset → stub client, pricing returns 502. |
| `DEV_AUTH_BYPASS` | `false` | DEV-ONLY. Skips all credential checks and resolves every request to a fixed `dev-user` principal (what the self-hosted bundle uses). Loud startup banner. **Refused at boot when `ENV=prod`** — both the settings validator and the app factory hard-fail rather than start a world-open API. |
| `ENV` | `dev` | `dev` / `staging` / `prod`. |
| `FIREBASE_PROJECT_ID` | unset | Enables Firebase bearer-token verification (API-key auth works alongside). |
| `TRACE_CAPTURE` | `true` | Record per-stage pricing traces into `app.pricing_traces` (best-effort post-response flush; never fails or slows a price). |
| `DEV_CORS_ORIGINS` | empty | DEV-ONLY comma-separated origins; empty = no CORS middleware (production posture: a reverse proxy owns CORS/TLS). |
| `ORCHESTRATOR_PORT` | `8080` | Bind port for `python -m quantra_orchestrator`. |
| `BUILD_SHA` | `dev` | Reported by `GET /health` / `/v1/version`; injected at image build time. |

Finer-grained knobs (retry budgets and backoff for the MD/engine
clients, MD cache size/TTL, trace payload cap, per-product concurrency
policy ids, per-role pool sizing) are documented on the fields of
`settings.py` and `packages/common/src/quantra_common/settings/base.py`.

## Tests

```bash
uv run pytest services/orchestrator
```

Layers, from fast to slow:

- **Hermetic (default):** route/validation tests against injected fake
  engines/MD clients/lookups, translator + wire-format tests (including
  byte-snapshot tests of the FlatBuffers encoding), assembler and
  MD-resolution tests. No network, no DB.
- **DB-backed (opt-in markers):** `app.*` data-layer integration tests
  against the dev Postgres.
- **Live smokes (opt-in):** end-to-end against a running engine / MD
  service. Hermetic tests cannot catch a wire-contract break with the
  real engine, so pricing changes are also verified against a live
  engine (`scripts/self_hosted_smoke.py` prices through the full
  stack and asserts input-sensitive NPVs).
