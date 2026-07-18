import { describe, expect, it, vi } from 'vitest';

import {
  persistSwaptionGraph,
  buildSwaptionPriceArm,
  asSwaptionAppGraph,
  type SwaptionCrudClients,
  type SwaptionAppGraph,
} from './swaptionSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}

function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

interface MockClients extends SwaptionCrudClients {
  calls: Array<{ entity: string; op: string; id?: string; body: unknown }>;
}

function makeClients(overrides?: {
  curveCreate?: () => OrchestratorResult<{ id: string }>;
  curveSetCreate?: () => OrchestratorResult<{ id: string }>;
  volSurfaceCreate?: () => OrchestratorResult<{ id: string }>;
  swaptionModelCreate?: () => OrchestratorResult<{ id: string }>;
  swaptionCreate?: () => OrchestratorResult<{ id: string }>;
}): MockClients {
  const calls: MockClients['calls'] = [];
  let curveSeq = 0;
  const curveCreate =
    overrides?.curveCreate ?? (() => ok({ id: `curve-uuid-${++curveSeq}` }));
  const curveSetCreate = overrides?.curveSetCreate ?? (() => ok({ id: 'curveset-uuid' }));
  const volSurfaceCreate = overrides?.volSurfaceCreate ?? (() => ok({ id: 'volsurface-uuid' }));
  const swaptionModelCreate = overrides?.swaptionModelCreate ?? (() => ok({ id: 'model-uuid' }));
  const swaptionCreate = overrides?.swaptionCreate ?? (() => ok({ id: 'swaption-uuid' }));
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
    curveSets: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'curve_set', op: 'create', body });
        return curveSetCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'curve_set', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    volSurfaces: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'vol_surface', op: 'create', body });
        return volSurfaceCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'vol_surface', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    swaptionModels: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'swaption_model', op: 'create', body });
        return swaptionModelCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'swaption_model', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    swaptions: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'swaption', op: 'create', body });
        return swaptionCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'swaption', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
  };
}

const baseInput = {
  name: 'European Payer EURIBOR Swaption',
  curveSetCurrency: 'EUR',
  volSurface: {
    name: 'swaptvol',
    kind: 'SwaptionVolSpec',
    payload: {
      swap_index_id: 'EUR_SWAP_6M',
      base: { reference_date: '2025-01-15', constant_vol: 0.2, volatility_type: 'Normal', shape: 'Constant' },
    },
  },
  swaptionModel: {
    name: 'swaptmodel',
    kind: 'HullWhiteLattice',
    payload: { param_mode: 'Explicit', hw_a: 0.05, hw_sigma: 0.01, lattice_steps: 60 },
  },
  swaptionRequest: {
    notional: 1_000_000,
    strike: 0.035,
    swap_type: 'Payer',
    exercise_date: '2026-01-15',
    effective_date: '2026-01-17',
    termination_date: '2031-01-17',
    exercise_type: 'European',
    settlement_type: 'Physical',
  },
};

function oneCurve() {
  return [{ key: 'discount', body: { name: 'disc', points: [{ point_type: 'DepositHelper' }], body: { role: 'discount' } } }];
}

function twoCurves() {
  return [
    { key: 'discount', body: { name: 'disc', points: [], body: { role: 'discount' } } },
    { key: 'forwarding', body: { name: 'fwd', points: [], body: { role: 'forwarding' } } },
  ];
}

// persistSwaptionGraph — fresh save (leaf→root create)

