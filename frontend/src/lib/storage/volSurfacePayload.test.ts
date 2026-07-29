import { describe, expect, it } from 'vitest';
import type { MatrixCell } from '../types/matrix2d';
import { flattenMatrixForWire } from '../types/matrix2d';
import type { VolSurfaceSpec } from './volSurfaces';
import {
  buildSwaptionVolWirePayload,
  checkMatrixShape,
  getSurfaceAxes,
  toQuoteMatrix2D,
  VolSurfacePayloadError,
  yearsToPeriod,
} from './volSurfacePayload';

const AS_OF = '2026-05-10';
const SWAP_INDEX = 'EUR_SWAP_6M';
const noQuotes = (_: string) => null;

function baseSurface(overrides: Partial<VolSurfaceSpec> = {}): VolSurfaceSpec {
  return {
    id: 'surf_test',
    payload_type: 'SwaptionVolSpec',
    base: {
      reference_date: AS_OF,
      calendar: 'TARGET',
      business_day_convention: 'ModifiedFollowing',
      day_counter: 'Actual365Fixed',
      volatility_type: 'Normal',
      shape: 'Constant',
      constant_vol: 0.01,
    },
    ...overrides,
  };
}

describe('toQuoteMatrix2D', () => {
  it('flattens a 2x3 number grid row-major with defaults', () => {
    expect(toQuoteMatrix2D([[1, 2, 3], [4, 5, 6]], 2, 3)).toEqual({
      n_rows: 2,
      n_cols: 3,
      values: [1, 2, 3, 4, 5, 6],
    });
  });

  it('pads missing cells with 0 (matches pre-change `?? 0` fallback)', () => {
    expect(toQuoteMatrix2D([[1]], 2, 2)).toEqual({
      n_rows: 2,
      n_cols: 2,
      values: [1, 0, 0, 0],
    });
  });
});

describe('yearsToPeriod (quantra_Period.n must be int per OpenAPI contract)', () => {
  it('returns Years for whole-year values', () => {
    expect(yearsToPeriod(1)).toEqual({ n: 1, unit: 'Years' });
    expect(yearsToPeriod(5)).toEqual({ n: 5, unit: 'Years' });
    expect(yearsToPeriod(30)).toEqual({ n: 30, unit: 'Years' });
  });

  it('promotes fractional years to integer Months where possible', () => {
    expect(yearsToPeriod(1 / 12)).toEqual({ n: 1, unit: 'Months' });
    expect(yearsToPeriod(2 / 12)).toEqual({ n: 2, unit: 'Months' });
    expect(yearsToPeriod(0.25)).toEqual({ n: 3, unit: 'Months' });
    expect(yearsToPeriod(0.5)).toEqual({ n: 6, unit: 'Months' });
    expect(yearsToPeriod(0.75)).toEqual({ n: 9, unit: 'Months' });
    expect(yearsToPeriod(1.5)).toEqual({ n: 18, unit: 'Months' });
  });

  it('handles the truncated `0.0833333333` (1 month) literal from the user-reported payload', () => {
    expect(yearsToPeriod(0.0833333333)).toEqual({ n: 1, unit: 'Months' });
    expect(yearsToPeriod(0.1666666667)).toEqual({ n: 2, unit: 'Months' });
  });

  it('falls back to Weeks for sub-monthly fractions where weeks divide cleanly', () => {
    expect(yearsToPeriod(1 / 52)).toEqual({ n: 1, unit: 'Weeks' });
    expect(yearsToPeriod(2 / 52)).toEqual({ n: 2, unit: 'Weeks' });
  });

  it('falls back to Days for sub-weekly fractions', () => {
    expect(yearsToPeriod(1 / 365)).toEqual({ n: 1, unit: 'Days' });
    // 100 days — not a clean Years/Months/Weeks value at 1e-6 tolerance,
    // so the Days branch wins.
    expect(yearsToPeriod(100 / 365)).toEqual({ n: 100, unit: 'Days' });
  });

  it('always emits an integer `n`', () => {
    for (const y of [0.0833333333, 0.166, 0.25, 0.333, 0.5, 1.5, 1 / 365, 0.789]) {
      const p = yearsToPeriod(y);
      expect(Number.isInteger(p.n)).toBe(true);
      expect(p.n).toBeGreaterThanOrEqual(1);
    }
  });

  it('defensively handles non-positive / non-finite inputs', () => {
    expect(yearsToPeriod(0)).toEqual({ n: 1, unit: 'Days' });
    expect(yearsToPeriod(-1)).toEqual({ n: 1, unit: 'Days' });
    expect(yearsToPeriod(NaN)).toEqual({ n: 1, unit: 'Days' });
  });
});

