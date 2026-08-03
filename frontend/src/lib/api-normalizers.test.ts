import { describe, expect, it } from 'vitest';
import { normalizeCurveForApi, normalizeCurvePointForApi, normalizePricingForApi, normalizeQuantraRequestForApi } from './api-normalizers';

describe('normalizePricingForApi', () => {
  it('moves legacy pricing fields into domain groups', () => {
    const normalized = normalizePricingForApi({
      as_of_date: '2026-03-05',
      settlement_date: '2026-03-07',
      quotes: [{ id: 'Q1', value: 0.01 }],
      indices: [{ id: 'USD_3M' }],
      swap_indices: [{ id: 'USD_SWAP_3M' }],
      curves: [{ id: 'discount' }],
      coupon_pricers: [{ id: 'iborpricer' }],
      credit_curves: [{ id: 'credit' }],
      vol_surfaces: [{ id: 'swaption_vol' }],
      models: [{ id: 'hw' }],
      equity_underlyings: [{ id: 'AAPL' }],
      inflation_indices: [{ id: 'UK_RPI' }],
      inflation_curves: [{ id: 'UK_RPI_ZC' }],
      bond_pricing_details: true,
      bond_pricing_flows: false,
      swaption_pricing_details: true,
      swaption_pricing_rebump: true,
    });

    expect(normalized).toEqual({
      as_of_date: '2026-03-05',
      settlement_date: '2026-03-07',
      quotes: [{ id: 'Q1', value: 0.01 }],
      rates: {
        indices: [{ id: 'USD_3M', tenor: { n: 0, unit: 'Days' } }],
        swap_indices: [{ id: 'USD_SWAP_3M' }],
        curves: [{ id: 'discount', points: undefined }],
        coupon_pricers: [{ id: 'iborpricer' }],
      },
      credit: {
        credit_curves: [{ id: 'credit' }],
      },
      volatility: {
        vol_surfaces: [{ id: 'swaption_vol' }],
        models: [{ id: 'hw' }],
      },
      equity: {
        equity_underlyings: [{ id: 'AAPL' }],
      },
      inflation: {
        inflation_indices: [{ id: 'UK_RPI' }],
        inflation_curves: [{ id: 'UK_RPI_ZC' }],
      },
      options: {
        bond_pricing_details: true,
        bond_pricing_flows: false,
        swaption_pricing_details: true,
        swaption_pricing_rebump: true,
      },
    });
  });

  it('preserves explicit nested values and only backfills missing fields', () => {
    const normalized = normalizePricingForApi({
      as_of_date: '2026-03-05',
      rates: {
        indices: [{ id: 'NESTED_INDEX' }],
        curves: [{ id: 'NESTED_CURVE' }],
      },
      indices: [{ id: 'LEGACY_INDEX' }],
      curves: [{ id: 'LEGACY_CURVE' }],
      swap_indices: [{ id: 'LEGACY_SWAP_INDEX' }],
      options: {
        bond_pricing_details: false,
      },
      bond_pricing_details: true,
      swaption_pricing_details: true,
    });

    expect(normalized).toEqual({
      as_of_date: '2026-03-05',
      rates: {
        indices: [{ id: 'NESTED_INDEX', tenor: { n: 0, unit: 'Days' } }],
        curves: [{ id: 'NESTED_CURVE', points: undefined }],
        swap_indices: [{ id: 'LEGACY_SWAP_INDEX' }],
      },
      options: {
        bond_pricing_details: false,
        swaption_pricing_details: true,
      },
    });
  });
});

describe('normalizeQuantraRequestForApi', () => {
  it('normalizes tenor fields while migrating pricing payloads', () => {
    const normalized = normalizeQuantraRequestForApi({
      pricing: {
        as_of_date: '2026-03-05',
        indices: [
          {
            id: 'USD_3M',
            tenor_number: 3,
            tenor_time_unit: 'Months',
          },
        ],
        curves: [
          {
            id: 'discount',
            points: [
              {
                point_type: 'DepositHelper',
                point: {
                  tenor_number: 6,
                  tenor_time_unit: 'Months',
                },
              },
            ],
          },
        ],
      },
      bonds: [{ fixed_rate_bond: { issue_date: '2026-03-05' } }],
    });

    expect(normalized).toEqual({
      pricing: {
        as_of_date: '2026-03-05',
        rates: {
          indices: [
            {
              id: 'USD_3M',
              tenor: { n: 3, unit: 'Months' },
            },
          ],
          curves: [
            {
              id: 'discount',
              points: [
                {
                  point_type: 'DepositHelper',
                  point: {
                    tenor: { n: 6, unit: 'Months' },
                  },
                },
              ],
            },
          ],
        },
      },
      bonds: [{ fixed_rate_bond: { issue_date: '2026-03-05' } }],
    });
  });
});

