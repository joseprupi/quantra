/**
 * Runtime (post-build) configuration.
 *
 * Vite inlines every `VITE_*` value at BUILD time. That is wrong for a
 * distributable self-hosted image: the *same* prebuilt image must be
 * re-pointable at a different orchestrator host/port at RUN time, without a
 * rebuild. The self-hosted container's entrypoint therefore writes a small
 * `/config.js` from env vars:
 *
 *   window.__QUANTRA_CONFIG__ = { orchestratorUrl: "...", devAuthBypass: true };
 *
 * `index.html` loads `/config.js` BEFORE the app bundle, so this object is
 * already present the first time any module reads it.
 *
 * Every getter falls back to the build-time `import.meta.env.VITE_*` value, so
 * the public hosted (Firebase) build — which ships a no-op `config.js` and
 * never sets these keys — behaves EXACTLY as before this module existed.
 */

export interface QuantraRuntimeConfig {
  /**
   * Orchestrator base URL prefixed onto every request path. In the shipped
   * self-hosted image this is the EMPTY STRING by default: the browser then
   * calls the portal's OWN origin (e.g. `/v1/price/swap/ir`) and nginx
   * reverse-proxies `/v1/` and `/auth/` to the orchestrator — same-origin, so
   * no CORS. It may still be set to an absolute base (e.g.
   * "http://host:8085") for the rare cross-origin deployment (needs CORS).
   */
  orchestratorUrl?: string;
  /** Opt into the dev-auth bypass (single implicit dev-user, no login). */
  devAuthBypass?: boolean;
  /**
   * Base URL/prefix prefixed onto every market-data (Quote Book / MD catalog)
   * request path. In the shipped self-hosted image this is a same-origin path
   * PREFIX (default `/_md`): the browser calls the portal's OWN origin (e.g.
   * `/_md/catalog/series`) and nginx strips `/_md/` and reverse-proxies to the
   * bundle's local `market_data` service — same-origin, so no CORS, and it
   * reads the LOCAL catalog. Absent ⇒ the build-time
   * `VITE_MARKET_DATA_API_URL`, then `http://localhost:8082` in dev or the
   * same-origin `/_md` prefix in a production build.
   */
  marketDataUrl?: string;
  /**
   * Optional ISO `YYYY-MM-DD` override for the pricing forms' INITIAL As-Of
   * date. The shipped self-hosted bundle's seeded demo curves live at a fixed
   * historical date, so a fresh user's default click-Price must anchor there
   * rather than at today. (Real public market data, once ingested, updates
   * daily and rolls this forward via the latest-date API.) Absent/null ⇒
   * no override, so the As-Of default stays "today". Only changes the INITIAL
   * value; the field remains user-editable.
   */
  defaultAsOf?: string | null;
}

/** The runtime config object injected by the self-hosted entrypoint, or {}. */
export function runtimeConfig(): QuantraRuntimeConfig {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: QuantraRuntimeConfig };
  return g.__QUANTRA_CONFIG__ ?? {};
}

/**
 * Orchestrator base URL, prefixed onto every request path.
 *
 * Precedence: runtime `config.js` `orchestratorUrl` when the KEY is present
 * (an empty string is meaningful: same-origin relative fetches, which nginx
 * reverse-proxies to the orchestrator — no CORS; a non-empty value is an
 * absolute cross-origin base); otherwise build-time `VITE_ORCHESTRATOR_URL`,
 * then `http://localhost:8080`.
 */
export function getOrchestratorUrl(): string {
  const runtime = runtimeConfig().orchestratorUrl;
  // Presence of the key (even as "") is authoritative — that is the injected
  // self-hosted config. Absence means dev / hosted, so fall back to VITE.
  if (typeof runtime === 'string') return runtime;
  return (import.meta.env.VITE_ORCHESTRATOR_URL as string | undefined) || 'http://localhost:8080';
}

/**
 * Market-data base URL/prefix, prefixed onto every Quote Book / market-data
 * catalog request path.
 *
 * Precedence mirrors `getOrchestratorUrl`: runtime `config.js` `marketDataUrl`
 * when the KEY is present (self-hosted default `/_md`, a same-origin prefix
 * that nginx strips and reverse-proxies to the bundle's local market-data
 * service); otherwise build-time `VITE_MARKET_DATA_API_URL`; otherwise a dev
 * build talks to a local market-data service (`http://localhost:8082`) and a
 * production build defaults to the same-origin `/_md` prefix.
 */
export function getMarketDataUrl(): string {
  const runtime = runtimeConfig().marketDataUrl;
  // Presence of the key (even as "") is authoritative — that is the injected
  // self-hosted config. Absence means dev / hosted, so fall back to VITE.
  if (typeof runtime === 'string') return runtime;
  const vite = import.meta.env.VITE_MARKET_DATA_API_URL as string | undefined;
  if (vite) return vite;
  return import.meta.env.DEV ? 'http://localhost:8082' : '/_md';
}

/**
 * True when the runtime `config.js` explicitly opts into the dev-auth bypass.
 * This is the ONLY signal that can enable the bypass in a production build; the
 * public hosted build ships no such flag, so it can never turn on there.
 */
export function isRuntimeDevAuthBypass(): boolean {
  return runtimeConfig().devAuthBypass === true;
}

/** Matches a strict ISO calendar date `YYYY-MM-DD`. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Optional runtime override for the pricing forms' INITIAL As-Of date, as an
 * ISO `YYYY-MM-DD` string, or `null` when unset/invalid.
 *
 * Precedence: runtime `config.js` `defaultAsOf` (injected by the self-hosted
 * entrypoint from `DEFAULT_AS_OF`) when it is a valid ISO date; otherwise
 * build-time `VITE_DEFAULT_AS_OF` if valid; otherwise `null` ("no override" —
 * callers keep their "today" default). Malformed values are treated as
 * absent → `null`, never propagated to the forms.
 */
export function getDefaultAsOf(): string | null {
  const runtime = runtimeConfig().defaultAsOf;
  if (typeof runtime === 'string' && ISO_DATE.test(runtime)) return runtime;
  // Only fall through to VITE when the runtime key is absent/null — an
  // explicitly injected (even if malformed) value should not be masked by a
  // build-time default. A present-but-invalid runtime value ⇒ null.
  if (runtime == null) {
    const vite = import.meta.env.VITE_DEFAULT_AS_OF as string | undefined;
    if (typeof vite === 'string' && ISO_DATE.test(vite)) return vite;
  }
  return null;
}