describe('getSurfaceAxes', () => {
  it('prefers axes_expiries / axes_tenors when present', () => {
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }],
      expiries: [10, 20], // ignored when axes_expiries is set
      tenors: [99],
    });
    expect(getSurfaceAxes(surface)).toEqual({
      expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      tenors: [{ n: 5, unit: 'Years' }],
    });
  });

  it('falls back to converting legacy expiries:number[] (years) to Period[]', () => {
    const surface = baseSurface({ expiries: [1, 5], tenors: [10] });
    expect(getSurfaceAxes(surface)).toEqual({
      expiries: [
        { n: 1, unit: 'Years' },
        { n: 5, unit: 'Years' },
      ],
      tenors: [{ n: 10, unit: 'Years' }],
    });
  });

  it('converts fractional legacy years to integer-n Months (regression: backend Period.n:int)', () => {
    // Reproduces the user-reported wire payload: a surface persisted with
    // fractional years like 1/12, 2/12, 0.25, 0.5, 0.75, 1.5 would emit
    // `{n: 0.0833333333, unit: 'Years'}` and the backend rejected the
    // payload because quantra_Period.n must be an int.
    const surface = baseSurface({
      expiries: [1 / 12, 2 / 12, 0.25, 0.5, 0.75, 1, 1.5],
      tenors: [1, 5],
    });
    const axes = getSurfaceAxes(surface);
    for (const p of axes.expiries) expect(Number.isInteger(p.n)).toBe(true);
    expect(axes.expiries).toEqual([
      { n: 1, unit: 'Months' },
      { n: 2, unit: 'Months' },
      { n: 3, unit: 'Months' },
      { n: 6, unit: 'Months' },
      { n: 9, unit: 'Months' },
      { n: 1, unit: 'Years' },
      { n: 18, unit: 'Months' },
    ]);
  });

  it('defensively coerces axes_expiries/axes_tenors with fractional n to integer Periods', () => {
    // Surfaces imported from old JSON snapshots could carry fractional `n`
    // on the new Period axes. getSurfaceAxes rounds them before emitting
    // to the wire so the backend never sees `n: 0.083...`.
    const surface = baseSurface({
      // 0.25 Years should promote to 3 Months (preserves resolution); 1.5
      // is just `Math.round` to keep it stable since the unit is fixed.
      axes_expiries: [
        { n: 0.0833333333, unit: 'Years' },
        { n: 0.25, unit: 'Years' },
      ],
      axes_tenors: [
        { n: 1.5, unit: 'Months' },
        { n: 5, unit: 'Years' },
      ],
    });
    const axes = getSurfaceAxes(surface);
    for (const p of [...axes.expiries, ...axes.tenors]) {
      expect(Number.isInteger(p.n)).toBe(true);
    }
    expect(axes.expiries).toEqual([
      { n: 1, unit: 'Months' },
      { n: 3, unit: 'Months' },
    ]);
    expect(axes.tenors[0]).toEqual({ n: 2, unit: 'Months' });
    expect(axes.tenors[1]).toEqual({ n: 5, unit: 'Years' });
  });

  it('throws when both new and legacy axes are empty', () => {
    expect(() => getSurfaceAxes(baseSurface())).toThrow(VolSurfacePayloadError);
    expect(() => getSurfaceAxes(baseSurface({ axes_expiries: [], axes_tenors: [] }))).toThrow(
      /no expiries/i,
    );
  });

  it('reports tenors when expiries are present but tenors are empty', () => {
    const surface = baseSurface({ axes_expiries: [{ n: 1, unit: 'Years' }] });
    expect(() => getSurfaceAxes(surface)).toThrow(/no tenors/i);
  });
});

describe('checkMatrixShape', () => {
  it('returns null for a well-shaped grid', () => {
    expect(checkMatrixShape([[1, 2], [3, 4]], 2, 2, 'g')).toBeNull();
  });

  it('flags missing grid', () => {
    expect(checkMatrixShape(undefined, 2, 2, 'alpha')).toBe('alpha (missing)');
    expect(checkMatrixShape([], 2, 2, 'alpha')).toBe('alpha (missing)');
  });

  it('flags wrong dimensions', () => {
    expect(checkMatrixShape([[1, 2]], 2, 2, 'beta')).toBe('beta (1×2, expected 2×2)');
    expect(checkMatrixShape([[1], [2]], 2, 2, 'rho')).toBe('rho (2×1, expected 2×2)');
  });

  it('flags ragged rows', () => {
    expect(checkMatrixShape([[1, 2], [3]], 2, 2, 'nu')).toBe(
      'nu (ragged row 1 of length 1, expected 2)',
    );
  });
});

describe('buildSwaptionVolWirePayload — Constant', () => {
  it('emits the SwaptionVolConstantSpec envelope with defaults filled', () => {
    const surface = baseSurface();
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    expect(wire).toEqual({
      id: 'surf_test',
      payload_type: 'SwaptionVolSpec',
      payload: {
        swap_index_id: SWAP_INDEX,
        payload_type: 'SwaptionVolConstantSpec',
        payload: {
          base: {
            reference_date: AS_OF,
            calendar: 'TARGET',
            business_day_convention: 'ModifiedFollowing',
            day_counter: 'Actual365Fixed',
            constant_vol: 0.01,
            volatility_type: 'Normal',
            shape: 'Constant',
          },
        },
      },
    });
  });

  it('falls back to as-of date when surface base is missing reference_date', () => {
    const surface = baseSurface({ base: { shape: 'Constant', constant_vol: 0.02 } });
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, '2030-01-01', noQuotes);
    const inner = wire.payload as unknown as {
      payload: { base: { reference_date: string; constant_vol: number } };
    };
    expect(inner.payload.base.reference_date).toBe('2030-01-01');
    expect(inner.payload.base.constant_vol).toBe(0.02);
  });
});

