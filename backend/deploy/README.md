# `backend/deploy/` — install paths + release tooling

Quantra installs three ways. All three run the SAME stack: Postgres + the
public pricing engine + market-data service + orchestrator + first-boot init
chain (migrate → seed → real-feed boot ingests → roll dates) + a daily
`ofelia` cron sidecar + the browser portal.

## The three install paths

### 1. Build from source (repo-root compose)

```bash
git clone https://github.com/joseprupi/quantra.git
cd quantra
docker compose up -d
# then open http://localhost:5173
```

Builds the backend services and the portal from this tree, pulls the public
pricing-engine image, migrates the database, ingests the real public
market-data feeds, and seeds the reference entities. See the repo-root
[`README.md`](../../README.md).

### 2. Pull the published images (`install.sh`)

Every release tag (`vX.Y.Z`) publishes the four platform images to GHCR —
`ghcr.io/joseprupi/quantra-{orchestrator,market-data,md-ingester,portal}` —
via [`.github/workflows/release.yml`](../../.github/workflows/release.yml).
The images are public: no registry account or login needed.

Download `docker-compose.release.yml`, `.env.example` and `install.sh` from
the [GitHub Release](https://github.com/joseprupi/quantra/releases) (they are
attached to every release) into one folder, then:

```bash
./install.sh
# pulls the pinned images, starts the stack, waits for health,
# prints http://localhost:5173
```

If the pull is denied ("denied"/"unauthorized"), the GHCR packages may still
be private (fresh first release — see the one-time operator step below); use
the offline tarball or the source build meanwhile.

### 3. Offline tarball (`install-offline.sh`)

Each release also attaches `quantra-selfhosted-<version>.tar.gz` — a
self-contained bundle with the four images `docker save`d inside. On a
machine with only Docker (no registry access for the in-house images):

```bash
tar xzf quantra-selfhosted-<version>.tar.gz
cd quantra-selfhosted-<version>
./install-offline.sh
```

Only the public pricing-engine + Postgres images are pulled at `up` time; on
a fully air-gapped host, `docker save`/`docker load` those two as well.

## One-time operator step after the FIRST release: flip the packages public

GHCR packages created by a workflow's `GITHUB_TOKEN` start **private**. After
the first `v*` tag publishes, make each of the four packages public — once;
later releases push new tags into the same (now public) packages:

1. Open <https://github.com/joseprupi?tab=packages> (or the repo → Packages).
2. For **each** of `quantra-orchestrator`, `quantra-market-data`,
   `quantra-md-ingester`, `quantra-portal`:
   1. Open the package → **Package settings**.
   2. **Danger Zone → Change package visibility → Public**, type the package
      name to confirm.
3. Verify with an anonymous pull: `docker logout ghcr.io && docker pull
   ghcr.io/joseprupi/quantra-orchestrator:<version>`.

(The pricing-engine package `quantra-server` is already public — it belongs
to the separate engine project.)

## What's in this directory

| File | What it is |
|---|---|
| `docker-compose.release.yml` | Image-pinned variant of the root compose (pull-and-run, no build). Used by paths 2 and 3. |
| `install.sh` | Pull-and-run installer for the published images (path 2). |
| `make-release-tarball.sh` | Builds the self-contained offline tarball (`docker save` of the 4 images + compose + installer). CI runs it on every release tag; maintainers can run it locally. |
| `install-offline.sh` | Installer run from inside an unpacked offline tarball (`docker load`, no registry — path 3). |
| `upgrade.sh` | Safe in-place upgrade (backup first, keep the database, `alembic upgrade head` on boot). |
| `UPGRADE.md` | The upgrade/backup/rollback runbook. |
| `.env.example` | Every knob the release compose reads, documented. |

## Configuration knobs

Every knob is documented in [`.env.example`](.env.example). The essentials:

| Variable | Default | Purpose |
|----------|---------|---------|
| `REGISTRY` | `ghcr.io/joseprupi` | Registry + namespace the release compose resolves image refs against (single swappable variable). |
| `QUANTRA_VERSION` | `0.3.0` | Version tag the release compose pins the platform images to. Release bundles ship it pre-pinned to their own version. |
| `QUANTRA_ENGINE_IMAGE` | `ghcr.io/joseprupi/quantra-server:0.2.0` | The public pricing-engine image (a separate open-source project). |
| `QUANTRA_ENGINE_COMMAND` | *(empty)* | Empty → the engine image's own entrypoint. |
| `FRED_API_KEY` | *(empty)* | Optional free key enabling the FRED feed (~31 extra US series). |
| `DEFAULT_AS_OF` | `2025-01-15` | Pre-ingest fallback for the portal's default pricing date; the portal rolls forward automatically once real data lands. |
| host ports | 5173 / 8080 / 8082 / 50051 / 5432 | Portal / orchestrator / MD / engine / postgres. |

## Building an offline tarball locally (maintainers)

