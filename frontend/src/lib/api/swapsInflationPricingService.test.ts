import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  orchPriceSwapsInflationMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    orchPriceSwapsInflationMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({ priceSwapsInflation: orchPriceSwapsInflationMock }));

import {
  buildSwapsInflationPriceBody,
  mapSwapsInflationOrchResult,
  priceSwapsInflation,
} from './swapsInflationPricingService';

// 9-row unit matrix

describe('swapsInflationPricingService — 9-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Row 1: orch 200 → success=true; wrapped shape; top-level + extras
  it('orch 200: success=true; swaps[0] carries top-level + extras flow arrays', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: -39671.61,
          fair_rate: 0.0300,
          fair_spread: null,
          fixed_leg_bps: 12.5,
          fixed_leg_npv: -1234.5,
          inflation_leg_npv: 1234.5,
          yoy_leg_bps: null,
          yoy_leg_npv: null,
          swap_kind: 'zero_coupon',
          extras: { fixed_leg_flows: [{ amount: 100 }], inflation_leg_flows: [{ amount: 200 }] },
        },
      },
      duration_ms: 18,
    });

    const result = await priceSwapsInflation({ swap: 'data' }, '2025-01-15');

    expect(orchPriceSwapsInflationMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.swaps[0].npv).toBe(-39671.61);
    expect(result.data?.swaps[0].fair_rate).toBe(0.0300);
    expect(result.data?.swaps[0].fixed_leg_npv).toBe(-1234.5);
    expect(result.data?.swaps[0].inflation_leg_npv).toBe(1234.5);
    expect(result.data?.swaps[0].swap_kind).toBe('zero_coupon');
    expect(Array.isArray(result.data?.swaps[0].fixed_leg_flows)).toBe(true);
    expect(Array.isArray(result.data?.swaps[0].inflation_leg_flows)).toBe(true);
    expect(result.duration_ms).toBe(18);
  });

  // Row 2: swap_inflation_not_found → not_found category (code wins)
  it('swap_inflation_not_found: not_found category (code wins over httpStatus)', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Inflation swap not found.', code: 'swap_inflation_not_found' },
      httpStatus: 500,
      duration_ms: 5,
    });

    const result = await priceSwapsInflation({}, '2025-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 3: swap_inflation_index_not_found → not_found
  it('swap_inflation_index_not_found: not_found category', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Inflation index not found.', code: 'swap_inflation_index_not_found' },
      httpStatus: 404,
      duration_ms: 5,
    });

    const result = await priceSwapsInflation({}, '2025-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 4: 502 engine_unavailable → unavailable category (no-swallow)
  it('502 engine_unavailable: unavailable category — no swallowed engine error', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 5,
    });

    const result = await priceSwapsInflation({}, '2025-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 5: network_error → network category
  it('network_error: network category', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceSwapsInflation({}, '2025-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 6: mapSwapsInflationOrchResult — null/undefined extras → no throw
  it('mapSwapsInflationOrchResult: null extras → fields undefined, no throw', () => {
    const mapped = mapSwapsInflationOrchResult({
      npv: null,
      fair_rate: null,
      fair_spread: null,
      fixed_leg_bps: null,
      fixed_leg_npv: null,
      inflation_leg_npv: null,
      yoy_leg_bps: null,
      yoy_leg_npv: null,
      swap_kind: 'zero_coupon',
      extras: undefined,
    });
    expect(mapped.npv).toBeUndefined();
    expect(mapped.fair_rate).toBeUndefined();
    expect(mapped.fixed_leg_flows).toBeUndefined();
    expect(mapped.inflation_leg_flows).toBeUndefined();
  });

  // Row 7: mapSwapsInflationOrchResult — YYIIS shape (yoy_leg_*; no inflation_leg_*)
  it('mapSwapsInflationOrchResult: YYIIS shape — yoy_leg_* populated; swap_kind echoed', () => {
    const mapped = mapSwapsInflationOrchResult({
      npv: 100.0,
      fair_rate: 0.025,
      fair_spread: 0.001,
      fixed_leg_bps: 5,
      fixed_leg_npv: 50.0,
      yoy_leg_bps: 6,
      yoy_leg_npv: 50.0,
      swap_kind: 'year_on_year',
      extras: { yoy_leg_flows: [{ amount: 25 }] },
    });
    expect(mapped.npv).toBe(100);
    expect(mapped.fair_spread).toBe(0.001);
    expect(mapped.yoy_leg_npv).toBe(50.0);
    expect(mapped.yoy_leg_bps).toBe(6);
    expect(mapped.swap_kind).toBe('year_on_year');
    expect(Array.isArray(mapped.yoy_leg_flows)).toBe(true);
  });

  // inline pass-through tests

  // Row 8: inline envelope is forwarded verbatim — flat (nested-swaps[]) trade
  // body, role-tagged top-level nominal+inflation curves, top-level
  // inflation_index, top-level as_of. NOT re-wrapped under `swap`.
  it('priceSwapsInflation — Thin-A envelope passed through verbatim', async () => {
    const thinAEnvelope = {
      swap: {
        swap_kind: 'zero_coupon',
        swaps: [{
          zero_coupon_inflation_swap: {
            swap_type: 'Payer',
            notional: 1_000_000.0,
            start_date: '2025-01-15',
            maturity_date: '2030-01-15',
            fixed_calendar: 'TARGET',
            fixed_convention: 'ModifiedFollowing',
            day_counter: 'Actual365Fixed',
            fixed_rate: 0.0217,
            inflation_index_id: 'EUHICP',
            observation_lag: { n: 3, unit: 'Months' },
            observation_interpolation: 'Linear',
            adjust_observation_dates: false,
            inflation_calendar: 'NullCalendar',
            inflation_convention: 'Following',
          },
        }],
      },
      curves: [
        {
          name: 'nominal',
          currency: 'EUR',
          day_counter: 'Actual365Fixed',
          reference_date: '2025-01-15',
          // nominal curve spans past 5Y maturity (deposit(1Y) +
          // swaps to 10Y) — the orchestrator engine_upstream_error
          // surfaces if the curve is shorter than the trade.
          points: [
            { point_type: 'DepositHelper', point: { tenor: { n: 1, unit: 'Years' }, quote_id: 'EUR.IRS.1Y' } },
            { point_type: 'SwapHelper', point: { tenor: { n: 2, unit: 'Years' }, quote_id: 'EUR.IRS.2Y' } },
            { point_type: 'SwapHelper', point: { tenor: { n: 5, unit: 'Years' }, quote_id: 'EUR.IRS.5Y' } },
            { point_type: 'SwapHelper', point: { tenor: { n: 10, unit: 'Years' }, quote_id: 'EUR.IRS.10Y' } },
          ],
          body: { role: 'nominal' },
        },
        {
          name: 'HICP_ZC',
          currency: 'EUR',
          day_counter: 'Actual365Fixed',
          reference_date: '2025-01-15',
          points: [
            { point_type: 'ZeroCouponInflationSwapHelper', point: { tenor: { n: 5, unit: 'Years' }, quote_id: 'EUR.HICP.5Y' } },
          ],
          body: { role: 'inflation', kind: 'ZeroInflation', index_id: 'EUHICP' },
        },
      ],
      inflation_index: {
        name: 'EU HICP',
        index_id: 'EUHICP',
        currency: 'EUR',
        body: {
          family_name: 'EU HICP',
          frequency: 'Monthly',
          availability_lag: { n: 2, unit: 'Months' },
          observation_lag: { n: 3, unit: 'Months' },
          fixings: [{ date: '2024-12-01', value: 100.4 }],
        },
      },
      as_of: '2025-01-15',
    };
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: -39671.61, fair_rate: 0.03, swap_kind: 'zero_coupon', extras: {} } },
      duration_ms: 25,
    });

    await priceSwapsInflation(thinAEnvelope, '2025-01-15');

    expect(orchPriceSwapsInflationMock).toHaveBeenCalledOnce();
    const sent = orchPriceSwapsInflationMock.mock.calls[0][0] as Record<string, unknown>;
    const sentSwap = sent.swap as Record<string, unknown>;
    const sentCurves = sent.curves as Array<Record<string, unknown>>;
    const sentIndex = sent.inflation_index as Record<string, unknown>;

    // No double-wrap: top-level shape matches the envelope.
    expect(sent.as_of).toBe('2025-01-15');
    expect(sentSwap.swap_kind).toBe('zero_coupon');
    // Trade body uses the nested swaps[] form (engine_io accepts it).
    expect(Array.isArray(sentSwap.swaps)).toBe(true);
    expect(((sentSwap.swaps as Array<Record<string, unknown>>)[0].zero_coupon_inflation_swap as Record<string, unknown>).notional).toBe(1_000_000.0);
    // Trap guard: no pricing.* nesting at the envelope root.
    expect(sent.pricing).toBeUndefined();
    // Curves: 2+ entries, role-tagged nominal + inflation.
    expect(sentCurves.length).toBeGreaterThanOrEqual(2);
    expect((sentCurves[0].body as Record<string, unknown>).role).toBe('nominal');
    expect((sentCurves[1].body as Record<string, unknown>).role).toBe('inflation');
    // nominal curve spans past 5Y maturity (max tenor in
    // sentCurves[0].points is ≥ 5Y).
    const nominalPoints = sentCurves[0].points as Array<Record<string, unknown>>;
    const maxNominalTenor = nominalPoints.reduce((max, wrap) => {
      const t = (wrap.point as Record<string, unknown>).tenor as { n: number; unit: string } | undefined;
      if (!t || t.unit !== 'Years') return max;
      return Math.max(max, t.n);
    }, 0);
    expect(maxNominalTenor).toBeGreaterThanOrEqual(5);
    // quote_id forwarded unresolved (no quote_value sub-key).
    const firstPoint = (sentCurves[0].points as Array<Record<string, unknown>>)[0];
    expect((firstPoint.point as Record<string, unknown>).quote_id).toBe('EUR.IRS.1Y');
    expect((firstPoint.point as Record<string, unknown>).value).toBeUndefined();
    // inflation_index at the top level.
    expect(sentIndex.index_id).toBe('EUHICP');
    expect((sentIndex.body as Record<string, unknown>).family_name).toBe('EU HICP');
  });

  // Row 9: non-envelope (legacy fat) input is wrapped under `swap` for
  // back-compat with any unmigrated call sites (notably the save-flow
  // round-trip path that still stores the fat shape in localStorage).
  it('priceSwapsInflation — legacy fat body wrapped under `swap` (back-compat)', async () => {
    orchPriceSwapsInflationMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: -1000, swap_kind: 'zero_coupon', extras: {} } },
      duration_ms: 5,
    });
    const legacy = {
      pricing: { as_of_date: '2025-01-15', curves: [], inflation_indices: [{}], inflation_curves: [{}] },
      swaps: [{ zero_coupon_inflation_swap: { swap_type: 'Payer' }, discounting_curve: 'discount' }],
    };
    await priceSwapsInflation(legacy, '2025-01-15');

    const sent = orchPriceSwapsInflationMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    // Wrapped, not flattened — preserves the legacy save-flow shape verbatim.
    expect(sent.swap).toBe(legacy);
  });
});