describe('buildSwaptionVolWirePayload — AtmMatrix2D', () => {
  // Load-bearing regression: hand-rolled fixture matching the pre-refactor
  // inline wire payload from VolWorkbench.buildRequest. This is the test the
  // plan calls out as the proof that Step 1 fixed the axis-sourcing bug
  // without changing the wire shape for the unaffected case.
  it('matches the pre-refactor inline AtmMatrix2D wire payload byte-for-byte', () => {
    const grid: MatrixCell[][] = [
      [0.0125, 0.013, 0.014],
      [0.0135, 0.014, 0.015],
    ];
    const surface = baseSurface({
      id: 'eur_atm_normal',
      axes_expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }, { n: 20, unit: 'Years' }],
      grid,
      base: {
        reference_date: AS_OF,
        calendar: 'TARGET',
        business_day_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
        volatility_type: 'Normal',
        shape: 'AtmMatrix2D',
        constant_vol: 0.01,
      },
    });

    // Reproduce the prior inline wire payload exactly (with the same toQuoteMatrix2D
    // and flattenMatrixForWire helpers and the same `{...base, shape}` spread order).
    const baseInline = {
      reference_date: AS_OF,
      calendar: 'TARGET',
      business_day_convention: 'ModifiedFollowing',
      day_counter: 'Actual365Fixed',
      constant_vol: 0.01,
      volatility_type: 'Normal' as const,
    };
    const nE = 2;
    const nT = 3;
    const inlineExpected = {
      id: 'eur_atm_normal',
      payload_type: 'SwaptionVolSpec' as const,
      payload: {
        swap_index_id: SWAP_INDEX,
        payload_type: 'SwaptionVolAtmMatrixSpec' as const,
        payload: {
          base: { ...baseInline, shape: 'AtmMatrix2D' as const },
          expiries: [
            { n: 1, unit: 'Years' as const },
            { n: 2, unit: 'Years' as const },
          ],
          tenors: [
            { n: 5, unit: 'Years' as const },
            { n: 10, unit: 'Years' as const },
            { n: 20, unit: 'Years' as const },
          ],
          vols: toQuoteMatrix2D(
            flattenMatrixForWire(grid, nE, nT, noQuotes),
            nE,
            nT,
          ),
        },
      },
    };

    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);

    // Deep-equal first (best error messages), then JSON byte-equality (catches
    // property-insertion-order regressions that deepEqual misses).
    expect(wire).toEqual(inlineExpected);
    expect(JSON.stringify(wire)).toBe(JSON.stringify(inlineExpected));
  });

  it('preserves displacement before shape in the base envelope', () => {
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }],
      grid: [[0.01]],
      base: {
        reference_date: AS_OF,
        calendar: 'TARGET',
        business_day_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
        volatility_type: 'ShiftedLognormal',
        displacement: 0.03,
        shape: 'AtmMatrix2D',
        constant_vol: 0.01,
      },
    });
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    const keys = Object.keys((wire.payload as { payload: { base: object } }).payload.base);
    expect(keys.indexOf('displacement')).toBeLessThan(keys.indexOf('shape'));
    expect(keys.indexOf('volatility_type')).toBeLessThan(keys.indexOf('displacement'));
  });

  it('resolves quote cells through the supplied callback', () => {
    const grid: MatrixCell[][] = [
      [{ quoteId: 'Q_A' }, 0.012],
      [0.013, { quoteId: 'Q_B' }],
    ];
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }],
      grid,
      base: { shape: 'AtmMatrix2D' },
    });
    const resolver = (id: string) => (id === 'Q_A' ? 0.011 : id === 'Q_B' ? 0.014 : null);
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, resolver);
    const inner = wire.payload as { payload: { vols: { values: number[] } } };
    expect(inner.payload.vols.values).toEqual([0.011, 0.012, 0.013, 0.014]);
  });

  it('decisions-log fix: surface owns its axes; sampling grid is independent', () => {
    // The pre-refactor branch sourced expiries/tenors from the sampling-grid
    // React state, so two different sampling grids produced two different
    // wire payloads for the same saved surface. The helper takes only the
    // surface (no sampling input) — there's literally no surrogate parameter
    // to vary, which is the structural fix.
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }],
      grid: [[0.01, 0.02], [0.03, 0.04]],
      base: { shape: 'AtmMatrix2D' },
    });
    const a = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    const b = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
    const inner = a.payload as { payload: { expiries: unknown; tenors: unknown } };
    expect(inner.payload.expiries).toEqual([
      { n: 1, unit: 'Years' },
      { n: 2, unit: 'Years' },
    ]);
    expect(inner.payload.tenors).toEqual([
      { n: 5, unit: 'Years' },
      { n: 10, unit: 'Years' },
    ]);
  });

  it('throws a clear error on empty axes (no more silent zero-pad)', () => {
    const surface = baseSurface({
      grid: [[0.01]],
      base: { shape: 'AtmMatrix2D' },
    });
    expect(() => buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes)).toThrow(
      /no expiries/i,
    );
  });

  it('throws on grid dimension mismatch (no more silent truncation)', () => {
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }],
      grid: [[0.01, 0.02, 0.03]], // wrong shape: 1×3, expected 2×2
      base: { shape: 'AtmMatrix2D' },
    });
    expect(() => buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes)).toThrow(
      /AtmMatrix dimension mismatch/i,
    );
  });

  it('legacy data with expiries:number[] (years) wires correctly', () => {
    const surface = baseSurface({
      expiries: [1, 2],
      tenors: [5, 10],
      grid: [[0.01, 0.02], [0.03, 0.04]],
      base: { shape: 'AtmMatrix2D' },
    });
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    const inner = wire.payload as { payload: { expiries: unknown; tenors: unknown } };
    expect(inner.payload.expiries).toEqual([
      { n: 1, unit: 'Years' },
      { n: 2, unit: 'Years' },
    ]);
    expect(inner.payload.tenors).toEqual([
      { n: 5, unit: 'Years' },
      { n: 10, unit: 'Years' },
    ]);
  });
});

