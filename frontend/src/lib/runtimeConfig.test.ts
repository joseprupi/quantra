import { afterEach, describe, expect, it } from 'vitest';
import {
  getDefaultAsOf,
  getMarketDataUrl,
  getOrchestratorUrl,
  isRuntimeDevAuthBypass,
  runtimeConfig,
} from './runtimeConfig';

// The self-hosted entrypoint injects `window.__QUANTRA_CONFIG__` before the app
// bundle loads. These tests exercise how getOrchestratorUrl() composes the base
// URL from that runtime config vs. the dev/hosted fallback — in particular the
// same-origin (empty-string) default the shipped image ships, which makes the
// orchestrator fetch relative so nginx can reverse-proxy it (no CORS).

type Cfg = { orchestratorUrl?: string; marketDataUrl?: string; devAuthBypass?: boolean; defaultAsOf?: string | null };

function setConfig(cfg: Cfg | undefined): void {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: Cfg };
  if (cfg === undefined) delete g.__QUANTRA_CONFIG__;
  else g.__QUANTRA_CONFIG__ = cfg;
}

describe('runtimeConfig.getOrchestratorUrl', () => {
  afterEach(() => setConfig(undefined));

  it('empty-string orchestratorUrl => same-origin (relative) base', () => {
    // Shipped self-hosted default: the KEY is present but empty. An empty base
    // makes `${base}/v1/price/...` resolve to `/v1/price/...` on the portal
    // origin — which nginx reverse-proxies to the orchestrator.
    setConfig({ orchestratorUrl: '', devAuthBypass: true });
    const base = getOrchestratorUrl();
    expect(base).toBe('');
    // Composes to a clean same-origin path (no host, no double slash).
    expect(`${base}/v1/price/swap/ir`).toBe('/v1/price/swap/ir');
  });

  it('non-empty orchestratorUrl => that absolute cross-origin base wins', () => {
    setConfig({ orchestratorUrl: 'http://host:8085', devAuthBypass: true });
    expect(getOrchestratorUrl()).toBe('http://host:8085');
    expect(`${getOrchestratorUrl()}/v1/curves`).toBe('http://host:8085/v1/curves');
  });

  // Dev `npm run dev` / public hosted build: config.js sets no orchestratorUrl,
  // so the base is the build-time VITE value (or the hard localhost default).
  const viteFallback =
    (import.meta.env.VITE_ORCHESTRATOR_URL as string | undefined) || 'http://localhost:8080';

  it('no runtime config key => falls back to build-time VITE / dev default', () => {
    setConfig({});
    expect(getOrchestratorUrl()).toBe(viteFallback);
  });

  it('absent __QUANTRA_CONFIG__ entirely => same fallback', () => {
    setConfig(undefined);
    expect(runtimeConfig()).toEqual({});
    expect(getOrchestratorUrl()).toBe(viteFallback);
  });
});

describe('runtimeConfig.getMarketDataUrl', () => {
  afterEach(() => setConfig(undefined));

  it('same-origin `/_md` prefix when config.js injects it (shipped self-hosted default)', () => {
    // Self-hosted image: the browser calls `/_md/catalog/series` on the portal
    // origin; nginx strips `/_md/` and reverse-proxies to the LOCAL market_data
    // service — so the Quote Book shows the local catalog + imported quotes.
    setConfig({ orchestratorUrl: '', marketDataUrl: '/_md', devAuthBypass: true });
    const base = getMarketDataUrl();
    expect(base).toBe('/_md');
    expect(`${base}/catalog/series`).toBe('/_md/catalog/series');
  });

  it('non-empty absolute marketDataUrl => that cross-origin base wins', () => {
    setConfig({ marketDataUrl: 'https://md.example.com' });
    expect(getMarketDataUrl()).toBe('https://md.example.com');
  });

  // Dev `npm run dev` / prebuilt image without config.js: no runtime
  // marketDataUrl, so the base is the build-time VITE value, then a local
  // dev service (:8082) in dev mode or the same-origin `/_md` prefix in a
  // production build.
  const mdViteFallback =
    (import.meta.env.VITE_MARKET_DATA_API_URL as string | undefined) ||
    (import.meta.env.DEV ? 'http://localhost:8082' : '/_md');

  it('no runtime key => falls back to build-time VITE / mode default', () => {
    setConfig({});
    expect(getMarketDataUrl()).toBe(mdViteFallback);
    setConfig(undefined);
    expect(getMarketDataUrl()).toBe(mdViteFallback);
  });
});

describe('runtimeConfig.isRuntimeDevAuthBypass', () => {
  afterEach(() => setConfig(undefined));

  it('true only when the runtime flag is explicitly true', () => {
    setConfig({ devAuthBypass: true });
    expect(isRuntimeDevAuthBypass()).toBe(true);
  });

  it('false when unset or false', () => {
    setConfig({});
    expect(isRuntimeDevAuthBypass()).toBe(false);
    setConfig({ devAuthBypass: false });
    expect(isRuntimeDevAuthBypass()).toBe(false);
  });
});

describe('runtimeConfig.getDefaultAsOf', () => {
  afterEach(() => setConfig(undefined));

  it('returns the injected ISO date when config.js sets a valid defaultAsOf', () => {
    // Self-hosted image: entrypoint injects DEFAULT_AS_OF=2025-01-15 so a fresh
    // user's pricing forms anchor on the bundle's demo-data date.
    setConfig({ orchestratorUrl: '', devAuthBypass: true, defaultAsOf: '2025-01-15' });
    expect(getDefaultAsOf()).toBe('2025-01-15');
  });

  it('null when defaultAsOf is null (self-hosted with no DEFAULT_AS_OF set)', () => {
    setConfig({ orchestratorUrl: '', devAuthBypass: true, defaultAsOf: null });
    expect(getDefaultAsOf()).toBeNull();
  });

  it('null when the key is absent entirely (dev / public hosted)', () => {
    setConfig({});
    expect(getDefaultAsOf()).toBeNull();
    setConfig(undefined);
    expect(getDefaultAsOf()).toBeNull();
  });

  it('null for a malformed defaultAsOf — a garbage date never reaches the forms', () => {
    setConfig({ defaultAsOf: 'not-a-date' });
    expect(getDefaultAsOf()).toBeNull();
    setConfig({ defaultAsOf: '2025-1-5' });
    expect(getDefaultAsOf()).toBeNull();
    setConfig({ defaultAsOf: '' });
    expect(getDefaultAsOf()).toBeNull();
  });
});
