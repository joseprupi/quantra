import { describe, expect, it } from 'vitest';
import {
  backfillSurfaceAxesFromLegacy,
  bpToStrike,
  buildCubeGeometry,
  cubeToNodesMatrix,
  expiryLabelToYears,
  nodesMatrixToCube,
  pickDefaultFloatIndex,
  rateFloatIndexOptions,
  strikeToBp,
  swapIndexIdForIndex,
} from './vol-workbench-helpers';
import type { VolSurfaceSampleResult } from '../../lib/quantra-types';
import type { VolSurfaceSpec } from '../../lib/storage/volSurfaces';
import type { StoredIndexSpec } from '../../lib/storage/indices';

// Demo-shaped index registry: an inflation index (EUHICP_YY) plus real rate
// indices, INCLUDING two IBOR tenors (1M, 6M). Ordered so the inflation index
// is FIRST and the WRONG-tenor IBOR (EURIBOR_1M) precedes the coherent one —
// this reproduces both bugs: `[0]` auto-selecting inflation, and a naive
// "first IBOR" picking EURIBOR_1M (which is incoherent with EUR_SWAP_6M and
// has no forwarding curve → "Unknown index id" → empty surface).
function idx(
  id: string,
  type: StoredIndexSpec['type'],
  tenor?: { n: number; unit: string },
  currency?: string,
): StoredIndexSpec {
  return {
    id,
    type,
    currency,
    tenor_number: tenor?.n,
    tenor_time_unit: tenor?.unit,
    fixing_days: 2,
    calendar: 'TARGET',
    day_counter: 'Actual360',
    createdAt: '2025-01-01T00:00:00Z',
    updatedAt: '2025-01-01T00:00:00Z',
  };
}

// Real seeded shape: family-named ids (EURIBOR_*) carry currency="EUR", so
// the swap-index derivation must yield EUR_SWAP_6M (not EURIBOR_SWAP_6M).
const DEMO_INDICES: StoredIndexSpec[] = [
  idx('EUHICP_YY', 'Inflation', undefined, 'EUR'),
  idx('EURIBOR_1M', 'IBOR', { n: 1, unit: 'Months' }, 'EUR'),
  idx('ESTR', 'Overnight', undefined, 'EUR'),
  idx('EURIBOR_6M', 'IBOR', { n: 6, unit: 'Months' }, 'EUR'),
];

describe('rateFloatIndexOptions', () => {
  it('excludes non-rate (inflation) indices, keeps IBOR + Overnight', () => {
    const options = rateFloatIndexOptions(DEMO_INDICES);
    expect(options).not.toContain('EUHICP_YY');
    expect(options).toEqual(['EURIBOR_1M', 'ESTR', 'EURIBOR_6M']);
  });

  it('returns empty when only inflation indices exist', () => {
    expect(rateFloatIndexOptions([idx('EUHICP_YY', 'Inflation')])).toEqual([]);
  });
});

describe('swapIndexIdForIndex', () => {
  it('derives CCY_SWAP_<tenor> from the index currency, not the family-named id prefix', () => {
    // EURIBOR_6M.split('_')[0] === "EURIBOR"; using currency yields "EUR".
    expect(swapIndexIdForIndex(idx('EURIBOR_6M', 'IBOR', { n: 6, unit: 'Months' }, 'EUR'))).toBe('EUR_SWAP_6M');
    expect(swapIndexIdForIndex(idx('EURIBOR_1M', 'IBOR', { n: 1, unit: 'Months' }, 'EUR'))).toBe('EUR_SWAP_1M');
  });

  it('falls back to the id prefix when no currency is set', () => {
    expect(swapIndexIdForIndex(idx('USD_3M', 'IBOR', { n: 3, unit: 'Months' }))).toBe('USD_SWAP_3M');
  });

  it('derives the <id>_OIS id for Overnight indices', () => {
    expect(swapIndexIdForIndex(idx('ESTR', 'Overnight', undefined, 'EUR'))).toBe('ESTR_OIS');
  });
});

