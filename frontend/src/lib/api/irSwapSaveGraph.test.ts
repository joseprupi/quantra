import { describe, expect, it, vi } from 'vitest';

import {
  persistIrSwapGraph,
  buildIrSwapPriceArm,
  asIrSwapAppGraph,
  type IrSwapCrudClients,
  type IrSwapAppGraph,
} from './irSwapSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}

function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

interface MockClients extends IrSwapCrudClients {
  calls: Array<{ entity: string; op: string; id?: string; body: unknown }>;
}

function makeClients(overrides?: {
  curveCreate?: () => OrchestratorResult<{ id: string }>;
  curveSetCreate?: () => OrchestratorResult<{ id: string }>;
  swapCreate?: () => OrchestratorResult<{ id: string }>;
}): MockClients {
  const calls: MockClients['calls'] = [];
  let curveSeq = 0;
  const curveCreate =
    overrides?.curveCreate ?? (() => ok({ id: `curve-uuid-${++curveSeq}` }));
  const curveSetCreate = overrides?.curveSetCreate ?? (() => ok({ id: 'curveset-uuid' }));
  const swapCreate = overrides?.swapCreate ?? (() => ok({ id: 'swap-uuid' }));
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
    swapsIr: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'swap_ir', op: 'create', body });
        return swapCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'swap_ir', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
  };
}

const baseInput = {
  name: 'My Vanilla Swap',
  curveSetCurrency: 'USD',
  swapRequest: {
    notional: 1_000_000,
    fixed_rate: 0.025,
    swap_type: 'Payer',
    effective_date: '2025-01-17',
    termination_date: '2030-01-17',
  },
};

function oneCurve() {
  return [{ key: 'discount', body: { name: 'disc', points: [{ point_type: 'SwapHelper' }] } }];
}

// persistIrSwapGraph — fresh save (leaf→root create)

describe('persistIrSwapGraph — fresh save', () => {
  it('persists leaf→root (curves → curve_set → swap) in order and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistIrSwapGraph(
      { ...baseInput, curves: oneCurve() },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;

    // Call order is leaf→root.
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'swap_ir:create',
    ]);
    expect(clients.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'swap_ir:create',
    ]);

    // Stamped UUIDs.
    expect(result.graph).toEqual<IrSwapAppGraph>({
      swapId: 'swap-uuid',
      curveSetId: 'curveset-uuid',
      curveIds: { discount: 'curve-uuid-1' },
    });
  });

  it('curve_set references the persisted curve UUID (D9 curve_refs[*].curve_id)', async () => {
    const clients = makeClients();
    await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const curveSetCall = clients.calls.find((c) => c.entity === 'curve_set');
    const body = curveSetCall?.body as { body: { curve_refs: Array<Record<string, unknown>> } };
    expect(body.body.curve_refs).toEqual([{ curve_id: 'curve-uuid-1', role: 'discount' }]);
  });

  it('injects the persisted curve_set UUID into swap.request.pricing.curve_set_id', async () => {
    const clients = makeClients();
    await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const swapCall = clients.calls.find((c) => c.entity === 'swap_ir');
    const body = swapCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.name).toBe('My Vanilla Swap');
    expect(body.request.pricing).toEqual({ curve_set_id: 'curveset-uuid' });
    // Flat trade levers are preserved alongside the pinned curve set.
    expect(body.request.notional).toBe(1_000_000);
    expect(body.request.fixed_rate).toBe(0.025);
  });

  it('never POSTs a curve under the wire role constant — names are product-derived + unique', async () => {
    const clients = makeClients();
    await persistIrSwapGraph(
      {
        ...baseInput,
        curves: [
          { key: 'discount', body: { name: 'discount', points: [] } },
          { key: 'forward', body: { name: 'forward', points: [] } },
        ],
      },
      null,
      clients,
    );

    const curveCreates = clients.calls.filter((c) => c.entity === 'curve' && c.op === 'create');
    expect(curveCreates).toHaveLength(2);
    const names = curveCreates.map((c) => (c.body as { name: string }).name);
    // The colliding constants must never reach the wire...
    expect(names).not.toContain('discount');
    expect(names).not.toContain('forward');
    // ...the derived names carry the product identity + role and are distinct.
    expect(names[0]).toContain('My Vanilla Swap');
    expect(names[0]).toContain('discount');
    expect(names[1]).toContain('My Vanilla Swap');
    expect(names[1]).toContain('forward');
    expect(new Set(names).size).toBe(2);
  });

  it('persists multiple curves in role order (discount first)', async () => {
    const clients = makeClients();
    const result = await persistIrSwapGraph(
      {
        ...baseInput,
        curves: [
          { key: 'discount', body: { name: 'disc', points: [] } },
          { key: 'forward', body: { name: 'fwd', points: [] } },
        ],
      },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.graph.curveIds).toEqual({ discount: 'curve-uuid-1', forward: 'curve-uuid-2' });
    const curveSetCall = clients.calls.find((c) => c.entity === 'curve_set');
    const body = curveSetCall?.body as { body: { curve_refs: Array<Record<string, unknown>> } };
    expect(body.body.curve_refs).toEqual([
      { curve_id: 'curve-uuid-1', role: 'discount' },
      { curve_id: 'curve-uuid-2', role: 'forward' },
    ]);
  });
});

