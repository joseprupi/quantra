import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Hoisted mocks (must precede all imports)

const {
  authMock,
  getIdTokenMock,
  orchPriceCdsMock,
} = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
    orchPriceCdsMock: vi.fn(),
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));
vi.mock('./orchestrator', () => ({ priceCds: orchPriceCdsMock }));

import { buildCdsPriceBody, mapCdsOrchResult, priceCds } from './cdsPricingService';

// 8-row unit matrix

describe('cdsPricingService — 9-row unit matrix', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Row 1: orchestrator 200 success — wrapped shape; rename protection_leg_npv → default_leg_npv
  it('orchestrator 200 success — wrapped shape; default_leg_npv from protection_leg_npv (D86)', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: -25000.0,
          fair_spread: 0.0105,
          fair_upfront: 0.001,
          protection_leg_npv: 30000.0,
          premium_leg_npv: -55000.0,
          extras: {},
        },
      },
      duration_ms: 18,
    });

    const result = await priceCds({ cds: 'data' }, '2026-01-15');

    expect(orchPriceCdsMock).toHaveBeenCalledOnce();
    expect(result.success).toBe(true);
    expect(result.data?.cds_list[0].npv).toBe(-25000.0);
    expect(result.data?.cds_list[0].fair_spread).toBe(0.0105);
    expect(result.data?.cds_list[0].default_leg_npv).toBe(30000.0);
    expect(result.data?.cds_list[0].premium_leg_npv).toBe(-55000.0);
    expect(result.duration_ms).toBe(18);
  });

  // Row 2: 422 cds_credit_curve_resolution_failed → validation category
  // Intentionally supply code-first route: code wins over httpStatus alone
  it('422 cds_credit_curve_resolution_failed — validation category (code wins, inv. 9)', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: false,
      envelope: {
        error: 'Credit curve resolution failed.',
        code: 'cds_credit_curve_resolution_failed',
      },
      httpStatus: 422,
      duration_ms: 5,
    });

    const result = await priceCds({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('validation');
  });

  // Row 3: 502 engine_unavailable → unavailable category
  it('502 engine_unavailable — unavailable category', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Engine is down.', code: 'engine_unavailable' },
      httpStatus: 502,
      duration_ms: 5,
    });

    const result = await priceCds({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('unavailable');
  });

  // Row 4: network_error → network category
  it('network_error — network category', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'Network failed.', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 0,
    });

    const result = await priceCds({}, '2026-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('network');
  });

  // Row 5: mapCdsOrchResult — rename: protection_leg_npv → default_leg_npv (top-level)
  it('mapCdsOrchResult: D86 rename — protection_leg_npv maps to default_leg_npv', () => {
    const mapped = mapCdsOrchResult({
      npv: -25000,
      fair_spread: 0.01,
      fair_upfront: 0.001,
      protection_leg_npv: 30000,
      premium_leg_npv: -55000,
      extras: {},
    });

    expect(mapped.npv).toBe(-25000);
    expect(mapped.fair_spread).toBe(0.01);
    expect(mapped.fair_upfront).toBe(0.001);
    expect(mapped.default_leg_npv).toBe(30000);
    expect(mapped.premium_leg_npv).toBe(-55000);
  });

  // Row 6: mapCdsOrchResult — extras fallback: default_leg_npv from extras when protection_leg_npv is null
  it('mapCdsOrchResult: D86 extras fallback — default_leg_npv from extras when protection_leg_npv is null', () => {
    const mapped = mapCdsOrchResult({
      npv: 0,
      fair_spread: 0,
      fair_upfront: 0,
      protection_leg_npv: null,
      premium_leg_npv: 0,
      extras: { default_leg_npv: 42000.0 },
    });

    expect(mapped.default_leg_npv).toBe(42000.0);
  });

  // Row 7: mapCdsOrchResult — null fields, null extras → all fields undefined, no throw
  it('mapCdsOrchResult: null fields + null extras — all fields undefined, no throw', () => {
    const mapped = mapCdsOrchResult({
      npv: null,
      fair_spread: null,
      fair_upfront: null,
      protection_leg_npv: null,
      premium_leg_npv: null,
      extras: null as unknown as undefined,
    });

    expect(mapped.npv).toBeUndefined();
    expect(mapped.fair_spread).toBeUndefined();
    expect(mapped.fair_upfront).toBeUndefined();
    expect(mapped.default_leg_npv).toBeUndefined();
    expect(mapped.premium_leg_npv).toBeUndefined();
  });

  // Row 8: inline POST body — CDS envelope passes through verbatim:
  // flat trade body under ``cds``, top-level ``curves`` (role-tagged via
  // ``body.role``), top-level ``credit_curve`` carrying inline
  // ``flat_hazard_rate`` or ``points`` (no quote_id), no ``pricing.*``
  // nesting, no ``cds_list`` wrapper, no ``upfront_date``.
  it('thin-A envelope: flat cds + top-level curves + top-level credit_curve forwarded verbatim', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: 86977.75,
          fair_spread: 0.0119,
          fair_upfront: null,
          protection_leg_npv: 100000.0,
          premium_leg_npv: -13022.25,
          extras: {},
        },
      },
      duration_ms: 7,
    });

    const thinABody = {
      cds: {
        side: 'Buyer',
        notional: 10_000_000.0,
        running_coupon: 0.01,
        day_counter: 'Actual360',
        business_day_convention: 'Following',
        schedule: {
          effective_date: '2025-01-15',
          termination_date: '2030-01-15',
          calendar: 'TARGET',
          frequency: 'Quarterly',
          convention: 'Following',
          termination_date_convention: 'Unadjusted',
          date_generation_rule: 'TwentiethIMM',
          end_of_month: false,
        },
      },
      curves: [
        {
          name: 'discount',
          day_counter: 'Actual365Fixed',
          reference_date: '2025-01-15',
          body: { role: 'discount' },
          points: [
            {
              point_type: 'DepositHelper',
              point: {
                tenor: { n: 1, unit: 'Years' },
                quote_id: 'EUR.IRS.1Y',
                fixing_days: 2,
                calendar: 'TARGET',
                business_day_convention: 'ModifiedFollowing',
                day_counter: 'Actual365Fixed',
              },
            },
          ],
        },
      ],
      credit_curve: {
        name: 'ACME-SR',
        source: 'flat',
        recovery_rate: 0.4,
        body: { flat_hazard_rate: 0.02 },
      },
      as_of: '2025-01-15',
    };

    await priceCds(thinABody, '2025-01-15');

    expect(orchPriceCdsMock).toHaveBeenCalledOnce();
    const posted = orchPriceCdsMock.mock.calls[0][0] as Record<string, unknown>;

    // Flat trade body — no nesting under cds.cds_list[0].cds.*
    expect(posted.cds).toEqual({
      side: 'Buyer',
      notional: 10_000_000.0,
      running_coupon: 0.01,
      day_counter: 'Actual360',
      business_day_convention: 'Following',
      schedule: {
        effective_date: '2025-01-15',
        termination_date: '2030-01-15',
        calendar: 'TARGET',
        frequency: 'Quarterly',
        convention: 'Following',
        termination_date_convention: 'Unadjusted',
        date_generation_rule: 'TwentiethIMM',
        end_of_month: false,
      },
    });

    // upfront_date MUST NOT appear unless the user supplied an upfront.
    expect((posted.cds as Record<string, unknown>).upfront_date).toBeUndefined();
    expect((posted.cds as Record<string, unknown>).upfront).toBeUndefined();

    // Curves lifted to top level; no ``pricing.curves`` nesting on the wire.
    expect(Array.isArray(posted.curves)).toBe(true);
    expect((posted as { pricing?: unknown }).pricing).toBeUndefined();
    expect((posted.cds as Record<string, unknown>).pricing).toBeUndefined();
    expect((posted.cds as Record<string, unknown>).cds_list).toBeUndefined();

    // No legacy ``cds_list`` wrapper anywhere in the wire body.
    expect((posted as { cds_list?: unknown }).cds_list).toBeUndefined();

    // ``as_of`` at the top level (inline envelope).
    expect(posted.as_of).toBe('2025-01-15');

    // Discount curve carries ``body.role``; its point keeps the unresolved
    // quote_id (the backend substitutes).
    const curve = (posted.curves as Array<Record<string, unknown>>)[0];
    expect((curve.body as Record<string, unknown>).role).toBe('discount');
    const points = curve.points as Array<{ point: Record<string, unknown> }>;
    expect(points[0].point.quote_id).toBe('EUR.IRS.1Y');
    expect(points[0].point.rate).toBeUndefined();

    // ``credit_curve`` at the top level (sibling of ``cds``/``curves``):
    // no ``quote_id`` on credit-curve points; inline literal hazard input.
    const cc = posted.credit_curve as Record<string, unknown>;
    expect(cc).toBeDefined();
    expect(cc.recovery_rate).toBe(0.4);
    expect((cc.body as Record<string, unknown>).flat_hazard_rate).toBe(0.02);
  });

  // Row 9: mapCdsOrchResult — top-level fields populated, extras empty
  it('mapCdsOrchResult: all top-level fields populated; extras empty', () => {
    const mapped = mapCdsOrchResult({
      npv: -10000,
      fair_spread: 0.009,
      fair_upfront: 0.0005,
      protection_leg_npv: 15000,
      premium_leg_npv: -25000,
      extras: {},
    });

    expect(mapped.npv).toBe(-10000);
    expect(mapped.fair_spread).toBe(0.009);
    expect(mapped.fair_upfront).toBe(0.0005);
    expect(mapped.default_leg_npv).toBe(15000);
    expect(mapped.premium_leg_npv).toBe(-25000);
  });
});