// by-reference arm selection

describe('buildSwapsInflationPriceBody — arm selection', () => {
  // Saved → by-reference: a minimal {swap_id, as_of}, NO inline body.
  it('saved swap_id → by-ref body {swap_id, as_of}; drops inline swap/curves/index', () => {
    const body = buildSwapsInflationPriceBody(
      {
        swap_id: 'swaps-inflation-uuid',
        as_of: '2025-01-15',
        // Stray inline fields must be dropped on the by-ref arm.
        swap: { swaps: [] },
        curves: [{ name: 'nominal' }],
        inflation_index: { index_id: 'EUHICP' },
      },
      '2099-12-31',
    );
    expect(body).toEqual({ swap_id: 'swaps-inflation-uuid', as_of: '2025-01-15' });
    expect(body.swap).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.inflation_index).toBeUndefined();
  });

  it('saved swap_id + snapshot_id → snapshot pin forwarded (D40/D131(a))', () => {
    const body = buildSwapsInflationPriceBody(
      { swap_id: 'swaps-inflation-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' },
      '2025-01-15',
    );
    expect(body).toEqual({
      swap_id: 'swaps-inflation-uuid',
      as_of: '2025-01-15',
      snapshot_id: 'snap-1',
    });
  });

  it('by-ref falls back to the asOf arg when the request omits as_of', () => {
    const body = buildSwapsInflationPriceBody({ swap_id: 'swaps-inflation-uuid' }, '2025-01-15');
    expect(body).toEqual({ swap_id: 'swaps-inflation-uuid', as_of: '2025-01-15' });
  });

  // Unsaved → inline: a ready envelope passes through verbatim.
  it('unsaved envelope → Thin-A inline passed through verbatim (no swap_id)', () => {
    const envelope = {
      swap: { swap_kind: 'zero_coupon', swaps: [{ zero_coupon_inflation_swap: {} }] },
      curves: [{ name: 'nominal' }, { name: 'inflation' }],
      inflation_index: { index_id: 'EUHICP' },
      as_of: '2025-01-15',
    };
    const body = buildSwapsInflationPriceBody(envelope, '2025-01-15');
    expect(body).toBe(envelope);
    expect(body.swap_id).toBeUndefined();
  });

  // Legacy fat body (non-envelope) → wrapped under `swap` (back-compat).
  it('unsaved legacy fat body → wrapped under `swap`', () => {
    const legacy = { swaps: [{ zero_coupon_inflation_swap: {} }], pricing: { curves: [] } };
    const body = buildSwapsInflationPriceBody(legacy, '2025-01-15');
    expect(body.swap).toBe(legacy);
    expect(body.as_of).toBe('2025-01-15');
    expect(body.swap_id).toBeUndefined();
  });

  // Empty-string swap_id must NOT trigger the by-ref arm (treated as unsaved).
  it('empty swap_id is ignored — falls back to inline arm', () => {
    const body = buildSwapsInflationPriceBody({ swap_id: '', swaps: [] }, '2025-01-15');
    expect(body.swap_id).toBeUndefined();
    expect(body.swap).toBeDefined();
  });
});
