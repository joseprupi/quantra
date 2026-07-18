import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { getMdBackendSettings } from './marketDataBackend';

// These tests pin the base-URL PRECEDENCE that the self-hosted bundle depends
// on: a runtime-injected `window.__QUANTRA_CONFIG__.marketDataUrl` (the shipped
// `/config.js`, default same-origin `/_md`) MUST win over a stale localStorage
// `quantra_md_backend_url`. Browsers that used the OLD same-origin portal at
// app.quantra.io carry a stale cross-origin value (https://market.quantra.io)
// that is now dead → CORS; the runtime same-origin value must override it and
// the stale key must be healed.

const MD_ENABLED_KEY = 'quantra_md_backend_enabled';
const MD_URL_KEY = 'quantra_md_backend_url';

type Cfg = { orchestratorUrl?: string; marketDataUrl?: string; devAuthBypass?: boolean };

function setConfig(cfg: Cfg | undefined): void {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: Cfg };
  if (cfg === undefined) delete g.__QUANTRA_CONFIG__;
  else g.__QUANTRA_CONFIG__ = cfg;
}

describe('getMdBackendSettings base-URL precedence', () => {
  beforeEach(() => {
    localStorage.clear();
    setConfig(undefined);
  });
  afterEach(() => {
    localStorage.clear();
    setConfig(undefined);
  });

  it('(a) runtime marketDataUrl wins over a STALE cross-origin localStorage value, and heals the key', () => {
    // Simulate a browser that used the OLD portal: stale absolute cloud URL.
    localStorage.setItem(MD_URL_KEY, 'https://market.quantra.io');
    // The shipped self-hosted bundle injects the same-origin prefix.
    setConfig({ orchestratorUrl: '', marketDataUrl: '/_md', devAuthBypass: true });

    const settings = getMdBackendSettings();

    // Runtime value wins — NOT the stale cross-origin one.
    expect(settings.baseUrl).toBe('/_md');
    // Stale key is healed to the runtime-derived same-origin value.
    expect(localStorage.getItem(MD_URL_KEY)).toBe('/_md');
    expect(localStorage.getItem(MD_URL_KEY)).not.toContain('market.quantra.io');
  });

  it('(b) NO runtime marketDataUrl (hosted/dev): a localStorage override still wins (back-compat)', () => {
    // No runtime config key present.
    setConfig({});
    localStorage.setItem(MD_URL_KEY, 'https://md.override.example.com');

    const settings = getMdBackendSettings();

    expect(settings.baseUrl).toBe('https://md.override.example.com');
    // The override is preserved (not clobbered).
    expect(localStorage.getItem(MD_URL_KEY)).toBe('https://md.override.example.com');
  });

  it('(b2) absent __QUANTRA_CONFIG__ entirely: localStorage override still wins', () => {
    setConfig(undefined);
    localStorage.setItem(MD_URL_KEY, 'https://md.override.example.com');

    expect(getMdBackendSettings().baseUrl).toBe('https://md.override.example.com');
  });

  it('(c) fresh (no localStorage) + runtime `/_md` => `/_md` and the key is seeded', () => {
    setConfig({ orchestratorUrl: '', marketDataUrl: '/_md', devAuthBypass: true });

    const settings = getMdBackendSettings();

    expect(settings.baseUrl).toBe('/_md');
    expect(localStorage.getItem(MD_URL_KEY)).toBe('/_md');
    // Enabled self-heals to true when unset.
    expect(settings.enabled).toBe(true);
    expect(localStorage.getItem(MD_ENABLED_KEY)).toBe('true');
  });

  it('runtime value already matching localStorage: no change, still same-origin', () => {
    localStorage.setItem(MD_URL_KEY, '/_md');
    setConfig({ marketDataUrl: '/_md' });

    expect(getMdBackendSettings().baseUrl).toBe('/_md');
    expect(localStorage.getItem(MD_URL_KEY)).toBe('/_md');
  });

  it('runtime enabled flag: a stored disabled flag is respected regardless of URL healing', () => {
    localStorage.setItem(MD_ENABLED_KEY, 'false');
    localStorage.setItem(MD_URL_KEY, 'https://market.quantra.io');
    setConfig({ marketDataUrl: '/_md' });

    const settings = getMdBackendSettings();
    expect(settings.enabled).toBe(false);
    // URL is still healed even when disabled.
    expect(settings.baseUrl).toBe('/_md');
    expect(localStorage.getItem(MD_URL_KEY)).toBe('/_md');
  });
});