describe('pickDefaultFloatIndex', () => {
  it('auto-selects the IBOR COHERENT with the active swap index (EUR_SWAP_6M ⇒ EURIBOR_6M)', () => {
    const options = rateFloatIndexOptions(DEMO_INDICES);
    const picked = pickDefaultFloatIndex(DEMO_INDICES, options, 'EUR_SWAP_6M');
    expect(picked).toBe('EURIBOR_6M');
    expect(picked).not.toBe('EURIBOR_1M');
    expect(picked).not.toBe('EUHICP_YY');
  });

  it('honors a different swap-index tenor (EUR_SWAP_1M ⇒ EURIBOR_1M)', () => {
    const options = rateFloatIndexOptions(DEMO_INDICES);
    expect(pickDefaultFloatIndex(DEMO_INDICES, options, 'EUR_SWAP_1M')).toBe('EURIBOR_1M');
  });

  it('falls back to prefer-IBOR when no swap index is supplied, never inflation', () => {
    const options = rateFloatIndexOptions(DEMO_INDICES);
    const picked = pickDefaultFloatIndex(DEMO_INDICES, options);
    expect(picked).toBe('EURIBOR_1M'); // first IBOR in registry order
    expect(picked).not.toBe('EUHICP_YY');
  });

  it('falls back to prefer-IBOR when swapIndexId matches no registered IBOR', () => {
    const options = rateFloatIndexOptions(DEMO_INDICES);
    expect(pickDefaultFloatIndex(DEMO_INDICES, options, 'USD_SWAP_3M')).toBe('EURIBOR_1M');
  });

  it('falls back to Overnight when no IBOR is available', () => {
    const indices = [idx('EUHICP_YY', 'Inflation'), idx('ESTR', 'Overnight')];
    const options = rateFloatIndexOptions(indices);
    expect(pickDefaultFloatIndex(indices, options, 'EUR_SWAP_6M')).toBe('ESTR');
  });

  it('returns undefined when no rate index exists', () => {
    const indices = [idx('EUHICP_YY', 'Inflation')];
    expect(pickDefaultFloatIndex(indices, rateFloatIndexOptions(indices))).toBeUndefined();
  });
});

describe('expiryLabelToYears', () => {
  it('parses ISO date using asOfDate year fraction', () => {
    const y = expiryLabelToYears('2026-03-16', '2026-02-15');
    expect(y).toBeGreaterThan(0.07);
    expect(y).toBeLessThan(0.09);
  });

  it('parses period tenor labels', () => {
    expect(expiryLabelToYears('6M', '2026-02-15')).toBeCloseTo(0.5, 10);
  });
});

describe('backfillSurfaceAxesFromLegacy', () => {
  // Mirrors the shape of the `swaptvol_dense` fixture in
  // public/example-market-data.json: SmileCube3D with legacy
  // `expiries`/`tenors: number[]` (years) including the fractional 1/12
  // (1 month) and no `axes_*` arrays.
  const legacySmileCube: VolSurfaceSpec = {
    id: 'swaptvol_dense_clone',
    payload_type: 'SwaptionVolSpec',
    base: { shape: 'SmileCube3D', volatility_type: 'Normal', constant_vol: 0.0072 },
    expiries: [1 / 12, 0.25, 1, 5],
    tenors: [1, 5, 10],
    grid: [],
  };

  it('seeds axes_expiries and axes_tenors from legacy years arrays for SmileCube', () => {
    const seeded = backfillSurfaceAxesFromLegacy(legacySmileCube, 'SmileCube');
    expect(seeded).not.toBeNull();
    expect(seeded!.axes_expiries).toEqual([
      // 1/12 year → 1 month, integer-`n` per quantra_Period.n contract
      { n: 1, unit: 'Months' },
      { n: 3, unit: 'Months' },
      { n: 1, unit: 'Years' },
      { n: 5, unit: 'Years' },
    ]);
    expect(seeded!.axes_tenors).toEqual([
      { n: 1, unit: 'Years' },
      { n: 5, unit: 'Years' },
      { n: 10, unit: 'Years' },
    ]);
  });

  it('returns null when the surface already has axes_* populated (idempotent)', () => {
    const alreadyMigrated: VolSurfaceSpec = {
      ...legacySmileCube,
      axes_expiries: [{ n: 1, unit: 'Months' }],
      axes_tenors: [{ n: 5, unit: 'Years' }],
    };
    expect(backfillSurfaceAxesFromLegacy(alreadyMigrated, 'SmileCube')).toBeNull();
  });

  it('seeds only the missing axis when one is already populated', () => {
    const partial: VolSurfaceSpec = {
      ...legacySmileCube,
      axes_expiries: [{ n: 6, unit: 'Months' }],
    };
    const seeded = backfillSurfaceAxesFromLegacy(partial, 'AtmMatrix');
    expect(seeded).not.toBeNull();
    expect(seeded!.axes_expiries).toEqual([{ n: 6, unit: 'Months' }]);
    expect(seeded!.axes_tenors).toEqual([
      { n: 1, unit: 'Years' },
      { n: 5, unit: 'Years' },
      { n: 10, unit: 'Years' },
    ]);
  });

  it('returns null when neither axes nor legacy fields are populated', () => {
    const empty: VolSurfaceSpec = {
      id: 'empty',
      payload_type: 'SwaptionVolSpec',
      base: { shape: 'AtmMatrix2D' },
    };
    expect(backfillSurfaceAxesFromLegacy(empty, 'AtmMatrix')).toBeNull();
  });

  it('also backfills axes for SabrCalibrate surfaces (Step 4 editor mount)', () => {
    // Mirrors the path opened by Step 4: a SabrCalibrate surface whose
    // user-supplied legacy axes are years-as-numbers should re-hydrate as
    // integer-`n` Periods so the cube editor can lay out (expiries × tenors
    // × strikes) correctly without forcing the user to retype axes.
    const legacyCalibrate: VolSurfaceSpec = {
      id: 'sabr_cal_legacy',
      payload_type: 'SwaptionVolSpec',
      base: { shape: 'SabrCalibrate', volatility_type: 'Lognormal' },
      expiries: [1 / 12, 1, 5],
      tenors: [5, 10],
    };
    const seeded = backfillSurfaceAxesFromLegacy(legacyCalibrate, 'SabrCalibrate');
    expect(seeded).not.toBeNull();
    expect(seeded!.axes_expiries).toEqual([
      { n: 1, unit: 'Months' },
      { n: 1, unit: 'Years' },
      { n: 5, unit: 'Years' },
    ]);
    expect(seeded!.axes_tenors).toEqual([
      { n: 5, unit: 'Years' },
      { n: 10, unit: 'Years' },
    ]);
  });
});

