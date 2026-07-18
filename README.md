# Quantra

An open-source, self-hostable derivatives-pricing platform:

- **Pricing engine** — [quantraserver](https://github.com/joseprupi/quantraserver),
  a C++ / [QuantLib](https://www.quantlib.org/) pricing server (gRPC +
  FlatBuffers, plus a public JSON API). Separate repository; consumed here as
  a Docker image.
- **[`backend/`](backend/)** — a Python monorepo with three services:
  the **orchestrator** (the public FastAPI REST API: users' reference data,
  saved products, server-side market-data resolution, drives the engine over
  gRPC), a read-only **market_data** service, and an **md_ingester**
  worker/CLI that ingests real public market data (Bank of England,
  US Treasury, ECB, FRED).
- **[`frontend/`](frontend/)** — the browser portal (React + TypeScript +
  Vite): pricing forms for six product families, curve builders with live
  bootstrap previews, a market-data Quote Book + import, vol tools, and a
  per-request pricing-trace inspector.

## Architecture

```
                              browser portal (React)             frontend/
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

The browser only ever talks to the orchestrator; the engine is stateless and
only ever receives fully self-contained requests (market data referenced by
quote id is resolved server-side). A single Postgres instance holds both
schemas: `app.*` for user data and `md.*` for market data.

## Quickstart

Prerequisites: Docker with the compose plugin, internet access on first boot
(for the engine image pull and the public market-data feeds).

```bash
docker compose up -d
```

This builds the backend services and the portal from this tree, pulls the
public pricing-engine image, migrates the database, ingests real public
market data, and seeds the reference entities. Then open
**<http://localhost:5173>**.

What you get:

- The portal on `:5173` (single-user dev-auth mode — no login), the
  orchestrator API on `:8080` (`/docs` for the OpenAPI UI).
- **Real, daily-refreshed public market data**: a genuine Bank of England
  SONIA OIS curve (price a GBP swap on real rates out of the box), US
  Treasury and BoE gilt government curves, ECB FX + inflation series, and —
  with a free `FRED_API_KEY` — ~31 additional US series (SOFR/EFFR, CPI/PCE,
  breakevens, credit OAS, VIX/MOVE). A cron sidecar re-ingests every feed
  daily and rolls the curves' reference date forward.
- **Six priceable product families**: interest-rate swaps, fixed and
  floating-rate bonds, credit default swaps, swaptions, equity options, and
  inflation swaps.

Tear down with `docker compose down` (add `-v` to also delete the market-data
volume).

## Development

Each subtree is a self-contained project with its own toolchain and test
gate — see [`backend/README.md`](backend/README.md) (Python, `uv`,
pytest/ruff/mypy) and [`frontend/README.md`](frontend/README.md) (Node,
vitest/eslint/tsc). CI runs both gates path-filtered from
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## License

**AGPL-3.0** — see [`LICENSE`](LICENSE). In short: you can use, modify, and
self-host Quantra freely; if you run a modified version as a network service
you must make your modified source available to its users.

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md).