describe('buildSwaptionVolWirePayload — SmileCube3D', () => {
  it('masks SmileCube3D as AtmMatrix2D on the wire (existing pre-refactor behaviour)', () => {
    const surface = baseSurface({
      axes_expiries: [{ n: 1, unit: 'Years' }],
      axes_tenors: [{ n: 5, unit: 'Years' }],
      grid: [[0.01]],
      base: { shape: 'SmileCube3D' },
    });
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    const inner = wire.payload as { payload_type: string };
    expect(inner.payload_type).toBe('SwaptionVolAtmMatrixSpec');
  });
});

describe('buildSwaptionVolWirePayload — SabrParams', () => {
  const goodAxes = {
    axes_expiries: [{ n: 1, unit: 'Years' as const }, { n: 2, unit: 'Years' as const }],
    axes_tenors: [{ n: 5, unit: 'Years' as const }, { n: 10, unit: 'Years' as const }],
  };
  const goodGrids = {
    sabr_alpha: [[0.02, 0.025], [0.022, 0.027]] as MatrixCell[][],
    sabr_beta: [[0.5, 0.5], [0.5, 0.5]] as MatrixCell[][],
    sabr_rho: [[-0.3, -0.2], [-0.25, -0.15]] as MatrixCell[][],
    sabr_nu: [[0.4, 0.45], [0.42, 0.47]] as MatrixCell[][],
  };

  function sabrSurface(overrides: Partial<VolSurfaceSpec> = {}): VolSurfaceSpec {
    return baseSurface({
      id: 'sabr_test',
      ...goodAxes,
      ...goodGrids,
      base: { shape: 'SabrParams', volatility_type: 'Lognormal' },
      ...overrides,
    });
  }

  it('emits SwaptionSabrParamsSpec with axes and four parameter matrices', () => {
    const wire = buildSwaptionVolWirePayload(sabrSurface(), SWAP_INDEX, AS_OF, noQuotes);
    expect(wire.id).toBe('sabr_test');
    expect(wire.payload_type).toBe('SwaptionVolSpec');
    const inner = wire.payload as {
      swap_index_id: string;
      payload_type: string;
      payload: {
        base: { shape: string; volatility_type: string };
        expiries: unknown;
        tenors: unknown;
        alpha: { n_rows: number; n_cols: number; values: number[] };
        beta: { n_rows: number; n_cols: number; values: number[] };
        rho: { n_rows: number; n_cols: number; values: number[] };
        nu: { n_rows: number; n_cols: number; values: number[] };
      };
    };
    expect(inner.swap_index_id).toBe(SWAP_INDEX);
    expect(inner.payload_type).toBe('SwaptionSabrParamsSpec');
    expect(inner.payload.base.shape).toBe('SabrParams');
    expect(inner.payload.base.volatility_type).toBe('Lognormal');
    expect(inner.payload.expiries).toEqual(goodAxes.axes_expiries);
    expect(inner.payload.tenors).toEqual(goodAxes.axes_tenors);
    expect(inner.payload.alpha.values).toEqual([0.02, 0.025, 0.022, 0.027]);
    expect(inner.payload.beta.values).toEqual([0.5, 0.5, 0.5, 0.5]);
    expect(inner.payload.rho.values).toEqual([-0.3, -0.2, -0.25, -0.15]);
    expect(inner.payload.nu.values).toEqual([0.4, 0.45, 0.42, 0.47]);
    expect(inner.payload.alpha.n_rows).toBe(2);
    expect(inner.payload.alpha.n_cols).toBe(2);
  });

  it('resolves quote cells in any of the four matrices', () => {
    const resolver = (id: string) => (id === 'Q_ALPHA' ? 0.099 : id === 'Q_RHO' ? -0.5 : null);
    const wire = buildSwaptionVolWirePayload(
      sabrSurface({
        sabr_alpha: [[{ quoteId: 'Q_ALPHA' }, 0.025], [0.022, 0.027]] as MatrixCell[][],
        sabr_rho: [[-0.3, -0.2], [{ quoteId: 'Q_RHO' }, -0.15]] as MatrixCell[][],
      }),
      SWAP_INDEX,
      AS_OF,
      resolver,
    );
    const inner = wire.payload as {
      payload: {
        alpha: { values: number[] };
        rho: { values: number[] };
      };
    };
    expect(inner.payload.alpha.values).toEqual([0.099, 0.025, 0.022, 0.027]);
    expect(inner.payload.rho.values).toEqual([-0.3, -0.2, -0.5, -0.15]);
  });

  it('per-matrix error reporting: lists every offending matrix, not a generic message', () => {
    const surface = sabrSurface({
      sabr_alpha: undefined, // missing
      sabr_beta: [[0.5]] as MatrixCell[][], // 1×1, expected 2×2
      sabr_rho: [[-0.3, -0.2]] as MatrixCell[][], // 1×2, expected 2×2
      // sabr_nu correct
    });
    let caught: Error | null = null;
    try {
      buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    } catch (e) {
      caught = e as Error;
    }
    expect(caught).toBeInstanceOf(VolSurfacePayloadError);
    const msg = caught?.message || '';
    expect(msg).toContain('SabrParams dimension mismatch');
    expect(msg).toContain('alpha (missing)');
    expect(msg).toContain('beta (1×1, expected 2×2)');
    expect(msg).toContain('rho (1×2, expected 2×2)');
    expect(msg).not.toContain('nu (');
  });

  it('rejects NaN literals with a row/col-localised error', () => {
    const surface = sabrSurface({
      sabr_alpha: [[0.02, NaN], [0.022, 0.027]] as MatrixCell[][],
    });
    expect(() => buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes)).toThrow(
      /alpha.*row 0, col 1/i,
    );
  });

  it('throws on empty axes even when the four grids are present', () => {
    const surface = sabrSurface({ axes_expiries: [], axes_tenors: [] });
    expect(() => buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes)).toThrow(
      /no expiries/i,
    );
  });

  // Backend contract: parser/vol_surface_parsers.cpp:1141 rejects Normal SABR
  // explicitly because SabrSmileSection is parameterised in lognormal terms.
  // Catch it here so users get a clear client-side error rather than the
  // 200 + per-query "Normal SABR is intentionally not supported" pattern.
  it('throws when volatility_type is Normal (SABR v1 is lognormal-only)', () => {
    const surface = sabrSurface({
      base: { shape: 'SabrParams', volatility_type: 'Normal' },
    });
    expect(() => buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes)).toThrow(
      /Lognormal or ShiftedLognormal/i,
    );
  });

  it('accepts ShiftedLognormal volatility_type', () => {
    const surface = sabrSurface({
      base: { shape: 'SabrParams', volatility_type: 'ShiftedLognormal', displacement: 0.01 },
    });
    const wire = buildSwaptionVolWirePayload(surface, SWAP_INDEX, AS_OF, noQuotes);
    expect(wire.payload.payload_type).toBe('SwaptionSabrParamsSpec');
  });
});

