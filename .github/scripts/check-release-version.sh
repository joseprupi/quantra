#!/usr/bin/env bash
###############################################################################
# Release version guard.
#
# Usage: check-release-version.sh <version>          (e.g. 0.3.0, no leading v)
#
# Fails (exit 1) with a precise message unless ALL THREE version sources agree
# with <version>:
#
#   1. backend/services/orchestrator/pyproject.toml   -> version = "X.Y.Z"
#   2. backend/.../quantra_orchestrator/api/version.py -> ORCHESTRATOR_VERSION
#   3. frontend/package.json                          -> "version": "X.Y.Z"
#
# Called by .github/workflows/release.yml with the pushed tag (v0.3.0 ->
# 0.3.0) so a mistagged release fails fast, before any image is built or
# pushed. Also runnable locally from the repo root:
#
#   .github/scripts/check-release-version.sh 0.3.0
###############################################################################
set -euo pipefail

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 <version>   (e.g. 0.3.0 — the tag without the leading 'v')" >&2
  exit 2
fi
EXPECTED="$1"

REPO_ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$REPO_ROOT"

PYPROJECT="backend/services/orchestrator/pyproject.toml"
VERSION_PY="backend/services/orchestrator/src/quantra_orchestrator/api/version.py"
PACKAGE_JSON="frontend/package.json"

pyproject_version=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$PYPROJECT" | head -1)
version_py_version=$(sed -n 's/^ORCHESTRATOR_VERSION = "\([^"]*\)".*/\1/p' "$VERSION_PY" | head -1)
package_json_version=$(python3 -c "import json; print(json.load(open('$PACKAGE_JSON'))['version'])")

fail=0
check() {
  # $1 = label, $2 = file, $3 = found value
  if [ -z "$3" ]; then
    echo "FAIL  $1 ($2): could not extract a version string" >&2
    fail=1
  elif [ "$3" != "$EXPECTED" ]; then
    echo "FAIL  $1 ($2): found '$3', expected '$EXPECTED'" >&2
    fail=1
  else
    echo "ok    $1 ($2): $3"
  fi
}

echo "Release version guard — expecting version: $EXPECTED"
check "orchestrator pyproject" "$PYPROJECT" "$pyproject_version"
check "ORCHESTRATOR_VERSION" "$VERSION_PY" "$version_py_version"
check "frontend package.json" "$PACKAGE_JSON" "$package_json_version"

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

The release tag does not match the versions committed in the tree.
To cut release v$EXPECTED, first bump ALL of:
  - $PYPROJECT                 (version = "$EXPECTED")
  - $VERSION_PY                (ORCHESTRATOR_VERSION = "$EXPECTED")
  - $PACKAGE_JSON              ("version": "$EXPECTED")
(and refresh backend/uv.lock with 'uv lock'), commit, then re-tag.
EOF
  exit 1
fi
echo "Version guard passed: tree is at $EXPECTED."
