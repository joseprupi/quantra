import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  orchPriceEquityOptionMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    orchPriceEquityOptionMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({ priceEquityOption: orchPriceEquityOptionMock }));

import { buildEquityOptionPriceBody, mapEquityOptionOrchResult, priceEquityOption } from './equityOptionPricingService';

// 10-row unit matrix

describe('equityOptionPricingService — 10-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Row 1: orchestrator 200 success — wrapped shape with Greek and implied_vol rename
  it('orchestrator 200 success — wrapped shape with Greek and implied_vol rename', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: 12.50,
          delta: 0.55,
          gamma: 0.02,
          vega: 8.5,
          theta: -0.15,
          rho: 3.2,
          implied_volatility: 0.22,
          used_spot: 100.0,
          extras: {},
        },
      },
      duration_ms: 22,
    });

    const result = await priceEquityOption({ equity_option: 'data' }, '2026-05-23');

    expect(orchPriceEquityOptionMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.options[0].npv).toBe(12.50);
    expect(result.data?.options[0].delta).toBe(0.55);
    expect(result.data?.options[0].implied_vol).toBe(0.22);
    expect(result.data?.options[0].used_spot).toBe(100.0);
    expect(result.duration_ms).toBe(22);
  });

  // Row 2: 404 equity_option_not_found — not_found category (code wins over httpStatus)
  // Supply code 'equity_option_not_found' with mismatched httpStatus: 200 to prove code branch fires.
  it('404 equity_option_not_found — not_found category (code wins over httpStatus, inv. 9)', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: {
        error: 'Equity option not found.',
        code: 'equity_option_not_found',
      },
      httpStatus: 200,
      duration_ms: 5,
    });

    const result = await priceEquityOption({}, '2026-05-23');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 3: 422 equity_option_spot_resolution_failed — validation category
  it('422 equity_option_spot_resolution_failed — validation category', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: {
        error: 'Spot quote not resolvable.',
        code: 'equity_option_spot_resolution_failed',
      },
      httpStatus: 422,
      duration_ms: 5,
    });

    const result = await priceEquityOption({}, '2026-05-23');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('validation');
  });

  // Row 4: 502 engine_unavailable — unavailable category
  it('502 engine_unavailable — unavailable category', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 5,
    });

    const result = await priceEquityOption({}, '2026-05-23');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 5: network_error — network category
  it('network_error — network category', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceEquityOption({}, '2026-05-23');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 6: mapEquityOptionOrchResult — implied_volatility → implied_vol rename; all six Greeks
  it('mapEquityOptionOrchResult — implied_volatility → implied_vol rename; all six Greeks', () => {
    const mapped = mapEquityOptionOrchResult({
      npv: 10.0,
      delta: 0.5,
      gamma: 0.01,
      vega: 5.0,
      theta: -0.1,
      rho: 2.0,
      implied_volatility: 0.2,
      extras: {},
    });

    expect(mapped.implied_vol).toBe(0.2);
    expect(mapped.implied_volatility).toBeUndefined();
    expect(mapped.npv).toBe(10.0);
    expect(mapped.delta).toBe(0.5);
    expect(mapped.gamma).toBe(0.01);
    expect(mapped.vega).toBe(5.0);
    expect(mapped.theta).toBe(-0.1);
    expect(mapped.rho).toBe(2.0);
  });

  // Row 7: mapEquityOptionOrchResult — null implied_volatility → implied_vol undefined
  it('mapEquityOptionOrchResult — null implied_volatility → implied_vol undefined', () => {
    const mapped = mapEquityOptionOrchResult({
      implied_volatility: null,
      extras: {},
    });

    expect(mapped.implied_vol).toBeUndefined();
  });

  // Row 8: mapEquityOptionOrchResult — engine_used from extras
  it('mapEquityOptionOrchResult — engine_used from extras', () => {
    const mapped = mapEquityOptionOrchResult({
      extras: { engine_used: 'AnalyticBSM' },
    });

    expect(mapped.engine_used).toBe('AnalyticBSM');
  });

  // Row 9: mapEquityOptionOrchResult — engine_used absent from extras → undefined; no throw
  it('mapEquityOptionOrchResult — engine_used absent from extras → undefined; no throw', () => {
    const mapped = mapEquityOptionOrchResult({
      extras: {},
    });

    expect(mapped.engine_used).toBeUndefined();
  });

  // Row 10: mapEquityOptionOrchResult — null extras → all fields undefined; no throw
  it('mapEquityOptionOrchResult — null extras → all fields undefined; no throw', () => {
    const mapped = mapEquityOptionOrchResult({
      npv: null,
      delta: null,
      extras: null as unknown as undefined,
    });

    expect(mapped.npv).toBeUndefined();
    expect(mapped.delta).toBeUndefined();
    expect(mapped.engine_used).toBeUndefined();
  });

  // inline pass-through tests

  // Row 11: inline envelope is forwarded verbatim (flat equity_option trade
  // body, role-tagged top-level curves, top-level vol_surface, top-level
  // spot, top-level as_of), not re-wrapped under `equity_option`.
  it('priceEquityOption — Thin-A envelope passed through verbatim', async () => {
    const thinAEnvelope = {
      equity_option: {
        option_type: 'Call',
        strike: 100.0,
        quantity: 1.0,
        expiry_date: '2026-01-15',
        settlement: 'Physical',
        trade_id: 'demo-AAPL',
      },
      curves: [
        {
          name: 'discount',
          points: [{ point_type: 'DepositHelper', point: { quote_id: 'USD.1Y', tenor: { n: 1, unit: 'Years' } } }],
          body: { role: 'discount' },
        },
        {
          name: 'dividend',
          points: [{ point_type: 'DepositHelper', point: { quote_id: 'AAPL.DIV.1Y', tenor: { n: 1, unit: 'Years' } } }],
          body: { role: 'dividend' },
        },
      ],
      vol_surface: {
        name: 'AAPL-vol',
        kind: 'BlackVolSpec',
        payload: { base: { reference_date: '2025-01-15', constant_vol: 0.20, shape: 'Constant' } },
      },
      spot: { value: 100.0, canonical_id: 'AAPL' },
      as_of: '2025-01-15',
    };
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: true,
      data: {
        assembled_request: {},
        result: { npv: 8.81, delta: 0.573, vega: 38.73, extras: {} },
      },
      duration_ms: 25,
    });

    await priceEquityOption(thinAEnvelope, '2025-01-15');

    expect(orchPriceEquityOptionMock).toHaveBeenCalledOnce();
    const sent = orchPriceEquityOptionMock.mock.calls[0][0] as Record<string, unknown>;
    const sentOpt = sent.equity_option as Record<string, unknown>;
    const sentCurves = sent.curves as Array<Record<string, unknown>>;
    const sentVol = sent.vol_surface as Record<string, unknown>;
    const sentSpot = sent.spot as Record<string, unknown>;

    // No double-wrap: top-level shape matches the envelope.
    expect(sent.as_of).toBe('2025-01-15');
    // Flat equity_option trade body — no options[*] wrapper, no pricing.* nesting.
    expect(sentOpt.option_type).toBe('Call');
    expect(sentOpt.strike).toBe(100.0);
    expect(sentOpt.expiry_date).toBe('2026-01-15');
    expect(sentOpt.settlement).toBe('Physical');
    expect((sentOpt as { options?: unknown }).options).toBeUndefined();
    expect((sentOpt as { pricing?: unknown }).pricing).toBeUndefined();
    expect((sentOpt as { underlying?: unknown }).underlying).toBeUndefined();
    // Role-tagged top-level curves; quote_id forwarded unresolved.
    expect(sentCurves).toHaveLength(2);
    expect((sentCurves[0].body as Record<string, unknown>).role).toBe('discount');
    expect((sentCurves[1].body as Record<string, unknown>).role).toBe('dividend');
    const firstPoint = (sentCurves[0].points as Array<Record<string, unknown>>)[0];
    expect((firstPoint.point as Record<string, unknown>).quote_id).toBe('USD.1Y');
    expect((firstPoint.point as Record<string, unknown>).value).toBeUndefined();
    // vol_surface at the top level — BlackVolSpec with base directly under payload.
    expect(sentVol.kind).toBe('BlackVolSpec');
    expect((sentVol.payload as Record<string, unknown>).base).toBeDefined();
    expect((sentVol.payload as { payload?: unknown }).payload).toBeUndefined();
    // spot at the top level — canonical_id present so the orchestrator
    // can derive the per-trade underlier id.
    expect(sentSpot.value).toBe(100.0);
    expect(sentSpot.canonical_id).toBe('AAPL');
    // No legacy pricing nesting at the envelope root either.
    expect(sent.pricing).toBeUndefined();
    expect((sent as { options?: unknown }).options).toBeUndefined();
  });

  // Row 12: non-envelope (legacy fat) input is wrapped under
  // `equity_option` for back-compat with the save-flow round-trip
  // path that still stores the fat shape in localStorage.
  it('priceEquityOption — legacy fat body wrapped under `equity_option` (back-compat)', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 8.81, extras: {} } },
      duration_ms: 5,
    });
    const legacy = {
      pricing: { as_of_date: '2025-01-15', curves: [], vol_surfaces: [{}] },
      options: [{ trade_id: 'legacy', option_type: 'Call' }],
    };
    await priceEquityOption(legacy, '2025-01-15');

    const sent = orchPriceEquityOptionMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    // Wrapped, not flattened — preserves the legacy save-flow shape verbatim.
    expect(sent.equity_option).toBe(legacy);
  });

  // by-reference tests

  // Row 13: a saved equity_option_id flips the body to the by-reference
  // arm — minimal {equity_option_id, as_of}; no inline equity_option/curves/
  // vol_surface/spot is sent (the orchestrator loads them from app.*).
  it('buildEquityOptionPriceBody — Thin-B by-reference body {equity_option_id, as_of}', () => {
    const body = buildEquityOptionPriceBody(
      {
        equity_option_id: 'eo-uuid',
        as_of: '2025-01-15',
        // stray inline fields must be dropped on the by-ref arm
        equity_option: { strike: 1 },
        curves: [{ name: 'd' }],
        vol_surface: {},
        spot: {},
      },
      '2099-01-01',
    );
    expect(body).toEqual({ equity_option_id: 'eo-uuid', as_of: '2025-01-15' });
  });

  // Row 14: by-reference + snapshot pin → snapshot_id forwarded.
  it('buildEquityOptionPriceBody — by-reference forwards snapshot_id', () => {
    const body = buildEquityOptionPriceBody(
      { equity_option_id: 'eo-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' },
      '2025-01-15',
    );
    expect(body).toEqual({ equity_option_id: 'eo-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  // Row 15: priceEquityOption posts the by-reference body through the
  // orchestrator unchanged; response mapping is identical to inline.
  it('priceEquityOption — by-reference body posted; no inline payload sent', async () => {
    orchPriceEquityOptionMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 8.81, delta: 0.573, vega: 38.73, extras: {} } },
      duration_ms: 9,
    });

    const result = await priceEquityOption({ equity_option_id: 'eo-uuid', as_of: '2025-01-15' }, '2025-01-15');

    expect(orchPriceEquityOptionMock).toHaveBeenCalledOnce();
    const sent = orchPriceEquityOptionMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent).toEqual({ equity_option_id: 'eo-uuid', as_of: '2025-01-15' });
    expect(sent.equity_option).toBeUndefined();
    expect(sent.curves).toBeUndefined();
    expect(sent.vol_surface).toBeUndefined();
    expect(sent.spot).toBeUndefined();
    expect(result.success).toBe(true);
    expect(result.data?.options[0].npv).toBe(8.81);
    expect(result.data?.options[0].vega).toBe(38.73);
  });

  // Row 16: inline is preserved — a body with no equity_option_id still wraps /
  // passes through as before (coexistence; the by-ref branch is additive).
  it('buildEquityOptionPriceBody — Thin-A inline preserved when no equity_option_id', () => {
    const envelope = { equity_option: { strike: 1 }, curves: [], vol_surface: {}, spot: {}, as_of: '2025-01-15' };
    expect(buildEquityOptionPriceBody(envelope, '2025-01-15')).toBe(envelope);
    const legacy = { options: [{ strike: 1 }] };
    expect(buildEquityOptionPriceBody(legacy, '2025-01-15')).toEqual({ equity_option: legacy, as_of: '2025-01-15' });
  });
});
