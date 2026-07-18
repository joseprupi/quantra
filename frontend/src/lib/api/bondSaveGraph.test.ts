import { describe, expect, it, vi } from 'vitest';

import {
  persistBondFixedGraph,
  persistBondFloatingGraph,
  buildBondFixedPriceArm,
  buildBondFloatingPriceArm,
  asBondFixedAppGraph,
  asBondFloatingAppGraph,
  type BondFixedCrudClients,
  type BondFloatingCrudClients,
  type BondFixedAppGraph,
  type BondFloatingAppGraph,
} from './bondSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}
function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

type Call = { entity: string; op: string; id?: string; body: unknown };

function curveMock(calls: Call[], seq: { n: number }, override?: () => OrchestratorResult<{ id: string }>) {
  return {
    create: vi.fn(async (body: unknown) => {
      calls.push({ entity: 'curve', op: 'create', body });
      return (override ? override() : ok({ id: `curve-uuid-${++seq.n}` })) as never;
    }),
    patch: vi.fn(async (id: string, body: unknown) => {
      calls.push({ entity: 'curve', op: 'patch', id, body });
      return ok({ id }) as never;
    }),
  };
}
function namedMock(calls: Call[], entity: string, id: string, override?: () => OrchestratorResult<{ id: string }>) {
  return {
    create: vi.fn(async (body: unknown) => {
      calls.push({ entity, op: 'create', body });
      return (override ? override() : ok({ id })) as never;
    }),
    patch: vi.fn(async (rid: string, body: unknown) => {
      calls.push({ entity, op: 'patch', id: rid, body });
      return ok({ id: rid }) as never;
    }),
  };
}

const fixedInput = {
  name: 'UST 5% 2030',
  curveSetCurrency: 'USD',
  bondRequest: {
    face_amount: 100,
    coupon_rate: 0.05,
    settlement_days: 2,
    redemption: 100,
    issue_date: '2025-01-15',
    effective_date: '2025-01-15',
    termination_date: '2030-01-15',
  },
};

const floatingInput = {
  name: 'FRN +10bp 2030',
  curveSetCurrency: 'EUR',
  index: { name: 'EURIBOR_6M', kind: 'IborIndex', currency: 'EUR', calendar: 'TARGET', day_counter: 'Actual360', body: { fixingDays: 2 } },
  bondRequest: {
    face_amount: 100,
    spread: 0.001,
    fixing_days: 2,
    in_arrears: false,
    settlement_days: 2,
    redemption: 100,
    issue_date: '2025-01-15',
    effective_date: '2025-01-15',
    termination_date: '2030-01-15',
  },
};

const oneCurve = () => [{ key: 'discount', body: { name: 'disc', points: [] } }];
const twoCurves = () => [
  { key: 'discount', body: { name: 'disc', points: [] } },
  { key: 'projection', body: { name: 'proj', points: [] } },
];

// Fixed — fresh save

describe('persistBondFixedGraph — fresh save', () => {
  function makeClients(override?: { bondCreate?: () => OrchestratorResult<{ id: string }> }): BondFixedCrudClients & { calls: Call[] } {
    const calls: Call[] = [];
    const seq = { n: 0 };
    return {
      calls,
      curves: curveMock(calls, seq),
      curveSets: namedMock(calls, 'curve_set', 'curveset-uuid'),
      bondsFixed: namedMock(calls, 'bond_fixed', 'bondfixed-uuid', override?.bondCreate),
    };
  }

  it('persists leaf→root (curve → curve_set → bonds_fixed) and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistBondFixedGraph({ ...fixedInput, curves: oneCurve() }, null, clients);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map(c => `${c.entity}:${c.op}`)).toEqual(['curve:create', 'curve_set:create', 'bond_fixed:create']);
    expect(result.graph).toEqual<BondFixedAppGraph>({
      bondId: 'bondfixed-uuid',
      curveSetId: 'curveset-uuid',
      curveIds: { discount: 'curve-uuid-1' },
    });
  });

  it('pins curve_set_id + discount_curve_id into bonds_fixed.request.pricing (D143 read-path)', async () => {
    const clients = makeClients();
    await persistBondFixedGraph({ ...fixedInput, curves: oneCurve() }, null, clients);
    const bondCall = clients.calls.find(c => c.entity === 'bond_fixed');
    const body = bondCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.request.pricing).toEqual({ curve_set_id: 'curveset-uuid', discount_curve_id: 'curve-uuid-1' });
    expect(body.request.coupon_rate).toBe(0.05); // flat trade levers preserved
    expect(body.request.face_amount).toBe(100);
  });

  it('never POSTs a curve under a wire/constant name — names are product-derived + unique', async () => {
    const clients = makeClients();
    await persistBondFixedGraph(
      { ...fixedInput, curves: [{ key: 'discount', body: { name: 'discount', points: [] } }] },
      null,
      clients,
    );
    const create = clients.calls.find(c => c.entity === 'curve' && c.op === 'create');
    const name = (create?.body as { name: string }).name;
    expect(name).not.toBe('discount');
    expect(name).toContain('UST 5% 2030');
    expect(name).toContain('discount');
  });

  it('short-circuits on a bonds_fixed failure and surfaces the error envelope', async () => {
    const clients = makeClients({ bondCreate: () => fail('name_conflict', 409) });
    const result = await persistBondFixedGraph({ ...fixedInput, curves: oneCurve() }, null, clients);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('bond_fixed');
    expect(result.envelope.code).toBe('name_conflict');
  });
});