describe('buildSwaptionVolWirePayload — SabrCalibrate', () => {
  const goodAxes = {
    axes_expiries: [{ n: 1, unit: 'Years' as const }, { n: 2, unit: 'Years' as const }],
    axes_tenors: [{ n: 5, unit: 'Years' as const }, { n: 10, unit: 'Years' as const }],
    axes_strikes: [-0.01, -0.005, 0, 0.005, 0.01],
  };
  // 3D cube: [expiry][tenor][strike], all positive lognormal vols.
  const goodCube: MatrixCell[][][] = [
    [
      [0.30, 0.27, 0.25, 0.27, 0.30],
      [0.28, 0.26, 0.24, 0.26, 0.28],
    ],
    [
      [0.29, 0.26, 0.24, 0.26, 0.29],
      [0.27, 0.25, 0.23, 0.25, 0.27],
    ],
  ];

  function calibrateSurface(overrides: Partial<VolSurfaceSpec> = {}): VolSurfaceSpec {
    return baseSurface({
      id: 'sabr_cal_test',
      ...goodAxes,
      sabr_market_vols: goodCube,
      sabr_calibration_options: { beta_fixed: true, beta_value: 0.5 },
      base: { shape: 'SabrCalibrate', volatility_type: 'Lognormal' },
      ...overrides,
    });
  }

  it('emits SwaptionSabrCalibrateSpec with axes, strikes, vols cube and options', () => {
    const wire = buildSwaptionVolWirePayload(calibrateSurface(), SWAP_INDEX, AS_OF, noQuotes);
    expect(wire.id).toBe('sabr_cal_test');
    expect(wire.payload_type).toBe('SwaptionVolSpec');
    const inner = wire.payload as {
      swap_index_id: string;
      payload_type: string;
      payload: {
        base: { shape: string; volatility_type: string };
        expiries: unknown;
        tenors: unknown;
        strikes: number[];
        vols: { n_1: number; n_2: number; n_3: number; values: number[] };
        beta_fixed: boolean;
        beta_value?: number;
        vega_weighted_smile_fit?: boolean;
      };
    };
    expect(inner.swap_index_id).toBe(SWAP_INDEX);
    expect(inner.payload_type).toBe('SwaptionSabrCalibrateSpec');
    expect(inner.payload.base.shape).toBe('SabrCalibrate');
    expect(inner.payload.base.volatility_type).toBe('Lognormal');
    expect(inner.payload.expiries).toEqual(goodAxes.axes_expiries);
    expect(inner.payload.tenors).toEqual(goodAxes.axes_tenors);
    expect(inner.payload.strikes).toEqual(goodAxes.axes_strikes);
    expect(inner.payload.vols.n_1).toBe(2);
    expect(inner.payload.vols.n_2).toBe(2);
    expect(inner.payload.vols.n_3).toBe(5);
    expect(inner.payload.vols.values.length).toBe(20);
    // Row-major: cube[0][0][0..4] then cube[0][1][0..4] then cube[1][0]...
    expect(inner.payload.vols.values.slice(0, 5)).toEqual([0.30, 0.27, 0.25, 0.27, 0.30]);
    expect(inner.payload.vols.values.slice(5, 10)).toEqual([0.28, 0.26, 0.24, 0.26, 0.28]);
    expect(inner.payload.beta_fixed).toBe(true);
    expect(inner.payload.beta_value).toBe(0.5);
    expect(inner.payload.vega_weighted_smile_fit).toBeUndefined();
  });

  it('passes vega_weighted_smile_fit through when set', () => {
    const wire = buildSwaptionVolWirePayload(
      calibrateSurface({
        sabr_calibration_options: { beta_fixed: false, vega_weighted_smile_fit: true },
      }),
      SWAP_INDEX,
      AS_OF,
      noQuotes,
    );
    const inner = wire.payload as { payload: { vega_weighted_smile_fit?: boolean; beta_fixed: boolean; beta_value?: number } };
    expect(inner.payload.vega_weighted_smile_fit).toBe(true);
    expect(inner.payload.beta_fixed).toBe(false);
    // beta_value is omitted when beta_fixed=false.
    expect(inner.payload.beta_value).toBeUndefined();
  });

  it('resolves quote cells inside the market-vols cube', () => {
    const cubeWithQuote = JSON.parse(JSON.stringify(goodCube)) as MatrixCell[][][];
    cubeWithQuote[0][0][2] = { quoteId: 'Q_ATM' };
    const resolver = (id: string) => (id === 'Q_ATM' ? 0.21 : null);
    const wire = buildSwaptionVolWirePayload(
      calibrateSurface({ sabr_market_vols: cubeWithQuote }),
      SWAP_INDEX,
      AS_OF,
      resolver,
    );
    const inner = wire.payload as { payload: { vols: { values: number[] } } };
    // Index of [0][0][2] in row-major nE*nT*nS = 0*2*5 + 0*5 + 2 = 2
    expect(inner.payload.vols.values[2]).toBe(0.21);
  });

  it('throws on cube dimension mismatch (expiry × tenor × strike)', () => {
    // 1×2×5 cube vs 2×2×5 expected
    const wrongCube: MatrixCell[][][] = [goodCube[0]];
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ sabr_market_vols: wrongCube }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/SabrCalibrate dimension mismatch.*1×2×5, expected 2×2×5/i);
  });

  it('throws a clear "empty cell" error on NaN (unfilled) cube cells', () => {
    const cubeWithNaN = JSON.parse(JSON.stringify(goodCube)) as MatrixCell[][][];
    cubeWithNaN[1][0][3] = NaN;
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ sabr_market_vols: cubeWithNaN }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/empty cell at expiry 1, tenor 0, strike 3/i);
  });

  it('throws a clear "empty cell" error on null cube cells (NaN after a JSON save/reload round trip)', () => {
    // A fresh cube is seeded with NaN literals; JSON.stringify(NaN) === "null",
    // so a saved-and-reloaded unfilled surface carries null cells. These must
    // NOT silently wire as 0 (the engine would reject with "vol must be > 0").
    const cubeWithNull = JSON.parse(JSON.stringify(goodCube)) as MatrixCell[][][];
    (cubeWithNull[0][1] as unknown[])[1] = null;
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ sabr_market_vols: cubeWithNull }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/empty cell at expiry 0, tenor 1, strike 1/i);
  });

  it('throws on missing strikes axis', () => {
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ axes_strikes: [] }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/no strikes/i);
  });

  it('throws on non-strict-monotone strikes (duplicate or out-of-order)', () => {
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ axes_strikes: [-0.01, 0, 0, 0.005] }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/strictly increasing/i);
  });

  it('rejects non-positive market vols (Hagan formula yields positive σ)', () => {
    const badCube = JSON.parse(JSON.stringify(goodCube)) as MatrixCell[][][];
    badCube[1][0][2] = -0.05;
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ sabr_market_vols: badCube }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/non-positive market vol literal at expiry 1, tenor 0, strike 2/i);
  });

  it('rejects zero market vols (must be strictly > 0)', () => {
    const badCube = JSON.parse(JSON.stringify(goodCube)) as MatrixCell[][][];
    badCube[0][0][0] = 0;
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({ sabr_market_vols: badCube }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/non-positive market vol literal/i);
  });

  // Backend contract: parser/vol_surface_parsers.cpp rejects Normal SABR
  // explicitly (same rule as SabrParams). Pre-empt client-side.
  it('throws when volatility_type is Normal (SABR v1 is lognormal-only)', () => {
    expect(() =>
      buildSwaptionVolWirePayload(
        calibrateSurface({
          base: { shape: 'SabrCalibrate', volatility_type: 'Normal' },
        }),
        SWAP_INDEX,
        AS_OF,
        noQuotes,
      ),
    ).toThrow(/Lognormal or ShiftedLognormal/i);
  });

  // Backend contract: SwaptionSabrCalibrateSpec rejects OIS swap-index in v1
  // (rejected at parse time). The portal stamps `_OIS` onto OIS swap-index
  // ids (VolWorkbench.swapIndexOptions); the helper guards on that suffix.
  it('throws on OIS swap-index id (backend rejects OIS+SabrCalibrate in v1)', () => {
    expect(() =>
      buildSwaptionVolWirePayload(calibrateSurface(), 'ESTR_OIS', AS_OF, noQuotes),
    ).toThrow(/cannot calibrate against an OIS swap index/i);
  });

  it('accepts ShiftedLognormal volatility_type', () => {
    const wire = buildSwaptionVolWirePayload(
      calibrateSurface({
        base: { shape: 'SabrCalibrate', volatility_type: 'ShiftedLognormal', displacement: 0.01 },
      }),
      SWAP_INDEX,
      AS_OF,
      noQuotes,
    );
    expect(wire.payload.payload_type).toBe('SwaptionSabrCalibrateSpec');
  });
});

