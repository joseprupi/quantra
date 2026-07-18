// Runtime configuration placeholder.
//
// In the self-hosted Docker image this file is OVERWRITTEN at container start
// by docker/entrypoint.sh, which bakes `orchestratorUrl` / `devAuthBypass` /
// `defaultAsOf` from env vars. In local dev and the public hosted build it
// stays this no-op default, so every reader (src/lib/runtimeConfig.ts) falls
// back to the build-time `VITE_*` values (and `defaultAsOf` ⇒ null ⇒ the
// pricing forms keep their "today" As-Of default).
window.__QUANTRA_CONFIG__ = window.__QUANTRA_CONFIG__ || {};
