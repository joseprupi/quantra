import { describe, it, expect } from 'vitest';
import {
  anchorPoint,
  convertValuePoints,
  makeValuePoint,
  mapServerErrorToRows,
  parsePastedTable,
  parsePillarToken,
  pillarDate,
  pinnedDfPoint,
  pointValue,
  quantityForTrait,
  quantitySpec,
  resolvedPillarIso,
  sortValuePoints,
  splitStoredValuePoints,
  stampZeroConventions,
  validateValuePoints,
} from './valueCurves';
import { ValueCurvePoint } from './types';

const REF = '2025-01-15';

function zeroPt(pillar: string, value?: number, quoteId?: string): ValueCurvePoint {
  const parsed = parsePillarToken(pillar)!;
  return makeValuePoint('zero', parsed, { value, quoteId });
}

describe('parsePillarToken', () => {
  it('parses tenors case-insensitively', () => {
    expect(parsePillarToken('6M')).toEqual({ kind: 'tenor', n: 6, unit: 'Months' });
    expect(parsePillarToken('10y')).toEqual({ kind: 'tenor', n: 10, unit: 'Years' });
    expect(parsePillarToken('2W')).toEqual({ kind: 'tenor', n: 2, unit: 'Weeks' });
    expect(parsePillarToken('30D')).toEqual({ kind: 'tenor', n: 30, unit: 'Days' });
    expect(parsePillarToken('18 m')).toEqual({ kind: 'tenor', n: 18, unit: 'Months' });
  });

  it('parses ISO dates', () => {
    expect(parsePillarToken('2026-03-01')).toEqual({ kind: 'date', iso: '2026-03-01' });
  });

  it('rejects garbage / non-positive tenors', () => {
    expect(parsePillarToken('')).toBeNull();
    expect(parsePillarToken('banana')).toBeNull();
    expect(parsePillarToken('0M')).toBeNull();
    expect(parsePillarToken('6 quarters')).toBeNull();
  });
});

describe('pillarDate / resolvedPillarIso', () => {
  it('anchors tenors at the reference date with month-end clamping', () => {
    const p = makeValuePoint('zero', { kind: 'tenor', n: 1, unit: 'Years' }, { value: 0.02 });
    expect(pillarDate(p, REF)?.toISOString().slice(0, 10)).toBe('2026-01-15');
    const eom = makeValuePoint('zero', { kind: 'tenor', n: 1, unit: 'Months' }, { value: 0.02 });
    expect(pillarDate(eom, '2025-01-31')?.toISOString().slice(0, 10)).toBe('2025-02-28');
  });

  it('prefers the explicit date', () => {
    const p = makeValuePoint('zero', { kind: 'date', iso: '2027-06-30' }, { value: 0.02 });
    expect(pillarDate(p, REF)?.toISOString().slice(0, 10)).toBe('2027-06-30');
  });

  it('resolvedPillarIso returns the ISO date or null for an unset maturity', () => {
    expect(resolvedPillarIso(zeroPt('1Y', 0.02), REF)).toBe('2026-01-15');
    expect(resolvedPillarIso({ point_type: 'ZeroRatePoint', point: {} }, REF)).toBeNull();
  });
});

describe('anchorPoint / splitStoredValuePoints', () => {
  it('builds the DF anchor as the mandatory 1.0 at the reference date', () => {
    expect(anchorPoint('df', REF)).toEqual(pinnedDfPoint(REF));
  });

  it('builds the zero / fwd anchor from the entered short-end value', () => {
    expect(anchorPoint('zero', REF, 0.021).point).toMatchObject({ date: REF, zero_rate: 0.021 });
    expect(pointValue(anchorPoint('fwd', REF))).toBeUndefined();
  });

  it('splits a stored curve into anchor value + editable rows', () => {
    const stored = [zeroPt(REF, 0.02), zeroPt('6M', 0.021), zeroPt('1Y', 0.022)];
    const { anchorValue, rows } = splitStoredValuePoints(stored, REF);
    expect(anchorValue).toBe(0.02);
    expect(rows).toHaveLength(2);
    expect(rows[0].point.tenor_number).toBe(6);
  });

  it('keeps a non-anchor first point as an editable row', () => {
    const stored = [zeroPt('6M', 0.021), zeroPt('1Y', 0.022)];
    const { anchorValue, rows } = splitStoredValuePoints(stored, REF);
    expect(anchorValue).toBeUndefined();
    expect(rows).toHaveLength(2);
  });
});

