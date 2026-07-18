import { describe, expect, it, vi } from 'vitest';

import {
  persistSwapsInflationGraph,
  buildSwapsInflationPriceArm,
  asSwapsInflationAppGraph,
  type SwapsInflationCrudClients,
  type SwapsInflationAppGraph,
} from './swapsInflationSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}

function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

interface MockClients extends SwapsInflationCrudClients {
  calls: Array<{ entity: string; op: string; id?: string; body: unknown }>;
}

function makeClients(overrides?: {
  curveCreate?: () => OrchestratorResult<{ id: string }>;
  indexCreate?: () => OrchestratorResult<{ id: string }>;
  swapCreate?: () => OrchestratorResult<{ id: string }>;
}): MockClients {
  const calls: MockClients['calls'] = [];
  let curveSeq = 0;
  const curveCreate =
    overrides?.curveCreate ?? (() => ok({ id: `curve-uuid-${++curveSeq}` }));
  const indexCreate = overrides?.indexCreate ?? (() => ok({ id: 'index-uuid' }));
  const swapCreate = overrides?.swapCreate ?? (() => ok({ id: 'swaps-inflation-uuid' }));
  return {
    calls,
    curves: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'curve', op: 'create', body });
        return curveCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'curve', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    indices: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'index', op: 'create', body });
        return indexCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'index', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    swapsInflation: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'swaps_inflation', op: 'create', body });
        return swapCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'swaps_inflation', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
  };
}

const baseInput = {
  name: 'ZC Payer EUHICP',
  index: {
    name: 'EU HICP',
    kind: 'Inflation',
    currency: 'EUR',
    day_counter: 'Actual365Fixed',
    body: {
      family_name: 'EU HICP',
      frequency: 'Monthly',
      availability_lag: { n: 2, unit: 'Months' },
      observation_lag: { n: 3, unit: 'Months' },
      fixings: [{ date: '2024-12-01', value: 100.4 }],
    },
  },
  swapKind: 'zero_coupon' as const,
  swapsInflationRequest: {
    swaps: [
      {
        zero_coupon_inflation_swap: {
          swap_type: 'Payer',
          notional: 1_000_000.0,
          start_date: '2025-01-15',
          maturity_date: '2030-01-15',
          fixed_rate: 0.0217,
          inflation_index_id: 'EUHICP',
        },
      },
    ],
    include_flows: true,
  },
};

// the nominal curve MUST span the trade maturity (5Y here) — deposit(1Y)
// + swaps to 10Y. A too-short nominal curve ABORTs the engine "past max curve
// time"; the save flow persists whatever span the page supplies.
function twoCurves() {
  return [
    {
      key: 'nominal',
      body: {
        name: 'nominal',
        currency: 'EUR',
        points: [
          { point_type: 'DepositHelper', point: { tenor: { n: 1, unit: 'Years' }, quote_id: 'EUR.IRS.1Y' } },
          { point_type: 'SwapHelper', point: { tenor: { n: 5, unit: 'Years' }, quote_id: 'EUR.IRS.5Y' } },
          { point_type: 'SwapHelper', point: { tenor: { n: 10, unit: 'Years' }, quote_id: 'EUR.IRS.10Y' } },
        ],
        body: { role: 'nominal' },
      },
    },
    {
      key: 'inflation',
      body: {
        name: 'HICP_ZC',
        currency: 'EUR',
        points: [
          { point_type: 'ZeroCouponInflationSwapHelper', point: { tenor: { n: 5, unit: 'Years' }, quote_id: 'EUR.HICP.5Y' } },
        ],
        body: { role: 'inflation', kind: 'ZeroInflation', index_id: 'EUHICP' },
      },
    },
  ];
}

// persistSwapsInflationGraph — fresh save (leaf→root create)

