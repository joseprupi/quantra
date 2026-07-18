import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  priceBondsFixedMock,
  priceBondsFloatingMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    priceBondsFixedMock: vi.fn(),
    priceBondsFloatingMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({
  priceBondsFixed: priceBondsFixedMock,
  priceBondsFloating: priceBondsFloatingMock,
}));

import {
  buildFixedBondPriceBody,
  buildFloatingBondPriceBody,
  mapFixedBondOrchResult,
  mapFloatingBondOrchResult,
  priceFixedBond,
  priceFloatingBond,
} from './bondPricingService';

// 13-row unit matrix

describe('bondPricingService — 13-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Adapter function tests

  // Row 1: priceFixedBond 200 success → wrapped shape with renames + extras
  it('priceFixedBond — 200 success → wrapped shape', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: true,
      data: {
        assembled_request: {},
        result: {
          npv: 98.5,
          clean_price: 98.0,
          dirty_price: 98.5,
          accrued: 0.5,
          yield_to_maturity: 0.045,
          cashflows: [],
          extras: { macaulay_duration: 4.2, modified_duration: 4.0 },
        },
      },
      duration_ms: 18,
    });

    const result = await priceFixedBond({ bond: 'data' }, '2026-01-15');

    expect(priceBondsFixedMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.bonds[0].npv).toBe(98.5);
    expect(result.data?.bonds[0].accrued_amount).toBe(0.5);
    expect(result.data?.bonds[0].yield).toBe(0.045);
    expect(result.data?.bonds[0].macaulay_duration).toBe(4.2);
    expect(result.duration_ms).toBe(18);
  });

  // Row 2: priceFixedBond 404 bond_fixed_not_found — code drives category
  // Intentionally supply httpStatus 500 (would give 'server') to prove code branch fires first
  it('priceFixedBond — 404 bond_fixed_not_found: not_found category (code wins over httpStatus)', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Bond not found.', code: 'bond_fixed_not_found' },
      httpStatus: 500,
      duration_ms: 5,
    });

    const result = await priceFixedBond({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 3: priceFixedBond 502 engine_unavailable → unavailable category
  it('priceFixedBond — 502 engine_unavailable: unavailable category', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 5,
    });

    const result = await priceFixedBond({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 4: priceFixedBond network_error → network category
  it('priceFixedBond — network error: network category', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceFixedBond({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 5: priceFloatingBond 200 success → wrapped shape; yield from extras
  it('priceFloatingBond — 200 success → wrapped shape', async () => {
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: true,
      data: {
        assembled_request: {},
        result: {
          npv: 99.1,
          clean_price: 98.8,
          dirty_price: 99.1,
          accrued: 0.3,
          cashflows: [],
          extras: { yield: 0.038, macaulay_duration: 3.5 },
        },
      },
      duration_ms: 22,
    });

    const result = await priceFloatingBond({ bond: 'data' }, '2026-01-15');

    expect(priceBondsFloatingMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.bonds[0].npv).toBe(99.1);
    expect(result.data?.bonds[0].accrued_amount).toBe(0.3);
    expect(result.data?.bonds[0].yield).toBe(0.038);  // from extras
    expect(result.data?.bonds[0].macaulay_duration).toBe(3.5);
    expect(result.duration_ms).toBe(22);
  });

  // Row 6: priceFloatingBond 404 bond_floating_not_found — code drives category
  it('priceFloatingBond — 404 bond_floating_not_found: not_found category (code wins over httpStatus)', async () => {
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Floating bond not found.', code: 'bond_floating_not_found' },
      httpStatus: 500,
      duration_ms: 5,
    });

    const result = await priceFloatingBond({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });

  // Row 7: priceFloatingBond 502 engine_unavailable → unavailable category
  it('priceFloatingBond — 502 engine_unavailable: unavailable category', async () => {
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 5,
    });

    const result = await priceFloatingBond({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Mapper unit tests

  // Row 8: mapFixedBondOrchResult — renames applied
  it('mapFixedBondOrchResult — renames applied', () => {
    const mapped = mapFixedBondOrchResult({
      npv: 100,
      clean_price: 99,
      dirty_price: 100,
      accrued: 1.0,
      yield_to_maturity: 0.05,
      cashflows: [{ type: 'coupon' }],
      extras: {},
    });

    expect(mapped.accrued_amount).toBe(1.0);
    expect(mapped.yield).toBe(0.05);
    expect(mapped.flows).toEqual([{ type: 'coupon' }]);
    expect(mapped.clean_price).toBe(99);
  });

  // Row 9: mapFixedBondOrchResult — extras fields populated
  it('mapFixedBondOrchResult — extras fields', () => {
    const mapped = mapFixedBondOrchResult({
      extras: {
        macaulay_duration: 4.5,
        modified_duration: 4.2,
        convexity: 0.2,
        bps: 0.001,
        accrued_days: 30,
      },
    });

    expect(mapped.macaulay_duration).toBe(4.5);
    expect(mapped.modified_duration).toBe(4.2);
    expect(mapped.convexity).toBe(0.2);
    expect(mapped.bps).toBe(0.001);
    expect(mapped.accrued_days).toBe(30);
    expect(mapped.flows).toBeUndefined();  // cashflows not provided
  });

  // Row 10: mapFixedBondOrchResult — null/missing top-level → all undefined, no throw
  it('mapFixedBondOrchResult — null/missing top-level → all fields undefined, no throw', () => {
    const mapped = mapFixedBondOrchResult({
      npv: null,
      clean_price: null,
      dirty_price: null,
      accrued: null,
      yield_to_maturity: null,
      extras: undefined,
    });

    expect(mapped.npv).toBeUndefined();
    expect(mapped.clean_price).toBeUndefined();
    expect(mapped.dirty_price).toBeUndefined();
    expect(mapped.accrued_amount).toBeUndefined();
    expect(mapped.yield).toBeUndefined();
    expect(mapped.flows).toBeUndefined();
    expect(mapped.macaulay_duration).toBeUndefined();
    expect(mapped.modified_duration).toBeUndefined();
    expect(mapped.convexity).toBeUndefined();
    expect(mapped.bps).toBeUndefined();
    expect(mapped.accrued_days).toBeUndefined();
  });

  // Row 11: mapFloatingBondOrchResult — yield from extras
  it('mapFloatingBondOrchResult — yield from extras', () => {
    const mapped = mapFloatingBondOrchResult({
      npv: 95.0,
      accrued: 0.2,
      cashflows: undefined,
      extras: { yield: 0.042 },
    });

    expect(mapped.accrued_amount).toBe(0.2);
    expect(mapped.yield).toBe(0.042);  // from extras
    expect(mapped.flows).toBeUndefined();
  });

  // Row 12: mapFloatingBondOrchResult — forecast_rates not in output.
  // `forecast_rates` is no longer part of the FloatingBondResult schema (the
  // backend dropped it), so the literal is widened: the mapper must still
  // ignore unknown legacy fields arriving on the wire.
  it('mapFloatingBondOrchResult — forecast_rates not in output', () => {
    const legacyWire = {
      npv: 90.0,
      forecast_rates: [{ date: '2025-01-01', rate: 0.04 }],
      extras: {},
    } as Parameters<typeof mapFloatingBondOrchResult>[0];
    const mapped = mapFloatingBondOrchResult(legacyWire);

    expect('forecast_rates' in mapped).toBe(false);
  });

  // Row 13: mapFloatingBondOrchResult — null extras → all extras-derived fields undefined, no throw
  it('mapFloatingBondOrchResult — null extras → extras-derived fields undefined, no throw', () => {
    const mapped = mapFloatingBondOrchResult({
      npv: 85.0,
      extras: undefined,
    });

    expect(mapped.npv).toBe(85.0);
    expect(mapped.yield).toBeUndefined();
    expect(mapped.macaulay_duration).toBeUndefined();
    expect(mapped.modified_duration).toBeUndefined();
    expect(mapped.convexity).toBeUndefined();
    expect(mapped.bps).toBeUndefined();
    expect(mapped.accrued_days).toBeUndefined();
  });

  // inline pass-through tests

  // Row 14: priceFixedBond — inline envelope is forwarded verbatim (flat bond
  // body + role-tagged top-level curves + as_of), not re-wrapped under `bond`.
  it('priceFixedBond — Thin-A envelope passed through verbatim', async () => {
    const thinAEnvelope = {
      bond: {
        face_amount: 100,
        coupon_rate: 0.045,
        settlement_days: 2,
        redemption: 100,
        issue_date: '2024-01-15',
        effective_date: '2024-01-15',
        termination_date: '2029-01-15',
      },
      curves: [{
        name: 'discount',
        points: [{ point_type: 'DepositHelper', point: { quote_id: 'EUR.3M', tenor: { n: 3, unit: 'Months' } } }],
        body: { role: 'discount' },
      }],
      as_of: '2025-01-15',
    };
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 107.43, clean_price: 107.42, extras: {} } },
      duration_ms: 14,
    });

    await priceFixedBond(thinAEnvelope, '2025-01-15');

    expect(priceBondsFixedMock).toHaveBeenCalledOnce();
    const sent = priceBondsFixedMock.mock.calls[0][0] as Record<string, unknown>;
    const sentBond = sent.bond as Record<string, unknown>;
    const sentCurves = sent.curves as Array<Record<string, unknown>>;
    // No double-wrap: top-level `bond`/`curves`/`as_of` match the envelope.
    expect(sent.as_of).toBe('2025-01-15');
    expect(sentBond.coupon_rate).toBe(0.045);  // canonical key
    expect(sentBond.face_amount).toBe(100);
    // Trap guard: flat trade body, no nested fixed_rate_bond wrapper.
    expect(sentBond.fixed_rate_bond).toBeUndefined();
    expect(sentBond.pricing).toBeUndefined();
    expect(Array.isArray(sentCurves)).toBe(true);
    expect((sentCurves[0].body as Record<string, unknown>).role).toBe('discount');
    // quote_id forwarded unresolved (no client-side numeric substitution).
    const firstPointWrap = (sentCurves[0].points as Array<Record<string, unknown>>)[0];
    const firstPoint = firstPointWrap.point as Record<string, unknown>;
    expect(firstPoint.quote_id).toBe('EUR.3M');
    expect(firstPoint.value).toBeUndefined();
  });

  // Row 15: priceFloatingBond — inline envelope (with top-level index) is
  // forwarded verbatim; trade body flat; curves role-tagged disc + projection.
  it('priceFloatingBond — Thin-A envelope passed through verbatim', async () => {
    const thinAEnvelope = {
      bond: {
        face_amount: 100,
        spread: 0.0025,
        fixing_days: 2,
        in_arrears: false,
        settlement_days: 2,
        redemption: 100,
        issue_date: '2025-01-17',
        effective_date: '2025-01-17',
        termination_date: '2030-01-17',
      },
      curves: [
        {
          name: 'discount',
          points: [{ point_type: 'DepositHelper', point: { quote_id: 'EUR.3M' } }],
          body: { role: 'discount' },
        },
        {
          name: 'projection',
          points: [{ point_type: 'DepositHelper', point: { quote_id: 'EUR.6M' } }],
          body: { role: 'projection' },
        },
      ],
      index: {
        name: 'EURIBOR_6M',
        kind: 'IborIndex',
        currency: 'EUR',
        calendar: 'TARGET',
        day_counter: 'Actual360',
        body: { fixingDays: 2, tenor: { n: 6, unit: 'Months' } },
      },
      as_of: '2025-01-15',
    };
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 101.15, clean_price: 101.17, extras: {} } },
      duration_ms: 16,
    });

    await priceFloatingBond(thinAEnvelope, '2025-01-15');

    expect(priceBondsFloatingMock).toHaveBeenCalledOnce();
    const sent = priceBondsFloatingMock.mock.calls[0][0] as Record<string, unknown>;
    const sentBond = sent.bond as Record<string, unknown>;
    const sentCurves = sent.curves as Array<Record<string, unknown>>;
    const sentIndex = sent.index as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    expect(sentBond.spread).toBe(0.0025);
    // Trap guard: flat trade body, no nested floating_rate_bond wrapper
    // and no per-trade index.id (the registry-union fix moves the
    // resolved id off the trade body; portal sends inline index as sibling).
    expect(sentBond.floating_rate_bond).toBeUndefined();
    expect(sentBond.pricing).toBeUndefined();
    expect(sentBond.index).toBeUndefined();
    expect((sentCurves[0].body as Record<string, unknown>).role).toBe('discount');
    expect((sentCurves[1].body as Record<string, unknown>).role).toBe('projection');
    expect(sentIndex.kind).toBe('IborIndex');
    expect((sentIndex.body as Record<string, unknown>).fixingDays).toBe(2);
    const firstPointWrap = (sentCurves[0].points as Array<Record<string, unknown>>)[0];
    expect((firstPointWrap.point as Record<string, unknown>).quote_id).toBe('EUR.3M');
  });

  // Row 16: priceFixedBond — non-envelope (legacy fat) input is wrapped under
  // `bond` for backward compatibility with any unmigrated call sites.
  it('priceFixedBond — legacy fat body wrapped under `bond` (back-compat)', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 100, extras: {} } },
      duration_ms: 5,
    });
    const legacy = { pricing: { as_of_date: '2025-01-15' }, bonds: [{ fixed_rate_bond: {} }] };
    await priceFixedBond(legacy, '2025-01-15');

    const sent = priceBondsFixedMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    // Wrapped, not flattened — preserves the legacy save-flow shape verbatim.
    expect(sent.bond).toBe(legacy);
  });

  // Row 17: priceFloatingBond — non-envelope legacy body is wrapped likewise.
  it('priceFloatingBond — legacy fat body wrapped under `bond` (back-compat)', async () => {
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 100, extras: {} } },
      duration_ms: 5,
    });
    const legacy = { pricing: { as_of_date: '2025-01-15' }, bonds: [{ floating_rate_bond: {} }] };
    await priceFloatingBond(legacy, '2025-01-15');

    const sent = priceBondsFloatingMock.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.as_of).toBe('2025-01-15');
    expect(sent.bond).toBe(legacy);
  });
});

