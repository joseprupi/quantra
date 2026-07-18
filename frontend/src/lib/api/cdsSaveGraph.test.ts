import { describe, expect, it, vi } from 'vitest';

import {
  persistCdsGraph,
  buildCdsPriceArm,
  asCdsAppGraph,
  type CdsCrudClients,
  type CdsAppGraph,
} from './cdsSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}

function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

interface MockClients extends CdsCrudClients {
  calls: Array<{ entity: string; op: string; id?: string; body: unknown }>;
}

function makeClients(overrides?: {
  curveCreate?: () => OrchestratorResult<{ id: string }>;
  curveSetCreate?: () => OrchestratorResult<{ id: string }>;
  creditCurveCreate?: () => OrchestratorResult<{ id: string }>;
  cdsCreate?: () => OrchestratorResult<{ id: string }>;
}): MockClients {
  const calls: MockClients['calls'] = [];
  let curveSeq = 0;
  const curveCreate =
    overrides?.curveCreate ?? (() => ok({ id: `curve-uuid-${++curveSeq}` }));
  const curveSetCreate = overrides?.curveSetCreate ?? (() => ok({ id: 'curveset-uuid' }));
  const creditCurveCreate = overrides?.creditCurveCreate ?? (() => ok({ id: 'creditcurve-uuid' }));
  const cdsCreate = overrides?.cdsCreate ?? (() => ok({ id: 'cds-uuid' }));
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
    creditCurves: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'credit_curve', op: 'create', body });
        return creditCurveCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'credit_curve', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
    cds: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'cds', op: 'create', body });
        return cdsCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'cds', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
  };
}

const baseInput = {
  name: 'Buyer ACME 5Y',
  curveSetCurrency: 'EUR',
  creditCurve: {
    name: 'ACME-SR',
    source: 'flat',
    recovery_rate: 0.4,
    body: { flat_hazard_rate: 0.02 },
  },
  cdsRequest: {
    side: 'Buyer',
    notional: 10_000_000,
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
};

function oneCurve() {
  return [{ key: 'discount', body: { name: 'disc', points: [{ point_type: 'SwapHelper' }] } }];
}

// persistCdsGraph — fresh save (leaf→root create)

describe('persistCdsGraph — fresh save', () => {
  it('persists leaf→root (curves → curve_set → credit_curve → cds) in order and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistCdsGraph(
      { ...baseInput, curves: oneCurve() },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;

    // Call order is leaf→root, including the cds-specific credit_curve leaf.
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'credit_curve:create',
      'cds:create',
    ]);
    expect(clients.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve_set:create',
      'credit_curve:create',
      'cds:create',
    ]);

    // Stamped UUIDs.
    expect(result.graph).toEqual<CdsAppGraph>({
      cdsId: 'cds-uuid',
      curveSetId: 'curveset-uuid',
      creditCurveId: 'creditcurve-uuid',
      curveIds: { discount: 'curve-uuid-1' },
    });
  });

  it('curve_set references the persisted curve UUID (D9 curve_refs[*].curve_id)', async () => {
    const clients = makeClients();
    await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const curveSetCall = clients.calls.find((c) => c.entity === 'curve_set');
    const body = curveSetCall?.body as { body: { curve_refs: Array<Record<string, unknown>> } };
    expect(body.body.curve_refs).toEqual([{ curve_id: 'curve-uuid-1', role: 'discount' }]);
  });

  it('persists the credit_curve with its literal body + recovery (D85 — no quote_id resolution)', async () => {
    const clients = makeClients();
    await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const ccCall = clients.calls.find((c) => c.entity === 'credit_curve');
    const body = ccCall?.body as Record<string, unknown>;
    expect(body.name).toBe('ACME-SR');
    expect(body.source).toBe('flat');
    expect(body.recovery_rate).toBe(0.4);
    expect((body.body as Record<string, unknown>).flat_hazard_rate).toBe(0.02);
  });

  it('pins curve_set_id + discount_curve_id + credit_curve_id into cds.request.pricing', async () => {
    const clients = makeClients();
    await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    const cdsCall = clients.calls.find((c) => c.entity === 'cds');
    const body = cdsCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.name).toBe('Buyer ACME 5Y');
    // curve_set_id (grouping) + discount_curve_id (what the assembler reads) +
    // credit_curve_id (pricing.credit_curve_id).
    expect(body.request.pricing).toEqual({
      curve_set_id: 'curveset-uuid',
      discount_curve_id: 'curve-uuid-1',
      credit_curve_id: 'creditcurve-uuid',
    });
    // Flat trade levers are preserved alongside the pinned refs.
    expect(body.request.side).toBe('Buyer');
    expect(body.request.notional).toBe(10_000_000);
    expect(body.request.running_coupon).toBe(0.01);
  });
});

// persistCdsGraph — idempotent re-save (PATCH by id)

