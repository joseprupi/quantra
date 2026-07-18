#!/usr/bin/env bash
###############################################################################
# Quantra self-hosted — build the distributable offline tarball (maintainers).
#
# Run this on a machine that already has the 4 in-house Quantra images
# present locally (built from this tree and tagged as the release refs), to
# produce ONE self-contained file for offline installs:
# quantra-selfhosted-<version>.tar.gz.
#
#   ./make-release-tarball.sh [version]      # version defaults to 0.3.0
#
# What goes in the tarball:
#   - images.tar.gz  <- `docker save` of the 4 in-house images
#                       (orchestrator, market-data, md-ingester, portal),
#                       gzip-compressed. The installer `docker load`s this;
#                       NO registry login or network pull required.
#   - docker-compose.release.yml
#   - .env.example
#   - install-offline.sh   (the offline installer)
#   - upgrade.sh           (the safe upgrade path)
#   - README-INSTALL.md    (short install readme, generated below)
#
# NOT saved into the tarball (deliberate): the pricing ENGINE image and the
# upstream POSTGRES image are pulled from their public registries at run time
# by `docker compose ... up`. Saving them would bloat the tarball by GBs for
# no benefit.
#
# Robust + idempotent: fails loudly if any image is missing locally,
# rebuilds the tarball cleanly on every run.
###############################################################################
set -euo pipefail

unset CDPATH
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

VERSION="${1:-0.3.0}"
REGISTRY="${REGISTRY:-ghcr.io/joseprupi}"

