# Quantra self-hosted — safe upgrade runbook

Update a Quantra install (orchestrator, market-data, md-ingester, portal,
and the pricing engine) **while preserving your database.** Written for the
offline-tarball install (`make-release-tarball.sh` + `install-offline.sh`);
the `docker compose pull` path exists in the script but is only useful once
prebuilt images are published (they are not yet — see [`README.md`](README.md)).

---

## 🟡 The golden rule

> **NEVER run `docker compose ... down -v`.**
> The `-v` flag deletes the `postgres_data` volume and with it **all of your
> data** — imported market data, saved products, curves, everything. To stop
> the stack use `down` **without** `-v`. To restart, `up -d`.

Your data lives in the named Docker volume `postgres_data`. Recreating the
containers (new images) reuses that volume, so upgrades never touch your data.
A one-shot `migrate` container runs `alembic upgrade head` on every boot, so the
schema advances *in place* over the existing volume.

---

## One command

From the deploy folder (where `docker-compose.release.yml` and your `.env`
live):

```bash
# Tarball / offline install — load the new images from the new bundle:
./upgrade.sh --tarball /path/to/quantra-selfhosted-<newversion>/images.tar.gz --version <newversion>

# Registry install (future — needs published images) — pull the new images:
./upgrade.sh --version <newversion>
```

`upgrade.sh` does, in order:

1. **Backs up first, always.** `pg_dump` of the running Postgres →
   `./backups/quantra-db-<timestamp>.sql.gz`. **If the backup fails, the upgrade
   aborts** and your running stack is untouched.
2. **Gets the new images** — `docker load` (tarball) or `docker compose pull`.
3. **Brings the stack up** — `docker compose up -d` (new images, **never** `-v`).
   The `migrate` container runs `alembic -n app upgrade head && alembic -n md
   upgrade head` automatically → schema advances over your existing data.
4. **Smokes it** — waits for the orchestrator to be healthy, confirms a core
   table (`app.users`) still has rows (proves your data survived), and runs the
   optional `scripts/self_hosted_smoke.py` pricing check if present. The smoke
   distinguishes "no market data ingested yet" (normal on an air-gapped host or
   right after a fresh boot — not an upgrade failure, no rollback needed) from
   a real pricing failure.
5. **Reports** the backup location and the rollback steps.

---

## Where backups go

`./backups/quantra-db-<YYYYMMDD-HHMMSS>.sql.gz` next to the compose file
(override with `QUANTRA_BACKUP_DIR`). Each is a **full compressed logical dump**
(`pg_dump`) of the `quantra` database. Keep the pre-upgrade backup until you have
confirmed the new version works. These files contain your data — store them
securely and prune old ones yourself.

To take a manual backup any time:

```bash
docker compose --env-file .env -f docker-compose.release.yml \
  exec -T postgres pg_dump -U quantra -d quantra | gzip > backups/manual-$(date +%Y%m%d-%H%M%S).sql.gz
```

`pg_dump` ships **inside** the `postgres:16` image, so this works with no host
Postgres client installed.

---

## Rollback

Migrations are **additive / forward-only** — there is no supported `alembic
downgrade`. To roll back you restore the pre-upgrade backup and redeploy the
previous version:

1. Repoint `.env` to the previous release:

   ```
   QUANTRA_VERSION=<old-version>
   ```

2. Recreate the database from your pre-upgrade backup. Because the backup is a
   **full logical dump**, restore it into a **fresh** volume. This is the one
   sanctioned use of `-v` — you have a verified backup in hand:

   ```bash
   DC="docker compose --env-file .env -f docker-compose.release.yml"
   $DC down -v                 # drops the volume — ONLY because we restore next
   $DC up -d postgres          # fresh empty volume
   gunzip -c backups/quantra-db-<timestamp>.sql.gz | $DC exec -T postgres psql -U quantra -d quantra
   ```

3. Bring the previous version back up:

   ```bash
   $DC up -d
   ```

(If you have not run any new migrations you may instead just repoint
`QUANTRA_VERSION` to the old value and `up -d` — but when in doubt, restore from
the backup, which is always correct.)

---

## Migration discipline (for maintainers)

- Migrations are **additive and forward-only**. Never write a migration that
  drops or rewrites data a rollback would need.
- **Test the upgrade from the last released version every release**: install the
  previous tarball, then run `upgrade.sh` to the new one and confirm the smoke
  passes and data survives. This is the acceptance gate for a release's upgrade
  path.
- Never rely on `alembic downgrade` in production — treat the pre-upgrade
  `pg_dump` as the rollback mechanism.

---

## Common mistakes the scripts guard against

- **`down -v` muscle memory** — `upgrade.sh` never passes `-v`; the install/
  upgrade help text repeats the golden rule.
- **Upgrading without a backup** — impossible: a failed/empty `pg_dump` aborts
  the run before any image is touched.
- **Losing the volume across a version bump** — the compose file always uses the
  named `postgres_data` volume; new images reuse it.
- **Running `upgrade.sh` on a fresh machine** — it refuses if there is no `.env`
  (that is an install, not an upgrade).