// by-reference arm

describe('buildFixedBondPriceBody / buildFloatingBondPriceBody — arm selection', () => {
  it('fixed Thin-B: saved bond_id → minimal {bond_id, as_of}; no inline leaks', () => {
    const body = buildFixedBondPriceBody(
      { bond_id: 'bf-uuid', as_of: '2025-01-15', bond: { face_amount: 100 }, curves: [{ name: 'd' }] },
      '2099-01-01',
    );
    expect(body).toEqual({ bond_id: 'bf-uuid', as_of: '2025-01-15' });
  });

  it('fixed Thin-B: snapshot_id forwarded; falls back to asOf arg', () => {
    expect(buildFixedBondPriceBody({ bond_id: 'bf', snapshot_id: 'snap' }, '2026-03-03'))
      .toEqual({ bond_id: 'bf', as_of: '2026-03-03', snapshot_id: 'snap' });
  });

  it('fixed Thin-A: ready envelope verbatim; bare fat body wrapped', () => {
    const env = { bond: { face_amount: 100 }, curves: [{ name: 'd' }], as_of: '2025-01-15' };
    expect(buildFixedBondPriceBody(env, '2025-01-15')).toBe(env);
    const fat = { face_amount: 100 };
    expect(buildFixedBondPriceBody(fat, '2025-01-15')).toEqual({ bond: fat, as_of: '2025-01-15' });
  });

  it('floating Thin-B: saved bond_id → minimal {bond_id, as_of}; index/curves never leak', () => {
    const body = buildFloatingBondPriceBody(
      { bond_id: 'bn-uuid', as_of: '2025-01-15', bond: {}, curves: [], index: { name: 'EURIBOR_6M' } },
      '2099-01-01',
    );
    expect(body).toEqual({ bond_id: 'bn-uuid', as_of: '2025-01-15' });
  });

  it('floating Thin-A: ready envelope (with index) verbatim; bare fat wrapped', () => {
    const env = { bond: {}, curves: [{ name: 'd' }], index: { name: 'EURIBOR_6M' }, as_of: '2025-01-15' };
    expect(buildFloatingBondPriceBody(env, '2025-01-15')).toBe(env);
    const fat = { face_amount: 100 };
    expect(buildFloatingBondPriceBody(fat, '2025-01-15')).toEqual({ bond: fat, as_of: '2025-01-15' });
  });
});