log()  { printf '  %s\n' "$*"; }
info() { printf '\n==> %s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

# The 4 platform images pinned to $VERSION (the same refs the release
# compose file resolves via REGISTRY + QUANTRA_VERSION).
IMAGES="
${REGISTRY}/quantra-orchestrator:${VERSION}
${REGISTRY}/quantra-market-data:${VERSION}
${REGISTRY}/quantra-md-ingester:${VERSION}
${REGISTRY}/quantra-portal:${VERSION}
"

# --- 0. Preflight ---------------------------------------------------------
info "Preflight"
if ! command -v docker >/dev/null 2>&1; then
  err "'docker' is not installed or not on PATH."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  err "The Docker daemon is not running / not reachable. Start Docker and retry."
  exit 1
fi
log "docker: $(docker --version)"
log "Building the offline-install tarball for version: ${VERSION}"
log "Images sourced from namespace: ${REGISTRY}"

# --- 1. Verify every platform image is present locally --------------------
# Fail LOUDLY (before doing any work) if an image is missing — the operator
# must `docker pull` or build it first. `docker save` on a missing image errors
# anyway, but a clear preflight message is friendlier.
info "Checking the 4 platform images are present locally"
_missing=0
for _img in $IMAGES; do
  if docker image inspect "$_img" >/dev/null 2>&1; then
    log "present: $_img"
  else
    err "MISSING: $_img"
    _missing=1
  fi
done
if [ "$_missing" -ne 0 ]; then
  err ""
  err "One or more images are not present locally. Either pull the published"
  err "release images (docker pull ${REGISTRY}/quantra-orchestrator:${VERSION}"
  err "...and the others), or build them from the repo tree and tag them at"
  err ":${VERSION}, e.g. from the repo root:"
  err "  docker build -f backend/services/orchestrator/Dockerfile \\"
  err "    -t ${REGISTRY}/quantra-orchestrator:${VERSION} backend   # ...and the others"
  err "Then re-run: ./make-release-tarball.sh ${VERSION}"
  exit 1
fi

# --- 2. Sanity: the deliverable files exist beside this script ------------
info "Checking bundle files"
for _f in docker-compose.release.yml .env.example install-offline.sh upgrade.sh; do
  if [ ! -f "$SCRIPT_DIR/$_f" ]; then
    err "Required bundle file missing next to this script: $_f"
    exit 1
  fi
  log "found: $_f"
done

# --- 3. Assemble a clean staging directory --------------------------------
# Everything is staged under a temp dir, then tarred, so the run is idempotent
# and leaves no half-built artifacts in deploy/ on failure.
STAGE_ROOT=$(mktemp -d)
trap 'rm -rf "$STAGE_ROOT"' EXIT
STAGE="$STAGE_ROOT/quantra-selfhosted-${VERSION}"
mkdir -p "$STAGE"

info "docker save -> images.tar.gz (this can take a few minutes)"
log "Saving:"
for _img in $IMAGES; do log "  $_img"; done
# Save all 4 into a single stream and gzip it. --output avoids a shell
# redirect (clearer failures); pipe through gzip for the compressed layer file.
docker save $IMAGES | gzip > "$STAGE/images.tar.gz"
_img_size=$(du -h "$STAGE/images.tar.gz" | cut -f1)
log "images.tar.gz written (${_img_size})."

info "Copying compose + env + scripts into the bundle"
cp "$SCRIPT_DIR/docker-compose.release.yml" "$STAGE/"
cp "$SCRIPT_DIR/.env.example"               "$STAGE/"
cp "$SCRIPT_DIR/install-offline.sh"         "$STAGE/"
cp "$SCRIPT_DIR/upgrade.sh"                  "$STAGE/"
chmod +x "$STAGE/install-offline.sh" "$STAGE/upgrade.sh"
log "copied."

# --- 4. Generate the install README ----------------------------------------
info "Writing README-INSTALL.md"
cat > "$STAGE/README-INSTALL.md" <<EOF
# Quantra — self-hosted edition (${VERSION})

Everything you need is in this folder. You do **not** need a registry account
or a network pull of the in-house Quantra images — they are bundled here as
\`images.tar.gz\`.

## Install

1. Install Docker (Desktop on macOS/Windows, Engine + compose plugin on Linux):
   https://docs.docker.com/get-docker/
2. From this folder, run:
   \`\`\`bash
   ./install-offline.sh
   \`\`\`
   It loads the bundled images, brings the stack up (pulling only the
   pricing-engine + Postgres images from their public registries), waits for
   the app to be healthy, and prints the URL.
3. Open the portal (default): http://localhost:5173

## Upgrade later (keeps your database)

When you receive a newer tarball, unpack it and run its \`upgrade.sh\`:
   \`\`\`bash
   ./upgrade.sh --tarball ../quantra-selfhosted-<newversion>/images.tar.gz
   \`\`\`
It **backs up your database first**, loads the new images, and restarts —
your data in the \`postgres_data\` volume is preserved. See \`UPGRADE.md\`
(in the Quantra deploy docs) for the golden rule and rollback steps.

> GOLDEN RULE: never run \`docker compose ... down -v\`. The \`-v\` deletes the
> Postgres volume and with it ALL your data. Use \`down\` (no \`-v\`) to stop.

## Configure (optional)

Copy \`.env.example\` to \`.env\` and edit host ports, container names, or an
optional FRED_API_KEY before installing. Defaults work out of the box.
EOF
log "README-INSTALL.md written."

# --- 5. Tar it up ---------------------------------------------------------
OUT="quantra-selfhosted-${VERSION}.tar.gz"
info "Creating ${OUT}"
# -C the stage root so the archive contains the single top-level dir
# quantra-selfhosted-<version>/... (clean unpack). Overwrite any prior build.
rm -f "$SCRIPT_DIR/$OUT"
tar -C "$STAGE_ROOT" -czf "$SCRIPT_DIR/$OUT" "quantra-selfhosted-${VERSION}"

# --- 6. Report size + sha256 ----------------------------------------------
info "Done"
_size=$(du -h "$SCRIPT_DIR/$OUT" | cut -f1)
if command -v sha256sum >/dev/null 2>&1; then
  _sha=$(sha256sum "$SCRIPT_DIR/$OUT" | cut -d' ' -f1)
elif command -v shasum >/dev/null 2>&1; then
  _sha=$(shasum -a 256 "$SCRIPT_DIR/$OUT" | cut -d' ' -f1)
else
  _sha="(no sha256 tool found)"
fi
log "Tarball:  $SCRIPT_DIR/$OUT"
log "Size:     ${_size}"
log "SHA-256:  ${_sha}"
log ""
log "Distribute this ONE file. After unpacking, run ./install-offline.sh —"
log "no token, no registry login needed."