describe('parsePastedTable', () => {
  it('parses tab/comma/space separated maturity-value lines (percent for zeros), sorted, no anchor row', () => {
    const { rows, anchorValue, errors } = parsePastedTable('6M\t2.0\n1Y, 2.25\n10Y 3.10', 'zero', REF);
    expect(errors).toEqual([]);
    expect(anchorValue).toBeUndefined();
    expect(rows).toHaveLength(3);
    expect(rows[0].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 });
    expect(rows[2].point).toMatchObject({ tenor_number: 10, tenor_time_unit: 'Years', zero_rate: 0.031 });
  });

  it('sorts parsed rows by resolved date', () => {
    const { rows } = parsePastedTable('10Y 3.1\n6M 2.0', 'zero', REF);
    expect(rows[0].point.tenor_number).toBe(6);
  });

  it('a reference-date line feeds the pinned anchor value (zero / fwd)', () => {
    const { rows, anchorValue } = parsePastedTable(`${REF} 2.0\n10Y 3.1`, 'zero', REF);
    expect(anchorValue).toBe(0.02);
    expect(rows).toHaveLength(1);
    expect(rows[0].point.tenor_number).toBe(10);
  });

  it('DF: keeps values raw and ignores a reference-date line with a note', () => {
    const plain = parsePastedTable('1Y 0.97\n5Y 0.85', 'df', REF);
    expect(plain.errors).toEqual([]);
    expect(plain.anchorNote).toBeUndefined();
    expect(plain.rows).toHaveLength(2);
    expect(plain.rows[0].point).toMatchObject({ discount_factor: 0.97 });

    const withRef = parsePastedTable(`${REF} 1.0\n1Y 0.97`, 'df', REF);
    expect(withRef.rows).toHaveLength(1);
    expect(withRef.anchorValue).toBeUndefined();
    expect(withRef.anchorNote).toContain('ignored');
  });

  it('accepts ISO-date maturities and reports bad lines without dropping good ones', () => {
    const { rows, errors } = parsePastedTable('2026-01-15 2.0\nnot-a-maturity 3\n2030-01-15 2.9', 'zero', REF);
    expect(rows).toHaveLength(2);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain('Line 2');
  });
});

describe('validateValuePoints', () => {
  it('accepts a sorted inline zero curve anchored at the reference date', () => {
    const v = validateValuePoints(
      [zeroPt(REF, 0.02), zeroPt('6M', 0.02), zeroPt('1Y', 0.022), zeroPt('5Y', 0.03)],
      'zero',
      REF,
    );
    expect(v.ok).toBe(true);
  });

  it('flags a zero curve whose first point is not at the reference date', () => {
    const v = validateValuePoints([zeroPt('6M', 0.02), zeroPt('1Y', 0.022)], 'zero', REF);
    expect(v.rowErrors.get(0)).toMatch(/anchored at its first point/);
  });

  it('flags out-of-order maturities as a safety net', () => {
    const v = validateValuePoints([zeroPt(REF, 0.02), zeroPt('5Y', 0.03), zeroPt('1Y', 0.022)], 'zero', REF);
    expect(v.ok).toBe(false);
    expect(v.rowErrors.get(2)).toMatch(/increasing date order/);
  });

  it('flags duplicate resolved dates with a dedicated message', () => {
    const v = validateValuePoints([zeroPt(REF, 0.02), zeroPt('1Y', 0.02), zeroPt('1Y', 0.022)], 'zero', REF);
    expect(v.rowErrors.get(2)).toMatch(/Duplicate maturity/);
  });

  it('flags a row duplicating the reference-date anchor', () => {
    const v = validateValuePoints([zeroPt(REF, 0.02), zeroPt('2025-01-15', 0.022)], 'zero', REF);
    expect(v.rowErrors.get(1)).toMatch(/Duplicate maturity/);
  });

  it('requires exactly one of value | quote', () => {
    const both = zeroPt('1Y', 0.02, 'EUR.ZERO.1Y');
    const neither = zeroPt('2Y');
    const v = validateValuePoints([zeroPt(REF, 0.02), both, neither], 'zero', REF);
    expect(v.rowErrors.get(1)).toMatch(/not both/);
    expect(v.rowErrors.get(2)).toMatch(/Value required/);
  });

  it('prompts for the short-end value on an empty zero / fwd anchor', () => {
    const v = validateValuePoints([anchorPoint('zero', REF), zeroPt('1Y', 0.022)], 'zero', REF);
    expect(v.rowErrors.get(0)).toMatch(/value at the reference date/);
  });

  it('flags an unset maturity', () => {
    const v = validateValuePoints(
      [zeroPt(REF, 0.02), { point_type: 'ZeroRatePoint', point: { zero_rate: 0.02 } }],
      'zero',
      REF,
    );
    expect(v.rowErrors.get(1)).toMatch(/Maturity required/);
  });

  it('quote-referenced rows skip the numeric checks', () => {
    const v = validateValuePoints(
      [zeroPt(REF, 0.02), zeroPt('1Y', undefined, 'EUR.ZERO.1Y'), zeroPt('5Y', 0.03)],
      'zero',
      REF,
    );
    expect(v.ok).toBe(true);
  });

  it('enforces DF (0,1] and the pinned first row at the reference date', () => {
    const bad = validateValuePoints(
      [pinnedDfPoint(REF), makeValuePoint('df', { kind: 'tenor', n: 5, unit: 'Years' }, { value: 1.2 })],
      'df',
      REF,
    );
    expect(bad.rowErrors.get(1)).toMatch(/\(0, 1\]/);

    const notPinned = validateValuePoints(
      [makeValuePoint('df', { kind: 'date', iso: REF }, { value: 0.98 })],
      'df',
      REF,
    );
    expect(notPinned.rowErrors.get(0)).toMatch(/exactly 1.0/);

    const wrongDate = validateValuePoints(
      [makeValuePoint('df', { kind: 'date', iso: '2025-02-15' }, { value: 1.0 })],
      'df',
      REF,
    );
    expect(wrongDate.rowErrors.get(0)).toMatch(/reference date/);
  });

  it('rejects an empty table', () => {
    const v = validateValuePoints([], 'zero', REF);
    expect(v.rowErrors.get(-1)).toBeTruthy();
  });
});