// Fixture test: the shipped example market-data file is the canonical "loaded
// example" users sample after clicking "Load Example". A regression in either
// the helper or the example JSON itself (typo in axes, wrong matrix shape,
// SABR-range violation) would silently break the Load-Example flow. Pin both
// here so the test fails loudly the moment either side drifts.
describe('public/example-market-data.json — SABR fixture', () => {
  // Read at test time, not module-load time, so a broken JSON parse becomes a
  // test failure rather than a vitest collection error.
  function loadExampleSabrSurface(): VolSurfaceSpec {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require('fs');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const path = require('path');
    const raw = fs.readFileSync(
      path.join(__dirname, '..', '..', '..', 'public', 'example-market-data.json'),
      'utf8',
    );
    const data = JSON.parse(raw);
    const sabr = (data.volSurfaces || []).find((v: VolSurfaceSpec) => v.id === 'swaptvol_sabr_eur');
    if (!sabr) throw new Error('swaptvol_sabr_eur fixture missing from example-market-data.json');
    return sabr;
  }

  it('has the expected SABR-params shape and 5×5 axes', () => {
    const sabr = loadExampleSabrSurface();
    expect(sabr.base?.shape).toBe('SabrParams');
    expect(sabr.axes_expiries?.length).toBe(5);
    expect(sabr.axes_tenors?.length).toBe(5);
    expect(sabr.sabr_alpha?.length).toBe(5);
    expect(sabr.sabr_alpha?.[0]?.length).toBe(5);
    expect(sabr.sabr_beta?.length).toBe(5);
    expect(sabr.sabr_rho?.length).toBe(5);
    expect(sabr.sabr_nu?.length).toBe(5);
  });

  // Regression guard: every SABR calibration node must stay inside the
  // example EUR multi-curve horizon (helpers max out at 30Y). Otherwise the
  // backend errors with "1st leg: time (X) is past max curve time (Y)"
  // during SabrSmileSection instantiation before sampling runs.
  it('every calibration node (expiry+tenor) stays under 30Y to fit the example EUR curve', () => {
    const sabr = loadExampleSabrSurface();
    const periodYears = (p: { n: number; unit: string }) => {
      if (p.unit === 'Years') return p.n;
      if (p.unit === 'Months') return p.n / 12;
      if (p.unit === 'Weeks') return p.n / 52;
      if (p.unit === 'Days') return p.n / 365;
      return Number.POSITIVE_INFINITY;
    };
    for (const e of sabr.axes_expiries || []) {
      for (const t of sabr.axes_tenors || []) {
        const total = periodYears(e) + periodYears(t);
        expect(total).toBeLessThanOrEqual(29.5);
      }
    }
  });

  // Backend contract: SabrParams rejects Normal. Make sure the shipped
  // example never ships with the unsupported vol type.
  it('uses a lognormal-family volatility_type (Normal SABR is unsupported)', () => {
    const sabr = loadExampleSabrSurface();
    expect(sabr.base?.volatility_type).not.toBe('Normal');
    expect(['Lognormal', 'ShiftedLognormal']).toContain(sabr.base?.volatility_type);
  });

  it('every Period axis has integer n (OpenAPI quantra_Period.n contract)', () => {
    const sabr = loadExampleSabrSurface();
    for (const p of sabr.axes_expiries || []) {
      expect(Number.isInteger(p.n)).toBe(true);
    }
    for (const p of sabr.axes_tenors || []) {
      expect(Number.isInteger(p.n)).toBe(true);
    }
  });

  it('SABR parameter literals satisfy α>0, β∈[0,1], ρ∈(−1,1), ν>0', () => {
    const sabr = loadExampleSabrSurface();
    const flat = (g?: MatrixCell[][]) =>
      (g || []).flat().filter((c): c is number => typeof c === 'number');
    for (const v of flat(sabr.sabr_alpha)) expect(v).toBeGreaterThan(0);
    for (const v of flat(sabr.sabr_beta)) {
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(1);
    }
    for (const v of flat(sabr.sabr_rho)) {
      expect(v).toBeGreaterThan(-1);
      expect(v).toBeLessThan(1);
    }
    for (const v of flat(sabr.sabr_nu)) expect(v).toBeGreaterThan(0);
  });

  it('builds a SwaptionSabrParamsSpec wire payload without errors', () => {
    const sabr = loadExampleSabrSurface();
    const wire = buildSwaptionVolWirePayload(sabr, SWAP_INDEX, AS_OF, noQuotes);
    expect(wire.payload_type).toBe('SwaptionVolSpec');
    expect(wire.payload.payload_type).toBe('SwaptionSabrParamsSpec');
    const payload = wire.payload as unknown as {
      payload: {
        expiries: { n: number }[];
        tenors: { n: number }[];
        alpha: { n_rows: number; n_cols: number; values: number[] };
        beta: { n_rows: number; n_cols: number; values: number[] };
        rho: { n_rows: number; n_cols: number; values: number[] };
        nu: { n_rows: number; n_cols: number; values: number[] };
      };
    };
    expect(payload.payload.expiries.length).toBe(5);
    expect(payload.payload.tenors.length).toBe(5);
    for (const m of [payload.payload.alpha, payload.payload.beta, payload.payload.rho, payload.payload.nu]) {
      expect(m.n_rows).toBe(5);
      expect(m.n_cols).toBe(5);
      expect(m.values.length).toBe(25);
    }
  });
});

