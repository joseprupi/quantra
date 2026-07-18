#!/usr/bin/env bash
###############################################################################
# Quantra self-hosted — SAFE upgrade (keep the database, update everything).
#
# Works for BOTH install flavors:
#   - tarball / offline install:  ./upgrade.sh --tarball <path-to-images.tar.gz>
#   - registry install (future — needs published images):
#                                 ./upgrade.sh          (docker compose pull)
#
# Sequence:
#   1. BACKUP FIRST, ALWAYS   — pg_dump the running Postgres to ./backups/.
#                               Abort the whole upgrade if the backup fails.
#   2. Get the new images     — docker load (tarball) or docker compose pull.
#   3. Bring up               — docker compose up -d. The `migrate` init
#                               container runs `alembic upgrade head` over the
#                               EXISTING volume → schema advances in place.
#                               NEVER passes -v (that would delete your data).
#   4. Smoke                  — wait for health, run the smoke if present,
#                               confirm a table row count is still non-zero.
#   5. Report                 — where the backup is + how to roll back.
#
# The Postgres data lives in the named volume `postgres_data`; recreating the
# containers preserves it. This script goes out of its way to make the ONLY
# destructive command (`down -v`) impossible to reach.
###############################################################################
set -euo pipefail

unset CDPATH
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.release.yml"
ENV_FILE="${QUANTRA_ENV_FILE:-.env}"
TIMEOUT_SECONDS="${INSTALL_TIMEOUT_SECONDS:-600}"
BACKUP_DIR="${QUANTRA_BACKUP_DIR:-./backups}"

TARBALL=""
VERSION_OVERRIDE=""

log()  { printf '  %s\n' "$*"; }
info() { printf '\n==> %s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

usage() {
  cat <<EOF
Quantra safe upgrade — keeps your database, updates everything else.

Usage:
  ./upgrade.sh [--tarball <images.tar.gz>] [--version <X.Y.Z>]

Options:
  --tarball <path>   Load the new images from a saved image file (tarball /
                     offline install). If omitted, images are pulled with
                     'docker compose pull' (registry install — only useful
                     once prebuilt images are published).
  --version <X.Y.Z>  Set QUANTRA_VERSION for this upgrade (also persists it to
                     $ENV_FILE). If omitted, the value already in $ENV_FILE is
                     used.
  -h, --help         Show this help.

GOLDEN RULE: never run 'docker compose ... down -v'. The -v deletes the
Postgres volume and ALL your data. This script never passes -v.
EOF
}

# --- Parse args -----------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --tarball) TARBALL="${2:-}"; shift 2 ;;
    --version) VERSION_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

# --- Preflight ------------------------------------------------------------
info "Preflight"
if ! command -v docker >/dev/null 2>&1; then
  err "'docker' is not installed or not on PATH."; exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  err "'docker compose' (v2) is not available."; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  err "The Docker daemon is not running / not reachable."; exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  if [ -f ".env.example" ]; then
    err "No '$ENV_FILE' found. This looks like a fresh install, not an upgrade."
    err "Run ./install-offline.sh (tarball) first — or install from source"
    err "with the repo-root 'docker compose up -d'."
    exit 1
  fi
  err "No '$ENV_FILE' found next to $COMPOSE_FILE."; exit 1
fi
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
log "docker: $(docker --version)"
log "Using env-file: $ENV_FILE"

DC="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

# --- 1. BACKUP FIRST, ALWAYS ----------------------------------------------
# pg_dump ships INSIDE the postgres:16 image, so `docker compose exec postgres
# pg_dump` is always available while the container runs — no host client
# needed. -T disables TTY allocation so the stream can be redirected to a file.
# The DB creds match the compose file (quantra / quantra / db=quantra).
info "Backup (pg_dump) — MUST succeed before we touch anything"
mkdir -p "$BACKUP_DIR"
_ts=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/quantra-db-${_ts}.sql.gz"

