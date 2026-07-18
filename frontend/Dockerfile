# syntax=docker/dockerfile:1
#
# Self-hosted portal image (PA-4). Multi-stage: build the Vite bundle, then
# serve the static files with nginx — mirroring how the engine/orchestrator
# ship as prebuilt images rather than `npm run dev`.
#
#   docker build -t quantra-portal:selfhosted .
#
# By default the browser talks ONLY to the portal origin: nginx reverse-proxies
# /v1/ and /auth/ to the orchestrator (same-origin, no CORS). Point the proxy at
# your orchestrator at RUN time (no rebuild):
#   docker run -e ORCHESTRATOR_PROXY_TARGET=http://orchestrator:8080 \
#     -e DEV_AUTH_BYPASS=true -p 5199:80 quantra-portal:selfhosted
# docker/entrypoint.sh writes /config.js AND the nginx proxy conf from env vars
# before nginx boots. Set ORCHESTRATOR_URL (absolute) only for the rarer
# cross-origin deployment where the orchestrator serves its own CORS headers.

# ── Stage 1: build the static bundle ────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app

# Install deps against the committed lockfile for a reproducible build.
COPY package.json package-lock.json ./
RUN npm ci

# Build (tsc + vite build → /app/dist). A production build hard-disables the
# dev-auth bypass at compile time; it is re-enabled only at RUN time via the
# runtime config.js (window.__QUANTRA_CONFIG__.devAuthBypass). No Firebase env
# is provided: the self-hosted bundle runs under the dev-auth bypass and never
# contacts Firebase; firebase.ts degrades to inert placeholders when the config
# is absent so initialization does not crash the app.
COPY . .
# Raise Node's old-space cap for the build: the Vite/tsc build peaks above the
# ~2GB default and OOMs ("Reached heap limit"), especially under CI arch
# contention. Build stage only — the nginx runtime stage never runs Node.
ENV NODE_OPTIONS=--max-old-space-size=4096
RUN npm run build

# ── Stage 2: serve the static files ─────────────────────────────────────────
FROM nginx:1.27-alpine AS serve

# Static bundle.
COPY --from=build /app/dist /usr/share/nginx/html

# SPA-aware server config TEMPLATE (history fallback + no-store on the runtime
# config + same-origin reverse proxy). Staged outside /etc/nginx/templates so
# the stock 20-envsubst-on-templates.sh does NOT touch it — our own entrypoint
# renders it with the resolved proxy target. Drop the stock welcome default.conf
# (our entrypoint writes the real one before nginx starts).
COPY docker/nginx.conf /etc/nginx/templates-quantra/nginx.conf
RUN rm -f /etc/nginx/conf.d/default.conf

# Runtime config + reverse-proxy writer. The stock nginx image runs every
# executable under /docker-entrypoint.d/*.sh before starting nginx, so this
# bakes /config.js and /etc/nginx/conf.d/default.conf from env on each start.
COPY docker/entrypoint.sh /docker-entrypoint.d/40-quantra-config.sh
RUN chmod +x /docker-entrypoint.d/40-quantra-config.sh

EXPOSE 80