// buildCdsPriceBody — inline ⊕ by-reference arm selection

describe('buildCdsPriceBody — arm selection', () => {
  it('Thin-B: a saved cds_id → minimal {cds_id, as_of}; no inline cds/curves/credit_curve', () => {
    const body = buildCdsPriceBody(
      {
        cds_id: 'cds-uuid',
        as_of: '2025-01-15',
        // These would be present on a fat localStorage record but must NOT
        // leak onto the by-reference wire body.
        cds: { notional: 1 },
        curves: [{ name: 'd' }],
        credit_curve: { recovery_rate: 0.4 },
      },
      '2099-01-01',
    );
    // ``as_of`` from the request wins over the fallback arg.
    expect(body).toEqual({ cds_id: 'cds-uuid', as_of: '2025-01-15' });
  });

  it('Thin-B: snapshot_id forwarded (D40); stray inline fields never leak onto the wire', () => {
    const body = buildCdsPriceBody(
      // credit_curve_id / curves on the input are NOT forwarded — the cds
      // by-reference body is strictly by-reference ({cds_id, as_of, snapshot_id?}).
      { cds_id: 'cds-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1', credit_curve_id: 'cc-2' },
      '2025-01-15',
    );
    expect(body).toEqual({
      cds_id: 'cds-uuid',
      as_of: '2025-01-15',
      snapshot_id: 'snap-1',
    });
  });

  it('Thin-B: falls back to the asOf arg when the request omits as_of', () => {
    const body = buildCdsPriceBody({ cds_id: 'cds-uuid' }, '2026-03-03');
    expect(body).toEqual({ cds_id: 'cds-uuid', as_of: '2026-03-03' });
  });

  it('Thin-A: a ready inline envelope (cds + curves + credit_curve + as_of) passes through verbatim', () => {
    const envelope = {
      cds: { side: 'Buyer', notional: 10 },
      curves: [{ name: 'discount', body: { role: 'discount' } }],
      credit_curve: { recovery_rate: 0.4, body: { flat_hazard_rate: 0.02 } },
      as_of: '2025-01-15',
    };
    const body = buildCdsPriceBody(envelope, '2025-01-15');
    expect(body).toBe(envelope);
    expect((body as Record<string, unknown>).cds_id).toBeUndefined();
  });

  it('Thin-A: a bare fat body (no envelope markers) is wrapped as { cds, as_of }', () => {
    const fat = { side: 'Buyer', notional: 10 };
    const body = buildCdsPriceBody(fat, '2025-01-15');
    expect(body).toEqual({ cds: fat, as_of: '2025-01-15' });
  });
});