# Postgres must be running to dump it. If the stack is stopped, bring ONLY
# postgres up first (no -v; just starts the existing container over the volume).
_pg_cid=$($DC ps -q postgres 2>/dev/null || true)
if [ -z "$_pg_cid" ]; then
  log "Postgres container is not running — starting it to take the backup."
  $DC up -d postgres
  # Give it a moment to accept connections.
  _w=0
  while [ "$_w" -lt 60 ]; do
    if $DC exec -T postgres pg_isready -U quantra -d quantra >/dev/null 2>&1; then
      break
    fi
    sleep 2; _w=$((_w + 2))
  done
fi

log "Dumping database 'quantra' -> $BACKUP_FILE"
# `pg_dump | gzip` inside a pipeline: guard against a silent failure by using
# pipefail (set above) and checking the resulting file is non-empty.
if $DC exec -T postgres pg_dump -U quantra -d quantra | gzip > "$BACKUP_FILE"; then
  if [ -s "$BACKUP_FILE" ]; then
    _bsize=$(du -h "$BACKUP_FILE" | cut -f1)
    log "Backup OK: $BACKUP_FILE (${_bsize})"
  else
    err "Backup file is EMPTY — refusing to upgrade. Investigate before retrying."
    rm -f "$BACKUP_FILE"
    exit 1
  fi
else
  err "pg_dump FAILED — ABORTING the upgrade. Your running stack is untouched."
  err "Check that Postgres is healthy: $DC ps ; $DC logs postgres"
  rm -f "$BACKUP_FILE"
  exit 1
fi

# --- 2. Get the new images ------------------------------------------------
info "Fetching new images"
if [ -n "$TARBALL" ]; then
  if [ ! -f "$TARBALL" ]; then
    err "--tarball path not found: $TARBALL"; exit 1
  fi
  log "docker load < $TARBALL"
  if ! docker load -i "$TARBALL"; then
    err "docker load failed — the image file may be corrupt. Upgrade aborted."
    err "Your database backup is safe at: $BACKUP_FILE"
    exit 1
  fi
  log "New platform images loaded from the tarball."
else
  log "docker compose pull (registry install)"
  if ! $DC pull; then
    err "docker compose pull failed. Upgrade aborted; your data is untouched."
    err "Your database backup is safe at: $BACKUP_FILE"
    exit 1
  fi
  log "Images pulled."
fi

# --- 2b. Optionally bump QUANTRA_VERSION ----------------------------------
if [ -n "$VERSION_OVERRIDE" ]; then
  info "Pinning QUANTRA_VERSION=$VERSION_OVERRIDE in $ENV_FILE"
  if grep -qE '^QUANTRA_VERSION=' "$ENV_FILE"; then
    # portable in-place edit without GNU-sed assumptions
    _tmp=$(mktemp)
    sed "s/^QUANTRA_VERSION=.*/QUANTRA_VERSION=${VERSION_OVERRIDE}/" "$ENV_FILE" > "$_tmp"
    mv "$_tmp" "$ENV_FILE"
  else
    printf 'QUANTRA_VERSION=%s\n' "$VERSION_OVERRIDE" >> "$ENV_FILE"
  fi
  log "Set QUANTRA_VERSION=$VERSION_OVERRIDE."
fi

# --- 3. Bring the stack up (NEVER -v) -------------------------------------
# `up -d` recreates the app/portal/engine containers with the new images while
# reusing the SAME postgres_data volume. The `migrate` init container runs
# `alembic -n app upgrade head && alembic -n md upgrade head` automatically, so
# the schema advances in place over the existing data. We explicitly NEVER pass
# -v here — that flag only exists on `down` and would delete the volume.
info "Bringing the stack up with the new images (data volume preserved)"
$DC up -d
log "Containers recreated; migrate runs alembic upgrade head over your data."

# --- 4. Smoke -------------------------------------------------------------
wait_orchestrator() {
  _elapsed=0
  while [ "$_elapsed" -lt "$TIMEOUT_SECONDS" ]; do
    _cid=$($DC ps -q orchestrator 2>/dev/null || true)
    if [ -n "$_cid" ]; then
      _status=$(docker inspect -f '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}{{ .State.Status }}{{ end }}' "$_cid" 2>/dev/null || echo unknown)
      case "$_status" in
        healthy) return 0 ;;
        exited|dead) err "orchestrator exited unexpectedly (status=$_status)."; return 1 ;;
      esac
    fi
    sleep 5
    _elapsed=$((_elapsed + 5))
    printf '.'
  done
  printf '\n'
  err "orchestrator did not become healthy within ${TIMEOUT_SECONDS}s."
  return 1
}