describe('persistCdsGraph — idempotent re-save', () => {
  const prior: CdsAppGraph = {
    cdsId: 'cds-uuid',
    curveSetId: 'curveset-uuid',
    creditCurveId: 'creditcurve-uuid',
    curveIds: { discount: 'curve-uuid-1' },
  };

  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const clients = makeClients();
    const result = await persistCdsGraph({ ...baseInput, curves: oneCurve() }, prior, clients);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch',
      'curve_set:patch',
      'credit_curve:patch',
      'cds:patch',
    ]);
    // No create was issued on any client.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.creditCurves.create).not.toHaveBeenCalled();
    expect(clients.cds.create).not.toHaveBeenCalled();
    // PATCH targets are the prior ids; the returned graph is stable.
    expect(clients.calls.find((c) => c.entity === 'curve')?.id).toBe('curve-uuid-1');
    expect(clients.calls.find((c) => c.entity === 'curve_set')?.id).toBe('curveset-uuid');
    expect(clients.calls.find((c) => c.entity === 'credit_curve')?.id).toBe('creditcurve-uuid');
    expect(clients.calls.find((c) => c.entity === 'cds')?.id).toBe('cds-uuid');
    expect(result.graph).toEqual(prior);
  });
});

// persistCdsGraph — error branches (branch on code)

describe('persistCdsGraph — error branches', () => {
  it('short-circuits on a curve failure and surfaces the error envelope + stage', async () => {
    const clients = makeClients({ curveCreate: () => fail('name_conflict', 409) });
    const result = await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.envelope.code).toBe('name_conflict');
    expect(result.httpStatus).toBe(409);
    expect(result.stage).toEqual({ entity: 'curve', op: 'create', key: 'discount' });
    // Downstream entities were never touched.
    expect(clients.curveSets.create).not.toHaveBeenCalled();
    expect(clients.creditCurves.create).not.toHaveBeenCalled();
    expect(clients.cds.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a curve_set failure', async () => {
    const clients = makeClients({ curveSetCreate: () => fail('validation_error', 422) });
    const result = await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('curve_set');
    expect(clients.creditCurves.create).not.toHaveBeenCalled();
    expect(clients.cds.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a credit_curve failure (the cds-specific leaf)', async () => {
    const clients = makeClients({ creditCurveCreate: () => fail('validation_error', 422) });
    const result = await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('credit_curve');
    // The cds (root) was never created.
    expect(clients.cds.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a cds failure', async () => {
    const clients = makeClients({ cdsCreate: () => fail('unauthenticated', 401) });
    const result = await persistCdsGraph({ ...baseInput, curves: oneCurve() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('cds');
    expect(result.envelope.code).toBe('unauthenticated');
  });
});

// buildCdsPriceArm — price-arm selection

describe('buildCdsPriceArm — price-arm selection', () => {
  const graph: CdsAppGraph = {
    cdsId: 'cds-uuid',
    curveSetId: 'curveset-uuid',
    creditCurveId: 'creditcurve-uuid',
    curveIds: { discount: 'c1' },
  };

  it('saved → Thin-B by-reference body {cds_id, as_of}', () => {
    const body = buildCdsPriceArm({
      appGraph: graph,
      inlineRequest: { cds: { notional: 1 }, curves: [], credit_curve: {}, as_of: '2025-01-15' },
      asOf: '2025-01-15',
    }) as Record<string, unknown>;
    expect(body).toEqual({ cds_id: 'cds-uuid', as_of: '2025-01-15' });
    expect(body.cds).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.credit_curve).toBeUndefined();
  });

  it('saved + snapshot pin → snapshot_id forwarded', () => {
    const body = buildCdsPriceArm({
      appGraph: graph,
      inlineRequest: undefined,
      asOf: '2025-01-15',
      snapshotId: 'snap-1',
    }) as Record<string, unknown>;
    expect(body).toEqual({ cds_id: 'cds-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  it('unsaved → Thin-A inline request passes through verbatim', () => {
    const inline = { cds: { notional: 1 }, curves: [{ name: 'd' }], credit_curve: {}, as_of: '2025-01-15' };
    const body = buildCdsPriceArm({ appGraph: null, inlineRequest: inline, asOf: '2025-01-15' });
    expect(body).toBe(inline);
  });
});

// asCdsAppGraph — narrowing

describe('asCdsAppGraph', () => {
  it('returns the graph for a valid record', () => {
    expect(
      asCdsAppGraph({
        cdsId: 'c',
        curveSetId: 'cs',
        creditCurveId: 'cc',
        curveIds: { discount: 'c1', x: 2 },
      }),
    ).toEqual({ cdsId: 'c', curveSetId: 'cs', creditCurveId: 'cc', curveIds: { discount: 'c1' } });
  });

  it('returns null for undefined / missing ids / non-object', () => {
    expect(asCdsAppGraph(undefined)).toBeNull();
    expect(asCdsAppGraph(null)).toBeNull();
    expect(asCdsAppGraph({})).toBeNull();
    expect(asCdsAppGraph({ cdsId: '', curveSetId: 'cs', creditCurveId: 'cc' })).toBeNull();
    expect(asCdsAppGraph({ cdsId: 'c', curveSetId: 'cs' })).toBeNull();
    expect(asCdsAppGraph('nope')).toBeNull();
  });
});