// Fixed — idempotent re-save

describe('persistBondFixedGraph — idempotent re-save', () => {
  const prior: BondFixedAppGraph = { bondId: 'bondfixed-uuid', curveSetId: 'curveset-uuid', curveIds: { discount: 'curve-uuid-1' } };
  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const calls: Call[] = [];
    const seq = { n: 0 };
    const clients: BondFixedCrudClients = {
      curves: curveMock(calls, seq),
      curveSets: namedMock(calls, 'curve_set', 'curveset-uuid'),
      bondsFixed: namedMock(calls, 'bond_fixed', 'bondfixed-uuid'),
    };
    const result = await persistBondFixedGraph({ ...fixedInput, curves: oneCurve() }, prior, clients);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map(c => `${c.entity}:${c.op}`)).toEqual(['curve:patch', 'curve_set:patch', 'bond_fixed:patch']);
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.bondsFixed.create).not.toHaveBeenCalled();
    expect(result.graph).toEqual(prior);
    // By-id only, no rename: the single curve PATCH targets the remembered
    // UUID and carries NO name, so an unrelated user curve sharing a name can
    // never be matched or renamed into a conflict.
    const curvePatch = calls.find(c => c.entity === 'curve' && c.op === 'patch');
    expect(curvePatch?.id).toBe('curve-uuid-1');
    expect('name' in (curvePatch?.body as Record<string, unknown>)).toBe(false);
    expect(clients.curves.patch).toHaveBeenCalledTimes(1);
  });
});

// Floating — fresh save (incl. projection curve + index)

describe('persistBondFloatingGraph — fresh save', () => {
  function makeClients(override?: {
    indexCreate?: () => OrchestratorResult<{ id: string }>;
    bondCreate?: () => OrchestratorResult<{ id: string }>;
  }): BondFloatingCrudClients & { calls: Call[] } {
    const calls: Call[] = [];
    const seq = { n: 0 };
    return {
      calls,
      curves: curveMock(calls, seq),
      curveSets: namedMock(calls, 'curve_set', 'curveset-uuid'),
      indices: namedMock(calls, 'index', 'index-uuid', override?.indexCreate),
      bondsFloating: namedMock(calls, 'bond_floating', 'bondfloat-uuid', override?.bondCreate),
    };
  }

  it('persists leaf→root (curves → curve_set → index → bonds_floating) incl. projection curve + index', async () => {
    const clients = makeClients();
    const result = await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, null, clients);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map(c => `${c.entity}:${c.op}`)).toEqual([
      'curve:create', // discount
      'curve:create', // projection
      'curve_set:create',
      'index:create',
      'bond_floating:create',
    ]);
    expect(result.graph).toEqual<BondFloatingAppGraph>({
      bondId: 'bondfloat-uuid',
      curveSetId: 'curveset-uuid',
      indexId: 'index-uuid',
      curveIds: { discount: 'curve-uuid-1', projection: 'curve-uuid-2' },
    });
  });

  it('curve_set references both role-tagged curves (D116)', async () => {
    const clients = makeClients();
    await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, null, clients);
    const csCall = clients.calls.find(c => c.entity === 'curve_set');
    const body = csCall?.body as { body: { curve_refs: Array<Record<string, unknown>> } };
    expect(body.body.curve_refs).toEqual([
      { curve_id: 'curve-uuid-1', role: 'discount' },
      { curve_id: 'curve-uuid-2', role: 'projection' },
    ]);
  });

  it('pins curve_set_id + discount_curve_id + forecast_curve_id + index_id (D143 read-path)', async () => {
    const clients = makeClients();
    await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, null, clients);
    const bondCall = clients.calls.find(c => c.entity === 'bond_floating');
    const body = bondCall?.body as { request: Record<string, unknown> };
    expect(body.request.pricing).toEqual({
      curve_set_id: 'curveset-uuid',
      discount_curve_id: 'curve-uuid-1',
      forecast_curve_id: 'curve-uuid-2',
      index_id: 'index-uuid',
    });
    expect(body.request.spread).toBe(0.001);
  });

  it('persists the projection index as an app.indices row (D120) with the inline shape', async () => {
    const clients = makeClients();
    await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, null, clients);
    const idxCall = clients.calls.find(c => c.entity === 'index');
    expect(idxCall?.body).toEqual(floatingInput.index);
  });

  it('short-circuits on an index failure before the bond is created', async () => {
    const clients = makeClients({ indexCreate: () => fail('validation_error', 422) });
    const result = await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, null, clients);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('index');
    expect(clients.bondsFloating.create).not.toHaveBeenCalled();
  });
});