describe('bondPricingService — Thin-B by-reference round-trip', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('priceFixedBond posts {bond_id, as_of} and maps the result', async () => {
    priceBondsFixedMock.mockResolvedValueOnce({
      ok: true,
      data: { assembled_request: {}, result: { npv: 107, clean_price: 107, extras: {} } },
      duration_ms: 9,
    });
    const result = await priceFixedBond({ bond_id: 'bf-uuid', as_of: '2025-01-15' }, '2025-01-15');
    const posted = priceBondsFixedMock.mock.calls[0][0] as Record<string, unknown>;
    expect(posted).toEqual({ bond_id: 'bf-uuid', as_of: '2025-01-15' });
    expect(posted.bond).toBeUndefined();
    expect(result.success).toBe(true);
    expect(result.data?.bonds[0].clean_price).toBe(107);
  });

  it('priceFloatingBond posts {bond_id, as_of}; bond_floating_not_found → not_found', async () => {
    priceBondsFloatingMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Bond not found.', code: 'bond_floating_not_found' },
      httpStatus: 404,
      duration_ms: 3,
    });
    const result = await priceFloatingBond({ bond_id: 'missing', as_of: '2025-01-15' }, '2025-01-15');
    const posted = priceBondsFloatingMock.mock.calls[0][0] as Record<string, unknown>;
    expect(posted).toEqual({ bond_id: 'missing', as_of: '2025-01-15' });
    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });
});
