import { describe, expect, it } from 'vitest';
import { normalizePricingForApi, normalizeQuantraRequestForApi } from './api-normalizers';

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
