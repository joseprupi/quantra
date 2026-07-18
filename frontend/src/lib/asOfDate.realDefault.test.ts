import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';

// Hoisted mock: the real-data probe asOfDate calls.
const { fetchLatestRealDateMock } = vi.hoisted(() => ({ fetchLatestRealDateMock: vi.fn() }));
vi.mock('./latestRealDate', () => ({ fetchLatestRealDate: fetchLatestRealDateMock }));

import {
  applyRealDataAsOfDefault,
  getAsOfDate,
  setAsOfDate,
  userHasChosenAsOf,
} from './asOfDate';

type Cfg = { defaultAsOf?: string | null };
function setConfig(cfg: Cfg | undefined): void {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: Cfg };
  if (cfg === undefined) delete g.__QUANTRA_CONFIG__;
  else g.__QUANTRA_CONFIG__ = cfg;
}
function today(): string {
  return new Date().toISOString().split('T')[0];
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  setConfig(undefined);
});

describe('asOfDate.applyRealDataAsOfDefault', () => {
  it('defaults the As-Of to the latest real date for a fresh user', async () => {
    setConfig({ defaultAsOf: '2025-01-15' }); // synthetic default present
    fetchLatestRealDateMock.mockResolvedValue('2026-07-14');
    const applied = await applyRealDataAsOfDefault();
    expect(applied).toBe('2026-07-14');
    expect(getAsOfDate()).toBe('2026-07-14');
    // An auto default is NOT treated as a user choice, so it can still roll.
    expect(userHasChosenAsOf()).toBe(false);
  });

  it('falls back to DEFAULT_AS_OF when there is no real date (null)', async () => {
    setConfig({ defaultAsOf: '2025-01-15' });
    fetchLatestRealDateMock.mockResolvedValue(null);
    const applied = await applyRealDataAsOfDefault();
    expect(applied).toBeNull();
    // No override applied → the existing DEFAULT_AS_OF default stands.
    expect(getAsOfDate()).toBe('2025-01-15');
  });

  it('falls back to today when there is neither a real date nor a DEFAULT_AS_OF', async () => {
    setConfig(undefined);
    fetchLatestRealDateMock.mockResolvedValue(null);
    await applyRealDataAsOfDefault();
    expect(getAsOfDate()).toBe(today());
  });

  it('never overrides an explicit user choice', async () => {
    setAsOfDate('2024-06-30'); // user picked
    expect(userHasChosenAsOf()).toBe(true);
    fetchLatestRealDateMock.mockResolvedValue('2026-07-14');
    const applied = await applyRealDataAsOfDefault();
    expect(applied).toBeNull();
    expect(getAsOfDate()).toBe('2024-06-30');
    // The probe should not even be consulted once the user has chosen.
    expect(fetchLatestRealDateMock).not.toHaveBeenCalled();
  });

  it('rolls a previously auto-applied date forward to a newer real date', async () => {
    fetchLatestRealDateMock.mockResolvedValueOnce('2026-07-14');
    await applyRealDataAsOfDefault();
    expect(getAsOfDate()).toBe('2026-07-14');
    // Next session: real data rolled to a newer date; still auto ⇒ update.
    fetchLatestRealDateMock.mockResolvedValueOnce('2026-07-15');
    await applyRealDataAsOfDefault();
    expect(getAsOfDate()).toBe('2026-07-15');
    expect(userHasChosenAsOf()).toBe(false);
    // ...but if the user then picks it, it sticks and stops rolling.
    setAsOfDate('2026-07-15');
    expect(userHasChosenAsOf()).toBe(true);
  });
});