describe('persistSwapsInflationGraph — fresh save', () => {
  it('persists leaf→root (curves → index → swaps_inflation) in order and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistSwapsInflationGraph(
      { ...baseInput, curves: twoCurves() },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;

    // Call order is leaf→root — both curves, then the inflation-specific index
    // leaf, then the swaps_inflation root. NO curve_set (inflation reads
    // pricing.curves directly).
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve:create',
      'index:create',
      'swaps_inflation:create',
    ]);
    expect(clients.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve:create',
      'index:create',
      'swaps_inflation:create',
    ]);

    // Stamped UUIDs — no curveSetId (inflation has no curve_set).
    expect(result.graph).toEqual<SwapsInflationAppGraph>({
      swapsInflationId: 'swaps-inflation-uuid',
      indexId: 'index-uuid',
      curveIds: { nominal: 'curve-uuid-1', inflation: 'curve-uuid-2' },
    });
  });

  it('persists the inflation index with its literal body (parity with Thin-A, D85 fixings)', async () => {
    const clients = makeClients();
    await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    const indexCall = clients.calls.find((c) => c.entity === 'index');
    const indexBody = indexCall?.body as Record<string, unknown>;
    expect(indexBody.kind).toBe('Inflation');
    expect((indexBody.body as Record<string, unknown>).fixings).toBeDefined();
  });

  it('pins pricing.curves (role-tagged list) + inflation_index_id + top-level swap_kind — NOT curve_set_id/discount_curve_id', async () => {
    const clients = makeClients();
    await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    const swapCall = clients.calls.find((c) => c.entity === 'swaps_inflation');
    const body = swapCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.name).toBe('ZC Payer EUHICP');

    // Exactly the refs the swaps_inflation assembler reads (verified against the backend read-path,
    // a role-tagged pricing.curves list, the pricing.inflation_index_id
    // scalar, and the top-level swap_kind discriminator. No curve_set_id
    // (swaption shape) and no discount_curve_id (cds shape).
    const pricing = body.request.pricing as Record<string, unknown>;
    expect(pricing.curves).toEqual([
      { curve_id: 'curve-uuid-1', role: 'nominal' },
      { curve_id: 'curve-uuid-2', role: 'inflation' },
    ]);
    expect(pricing.inflation_index_id).toBe('index-uuid');
    expect(pricing.curve_set_id).toBeUndefined();
    expect(pricing.discount_curve_id).toBeUndefined();

    // swap_kind is persisted at the request ROOT (the backend reads it off
    // swap_request.swap_kind), not under pricing.
    expect(body.request.swap_kind).toBe('zero_coupon');
    expect(pricing.swap_kind).toBeUndefined();

    // Flat trade levers preserved alongside the pinned refs (engine_io reads
    // these off the swaps_inflation root).
    expect(Array.isArray(body.request.swaps)).toBe(true);
    expect(body.request.include_flows).toBe(true);

    // the persisted nominal curve spans past the 5Y maturity (max tenor
    // in the nominal curve points is ≥ 5Y) — forwarded verbatim from input.
    const nominalCurveCall = clients.calls.find((c) => c.entity === 'curve');
    const nominalBody = nominalCurveCall?.body as { points: Array<Record<string, unknown>> };
    const maxNominalTenor = nominalBody.points.reduce((max, wrap) => {
      const t = (wrap.point as Record<string, unknown>).tenor as { n: number; unit: string } | undefined;
      if (!t || t.unit !== 'Years') return max;
      return Math.max(max, t.n);
    }, 0);
    expect(maxNominalTenor).toBeGreaterThanOrEqual(5);
  });

  it('persists a YYIIS swap_kind faithfully (D90 — both kinds round-trip)', async () => {
    const clients = makeClients();
    await persistSwapsInflationGraph(
      {
        ...baseInput,
        swapKind: 'year_on_year',
        swapsInflationRequest: {
          swaps: [{ year_on_year_inflation_swap: { swap_type: 'Payer', notional: 1_000_000.0 } }],
        },
        curves: twoCurves(),
      },
      null,
      clients,
    );
    const swapCall = clients.calls.find((c) => c.entity === 'swaps_inflation');
    const body = swapCall?.body as { request: Record<string, unknown> };
    expect(body.request.swap_kind).toBe('year_on_year');
  });
});

// persistSwapsInflationGraph — idempotent re-save (PATCH by id)

describe('persistSwapsInflationGraph — idempotent re-save', () => {
  const prior: SwapsInflationAppGraph = {
    swapsInflationId: 'swaps-inflation-uuid',
    indexId: 'index-uuid',
    curveIds: { nominal: 'curve-uuid-1', inflation: 'curve-uuid-2' },
  };

  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const clients = makeClients();
    const result = await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, prior, clients);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch',
      'curve:patch',
      'index:patch',
      'swaps_inflation:patch',
    ]);
    // No create was issued on any client.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.indices.create).not.toHaveBeenCalled();
    expect(clients.swapsInflation.create).not.toHaveBeenCalled();
    // PATCH targets are the prior ids; the returned graph is stable.
    expect(clients.calls.find((c) => c.entity === 'curve' && c.id === 'curve-uuid-1')).toBeDefined();
    expect(clients.calls.find((c) => c.entity === 'index')?.id).toBe('index-uuid');
    expect(clients.calls.find((c) => c.entity === 'swaps_inflation')?.id).toBe('swaps-inflation-uuid');
    expect(result.graph).toEqual(prior);
  });
});