// Floating — idempotent re-save

describe('persistBondFloatingGraph — idempotent re-save', () => {
  const prior: BondFloatingAppGraph = {
    bondId: 'bondfloat-uuid',
    curveSetId: 'curveset-uuid',
    indexId: 'index-uuid',
    curveIds: { discount: 'curve-uuid-1', projection: 'curve-uuid-2' },
  };
  it('PATCHes every entity (incl. index) by its prior UUID — no duplicate creates', async () => {
    const calls: Call[] = [];
    const seq = { n: 0 };
    const clients: BondFloatingCrudClients = {
      curves: curveMock(calls, seq),
      curveSets: namedMock(calls, 'curve_set', 'curveset-uuid'),
      indices: namedMock(calls, 'index', 'index-uuid'),
      bondsFloating: namedMock(calls, 'bond_floating', 'bondfloat-uuid'),
    };
    const result = await persistBondFloatingGraph({ ...floatingInput, curves: twoCurves() }, prior, clients);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map(c => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch', 'curve:patch', 'curve_set:patch', 'index:patch', 'bond_floating:patch',
    ]);
    expect(clients.indices.create).not.toHaveBeenCalled();
    expect(clients.bondsFloating.create).not.toHaveBeenCalled();
    expect(result.graph).toEqual(prior);
  });
});

// Price-arm selection

describe('bond price-arm selection', () => {
  it('fixed saved → {bond_id, as_of}; no inline', () => {
    const graph: BondFixedAppGraph = { bondId: 'bf', curveSetId: 'cs', curveIds: { discount: 'c1' } };
    const body = buildBondFixedPriceArm({ appGraph: graph, inlineRequest: { bond: {}, curves: [], as_of: 'x' }, asOf: '2025-01-15' }) as Record<string, unknown>;
    expect(body).toEqual({ bond_id: 'bf', as_of: '2025-01-15' });
    expect(body.bond).toBeUndefined();
    expect(body.curves).toBeUndefined();
  });

  it('fixed saved + snapshot → snapshot_id forwarded', () => {
    const graph: BondFixedAppGraph = { bondId: 'bf', curveSetId: 'cs', curveIds: {} };
    const body = buildBondFixedPriceArm({ appGraph: graph, inlineRequest: undefined, asOf: '2025-01-15', snapshotId: 'snap' }) as Record<string, unknown>;
    expect(body).toEqual({ bond_id: 'bf', as_of: '2025-01-15', snapshot_id: 'snap' });
  });

  it('fixed unsaved → inline verbatim', () => {
    const inline = { bond: {}, curves: [], as_of: 'x' };
    expect(buildBondFixedPriceArm({ appGraph: null, inlineRequest: inline, asOf: 'x' })).toBe(inline);
  });

  it('floating saved → {bond_id, as_of}; no inline bond/curves/index', () => {
    const graph: BondFloatingAppGraph = { bondId: 'bn', curveSetId: 'cs', indexId: 'ix', curveIds: { discount: 'c1', projection: 'c2' } };
    const body = buildBondFloatingPriceArm({ appGraph: graph, inlineRequest: { bond: {}, curves: [], index: {}, as_of: 'x' }, asOf: '2025-01-15' }) as Record<string, unknown>;
    expect(body).toEqual({ bond_id: 'bn', as_of: '2025-01-15' });
    expect(body.index).toBeUndefined();
  });

  it('floating unsaved → inline verbatim', () => {
    const inline = { bond: {}, curves: [], index: {}, as_of: 'x' };
    expect(buildBondFloatingPriceArm({ appGraph: null, inlineRequest: inline, asOf: 'x' })).toBe(inline);
  });
});

// Narrowers

describe('appGraph narrowers', () => {
  it('asBondFixedAppGraph', () => {
    expect(asBondFixedAppGraph({ bondId: 'b', curveSetId: 'cs', curveIds: { discount: 'c1', x: 2 } }))
      .toEqual({ bondId: 'b', curveSetId: 'cs', curveIds: { discount: 'c1' } });
    expect(asBondFixedAppGraph({ bondId: '', curveSetId: 'cs' })).toBeNull();
    expect(asBondFixedAppGraph(null)).toBeNull();
  });

  it('asBondFloatingAppGraph requires indexId', () => {
    expect(asBondFloatingAppGraph({ bondId: 'b', curveSetId: 'cs', indexId: 'ix', curveIds: { discount: 'c1' } }))
      .toEqual({ bondId: 'b', curveSetId: 'cs', indexId: 'ix', curveIds: { discount: 'c1' } });
    expect(asBondFloatingAppGraph({ bondId: 'b', curveSetId: 'cs' })).toBeNull();
    expect(asBondFloatingAppGraph('nope')).toBeNull();
  });
});
