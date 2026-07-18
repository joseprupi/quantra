#!/usr/bin/env bash
# Regenerate src/lib/api/_generated/orchestrator.d.ts from the running
# orchestrator's /openapi.json (D89).
#
# Usage:
#   ./scripts/regen_orchestrator_types.sh            # uses http://localhost:8080
#   ORCH_URL=http://my-orchestrator-host:8080 ./scripts/regen_orchestrator_types.sh
#
# The generated file is committed; it is NOT .gitignored. Regenerate whenever
# the orchestrator schema changes and commit the result.

set -euo pipefail

ORCH_URL="${ORCH_URL:-http://localhost:8080}"
SCHEMA_URL="${ORCH_URL}/openapi.json"
OUT="src/lib/api/_generated/orchestrator.d.ts"

echo "Fetching schema from ${SCHEMA_URL} ..."
TMP=$(mktemp /tmp/orchestrator_openapi_XXXXXX.json)
trap 'rm -f "$TMP"' EXIT

curl --fail --silent --show-error "$SCHEMA_URL" -o "$TMP"

echo "Running openapi-typescript ..."
npx openapi-typescript "$TMP" -o "$OUT"

echo "Generated: $OUT"