describe('sortValuePoints / conversions', () => {
  it('sorts mixed tenor + date maturities chronologically', () => {
    const pts = [zeroPt('10Y', 0.03), zeroPt('2026-01-01', 0.021), zeroPt('6M', 0.02)];
    const sorted = sortValuePoints(pts, REF);
    expect(sorted.map(p => p.point.tenor_number ?? p.point.date)).toEqual([6, '2026-01-01', 10]);
  });

  it('zero -> fwd keeps maturities, values and quote refs', () => {
    const pts = [zeroPt('6M', 0.02), zeroPt('1Y', undefined, 'EUR.Z.1Y')];
    const out = convertValuePoints(pts, 'zero', 'fwd');
    expect(out[0]).toMatchObject({ point_type: 'ForwardRatePoint', point: { forward_rate: 0.02 } });
    expect(out[1].point.quote_id).toBe('EUR.Z.1Y');
  });

  it('zero -> df drops inline values (unit change), keeps maturities', () => {
    const out = convertValuePoints([zeroPt('1Y', 0.02)], 'zero', 'df');
    expect(out).toHaveLength(1);
    expect(out[0].point_type).toBe('DiscountFactorPoint');
    expect(out[0].point).toMatchObject({ tenor_number: 1, tenor_time_unit: 'Years' });
    expect(pointValue(out[0])).toBeUndefined();
  });

  it('df -> zero re-types rows with values cleared (unit change)', () => {
    const pts = [makeValuePoint('df', { kind: 'tenor', n: 1, unit: 'Years' }, { value: 0.97 })];
    const out = convertValuePoints(pts, 'df', 'zero');
    expect(out).toHaveLength(1);
    expect(out[0].point_type).toBe('ZeroRatePoint');
    expect(pointValue(out[0])).toBeUndefined();
  });
});

describe('stampZeroConventions', () => {
  it('stamps compounding + frequency on every zero point only', () => {
    const out = stampZeroConventions([zeroPt('1Y', 0.02)], 'Continuous', 'Annual');
    expect(out[0].point).toMatchObject({ compounding: 'Continuous', frequency: 'Annual' });
  });
});

describe('mapServerErrorToRows', () => {
  const pts = [zeroPt('6M', 0.02), zeroPt('1Y', undefined, 'EUR.NOPE.1Y')];

  it('maps structured details by point_index', () => {
    const m = mapServerErrorToRows('Curve translation failed: bad', [{ rule: 'pillars_not_sorted', point_index: 1 }], pts);
    expect(m.get(1)).toBeTruthy();
  });

  it('falls back to prose "point N" parsing', () => {
    const m = mapServerErrorToRows('Curve translation failed: ZeroRatePoint point 0 of curve x is broken', null, pts);
    expect(m.get(0)).toBeTruthy();
  });

  it('maps unresolved quote ids onto their rows (both prose variants)', () => {
    const m = mapServerErrorToRows('Unresolved quote id(s): EUR.NOPE.1Y', null, pts);
    expect(m.get(1)).toMatch(/could not be resolved/);
    const m2 = mapServerErrorToRows(
      'Could not resolve 1 quote id(s) at 2026-07-24: EUR.NOPE.1Y',
      null,
      pts,
    );
    expect(m2.get(1)).toMatch(/could not be resolved/);
  });
});

describe('quantity model', () => {
  it('maps quantities to traits and back', () => {
    expect(quantitySpec('zero').trait).toBe('InterpolatedZero');
    expect(quantitySpec('df').trait).toBe('InterpolatedDiscount');
    expect(quantitySpec('fwd').trait).toBe('InterpolatedFwd');
    expect(quantityForTrait('InterpolatedFwd')?.quantity).toBe('fwd');
    expect(quantityForTrait('Discount')).toBeUndefined();
  });
});
