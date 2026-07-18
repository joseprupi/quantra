import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { getAsOfDate } from './asOfDate';

// getAsOfDate() is the single shared source of truth every pricing form reads
// (via useAsOfDate) for its INITIAL As-Of. These tests pin the fresh-user
// default-selection precedence: a saved localStorage value always wins, then
// the optional self-hosted `defaultAsOf` override, then "today".

const KEY = 'quantra_as_of_date';

type Cfg = { defaultAsOf?: string | null };
function setConfig(cfg: Cfg | undefined): void {
  const g = globalThis as unknown as { __QUANTRA_CONFIG__?: Cfg };
  if (cfg === undefined) delete g.__QUANTRA_CONFIG__;
  else g.__QUANTRA_CONFIG__ = cfg;
}

function today(): string {
  return new Date().toISOString().split('T')[0];
}

describe('asOfDate.getAsOfDate default selection', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    localStorage.clear();
    setConfig(undefined);
  });

  it('fresh user (no saved value), no override => today (dev / public hosted)', () => {
    setConfig(undefined);
    expect(getAsOfDate()).toBe(today());
    setConfig({}); // self-hosted with defaultAsOf absent
    expect(getAsOfDate()).toBe(today());
  });

  it('fresh user (no saved value) + self-hosted defaultAsOf => the override', () => {
    setConfig({ defaultAsOf: '2025-01-15' });
    expect(getAsOfDate()).toBe('2025-01-15');
  });

  it('a saved value always wins over the defaultAsOf override', () => {
    localStorage.setItem(KEY, '2024-06-30');
    setConfig({ defaultAsOf: '2025-01-15' });
    expect(getAsOfDate()).toBe('2024-06-30');
  });

  it('malformed defaultAsOf falls back to today (never a garbage date)', () => {
    setConfig({ defaultAsOf: 'nope' });
    expect(getAsOfDate()).toBe(today());
  });
});
