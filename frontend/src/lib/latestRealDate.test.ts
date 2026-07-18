import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';

// Hoisted mock: the single orchestrator GET the module issues.
const { orchestratorGetMock } = vi.hoisted(() => ({ orchestratorGetMock: vi.fn() }));
vi.mock('./api/orchestrator', () => ({ orchestratorGet: orchestratorGetMock }));

import { fetchLatestRealDate, resolveDefaultAsOf, GBP_OIS_PREFIX } from './latestRealDate';

type Cfg = { defaultAsOf?: string | null };
function setConfig(cfg: Cfg | undefined): void {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: Cfg };
  if (cfg === undefined) delete g.__QUANTRA_CONFIG__;
  else g.__QUANTRA_CONFIG__ = cfg;
}
function today(): string {
  return new Date().toISOString().split('T')[0];
}

beforeEach(() => setConfig(undefined));
afterEach(() => {
  vi.clearAllMocks();
  setConfig(undefined);
});

describe('latestRealDate.fetchLatestRealDate', () => {
  it('queries the BoE SONIA OIS prefix and returns the latest real date', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: '2026-07-14' } });
    const date = await fetchLatestRealDate();
    expect(date).toBe('2026-07-14');
    // Wiring: it hits the latest-date endpoint with the real GBP OIS prefix.
    const path = orchestratorGetMock.mock.calls[0][0] as string;
    expect(path).toContain('/v1/market-data/latest-date');
    expect(path).toContain(encodeURIComponent(GBP_OIS_PREFIX));
  });

  it('returns null when the endpoint reports no data yet (latest_date: null)', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: null } });
    expect(await fetchLatestRealDate()).toBeNull();
  });

  it('returns null on an error/unreachable endpoint', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: false, envelope: { code: 'network_error', error: 'x' } });
    expect(await fetchLatestRealDate()).toBeNull();
  });

  it('returns null on a malformed date', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: 'nope' } });
    expect(await fetchLatestRealDate()).toBeNull();
  });
});

describe('latestRealDate.resolveDefaultAsOf', () => {
  it('defaults As-Of to the latest real date when available', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: '2026-07-14' } });
    setConfig({ defaultAsOf: '2025-01-15' });
    expect(await resolveDefaultAsOf()).toBe('2026-07-14');
  });

  it('falls back to DEFAULT_AS_OF when there is no real date (latest_date: null)', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: null } });
    setConfig({ defaultAsOf: '2025-01-15' });
    expect(await resolveDefaultAsOf()).toBe('2025-01-15');
  });

  it('falls back to today when there is neither a real date nor a DEFAULT_AS_OF', async () => {
    orchestratorGetMock.mockResolvedValue({ ok: true, data: { latest_date: null } });
    setConfig(undefined);
    expect(await resolveDefaultAsOf()).toBe(today());
  });
});