CI attaches the tarball to every release, but it can also be built by hand on
a machine with the four images present locally (pulled from GHCR, or built
from this tree and tagged as the release refs
`${REGISTRY}/quantra-<svc>:${VERSION}`):

```bash
# from the repo root — build + tag (or docker pull the published refs):
docker build -f backend/services/orchestrator/Dockerfile -t ghcr.io/joseprupi/quantra-orchestrator:0.3.0 backend
docker build -f backend/services/market_data/Dockerfile  -t ghcr.io/joseprupi/quantra-market-data:0.3.0  backend
docker build -f backend/services/md_ingester/Dockerfile  -t ghcr.io/joseprupi/quantra-md-ingester:0.3.0  backend
docker build -t ghcr.io/joseprupi/quantra-portal:0.3.0 frontend

cd backend/deploy
./make-release-tarball.sh 0.3.0
```

That produces `quantra-selfhosted-<version>.tar.gz` containing the images,
the release compose file, `.env.example`, `install-offline.sh` and
`upgrade.sh`.

## Daily real market data (BoE SONIA OIS, US Treasury, ECB, FRED)

Both compose flavors serve REAL, daily-refreshed public market data — no
synthetic data is seeded.

Keyless — always on, nothing to configure:

* **BoE SONIA OIS** → `GBP.RATES.BOE.OIS.{6M..25Y}.PAR` — the flagship. The
  seeded "GBP SONIA OIS (BoE, daily public)" curve references these quote ids.
* **US Treasury** → `USD.RATES.UST.OFFICIAL.*.YIELD` — the seeded
  "USD Treasury (public, daily)" curve references these.
* **BoE gilt** govt curve — refreshed daily too.
* **ECB** → EUR FX reference rates + inflation.

Needs a free API key:

* **FRED** → ~31 US series (UST DGS yields, SOFR/EFFR, CPI/PCE, inflation
  breakevens, credit OAS, VIX/MOVE). **FRED is the only feed requiring
  `FRED_API_KEY`** (free, instant: <https://fredaccount.stlouisfed.org/apikeys>;
  set it in `.env` — see `.env.example`). **Leave it unset and the FRED boot
  ingest + the FRED cron tick simply fail-and-skip** — the app boots normally
  and every keyless feed above still works; you just get no FRED series.

### Boot: a fresh install prices real data once the first ingest lands

One-shot init containers run the real ingest at boot (best-effort — a vendor
outage, or a missing `FRED_API_KEY`, never blocks the app: nothing gates on
them):

1. `boe-ois-ingest` — `ingest --source boe_ois --since-month-start`. The
   `--since-month-start` window pulls the fresher BoE "latest" workbook
   (carries yesterday's fixing) rather than an empty one-day window.
2. `treasury-ingest` — same, for the UST curve.
3. `ecb-ingest` — same, for the ECB series (keyless).
4. `fred-ingest` — same, for the FRED series. Exits immediately with
   "FRED_API_KEY is required" when no key is set; the stack comes up healthy
   regardless.
5. `roll-dates` — after `init-seed` + the ingests, runs `roll-curve-dates`,
   which bumps the seeded curves' `reference_date` to the latest ingested
   business day (owner-scoped, marker-scoped, idempotent). So a fresh
   install's curves are dated to real, current data and price immediately at
   that date.

The portal reads **`GET /v1/market-data/latest-date`** (optionally
`?source=` / `?prefix=`) to discover the freshest ingested date and defaults
the pricing As-Of accordingly; `DEFAULT_AS_OF` is only the pre-ingest
fallback.

### Daily: the `ofelia` cron sidecar keeps it current

The `ofelia` service (`docker run`s the md-ingester image once per tick on
the stack's network; needs the host Docker socket, mounted read-only)
schedules, in UTC:

| job | time | command |
|---|---|---|
| `md-ingest-boe-ois` | 12:30 | `ingest --source boe_ois --since-month-start` |
| `md-ingest-boe`     | 17:00 | `ingest --source boe` |
| `md-ingest-ecb`     | 17:30 | `ingest --source ecb` (after the ~16:00 CET publication, in both DST regimes) |
| `md-ingest-treasury`| 21:00 | `ingest --source treasury --since-month-start` |
| `md-roll-curve-dates`| 21:30 | `roll-curve-dates` (after the ingests it consumes) |
| `md-ingest-fred`    | 22:00 | `ingest --source fred` — **needs `FRED_API_KEY`; skipped without it** |

Everything lands on the persistent `postgres_data` volume, so real history
accumulates and survives upgrades.

**Weekends / holidays.** Public curves publish only on business days. The
roll sets `reference_date` to the *latest available* ingested date
(`max(as_of)`), so on a Saturday it stays at Friday's fixing — coherent, not
stale. On a fresh volume before any successful real ingest (for example a
fully air-gapped host), the curves keep their seeded `reference_date` and no
market data resolves; real pricing activates once the first ingest succeeds
(`scripts/self_hosted_smoke.py` reports this state distinctly).
