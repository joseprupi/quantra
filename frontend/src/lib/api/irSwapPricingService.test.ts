import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  priceSwapIrMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    priceSwapIrMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({ priceSwapIr: priceSwapIrMock }));

import { mapIrSwapOrchResult, priceIrSwap } from './irSwapPricingService';

// 6-row unit matrix

describe('irSwapPricingService — 6-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Row 1: orch 200 → success=true; npv from r.npv; fair_rate from extras
  it('orch 200: success=true, data mapped from result.data.result', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: 1234.5,
          extras: { fair_rate: 0.05, fixed_leg_bps: 150 },
        },
      },
      duration_ms: 42,
    });

    const result = await priceIrSwap({ swap: 'data' }, '2026-01-15');

    expect(priceSwapIrMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.swaps[0].npv).toBe(1234.5);
    expect(result.data?.swaps[0].fair_rate).toBe(0.05);
    expect(result.duration_ms).toBe(42);
  });

  // Row 2: 404 swap_ir_not_found → category driven by code, not httpStatus
  // Uses httpStatus 500 (would give 'server') to prove the code branch fires first
  it('swap_ir_not_found code: not_found category (code wins over httpStatus)', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Swap not found.', code: 'swap_ir_not_found' },
      httpStatus: 500,
      duration_ms: 10,
    });

    const result = await priceIrSwap({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 3: 502 engine_unavailable → unavailable category
  it('502 engine_unavailable: unavailable category', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 10,
    });

    const result = await priceIrSwap({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 4: network_error → network category
  it('network_error: network category', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceIrSwap({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 5: mapIrSwapOrchResult — null/undefined extras → all fields undefined, no throw
  it('mapIrSwapOrchResult: null extras returns all fields undefined without throw', () => {
    const mapped = mapIrSwapOrchResult({ npv: null, extras: undefined });

    expect(mapped.npv).toBeUndefined();
    expect(mapped.fair_rate).toBeUndefined();
    expect(mapped.fair_spread).toBeUndefined();
    expect(mapped.fixed_leg_bps).toBeUndefined();
    expect(mapped.floating_leg_bps).toBeUndefined();
    expect(mapped.overnight_leg_npv).toBeUndefined();
    expect(mapped.fair_spread_leg1).toBeUndefined();
    expect(mapped.cms_leg_bps).toBeUndefined();
    expect(mapped.used_cms_pricer_type).toBeUndefined();
    expect(mapped.used_cms_hagan_hard_upper_limit).toBeUndefined();
  });

  // Row 6: inline POST body — vanilla swap_ir envelope passes through
  // verbatim: flat trade body under ``swap``, top-level ``curves`` carrying
  // unresolved ``quote_id`` points, no ``pricing.*`` nesting, no client-side
  // resolved numeric points.
  it('thin-A envelope: flat swap + top-level curves + unresolved quote_id forwarded', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: { npv: -22894.84, extras: { fair_rate: 0.03 } },
      },
      duration_ms: 5,
    });

    const thinABody = {
      swap: {
        notional: 1_000_000.0,
        fixed_rate: 0.035,
        swap_type: 'Payer',
        effective_date: '2025-01-17',
        termination_date: '2030-01-17',
      },
      curves: [
        {
          name: 'discount',
          day_counter: 'Actual365Fixed',
          reference_date: '2025-01-15',
          points: [
            {
              point_type: 'DepositHelper',
              point: {
                tenor: { n: 1, unit: 'Years' },
                quote_id: 'USD.IRS.1Y',
                fixing_days: 2,
                calendar: 'TARGET',
                business_day_convention: 'ModifiedFollowing',
                day_counter: 'Actual365Fixed',
              },
            },
            {
              point_type: 'SwapHelper',
              point: {
                tenor: { n: 5, unit: 'Years' },
                quote_id: 'USD.IRS.5Y',
                calendar: 'TARGET',
                sw_fixed_leg_frequency: 'Annual',
                sw_fixed_leg_convention: 'ModifiedFollowing',
                sw_fixed_leg_day_counter: 'Thirty360',
              },
            },
          ],
        },
      ],
      as_of: '2025-01-15',
    };

    await priceIrSwap(thinABody, '2025-01-15');

    expect(priceSwapIrMock).toHaveBeenCalledOnce();
    const posted = priceSwapIrMock.mock.calls[0][0] as Record<string, unknown>;

    // Flat trade body, no nesting under swap.swaps[0].vanilla_swap.fixed_leg.*
    expect(posted.swap).toEqual({
      notional: 1_000_000.0,
      fixed_rate: 0.035,
      swap_type: 'Payer',
      effective_date: '2025-01-17',
      termination_date: '2030-01-17',
    });

    // Curves lifted to top level (no pricing.curves nesting on the wire).
    expect(Array.isArray(posted.curves)).toBe(true);
    expect((posted as { pricing?: unknown }).pricing).toBeUndefined();
    expect((posted.swap as Record<string, unknown>).pricing).toBeUndefined();

    // ``as_of`` lives at the top level (inline envelope).
    expect(posted.as_of).toBe('2025-01-15');

    // Curve points keep their ``quote_id`` unresolved (the backend
    // substitutes); no top-level numeric ``rate``/``price`` slipped in.
    const curve = (posted.curves as Array<Record<string, unknown>>)[0];
    const helperPoints = curve.points as Array<{ point: Record<string, unknown> }>;
    expect(helperPoints[0].point.quote_id).toBe('USD.IRS.1Y');
    expect(helperPoints[0].point.rate).toBeUndefined();
    expect(helperPoints[1].point.quote_id).toBe('USD.IRS.5Y');
    expect(helperPoints[1].point.rate).toBeUndefined();
  });

  // Row 8: by-reference — a swap_id posts a minimal
  // by-reference body. swap_id present; NO inline swap/curves; as_of at top
  // level. The orchestrator loads the trade + curve chain from app.*.
  it('thin-B by-reference: swap_id posts {swap_id, as_of}, no inline swap/curves', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: { npv: -22894.84, extras: { fair_rate: 0.03 } },
      },
      duration_ms: 7,
    });

    const result = await priceIrSwap(
      { swap_id: '226b3355-0000-0000-0000-000000000000', as_of: '2025-01-15' },
      '2025-01-15',
    );

    expect(priceSwapIrMock).toHaveBeenCalledOnce();
    const posted = priceSwapIrMock.mock.calls[0][0] as Record<string, unknown>;

    // By-reference body: id + date only, no inline trade or curves on the wire.
    expect(posted.swap_id).toBe('226b3355-0000-0000-0000-000000000000');
    expect(posted.as_of).toBe('2025-01-15');
    expect(posted.swap).toBeUndefined();
    expect(posted.curves).toBeUndefined();
    expect(posted.snapshot_id).toBeUndefined();

    // Response handling identical to inline.
    expect(result.success).toBe(true);
    expect(result.data?.swaps[0].npv).toBe(-22894.84);
    expect(result.data?.swaps[0].fair_rate).toBe(0.03);
  });

  // Row 9: by-reference with a snapshot pin — snapshot_id
  // is forwarded so the orchestrator prices against the pinned MD snapshot.
  it('thin-B by-reference: snapshot_id is forwarded when present', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: { npv: -22894.84, extras: { fair_rate: 0.03 } },
      },
      duration_ms: 7,
    });

    await priceIrSwap(
      {
        swap_id: '226b3355-0000-0000-0000-000000000000',
        as_of: '2025-01-15',
        snapshot_id: 'aaaa1111-0000-0000-0000-000000000000',
      },
      '2025-01-15',
    );

    const posted = priceSwapIrMock.mock.calls[0][0] as Record<string, unknown>;
    expect(posted.swap_id).toBe('226b3355-0000-0000-0000-000000000000');
    expect(posted.snapshot_id).toBe('aaaa1111-0000-0000-0000-000000000000');
    expect(posted.swap).toBeUndefined();
  });

  // Row 10: by-reference MAY carry a curve override — forwarded
  // alongside swap_id so the assembler re-prices the saved trade on a different
  // curve set (request-level curves beat the saved chain).
  it('thin-B by-reference: curve override is forwarded alongside swap_id', async () => {
    priceSwapIrMock.mockResolvedValueOnce({
      ok: true,
      data: { pricing_history_id: null, assembled_request: {}, result: { npv: 1, extras: {} } },
      duration_ms: 1,
    });

    const curves = [{ id: 'cccc2222-0000-0000-0000-000000000000' }];
    await priceIrSwap(
      { swap_id: '226b3355-0000-0000-0000-000000000000', as_of: '2025-01-15', curves },
      '2025-01-15',
    );

    const posted = priceSwapIrMock.mock.calls[0][0] as Record<string, unknown>;
    expect(posted.swap_id).toBe('226b3355-0000-0000-0000-000000000000');
    expect(posted.curves).toEqual(curves);
    expect(posted.swap).toBeUndefined();
  });

  // Row 11: arm coexistence — the SAME inline body (Row 6 inline) and a
  // by-reference body map their successful responses identically.
  // Proves the two arms differ only in the request, never in the response path.
  it('arm coexistence: inline and by-reference map an identical response identically', async () => {
    const orchResult = {
      ok: true as const,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: { npv: -22894.84, extras: { fair_rate: 0.03, fixed_leg_bps: 150 } },
      },
      duration_ms: 9,
    };
    priceSwapIrMock.mockResolvedValueOnce(orchResult).mockResolvedValueOnce(orchResult);

    const inline = await priceIrSwap(
      { swap: { notional: 1_000_000.0 }, curves: [], as_of: '2025-01-15' },
      '2025-01-15',
    );
    const byRef = await priceIrSwap(
      { swap_id: '226b3355-0000-0000-0000-000000000000', as_of: '2025-01-15' },
      '2025-01-15',
    );

    expect(inline.success).toBe(true);
    expect(byRef.success).toBe(true);
    expect(byRef.data).toEqual(inline.data);

    // ...but the request bodies differ: inline carries `swap`, by-ref `swap_id`.
    const inlineBody = priceSwapIrMock.mock.calls[0][0] as Record<string, unknown>;
    const byRefBody = priceSwapIrMock.mock.calls[1][0] as Record<string, unknown>;
    expect(inlineBody.swap).toBeDefined();
    expect(inlineBody.swap_id).toBeUndefined();
    expect(byRefBody.swap_id).toBeDefined();
    expect(byRefBody.swap).toBeUndefined();
  });

  // Row 7: mapIrSwapOrchResult — fully populated extras maps all 20+ fields correctly
  it('mapIrSwapOrchResult: fully populated extras maps all fields through', () => {
    const extras = {
      fair_rate: 0.03,
      fair_spread: 0.001,
      fixed_leg_bps: 150,
      floating_leg_bps: 120,
      fixed_leg_npv: 500,
      floating_leg_npv: -500,
      fixed_leg_flows: [{ date: '2026-06-15', amount: 1000 }],
      floating_leg_flows: [{ date: '2026-06-15', amount: -1000 }],
      overnight_leg_bps: 110,
      overnight_leg_npv: -400,
      overnight_leg_flows: [{ date: '2026-06-15', amount: -400 }],
      fair_spread_leg1: 0.002,
      fair_spread_leg2: 0.003,
      leg1_bps: 130,
      leg2_bps: 140,
      leg1_npv: 300,
      leg2_npv: -300,
      leg1_flows: [{ date: '2026-06-15', amount: 300 }],
      leg2_flows: [{ date: '2026-06-15', amount: -300 }],
      cms_leg_bps: 160,
      cms_leg_npv: 200,
      cms_leg_flows: [{ date: '2026-06-15', amount: 200 }],
      used_cms_pricer_type: 'LinearTsr',
      used_cms_yield_curve_model: 'Standard',
      used_cms_mean_reversion: 0.01,
      used_cms_hagan_lower_limit: 0.0,
      used_cms_hagan_upper_limit: 0.3,
      used_cms_hagan_precision: 1e-6,
      used_cms_hagan_hard_upper_limit: 0.5,
    };

    const mapped = mapIrSwapOrchResult({ npv: 99, extras });

    expect(mapped.npv).toBe(99);
    for (const [key, val] of Object.entries(extras)) {
      expect(mapped[key]).toEqual(val);
    }
  });
});
