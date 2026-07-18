import { describe, it, expect } from 'vitest';
import { frequencyFromIndexTenor, frequencyFromIndexDef } from './frequencyFromTenor';

// the floating leg's payment-schedule frequency defaults from the
// picked index tenor (3M index -> quarterly coupons). Mirrors the backend's
// table so portal default and engine derivation agree.
describe('frequencyFromIndexTenor', () => {
  it('maps the clean month tenors onto their coupon frequency', () => {
    expect(frequencyFromIndexTenor(1, 'Months')).toBe('Monthly');
    expect(frequencyFromIndexTenor(2, 'Months')).toBe('Bimonthly');
    expect(frequencyFromIndexTenor(3, 'Months')).toBe('Quarterly');
    expect(frequencyFromIndexTenor(4, 'Months')).toBe('EveryFourthMonth');
    expect(frequencyFromIndexTenor(6, 'Months')).toBe('Semiannual');
    expect(frequencyFromIndexTenor(12, 'Months')).toBe('Annual');
  });

  it('normalises year tenors through the month table (1Y -> Annual)', () => {
    expect(frequencyFromIndexTenor(1, 'Years')).toBe('Annual');
    expect(frequencyFromIndexTenor(2, 'Years')).toBeNull();
  });

  it('maps week and day tenors', () => {
    expect(frequencyFromIndexTenor(1, 'Weeks')).toBe('Weekly');
    expect(frequencyFromIndexTenor(2, 'Weeks')).toBe('Biweekly');
    expect(frequencyFromIndexTenor(4, 'Weeks')).toBe('EveryFourthWeek');
    expect(frequencyFromIndexTenor(1, 'Days')).toBe('Daily');
    expect(frequencyFromIndexTenor(3, 'Days')).toBeNull();
  });

  it('returns null for unclean tenors so the caller keeps its current default', () => {
    expect(frequencyFromIndexTenor(5, 'Months')).toBeNull(); // no integer annual frequency
    expect(frequencyFromIndexTenor(3, 'Weeks')).toBeNull();
    expect(frequencyFromIndexTenor(0, 'Months')).toBeNull();
    expect(frequencyFromIndexTenor(-3, 'Months')).toBeNull();
    expect(frequencyFromIndexTenor(1.5, 'Months')).toBeNull();
    expect(frequencyFromIndexTenor(undefined, 'Months')).toBeNull();
    expect(frequencyFromIndexTenor(3, undefined)).toBeNull();
    expect(frequencyFromIndexTenor(3, 'Fortnights')).toBeNull();
  });
});

describe('frequencyFromIndexDef', () => {
  it('reads the canonical tenor {n, unit} shape', () => {
    expect(frequencyFromIndexDef({ tenor: { n: 3, unit: 'Months' } })).toBe('Quarterly');
    expect(frequencyFromIndexDef({ tenor: { n: 6, unit: 'Months' } })).toBe('Semiannual');
  });

  it('falls back to the legacy flat tenor_number/tenor_time_unit pair', () => {
    expect(frequencyFromIndexDef({ tenor_number: 3, tenor_time_unit: 'Months' })).toBe('Quarterly');
    expect(frequencyFromIndexDef({ tenor_number: 1, tenor_time_unit: 'Years' })).toBe('Annual');
  });

  it('prefers the canonical shape when both are present', () => {
    expect(
      frequencyFromIndexDef({
        tenor: { n: 3, unit: 'Months' },
        tenor_number: 6,
        tenor_time_unit: 'Months',
      }),
    ).toBe('Quarterly');
  });

  it('returns null for a missing def or tenor', () => {
    expect(frequencyFromIndexDef(null)).toBeNull();
    expect(frequencyFromIndexDef(undefined)).toBeNull();
    expect(frequencyFromIndexDef({})).toBeNull();
  });
});