describe('public/example-market-data.json — SabrCalibrate fixture', () => {
  function loadExampleCalibrateSurface(): VolSurfaceSpec {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require('fs');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const path = require('path');
    const raw = fs.readFileSync(
      path.join(__dirname, '..', '..', '..', 'public', 'example-market-data.json'),
      'utf8',
    );
    const data = JSON.parse(raw);
    const cal = (data.volSurfaces || []).find(
      (v: VolSurfaceSpec) => v.id === 'swaptvol_sabr_calibrate_eur',
    );
    if (!cal) throw new Error('swaptvol_sabr_calibrate_eur fixture missing from example-market-data.json');
    return cal;
  }

  it('has the expected SabrCalibrate shape, 5×5×5 cube, and lognormal vol type', () => {
    const cal = loadExampleCalibrateSurface();
    expect(cal.base?.shape).toBe('SabrCalibrate');
    expect(['Lognormal', 'ShiftedLognormal']).toContain(cal.base?.volatility_type);
    expect(cal.axes_expiries?.length).toBe(5);
    expect(cal.axes_tenors?.length).toBe(5);
    expect(cal.axes_strikes?.length).toBe(5);
    const cube = cal.sabr_market_vols;
    expect(cube?.length).toBe(5);
    expect(cube?.[0]?.length).toBe(5);
    expect(cube?.[0]?.[0]?.length).toBe(5);
  });

  // Same horizon guard as the SabrParams fixture: max calibration node must
  // stay inside the example EUR multi-curve horizon (~30Y) so the backend
  // doesn't trip "1st leg: time (X) is past max curve time (Y)" during
  // SabrSmileSection instantiation per node.
  it('every calibration node (expiry+tenor) stays under 30Y to fit the example EUR curve', () => {
    const cal = loadExampleCalibrateSurface();
    const periodYears = (p: { n: number; unit: string }) => {
      if (p.unit === 'Years') return p.n;
      if (p.unit === 'Months') return p.n / 12;
      if (p.unit === 'Weeks') return p.n / 52;
      if (p.unit === 'Days') return p.n / 365;
      return Number.POSITIVE_INFINITY;
    };
    for (const e of cal.axes_expiries || []) {
      for (const t of cal.axes_tenors || []) {
        expect(periodYears(e) + periodYears(t)).toBeLessThanOrEqual(29.5);
      }
    }
  });

  // axes_strikes must be strict-monotone (XabrSwaptionVolatilityCube assumes
  // a sorted strike grid; the wire helper rejects duplicates / out-of-order).
  it('axes_strikes is strictly increasing', () => {
    const cal = loadExampleCalibrateSurface();
    const strikes = cal.axes_strikes || [];
    for (let i = 1; i < strikes.length; i += 1) {
      expect(strikes[i]).toBeGreaterThan(strikes[i - 1]);
    }
  });

  // Backend rejects non-positive market vols at parse time. Pin the fixture
  // so a future "edit a cell" never accidentally ships a 0 / negative σ.
  it('every market vol is finite and strictly positive', () => {
    const cal = loadExampleCalibrateSurface();
    const cube = cal.sabr_market_vols || [];
    for (const plane of cube) {
      for (const row of plane) {
        for (const cell of row) {
          expect(typeof cell).toBe('number');
          const v = cell as number;
          expect(Number.isFinite(v)).toBe(true);
          expect(v).toBeGreaterThan(0);
        }
      }
    }
  });

  it('builds a SwaptionSabrCalibrateSpec wire payload without errors', () => {
    const cal = loadExampleCalibrateSurface();
    const wire = buildSwaptionVolWirePayload(cal, SWAP_INDEX, AS_OF, noQuotes);
    expect(wire.payload_type).toBe('SwaptionVolSpec');
    expect(wire.payload.payload_type).toBe('SwaptionSabrCalibrateSpec');
    const payload = wire.payload as unknown as {
      payload: {
        expiries: { n: number }[];
        tenors: { n: number }[];
        strikes: number[];
        vols: { n_1: number; n_2: number; n_3: number; values: number[] };
        beta_fixed: boolean;
        beta_value?: number;
        vega_weighted_smile_fit?: boolean;
      };
    };
    expect(payload.payload.expiries.length).toBe(5);
    expect(payload.payload.tenors.length).toBe(5);
    expect(payload.payload.strikes.length).toBe(5);
    expect(payload.payload.vols.n_1).toBe(5);
    expect(payload.payload.vols.n_2).toBe(5);
    expect(payload.payload.vols.n_3).toBe(5);
    expect(payload.payload.vols.values.length).toBe(125);
    // calibration options surface through unchanged
    expect(payload.payload.beta_fixed).toBe(true);
    expect(payload.payload.beta_value).toBe(0.5);
    expect(payload.payload.vega_weighted_smile_fit).toBe(true);
  });
});