describe('buildCubeGeometry', () => {
  it('builds x/y/z arrays with expected dimensions', () => {
    const sample: VolSurfaceSampleResult = {
      vol_id: 'test',
      expiries: ['1M', '6M'],
      tenors: [{ n: 1, unit: 'Years' }, { n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }],
      strikes: [-0.01, 0.0],
      n_expiries: 2,
      n_tenors: 3,
      n_strikes: 2,
      vols: [
        0.01, 0.011, 0.012, 0.013, 0.014, 0.015,
        0.016, 0.017, 0.018, 0.019, 0.02, 0.021,
      ],
    };
    const g = buildCubeGeometry(sample, 1, 'decimal', '2026-02-15');
    expect(g.ok).toBe(true);
    if (!g.ok) return;
    expect(g.x.length).toBe(3);
    expect(g.y.length).toBe(2);
    expect(g.z.length).toBe(2);
    expect(g.z[0].length).toBe(3);
  });
});

describe('cubeToNodesMatrix / nodesMatrixToCube', () => {
  const cube = [
    [
      [0.30, 0.25, 0.28],
      [0.29, 0.24, 0.27],
    ],
    [
      [0.28, 0.23, 0.26],
      [0.27, 0.22, 0.25],
    ],
  ];

  it('projects the cube row-major: row = expiry * nT + tenor, col = strike', () => {
    const m = cubeToNodesMatrix(cube, 2, 2, 3);
    expect(m).toHaveLength(4);
    expect(m[0]).toEqual([0.30, 0.25, 0.28]); // (e0, t0)
    expect(m[1]).toEqual([0.29, 0.24, 0.27]); // (e0, t1)
    expect(m[2]).toEqual([0.28, 0.23, 0.26]); // (e1, t0)
    expect(m[3]).toEqual([0.27, 0.22, 0.25]); // (e1, t1)
  });

  it('coerces null cells (JSON-round-tripped NaN) to NaN literals', () => {
    const withNull = JSON.parse(JSON.stringify(cube)) as unknown[][][];
    withNull[1][0][2] = null;
    const m = cubeToNodesMatrix(withNull as never, 2, 2, 3);
    expect(Number.isNaN(m[2][2])).toBe(true);
    // Quote cells survive the projection.
    withNull[0][0][0] = { quoteId: 'Q1' };
    const m2 = cubeToNodesMatrix(withNull as never, 2, 2, 3);
    expect(m2[0][0]).toEqual({ quoteId: 'Q1' });
  });

  it('round-trips through nodesMatrixToCube', () => {
    const m = cubeToNodesMatrix(cube, 2, 2, 3);
    const back = nodesMatrixToCube(m, 2, 2, 3);
    expect(back).toEqual(cube);
  });

  it('fills missing rows/cells with NaN when rebuilding the cube', () => {
    const back = nodesMatrixToCube([[0.3]], 2, 2, 3);
    expect(back[0][0][0]).toBe(0.3);
    expect(Number.isNaN(back[0][0][1] as number)).toBe(true);
    expect(Number.isNaN(back[1][1][2] as number)).toBe(true);
  });
});

describe('strikeToBp / bpToStrike', () => {
  it('converts rate decimals to display basis points without float noise', () => {
    expect(strikeToBp(-0.02)).toBe(-200);
    expect(strikeToBp(0.005)).toBe(50);
    expect(strikeToBp(0)).toBe(0);
  });
  it('converts basis points back to rate decimals', () => {
    expect(bpToStrike(-200)).toBe(-0.02);
    expect(bpToStrike(100)).toBe(0.01);
  });
});