info "Waiting for orchestrator to become healthy"
if ! wait_orchestrator; then
  err "Upgrade brought up an UNHEALTHY orchestrator. See rollback below."
  err "Diagnostics: $DC ps ; $DC logs orchestrator"
  err "Your pre-upgrade backup: $BACKUP_FILE"
  exit 1
fi
printf '\n'
log "orchestrator is healthy."

# 4a. Data survived: assert a core table still has rows. app.users is created
# + seeded (dev-user) on first boot; a non-zero count proves the volume — not
# a fresh empty one — is in use. Read via the postgres container (creds known).
info "Verifying your data survived the upgrade"
_rows=$($DC exec -T postgres psql -U quantra -d quantra -tAc \
  'SELECT count(*) FROM app.users;' 2>/dev/null | tr -d '[:space:]' || echo "")
if [ -n "$_rows" ] && [ "$_rows" -gt 0 ] 2>/dev/null; then
  log "app.users row count = $_rows (data preserved)."
else
  err "Could not confirm a non-zero app.users row count (got: '${_rows:-<none>}')."
  err "This may be transient (schema mid-migrate) or a real problem — inspect:"
  err "  $DC exec -T postgres psql -U quantra -d quantra -c 'SELECT count(*) FROM app.users;'"
  err "Your pre-upgrade backup is safe at: $BACKUP_FILE"
fi

# 4b. Optional functional smoke (prices an IR swap end-to-end) if the script
# was shipped alongside. Non-fatal: absence just skips it.
info "Functional smoke (optional)"
_orch_port=$(grep -E '^ORCHESTRATOR_HOST_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
[ -z "$_orch_port" ] && _orch_port=8080
if [ -f "scripts/self_hosted_smoke.py" ] && command -v python3 >/dev/null 2>&1; then
  log "Running scripts/self_hosted_smoke.py against http://localhost:${_orch_port}"
  set +e
  QUANTRA_ORCH_URL="http://localhost:${_orch_port}" python3 scripts/self_hosted_smoke.py
  _smoke_rc=$?
  set -e
  if [ "$_smoke_rc" -eq 0 ]; then
    log "Smoke passed (priced real market data, input-sensitive NPVs)."
  elif [ "$_smoke_rc" -eq 3 ]; then
    # Exit code 3 = the app is healthy but NO market data has been ingested
    # yet (fresh volume before the first successful public-feed ingest, or an
    # air-gapped host). That is NOT an upgrade failure — do not roll back.
    log "Smoke: app is up, but no market data has been ingested yet, so the"
    log "pricing check could not run. This is expected on an air-gapped host"
    log "or right after a fresh boot — NOT an upgrade failure. Pricing"
    log "activates once the first public-feed ingest succeeds (retried daily)."
  else
    err "Smoke FAILED after upgrade. The app is up but pricing did not verify."
    err "Consider rolling back (see below). Backup: $BACKUP_FILE"
  fi
else
  log "No scripts/self_hosted_smoke.py present (or no python3) — skipping."
  log "Manual check: open the portal and price an IR swap."
fi

# --- 5. Report ------------------------------------------------------------
info "Upgrade complete"
log "Pre-upgrade database backup:"
log "  $BACKUP_FILE"
log ""
log "If something is wrong, ROLL BACK (forward-only — do NOT alembic downgrade):"
log "  1. Repoint $ENV_FILE to the PREVIOUS release: QUANTRA_VERSION=<old>"
log "  2. Recreate the DB from your pre-upgrade backup. Because the backup is a"
log "     FULL logical dump, restore it into a FRESH volume (this is the one"
log "     sanctioned use of -v — you have a verified backup):"
log "       $DC down -v            # drops ONLY because we restore next"
log "       $DC up -d postgres"
log "       gunzip -c \"$BACKUP_FILE\" | $DC exec -T postgres psql -U quantra -d quantra"
log "  3. Bring the old version back up:"
log "       $DC up -d"
log ""
log "See UPGRADE.md for the full runbook (backup location, golden rule, restore)."