describe('normalizeCurvePointForApi — engine-0.6 OIS overnight params', () => {
  it('round-trips all five overnight params on an OISHelper point unmodified', () => {
    const point = {
      point_type: 'OISHelper',
      point: {
        rate: 0.0533,
        tenor_number: 5,
        tenor_time_unit: 'Years',
        overnight_index: { id: 'SOFR' },
        settlement_days: 2,
        calendar: 'UnitedStatesGovernmentBond',
        fixed_leg_frequency: 'Annual',
        fixed_leg_convention: 'ModifiedFollowing',
        fixed_leg_day_counter: 'Actual360',
        payment_lag: 2,
        averaging_method: 'Compound',
        lookback_days: 5,
        lockout_days: 2,
        apply_observation_shift: true,
      },
    };

    const normalized = normalizeCurvePointForApi(point);

    // Tenor is nested; every other key must survive verbatim.
    expect(normalized).toEqual({
      point_type: 'OISHelper',
      point: {
        rate: 0.0533,
        tenor: { n: 5, unit: 'Years' },
        overnight_index: { id: 'SOFR' },
        settlement_days: 2,
        calendar: 'UnitedStatesGovernmentBond',
        fixed_leg_frequency: 'Annual',
        fixed_leg_convention: 'ModifiedFollowing',
        fixed_leg_day_counter: 'Actual360',
        payment_lag: 2,
        averaging_method: 'Compound',
        lookback_days: 5,
        lockout_days: 2,
        apply_observation_shift: true,
      },
    });
    // Ints stay numbers, never strings.
    expect(typeof normalized.point.payment_lag).toBe('number');
    expect(typeof normalized.point.lookback_days).toBe('number');
    expect(typeof normalized.point.lockout_days).toBe('number');
  });

  it('round-trips a DatedOISHelper point incl. fixed_leg_frequency and the five params', () => {
    const point = {
      point_type: 'DatedOISHelper',
      point: {
        rate: 0.031,
        start_date: '2025-01-15',
        end_date: '2026-01-15',
        overnight_index: { id: 'ESTR' },
        settlement_days: 2,
        calendar: 'TARGET',
        fixed_leg_frequency: 'Quarterly',
        fixed_leg_convention: 'ModifiedFollowing',
        fixed_leg_day_counter: 'Actual360',
        payment_lag: 1,
        averaging_method: 'Simple',
        lookback_days: 0,
        lockout_days: 0,
        apply_observation_shift: false,
      },
    };

    // DatedOIS has no tenor — the point must pass through byte-identical.
    expect(normalizeCurvePointForApi(point)).toEqual(point);
  });

  it('keeps zero/false legacy defaults on the wire (0 is not dropped)', () => {
    const normalized = normalizeCurvePointForApi({
      point_type: 'OISHelper',
      point: {
        rate: 0.035,
        tenor_number: 1,
        tenor_time_unit: 'Years',
        payment_lag: 0,
        averaging_method: 'Compound',
        lookback_days: 0,
        lockout_days: 0,
        apply_observation_shift: false,
      },
    });
    expect(normalized.point.payment_lag).toBe(0);
    expect(normalized.point.lookback_days).toBe(0);
    expect(normalized.point.lockout_days).toBe(0);
    expect(normalized.point.apply_observation_shift).toBe(false);
    expect(normalized.point.averaging_method).toBe('Compound');
  });
});

describe('normalizeCurveForApi — OIS overnight params on a full curve', () => {
  it('preserves the params across the curve-level normalization used by save/preview', () => {
    const curve = normalizeCurveForApi({
      id: 'sofr-discount',
      day_counter: 'Actual365Fixed',
      reference_date: '2025-01-15',
      points: [
        {
          point_type: 'OISHelper',
          point: {
            rate: 0.0295,
            tenor_number: 50,
            tenor_time_unit: 'Years',
            overnight_index: { id: 'SOFR' },
            payment_lag: 2,
            averaging_method: 'Compound',
            lookback_days: 0,
            lockout_days: 0,
            apply_observation_shift: false,
          },
        },
      ],
    });
    expect(curve.points[0].point).toMatchObject({
      payment_lag: 2,
      averaging_method: 'Compound',
      lookback_days: 0,
      lockout_days: 0,
      apply_observation_shift: false,
      tenor: { n: 50, unit: 'Years' },
    });
  });
});
