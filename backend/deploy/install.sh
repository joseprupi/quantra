#!/usr/bin/env bash
###############################################################################
# Quantra self-hosted — pull-and-run installer (published-images edition).
#
# Pulls the prebuilt, version-pinned platform images from the public registry
# (GHCR by default) and runs the full stack with docker-compose.release.yml.
# No registry account or login is needed: the images are public.
#
# Run it from a folder containing docker-compose.release.yml + .env.example
# (this script ships beside them in every GitHub Release of
# github.com/joseprupi/quantra):
#
#   ./install.sh
#
# Fully offline instead? Use the release's quantra-selfhosted-<version>.tar.gz
# and its ./install-offline.sh (docker load, no registry at all).
#
# Idempotent + re-runnable: `pull` and `up -d` reconcile in place and the
# health waits short-circuit once settled.
###############################################################################
set -euo pipefail

unset CDPATH
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.release.yml"
ENV_FILE="${QUANTRA_ENV_FILE:-.env}"
TIMEOUT_SECONDS="${INSTALL_TIMEOUT_SECONDS:-600}"

log()  { printf '  %s\n' "$*"; }
info() { printf '\n==> %s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

# --- 1. Preflight: docker + docker compose --------------------------------
info "Preflight"
if ! command -v docker >/dev/null 2>&1; then
  err "'docker' is not installed or not on PATH."
  err "Install Docker Desktop (macOS/Windows) or Docker Engine (Linux):"
  err "  https://docs.docker.com/get-docker/"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  err "'docker compose' (v2) is not available."
  err "Docker Desktop bundles it; on Linux install the docker-compose-plugin."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  err "The Docker daemon is not running / not reachable. Start Docker and retry."
  exit 1
fi
if [ ! -f "$COMPOSE_FILE" ]; then
  err "$COMPOSE_FILE not found next to this script."
  err "Run install.sh from the unpacked release bundle folder (it ships"
  err "beside docker-compose.release.yml and .env.example)."
  exit 1
fi
log "docker: $(docker --version)"
log "compose: $(docker compose version --short 2>/dev/null || echo v2)"

# Release images are amd64-only. Pin so arm64 hosts (Apple Silicon) pull + run
# under Rosetta/qemu instead of failing on "no matching manifest". Respect a
# value the user already set.
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
log "DOCKER_DEFAULT_PLATFORM=$DOCKER_DEFAULT_PLATFORM (release images are amd64-only)."

ARCH=$(uname -m 2>/dev/null || echo unknown)
case "$ARCH" in
  arm64|aarch64)
    log "Detected $ARCH (Apple Silicon / arm64). The amd64-only images run"
    log "under emulation (Rosetta/qemu) — the first boot is slower."
    ;;
  *) log "Detected $ARCH." ;;
esac

# --- 2. Ensure an env-file exists (cp .env.example -> .env) ----------------
info "Configuration"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example "$ENV_FILE"
    log "No '$ENV_FILE' found — created it from .env.example (defaults are fine)."
  else
    err "Neither $ENV_FILE nor .env.example present in $SCRIPT_DIR."
    exit 1
  fi
else
  log "Using existing env-file: $ENV_FILE"
fi

# Compose invocation shared by every step below.
DC="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

# --- 3. Pull the published images ------------------------------------------
info "Pulling the published images (public registry — no login needed)"
if ! $DC pull; then
  err ""
  err "Pulling the Quantra images failed."
  err ""
  err "Most likely causes:"
  err "  * The GHCR packages may still be PRIVATE ('denied' / 'unauthorized')."
  err "    Right after a first release the packages start private until the"
  err "    maintainer flips them public. Either wait for that, or install"
  err "    fully offline with the release's quantra-selfhosted-<version>"
  err "    tarball (./install-offline.sh — no registry), or build from"
  err "    source: git clone https://github.com/joseprupi/quantra.git &&"
  err "    cd quantra && docker compose up -d"
  err "  * The pinned tag does not exist ('manifest unknown'): check"
  err "    QUANTRA_VERSION in '$ENV_FILE' against the published releases."
  err "  * No network / registry outage: retry later."
  exit 1
fi
log "Images pulled."

# --- 4. Bring the stack up ------------------------------------------------
info "Starting the stack"
$DC up -d
log "Containers started; waiting for first-boot to settle."

# --- 5. Wait for orchestrator healthy + init-seed complete ----------------
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

wait_init_seed() {
  _elapsed=0
  while [ "$_elapsed" -lt "$TIMEOUT_SECONDS" ]; do
    _cid=$($DC ps -aq init-seed 2>/dev/null || true)
    if [ -n "$_cid" ]; then
      _state=$(docker inspect -f '{{ .State.Status }}' "$_cid" 2>/dev/null || echo unknown)
      _rc=$(docker inspect -f '{{ .State.ExitCode }}' "$_cid" 2>/dev/null || echo -1)
      if [ "$_state" = "exited" ]; then
        [ "$_rc" = "0" ] && return 0
        err "init-seed exited non-zero (code=$_rc). Logs:"
        $DC logs --tail 40 init-seed >&2 || true
        return 1
      fi
    fi
    sleep 5
    _elapsed=$((_elapsed + 5))
    printf '.'
  done
  printf '\n'
  err "init-seed did not complete within ${TIMEOUT_SECONDS}s."
  return 1
}

info "Waiting for orchestrator to become healthy"
if ! wait_orchestrator; then
  err "Diagnostics: $DC ps ; $DC logs orchestrator"
  exit 1
fi
printf '\n'
log "orchestrator is healthy."

info "Waiting for init-seed (reference entities) to complete"
if ! wait_init_seed; then
  err "Diagnostics: $DC logs init-seed"
  exit 1
fi
printf '\n'
log "init-seed completed — reference entities are provisioned."

# --- 6. Done --------------------------------------------------------------
PORTAL_PORT=$(grep -E '^PORTAL_HOST_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
[ -z "$PORTAL_PORT" ] && PORTAL_PORT=5173
info "Quantra is up"
log "Open the portal:  http://localhost:${PORTAL_PORT}"
log "Dev-auth bypass logs you in automatically as the single 'dev-user'."
log "Open Products -> IR Swap -> New, keep the prefilled As-Of, pick a seeded"
log "curve + its index, and click Price for a non-zero NPV."
log ""
log "Manage the stack:"
log "  status:    $DC ps"
log "  logs:      $DC logs -f orchestrator"
log "  stop:      $DC down        (KEEPS your data — never add -v)"
log "  upgrade:   ./upgrade.sh    (backs up + updates; keeps your data)"