describe('persistSwaptionGraph — fresh save', () => {
  it('persists leaf→root (curves → curve_set → vol_surface → swaption_model → swaption) in order and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistSwaptionGraph(
      { ...baseInput, curves: oneCurve() },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;

    // Call order is leaf→root, including the swaption-specific vol_surface +
    // swaption_model leaves.
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'vol_surface:create',
      'swaption_model:create',
      'swaption:create',
    ]);
    expect(clients.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'vol_surface:create',
      'swaption_model:create',
      'swaption:create',
    ]);

    // Stamped UUIDs.
    expect(result.graph).toEqual<SwaptionAppGraph>({
      swaptionId: 'swaption-uuid',
      curveSetId: 'curveset-uuid',
      volSurfaceId: 'volsurface-uuid',
      swaptionModelId: 'model-uuid',
      curveIds: { discount: 'curve-uuid-1' },
    });
  });

  it('curve_set references the persisted curve UUIDs discount-first (D9 + D116 role order)', async () => {
    const clients = makeClients();
    await persistSwaptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    const curveSetCall = clients.calls.find((c) => c.entity === 'curve_set');
    const body = curveSetCall?.body as { body: { curve_refs: Array<Record<string, unknown>> } };
    expect(body.body.curve_refs).toEqual([
      { curve_id: 'curve-uuid-1', role: 'discount' },
      { curve_id: 'curve-uuid-2', role: 'forwarding' },
    ]);
  });

  it('persists the vol_surface + swaption_model with their literal payloads (parity with Thin-A)', async () => {
    const clients = makeClients();
    await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const volCall = clients.calls.find((c) => c.entity === 'vol_surface');
    const volBody = volCall?.body as Record<string, unknown>;
    expect(volBody.kind).toBe('SwaptionVolSpec');
    expect((volBody.payload as Record<string, unknown>).base).toBeDefined();

    const modelCall = clients.calls.find((c) => c.entity === 'swaption_model');
    const modelBody = modelCall?.body as Record<string, unknown>;
    // Production model is HullWhiteLattice, prices off hw_sigma.
    expect(modelBody.kind).toBe('HullWhiteLattice');
    expect((modelBody.payload as Record<string, unknown>).hw_sigma).toBe(0.01);
  });

  it('pins curve_set_id + vol_surface_id + swaption_model_id into swaption.request.pricing', async () => {
    const clients = makeClients();
    await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const swaptionCall = clients.calls.find((c) => c.entity === 'swaption');
    const body = swaptionCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.name).toBe('European Payer EURIBOR Swaption');
    // Exactly the three refs the swaption assembler reads (no discount_curve_id —
    // swaption resolves curves via curve_set_id only).
    expect(body.request.pricing).toEqual({
      curve_set_id: 'curveset-uuid',
      vol_surface_id: 'volsurface-uuid',
      swaption_model_id: 'model-uuid',
    });
    // Flat trade levers are preserved alongside the pinned refs.
    expect(body.request.notional).toBe(1_000_000);
    expect(body.request.strike).toBe(0.035);
    expect(body.request.swap_type).toBe('Payer');
  });
});

// persistSwaptionGraph — idempotent re-save (PATCH by id)

describe('persistSwaptionGraph — idempotent re-save', () => {
  const prior: SwaptionAppGraph = {
    swaptionId: 'swaption-uuid',
    curveSetId: 'curveset-uuid',
    volSurfaceId: 'volsurface-uuid',
    swaptionModelId: 'model-uuid',
    curveIds: { discount: 'curve-uuid-1' },
  };

  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const clients = makeClients();
    const result = await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, prior, clients);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch',
      'curve_set:patch',
      'vol_surface:patch',
      'swaption_model:patch',
      'swaption:patch',
    ]);
    // No create was issued on any client.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.volSurfaces.create).not.toHaveBeenCalled();
    expect(clients.swaptionModels.create).not.toHaveBeenCalled();
    expect(clients.swaptions.create).not.toHaveBeenCalled();
    // PATCH targets are the prior ids; the returned graph is stable.
    expect(clients.calls.find((c) => c.entity === 'curve')?.id).toBe('curve-uuid-1');
    expect(clients.calls.find((c) => c.entity === 'curve_set')?.id).toBe('curveset-uuid');
    expect(clients.calls.find((c) => c.entity === 'vol_surface')?.id).toBe('volsurface-uuid');
    expect(clients.calls.find((c) => c.entity === 'swaption_model')?.id).toBe('model-uuid');
    expect(clients.calls.find((c) => c.entity === 'swaption')?.id).toBe('swaption-uuid');
    expect(result.graph).toEqual(prior);
  });
});

// persistSwaptionGraph — error branches (branch on code)