// priceCds — by-reference round-trip

describe('cdsPricingService — Thin-B by-reference', () => {
  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('posts the minimal {cds_id, as_of} body and maps the result unchanged', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: true,
      data: {
        pricing_history_id: null,
        assembled_request: {},
        result: {
          npv: -25000.0,
          fair_spread: 0.0105,
          fair_upfront: 0.001,
          protection_leg_npv: 30000.0,
          premium_leg_npv: -55000.0,
          extras: {},
        },
      },
      duration_ms: 12,
    });

    const result = await priceCds({ cds_id: 'cds-uuid', as_of: '2025-01-15' }, '2025-01-15');

    expect(orchPriceCdsMock).toHaveBeenCalledOnce();
    const posted = orchPriceCdsMock.mock.calls[0][0] as Record<string, unknown>;
    expect(posted).toEqual({ cds_id: 'cds-uuid', as_of: '2025-01-15' });
    expect(posted.cds).toBeUndefined();
    expect(posted.curves).toBeUndefined();
    expect(posted.credit_curve).toBeUndefined();

    expect(result.success).toBe(true);
    expect(result.data?.cds_list[0].npv).toBe(-25000.0);
    expect(result.data?.cds_list[0].fair_spread).toBe(0.0105);
    expect(result.data?.cds_list[0].default_leg_npv).toBe(30000.0);
  });

  it('cds_not_found 404 on a stale by-ref id → not_found category (invariant 9)', async () => {
    orchPriceCdsMock.mockResolvedValueOnce({
      ok: false,
      envelope: { error: 'CDS not found.', code: 'cds_not_found' },
      httpStatus: 404,
      duration_ms: 4,
    });

    const result = await priceCds({ cds_id: 'missing', as_of: '2025-01-15' }, '2025-01-15');

    expect(result.success).toBe(false);
    expect(result.errorInfo?.category).toBe('not_found');
  });
});
