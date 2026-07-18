import { describe, expect, it } from 'vitest';
import {
  buildInflationCurveSpec,
  ensureInflationBaseFixings,
} from './inflation-curves';
import { DEFAULT_INFLATION_CURVE, type Curve, type InflationCurveConfig } from './types';

function inflationCurve(config: InflationCurveConfig): Curve {
  return {
    id: 'hicp_zc',
    name: 'HICP ZC',
    currency: 'EUR',
    role: 'inflation',
    day_counter: 'Actual365Fixed',
    interpolator: 'Linear',
    bootstrap_trait: 'Discount',
    reference_date: '2025-01-15',
    points: [],
    inflation_curve: config,
    createdAt: '',
    updatedAt: '',
  } as Curve;
}

function withQuoteRefPoint(): InflationCurveConfig {
  return {
    ...JSON.parse(JSON.stringify(DEFAULT_INFLATION_CURVE)),
    points: [
      {
        point_type: 'ZeroCouponInflationSwapHelper',
        point: {
          tenor: { n: 5, unit: 'Years' },
          quote_id: 'EUR.HICP.5Y',
          swap_observation_lag: { n: 3, unit: 'Months' },
          calendar: 'TARGET',
          payment_convention: 'ModifiedFollowing',
          day_counter: 'Actual365Fixed',
          observation_interpolation: 'AsIndex',
        },
      },
    ],
  } as InflationCurveConfig;
}

describe('buildInflationCurveSpec — orchestrator cutover (quote_ids unresolved)', () => {
  it('keeps helper quote_id UNRESOLVED when resolveQuotes:false (invariant #8)', () => {
    const spec = buildInflationCurveSpec(
      inflationCurve(withQuoteRefPoint()),
      '2025-01-15',
      { resolveQuotes: false },
    );
    expect(spec.points).toHaveLength(1);
    const point = spec.points[0].point as { quote_id?: string; quote_value?: number };
    // quote_id survives; no client-side value substitution happened.
    expect(point.quote_id).toBe('EUR.HICP.5Y');
    expect(point.quote_value).toBeUndefined();
  });

  it('does NOT reach the client-side quote book for an unknown id (no throw)', () => {
    expect(() =>
      buildInflationCurveSpec(inflationCurve(withQuoteRefPoint()), '2025-01-15', {
        resolveQuotes: false,
      }),
    ).not.toThrow();
  });

  it('still resolves client-side by default (legacy callers) — unknown id throws', () => {
    // The default (resolveQuotes omitted) preserves the pre-cutover behaviour
    // used by the still-legacy CurveSet / saved-swap fat-shape callers.
    expect(() =>
      buildInflationCurveSpec(inflationCurve(withQuoteRefPoint()), '2025-01-15'),
    ).toThrow(/Unknown quote id/);
  });

  it('passes inline quote_value points through unchanged with resolveQuotes:false', () => {
    const spec = buildInflationCurveSpec(
      inflationCurve(DEFAULT_INFLATION_CURVE),
      '2025-01-15',
      { resolveQuotes: false },
    );
    expect(spec.points.length).toBeGreaterThan(0);
    const point = spec.points[0].point as { quote_value?: number };
    expect(point.quote_value).toBe(0.02);
  });
});

describe('ensureInflationBaseFixings', () => {
  it('synthesises a non-empty base fixing series when none is supplied', () => {
    const out = ensureInflationBaseFixings({ id: 'EUHICP' } as never, '2025-01-15');
    expect(out.fixings.length).toBeGreaterThanOrEqual(1);
    for (const fixing of out.fixings) {
      expect(typeof fixing.value).toBe('number');
      expect(fixing.value).toBeGreaterThan(0);
      // Every synthesised fixing lands on/before the reference month.
      expect(fixing.date <= '2025-01-15').toBe(true);
    }
  });

  it('leaves an existing fixings block untouched', () => {
    const existing = { fixings: [{ date: '2024-12-01', value: 123.4 }] };
    const out = ensureInflationBaseFixings(existing, '2025-01-15');
    expect(out.fixings).toEqual(existing.fixings);
  });
});