describe('persistSwaptionGraph — error branches', () => {
  it('short-circuits on a curve failure and surfaces the error envelope + stage', async () => {
    const clients = makeClients({ curveCreate: () => fail('name_conflict', 409) });
    const result = await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.envelope.code).toBe('name_conflict');
    expect(result.httpStatus).toBe(409);
    expect(result.stage).toEqual({ entity: 'curve', op: 'create', key: 'discount' });
    // Downstream entities were never touched.
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.volSurfaces.create).not.toHaveBeenCalled();
    expect(clients.swaptionModels.create).not.toHaveBeenCalled();
    expect(clients.swaptions.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a vol_surface failure (the swaption-specific leaf)', async () => {
    const clients = makeClients({ volSurfaceCreate: () => fail('validation_error', 422) });
    const result = await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('vol_surface');
    expect(clients.swaptionModels.create).not.toHaveBeenCalled();
    expect(clients.swaptions.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a swaption_model failure', async () => {
    const clients = makeClients({ swaptionModelCreate: () => fail('validation_error', 422) });
    const result = await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('swaption_model');
    // The swaption (root) was never created.
    expect(clients.swaptions.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a swaption failure', async () => {
    const clients = makeClients({ swaptionCreate: () => fail('unauthenticated', 401) });
    const result = await persistSwaptionGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('swaption');
    expect(result.envelope.code).toBe('unauthenticated');
  });
});

// buildSwaptionPriceArm — price-arm selection

describe('buildSwaptionPriceArm — price-arm selection', () => {
  const graph: SwaptionAppGraph = {
    swaptionId: 'swaption-uuid',
    curveSetId: 'curveset-uuid',
    volSurfaceId: 'volsurface-uuid',
    swaptionModelId: 'model-uuid',
    curveIds: { discount: 'c1' },
  };

  it('saved → Thin-B by-reference body {swaption_id, as_of}, no inline', () => {
    const body = buildSwaptionPriceArm({
      appGraph: graph,
      inlineRequest: { swaption: { notional: 1 }, curves: [], vol_surface: {}, swaption_model: {}, as_of: '2025-01-15' },
      asOf: '2025-01-15',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swaption_id: 'swaption-uuid', as_of: '2025-01-15' });
    expect(body.swaption).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.vol_surface).toBeUndefined();
    expect(body.swaption_model).toBeUndefined();
  });

  it('saved + snapshot pin → snapshot_id forwarded', () => {
    const body = buildSwaptionPriceArm({
      appGraph: graph,
      inlineRequest: undefined,
      asOf: '2025-01-15',
      snapshotId: 'snap-1',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swaption_id: 'swaption-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  it('unsaved → Thin-A inline request passes through verbatim', () => {
    const inline = { swaption: { notional: 1 }, curves: [{ name: 'd' }], vol_surface: {}, swaption_model: {}, as_of: '2025-01-15' };
    const body = buildSwaptionPriceArm({ appGraph: null, inlineRequest: inline, asOf: '2025-01-15' });
    expect(body).toBe(inline);
  });
});

// asSwaptionAppGraph — narrowing

describe('asSwaptionAppGraph', () => {
  it('returns the graph for a valid record', () => {
    expect(
      asSwaptionAppGraph({
        swaptionId: 's',
        curveSetId: 'cs',
        volSurfaceId: 'vs',
        swaptionModelId: 'm',
        curveIds: { discount: 'c1', x: 2 },
      }),
    ).toEqual({ swaptionId: 's', curveSetId: 'cs', volSurfaceId: 'vs', swaptionModelId: 'm', curveIds: { discount: 'c1' } });
  });

  it('returns null for undefined / missing ids / non-object', () => {
    expect(asSwaptionAppGraph(undefined)).toBeNull();
    expect(asSwaptionAppGraph(null)).toBeNull();
    expect(asSwaptionAppGraph({})).toBeNull();
    expect(asSwaptionAppGraph({ swaptionId: '', curveSetId: 'cs', volSurfaceId: 'vs', swaptionModelId: 'm' })).toBeNull();
    expect(asSwaptionAppGraph({ swaptionId: 's', curveSetId: 'cs' })).toBeNull();
    expect(asSwaptionAppGraph({ swaptionId: 's', curveSetId: 'cs', volSurfaceId: 'vs' })).toBeNull();
    expect(asSwaptionAppGraph('nope')).toBeNull();
  });
});