// persistSwapsInflationGraph — error branches (branch on code)

describe('persistSwapsInflationGraph — error branches', () => {
  it('short-circuits on a curve failure and surfaces the error envelope + stage', async () => {
    const clients = makeClients({ curveCreate: () => fail('name_conflict', 409) });
    const result = await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.envelope.code).toBe('name_conflict');
    expect(result.httpStatus).toBe(409);
    expect(result.stage).toEqual({ entity: 'curve', op: 'create', key: 'nominal' });
    // Downstream entities were never touched.
    expect(clients.indices.create).not.toHaveBeenCalled();
    expect(clients.swapsInflation.create).not.toHaveBeenCalled();
  });

  it('short-circuits on an index failure (the inflation-specific leaf)', async () => {
    const clients = makeClients({ indexCreate: () => fail('validation_error', 422) });
    const result = await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('index');
    // The swaps_inflation (root) was never created.
    expect(clients.swapsInflation.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a swaps_inflation failure', async () => {
    const clients = makeClients({ swapCreate: () => fail('unauthenticated', 401) });
    const result = await persistSwapsInflationGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('swaps_inflation');
    expect(result.envelope.code).toBe('unauthenticated');
  });
});

// buildSwapsInflationPriceArm — price-arm selection

describe('buildSwapsInflationPriceArm — price-arm selection', () => {
  const graph: SwapsInflationAppGraph = {
    swapsInflationId: 'swaps-inflation-uuid',
    indexId: 'index-uuid',
    curveIds: { nominal: 'c1', inflation: 'c2' },
  };

  it('saved → Thin-B by-reference body {swap_id, as_of}, no inline', () => {
    const body = buildSwapsInflationPriceArm({
      appGraph: graph,
      inlineRequest: { swap: { swaps: [] }, curves: [], inflation_index: {}, as_of: '2025-01-15' },
      asOf: '2025-01-15',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swap_id: 'swaps-inflation-uuid', as_of: '2025-01-15' });
    expect(body.swap).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.inflation_index).toBeUndefined();
  });

  it('saved + snapshot pin → snapshot_id forwarded', () => {
    const body = buildSwapsInflationPriceArm({
      appGraph: graph,
      inlineRequest: undefined,
      asOf: '2025-01-15',
      snapshotId: 'snap-1',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swap_id: 'swaps-inflation-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  it('unsaved → Thin-A inline request passes through verbatim', () => {
    const inline = { swap: { swaps: [] }, curves: [{ name: 'n' }], inflation_index: {}, as_of: '2025-01-15' };
    const body = buildSwapsInflationPriceArm({ appGraph: null, inlineRequest: inline, asOf: '2025-01-15' });
    expect(body).toBe(inline);
  });
});

// asSwapsInflationAppGraph — narrowing

describe('asSwapsInflationAppGraph', () => {
  it('returns the graph for a valid record', () => {
    expect(
      asSwapsInflationAppGraph({
        swapsInflationId: 'si',
        indexId: 'ix',
        curveIds: { nominal: 'c1', inflation: 'c2', x: 2 },
      }),
    ).toEqual({ swapsInflationId: 'si', indexId: 'ix', curveIds: { nominal: 'c1', inflation: 'c2' } });
  });

  it('returns null for undefined / missing ids / non-object', () => {
    expect(asSwapsInflationAppGraph(undefined)).toBeNull();
    expect(asSwapsInflationAppGraph(null)).toBeNull();
    expect(asSwapsInflationAppGraph({})).toBeNull();
    expect(asSwapsInflationAppGraph({ swapsInflationId: '', indexId: 'ix' })).toBeNull();
    expect(asSwapsInflationAppGraph({ swapsInflationId: 'si' })).toBeNull();
    expect(asSwapsInflationAppGraph('nope')).toBeNull();
  });
});