// persistIrSwapGraph — idempotent re-save (PATCH by id)

describe('persistIrSwapGraph — idempotent re-save', () => {
  const prior: IrSwapAppGraph = {
    swapId: 'swap-uuid',
    curveSetId: 'curveset-uuid',
    curveIds: { discount: 'curve-uuid-1' },
  };

  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const clients = makeClients();
    const result = await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, prior, clients);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch',
      'curve_set:patch',
      'swap_ir:patch',
    ]);
    // No create was issued on any client.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.swapsIr.create).not.toHaveBeenCalled();
    // PATCH targets are the prior ids; the returned graph is stable.
    expect(clients.calls.find((c) => c.entity === 'curve')?.id).toBe('curve-uuid-1');
    expect(clients.calls.find((c) => c.entity === 'curve_set')?.id).toBe('curveset-uuid');
    expect(clients.calls.find((c) => c.entity === 'swap_ir')?.id).toBe('swap-uuid');
    expect(result.graph).toEqual(prior);
  });

  it('re-save PATCH bodies carry NO name and touch ONLY the remembered ids (no by-name matching, unrelated curves untouchable)', async () => {
    const clients = makeClients();
    await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, prior, clients);

    const curvePatches = clients.calls.filter((c) => c.entity === 'curve' && c.op === 'patch');
    expect(curvePatches).toHaveLength(1);
    // By-id only: the single patch targets exactly the remembered UUID.
    expect(curvePatches[0].id).toBe('curve-uuid-1');
    // No rename on re-save: the body has no name, so the row keeps its
    // identity and a curve that merely SHARES a name can never be matched.
    expect('name' in (curvePatches[0].body as Record<string, unknown>)).toBe(false);
    // Nothing else on the curve surface was called at all.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.curves.patch).toHaveBeenCalledTimes(1);
  });
});

// persistIrSwapGraph — error branches (branch on code)

describe('persistIrSwapGraph — error branches', () => {
  it('short-circuits on a curve failure and surfaces the error envelope + stage', async () => {
    const clients = makeClients({ curveCreate: () => fail('name_conflict', 409) });
    const result = await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.envelope.code).toBe('name_conflict');
    expect(result.httpStatus).toBe(409);
    expect(result.stage).toEqual({ entity: 'curve', op: 'create', key: 'discount' });
    // Downstream entities were never touched.
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.swapsIr.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a curve_set failure', async () => {
    const clients = makeClients({ curveSetCreate: () => fail('validation_error', 422) });
    const result = await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('curve_set');
    expect(clients.swapsIr.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a swap failure', async () => {
    const clients = makeClients({ swapCreate: () => fail('unauthenticated', 401) });
    const result = await persistIrSwapGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('swap_ir');
    expect(result.envelope.code).toBe('unauthenticated');
  });
});

// buildIrSwapPriceArm — price-arm selection

describe('buildIrSwapPriceArm — price-arm selection', () => {
  const graph: IrSwapAppGraph = {
    swapId: 'swap-uuid',
    curveSetId: 'curveset-uuid',
    curveIds: { discount: 'c1' },
  };

  it('saved → Thin-B by-reference body {swap_id, as_of}', () => {
    const body = buildIrSwapPriceArm({
      appGraph: graph,
      inlineRequest: { swap: { notional: 1 }, curves: [], as_of: '2025-01-15' },
      asOf: '2025-01-15',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swap_id: 'swap-uuid', as_of: '2025-01-15' });
    expect(body.swap).toBeUndefined();
    expect(body.curves).toBeUndefined();
  });

  it('saved + snapshot pin → snapshot_id forwarded', () => {
    const body = buildIrSwapPriceArm({
      appGraph: graph,
      inlineRequest: undefined,
      asOf: '2025-01-15',
      snapshotId: 'snap-1',
    }) as Record<string, unknown>;
    expect(body).toEqual({ swap_id: 'swap-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  it('unsaved → Thin-A inline request passes through verbatim', () => {
    const inline = { swap: { notional: 1 }, curves: [{ name: 'd' }], as_of: '2025-01-15' };
    const body = buildIrSwapPriceArm({ appGraph: null, inlineRequest: inline, asOf: '2025-01-15' });
    expect(body).toBe(inline);
  });
});

// asIrSwapAppGraph — narrowing

describe('asIrSwapAppGraph', () => {
  it('returns the graph for a valid record', () => {
    expect(
      asIrSwapAppGraph({ swapId: 's', curveSetId: 'cs', curveIds: { discount: 'c1', x: 2 } }),
    ).toEqual({ swapId: 's', curveSetId: 'cs', curveIds: { discount: 'c1' } });
  });

  it('returns null for undefined / missing swapId / non-object', () => {
    expect(asIrSwapAppGraph(undefined)).toBeNull();
    expect(asIrSwapAppGraph(null)).toBeNull();
    expect(asIrSwapAppGraph({})).toBeNull();
    expect(asIrSwapAppGraph({ swapId: '', curveSetId: 'cs' })).toBeNull();
    expect(asIrSwapAppGraph('nope')).toBeNull();
  });
});
