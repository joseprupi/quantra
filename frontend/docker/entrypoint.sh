#!/bin/sh
# Runtime config + reverse-proxy writer for the self-hosted portal image.
#
# The stock nginx image runs every executable under /docker-entrypoint.d/*.sh
# before starting nginx, so this script does two things on each container
# start, before nginx boots:
#
#   1. Writes /config.js (loaded by index.html BEFORE the app bundle) from env
#      vars. Vite inlines VITE_* at build time, so the prebuilt bundle cannot be
#      re-pointed at a different orchestrator without this; src/lib/
#      runtimeConfig.ts reads it with a fallback to the build-time VITE_* values.
#
#   2. Renders /etc/nginx/conf.d/default.conf from docker/nginx.conf (a template)
#      via envsubst, baking the reverse-proxy upstream in. The browser only ever
#      talks to the portal origin; nginx proxies /v1/ and /auth/ to the
#      orchestrator, so there is no cross-origin request and no CORS.
#
#   ORCHESTRATOR_URL          Base URL the browser prefixes onto request paths.
#                             DEFAULT: "" (empty) = SAME-ORIGIN — the browser
#                             calls the portal (/v1/..., /auth/...) and nginx
#                             reverse-proxies to the orchestrator (recommended,
#                             no CORS). Set to an absolute base (e.g.
#                             http://host:8085) only for a cross-origin
#                             orchestrator that itself serves CORS headers.
#   ORCHESTRATOR_PROXY_TARGET Upstream nginx proxies /v1/ and /auth/ to.
#                             DEFAULT: http://orchestrator:8080 (the compose
#                             service). Point it at a host-mapped port
#                             (e.g. http://host.docker.internal:8085) when the
#                             orchestrator is on a different docker network.
#   MARKET_DATA_URL           Base URL/prefix the browser prefixes onto Quote
#                             Book / MD-catalog request paths.
#                             DEFAULT: "/_md" = SAME-ORIGIN — the browser calls
#                             the portal (/_md/catalog/series, …) and nginx
#                             strips /_md/ and reverse-proxies to the bundle's
#                             LOCAL market_data service (no CORS; the Quote Book
#                             shows the local catalog + user-imported quotes,
#                             not an external cloud catalog). Set to an absolute
#                             base only for a cross-origin MD service that
#                             serves CORS headers.
#   MD_PROXY_TARGET           Upstream nginx proxies /_md/ to (prefix stripped).
#                             DEFAULT: http://market_data:8082 (the compose
#                             service). Mirror ORCHESTRATOR_PROXY_TARGET when the
#                             MD service is on a different docker network.
#   DEV_AUTH_BYPASS           "true" to run the single-user dev-auth bypass
#                             (matches the backend self-hosted profile).
#                             Default: true.
#   DEFAULT_AS_OF             Optional ISO YYYY-MM-DD override for the pricing
#                             forms' INITIAL As-Of date. The shipped demo curves
#                             + synthetic market data live at a fixed historical
#                             date, so set this so a fresh user's default
#                             click-Price anchors there. Unset/malformed ⇒ null
#                             ⇒ no override (forms default to today, as before).
set -eu

: "${ORCHESTRATOR_URL:=}"
: "${ORCHESTRATOR_PROXY_TARGET:=http://orchestrator:8080}"
: "${MARKET_DATA_URL:=/_md}"
: "${MD_PROXY_TARGET:=http://market_data:8082}"
: "${DEV_AUTH_BYPASS:=true}"
: "${DEFAULT_AS_OF:=}"

# Normalise the bypass flag to a JSON boolean.
case "$(printf '%s' "$DEV_AUTH_BYPASS" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on) BYPASS_JSON=true ;;
  *)             BYPASS_JSON=false ;;
esac

# Validate DEFAULT_AS_OF as a strict ISO YYYY-MM-DD; emit a JSON string when
# valid, else JSON null (no override). Guarding here keeps a typo from silently
# feeding a garbage date into the forms.
if printf '%s' "$DEFAULT_AS_OF" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  DEFAULT_AS_OF_JSON="\"${DEFAULT_AS_OF}\""
else
  DEFAULT_AS_OF_JSON=null
fi

# ── 1. Runtime config.js ─────────────────────────────────────────────────────
CONFIG_PATH=/usr/share/nginx/html/config.js

cat > "$CONFIG_PATH" <<EOF
// Generated at container start by docker/entrypoint.sh — do not edit.
window.__QUANTRA_CONFIG__ = {
  orchestratorUrl: "${ORCHESTRATOR_URL}",
  marketDataUrl: "${MARKET_DATA_URL}",
  devAuthBypass: ${BYPASS_JSON},
  defaultAsOf: ${DEFAULT_AS_OF_JSON}
};
EOF

# ── 2. nginx reverse-proxy conf ──────────────────────────────────────────────
# Substitute ONLY the proxy targets so nginx's own $-variables ($uri, $host,
# $remote_addr, …) in the template survive untouched.
export ORCHESTRATOR_PROXY_TARGET MD_PROXY_TARGET
envsubst '${ORCHESTRATOR_PROXY_TARGET} ${MD_PROXY_TARGET}' \
  < /etc/nginx/templates-quantra/nginx.conf \
  > /etc/nginx/conf.d/default.conf

echo "[quantra-portal] wrote ${CONFIG_PATH}: orchestratorUrl='${ORCHESTRATOR_URL}' marketDataUrl='${MARKET_DATA_URL}' devAuthBypass=${BYPASS_JSON} defaultAsOf=${DEFAULT_AS_OF_JSON}"
echo "[quantra-portal] reverse-proxy /v1/ + /auth/ -> ${ORCHESTRATOR_PROXY_TARGET}; /_md/ -> ${MD_PROXY_TARGET}"
