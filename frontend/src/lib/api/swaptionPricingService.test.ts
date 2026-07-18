import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  orchPriceSwaptionMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    orchPriceSwaptionMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({ priceSwaption: orchPriceSwaptionMock }));

import { buildSwaptionPriceBody, mapSwaptionOrchResult, priceSwaption } from './swaptionPricingService';

// 7-row unit matrix

describe('swaptionPricingService — 7-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Row 1: orch 200 → success=true; wrapped shape; top-level delta + extras implied_volatility
  it('orch 200: success=true; swaptions[0] carries top-level + extras fields', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: 500.0,
          delta: -0.45,
          vega: 123.0,
          extras: { implied_volatility: 0.05, atm_forward: 0.03 },
        },
      },
      duration_ms: 20,
    });

    const result = await priceSwaption({ swaption: 'data' }, '2026-01-15');

    expect(orchPriceSwaptionMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.swaptions[0].npv).toBe(500.0);
    expect(result.data?.swaptions[0].delta).toBe(-0.45);
    expect(result.data?.swaptions[0].vega).toBe(123.0);
    expect(result.data?.swaptions[0].implied_volatility).toBe(0.05);
    expect(result.data?.swaptions[0].atm_forward).toBe(0.03);
    expect(result.duration_ms).toBe(20);
  });

  // Row 2: 404 swaption_not_found → category driven by code, not httpStatus
  // Intentionally supply httpStatus 500 (would give 'server') to prove code branch fires first
  it('swaption_not_found code: not_found category (code wins over httpStatus)', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Swaption not found.', code: 'swaption_not_found' },
      httpStatus: 500,
      duration_ms: 10,
    });

    const result = await priceSwaption({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 3: 502 engine_unavailable → unavailable category
  it('502 engine_unavailable: unavailable category', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 10,
    });

    const result = await priceSwaption({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 4: network_error → network category
  it('network_error: network category', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceSwaption({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 5: mapSwaptionOrchResult — null/undefined extras → all fields undefined, no throw
  it('mapSwaptionOrchResult: null extras returns all fields undefined without throw', () => {
    const mapped = mapSwaptionOrchResult({ npv: null, delta: null, vega: null, extras: undefined });

    expect(mapped.npv).toBeUndefined();
    expect(mapped.delta).toBeUndefined();
    expect(mapped.vega).toBeUndefined();
    expect(mapped.implied_volatility).toBeUndefined();
    expect(mapped.atm_forward).toBeUndefined();
    expect(mapped.annuity).toBeUndefined();
    expect(mapped.dv01).toBeUndefined();
    expect(mapped.gamma).toBeUndefined();
    expect(mapped.theta).toBeUndefined();
  });

  // Row 6: mapSwaptionOrchResult — top-level fields populated, extras empty
  it('mapSwaptionOrchResult: top-level npv/delta/vega read directly; empty extras → undefined', () => {
    const mapped = mapSwaptionOrchResult({ npv: 200, delta: -0.3, vega: 50, extras: {} });

    expect(mapped.npv).toBe(200);
    expect(mapped.delta).toBe(-0.3);
    expect(mapped.vega).toBe(50);
    expect(mapped.implied_volatility).toBeUndefined();
    expect(mapped.atm_forward).toBeUndefined();
    expect(mapped.annuity).toBeUndefined();
  });

  // Row 7: mapSwaptionOrchResult — all 9 fields populated (3 top-level + 6 extras)
  it('mapSwaptionOrchResult: fully populated — all 9 SwaptionResultData fields mapped', () => {
    const extras = {
      implied_volatility: 0.04,
      atm_forward:        0.025,
      annuity:            5.0,
      dv01:               0.001,
      gamma:              0.01,
      theta:              -0.0005,
    };
    const mapped = mapSwaptionOrchResult({ npv: 100, delta: -0.2, vega: 10, extras });

    expect(mapped.npv).toBe(100);
    expect(mapped.delta).toBe(-0.2);
    expect(mapped.vega).toBe(10);
    for (const [key, val] of Object.entries(extras)) {
      expect(mapped[key]).toEqual(val);
    }
  });

  // inline pass-through tests

  // Row 8: inline envelope is forwarded verbatim (flat swaption trade body,
  // role-tagged top-level curves, top-level vol_surface, top-level
  // swaption_model, top-level as_of), not re-wrapped under `swaption`.
  it('priceSwaption — Thin-A envelope passed through verbatim', async () => {
    const thinAEnvelope = {
      swaption: {
        notional: 1_000_000,
        strike: 0.035,
        swap_type: 'Payer',
        exercise_date: '2026-01-15',
        effective_date: '2026-01-17',
        termination_date: '2031-01-17',
        exercise_type: 'European',
        settlement_type: 'Physical',
      },
      curves: [{
        name: 'discount',
        points: [{ point_type: 'DepositHelper', point: { quote_id: 'EUR.3M', tenor: { n: 3, unit: 'Months' } } }],
        body: { role: 'discount' },
      }],
      vol_surface: {
        name: 'swaptvol',
        kind: 'SwaptionVolSpec',
        // Orchestrator-side flat payload: ``base`` lives directly under
        // ``payload``. The portal's
        // ``toThinAVolSurface`` collapses the engine-FB extra nesting
        // before posting.
        payload: {
          swap_index_id: 'EUR_SWAP_6M',
          base: { reference_date: '2025-01-15', constant_vol: 0.20, volatility_type: 'Normal', shape: 'Constant' },
        },
      },
      swaption_model: {
        name: 'swaptmodel',
        kind: 'HullWhiteLattice',
        payload: { param_mode: 'Explicit', hw_a: 0.05, hw_sigma: 0.01, lattice_steps: 60 },
      },
      as_of: '2025-01-15',
    };
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 7133.03, delta: 0.5, vega: 100, extras: { implied_volatility: 0.20 } } },
      duration_ms: 25,
    });

    await priceSwaption(thinAEnvelope, '2025-01-15');

    expect(orchPriceSwaptionMock).toHaveBeenCalledOnce();
    const sent = orchPriceSwaptionMock.mock.calls[0][0] as Record<string, unknown>;
    const sentSwaption = sent.swaption as Record<string, unknown>;
    const sentCurves = sent.curves as Array<Record<string, unknown>>;
    const sentVol = sent.vol_surface as Record<string, unknown>;
    const sentModel = sent.swaption_model as Record<string, unknown>;

    // No double-wrap: top-level shape matches the envelope.
    expect(sent.as_of).toBe('2025-01-15');
    expect(sentSwaption.strike).toBe(0.035);
    expect(sentSwaption.notional).toBe(1_000_000);
    expect(sentSwaption.swap_type).toBe('Payer');
    expect(sentSwaption.exercise_date).toBe('2026-01-15');
    // Trap guard: flat trade body, no nested swaptions[*] wrapper.
    expect((sentSwaption as { swaptions?: unknown }).swaptions).toBeUndefined();
    expect((sentSwaption as { pricing?: unknown }).pricing).toBeUndefined();
    expect((sentSwaption as { underlying?: unknown }).underlying).toBeUndefined();
    // Role-tagged top-level curves; quote_id forwarded unresolved.
    expect((sentCurves[0].body as Record<string, unknown>).role).toBe('discount');
    const firstPoint = (sentCurves[0].points as Array<Record<string, unknown>>)[0];
    expect((firstPoint.point as Record<string, unknown>).quote_id).toBe('EUR.3M');
    expect((firstPoint.point as Record<string, unknown>).value).toBeUndefined();
    // vol_surface + swaption_model at the top level.
    expect(sentVol.kind).toBe('SwaptionVolSpec');
    // Trap guard: ``base`` lives directly under ``payload`` (the
    // orchestrator translator reads ``payload.base``).
    // No extra ``payload.payload.base`` nesting from the engine FB shape.
    expect((sentVol.payload as Record<string, unknown>).base).toBeDefined();
    expect((sentVol.payload as { payload?: unknown }).payload).toBeUndefined();
    expect(sentModel.kind).toBe('HullWhiteLattice');
    expect((sentModel.payload as Record<string, unknown>).hw_sigma).toBe(0.01);
    // No legacy pricing nesting at the envelope root either.
    expect(sent.pricing).toBeUndefined();
    expect((sent as { swaptions?: unknown }).swaptions).toBeUndefined();
  });

  // Row 9: non-envelope (legacy fat) input is wrapped under `swaption` for
  // back-compat with any unmigrated call sites (notably the save-flow
  // round-trip path that still stores the fat shape in localStorage).
  it('priceSwaption — legacy fat body wrapped under `swaption` (back-compat)', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 100, delta: null, vega: null, extras: {} } },
      duration_ms: 5,
    });
    const legacy = {
      pricing: { as_of_date: '2025-01-15', vol_surfaces: [{}], models: [{}], curves: [] },
      swaptions: [{ swaption: { underlying_type: 'VanillaSwap' } }],
    };
    await priceSwaption(legacy, '2025-01-15');

    const sent = orchPriceSwaptionMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    // Wrapped, not flattened — preserves the legacy save-flow shape verbatim.
    expect(sent.swaption).toBe(legacy);
  });

  // by-reference arm

  // Row 10: a saved swaption_id flips the body to the minimal by-reference
  // shape — no inline trade/curves/vol_surface/swaption_model leak through.
  it('buildSwaptionPriceBody — saved swaption_id → Thin-B {swaption_id, as_of}, no inline', () => {
    const body = buildSwaptionPriceBody(
      {
        swaption_id: 'swaption-uuid',
        as_of: '2025-01-15',
        // These MUST be stripped on the by-reference arm.
        swaption: { notional: 1 },
        curves: [{ name: 'd' }],
        vol_surface: { kind: 'SwaptionVolSpec' },
        swaption_model: { kind: 'HullWhiteLattice' },
      },
      '2025-01-15',
    );
    expect(body).toEqual({ swaption_id: 'swaption-uuid', as_of: '2025-01-15' });
    expect(body.swaption).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.vol_surface).toBeUndefined();
    expect(body.swaption_model).toBeUndefined();
  });

  // Row 11: snapshot pin forwarded on the by-reference arm.
  it('buildSwaptionPriceBody — saved + snapshot_id forwarded', () => {
    const body = buildSwaptionPriceBody(
      { swaption_id: 'swaption-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' },
      '2025-02-01',
    );
    expect(body).toEqual({ swaption_id: 'swaption-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  // Row 12: unsaved inline envelope passes through verbatim (no by-ref hijack).
  it('buildSwaptionPriceBody — Thin-A envelope (no swaption_id) passes through verbatim', () => {
    const envelope = {
      swaption: { notional: 1 },
      curves: [{ name: 'd' }],
      vol_surface: { kind: 'SwaptionVolSpec' },
      swaption_model: { kind: 'HullWhiteLattice' },
      as_of: '2025-01-15',
    };
    expect(buildSwaptionPriceBody(envelope, '2025-01-15')).toBe(envelope);
  });

  // Row 13: priceSwaption routes the by-reference body through to the orch client.
  it('priceSwaption — saved swaption_id posts the Thin-B body', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 7133.03, delta: 0.5, vega: 100, extras: {} } },
      duration_ms: 12,
    });
    const result = await priceSwaption({ swaption_id: 'swaption-uuid', as_of: '2025-01-15' }, '2025-01-15');

    expect(result.success).toBe(true);
    const sent = orchPriceSwaptionMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent).toEqual({ swaption_id: 'swaption-uuid', as_of: '2025-01-15' });
    expect(result.data?.swaptions[0].npv).toBe(7133.03);
  });

  // Row 14: a stale by-reference vol-surface ref surfaces as not_found (code wins).
  it('swaption_vol_surface_not_found code: not_found category', async () => {
    orchPriceSwaptionMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Vol surface not found.', code: 'swaption_vol_surface_not_found' },
      httpStatus: 404,
      duration_ms: 10,
    });
    const result = await priceSwaption({ swaption_id: 'swaption-uuid', as_of: '2025-01-15' }, '2025-01-15');
    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });
});
