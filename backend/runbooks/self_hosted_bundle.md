# Self-hosted quickstart (pointer)

The self-hosted stack is owned by the **repo-root compose file** — one level
above `backend/`:

```bash
git clone https://github.com/joseprupi/quantra.git
cd quantra
docker compose up -d
```

That single command builds the three backend services and the portal from
source, pulls the public pricing-engine image, migrates the database
(`app.*` + `md.*`), ingests the real public market-data feeds (BoE SONIA
OIS, US Treasury, BoE gilt, ECB — plus FRED with a free `FRED_API_KEY`),
seeds the reference entities, and starts the daily ingest cron. Then open
**<http://localhost:5173>**.

Details:

- **Install / what comes up / configuration** — repo-root
  [`README.md`](../../README.md) and the header comment of the repo-root
  [`docker-compose.yml`](../../docker-compose.yml).
- **Boot smoke test** — once the stack is settled:

  ```bash
  # from backend/
  uv run python scripts/self_hosted_smoke.py
  ```

  It prices a payer swap on both seeded real-data curves (GBP SONIA OIS +
  US Treasury) at two fixed rates and asserts non-zero, input-sensitive
  NPVs. Exit code 3 means the app is healthy but no market data has been
  ingested yet (fresh boot / air-gapped host) — not a failure of the stack.
- **Upgrades (keep your data)** — [`../deploy/UPGRADE.md`](../deploy/UPGRADE.md).
- **Release / offline-tarball tooling (maintainers)** —
  [`../deploy/README.md`](../deploy/README.md). Prebuilt images are not
  published yet; the root compose is the supported install.
- **Backend-only dev stack** (Postgres + opt-in ingester/engine profiles) —
  [`../docker-compose.yml`](../docker-compose.yml).

To run an isolated second copy alongside a running stack, give it its own
project name, ports, container names and network (all env-overridable):

```bash
PORTAL_HOST_PORT=5273 ORCHESTRATOR_HOST_PORT=8180 MD_SERVICE_HOST_PORT=8182 \
QUANTRA_ENGINE_HOST_PORT=50151 POSTGRES_HOST_PORT=5532 \
POSTGRES_CONTAINER_NAME=quantra-iso-postgres \
ENGINE_CONTAINER_NAME=quantra-iso-engine \
MD_CONTAINER_NAME=quantra-iso-market-data \
ORCHESTRATOR_CONTAINER_NAME=quantra-iso-orchestrator \
PORTAL_CONTAINER_NAME=quantra-iso-portal \
OFELIA_CONTAINER_NAME=quantra-iso-ofelia \
QUANTRA_NET_NAME=quantra-iso-net \
docker compose -p quantra_iso up -d --build

# teardown (also drops that copy's data volume):
docker compose -p quantra_iso down -v
```
