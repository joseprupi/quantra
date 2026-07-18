import { describe, expect, it, vi } from 'vitest';

import {
  persistEquityOptionGraph,
  buildEquityOptionPriceArm,
  asEquityOptionAppGraph,
  type EquityOptionCrudClients,
  type EquityOptionAppGraph,
} from './equityOptionSaveGraph';
import type { OrchestratorResult } from './types';

// Mock CRUD helpers

function ok<T>(data: T): OrchestratorResult<T> {
  return { ok: true, data, duration_ms: 1 };
}

function fail(code: string, httpStatus = 422): OrchestratorResult<never> {
  return { ok: false, envelope: { error: `${code} boom`, code }, httpStatus, duration_ms: 1 };
}

interface MockClients extends EquityOptionCrudClients {
  calls: Array<{ entity: string; op: string; id?: string; body: unknown }>;
}

function makeClients(overrides?: {
  curveCreate?: () => OrchestratorResult<{ id: string }>;
  volSurfaceCreate?: () => OrchestratorResult<{ id: string }>;
  equityOptionCreate?: () => OrchestratorResult<{ id: string }>;
}): MockClients {
  const calls: MockClients['calls'] = [];
  let curveSeq = 0;
  const curveCreate =
    overrides?.curveCreate ?? (() => ok({ id: `curve-uuid-${++curveSeq}` }));
  const volSurfaceCreate = overrides?.volSurfaceCreate ?? (() => ok({ id: 'volsurface-uuid' }));
  const equityOptionCreate = overrides?.equityOptionCreate ?? (() => ok({ id: 'equity-option-uuid' }));
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
    equityOptions: {
      create: vi.fn(async (body) => {
        calls.push({ entity: 'equity_option', op: 'create', body });
        return equityOptionCreate() as never;
      }),
      patch: vi.fn(async (id, body) => {
        calls.push({ entity: 'equity_option', op: 'patch', id, body });
        return ok({ id }) as never;
      }),
    },
  };
}

const baseInput = {
  name: 'AAPL Call 100 2026-01-15',
  volSurface: {
    name: 'AAPL-vol',
    kind: 'BlackVolSpec',
    payload: { base: { reference_date: '2025-01-15', constant_vol: 0.2, shape: 'Constant' } },
  },
  spot: { value: 100.0, canonical_id: 'AAPL' },
  equityOptionRequest: {
    option_type: 'Call',
    strike: 100.0,
    quantity: 1.0,
    expiry_date: '2026-01-15',
    settlement: 'Cash',
    trade_id: 'demo-AAPL',
  },
};

function twoCurves() {
  return [
    { key: 'discount', body: { name: 'discount', points: [], body: { role: 'discount' } } },
    { key: 'dividend', body: { name: 'dividend', points: [], body: { role: 'dividend' } } },
  ];
}

// persistEquityOptionGraph — fresh save (leaf→root create)

describe('persistEquityOptionGraph — fresh save', () => {
  it('persists leaf→root (curves → vol_surface → equity_option) in order and stamps appIds', async () => {
    const clients = makeClients();
    const result = await persistEquityOptionGraph(
      { ...baseInput, curves: twoCurves() },
      null,
      clients,
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;

    // Call order is leaf→root — both curves, then the equity-specific
    // vol_surface leaf, then the equity_option root. NO curve_set (equity reads
    // pricing.curves directly) and NO model entity (DEFAULT_EQUITY_MODEL_ID).
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve:create',
      'vol_surface:create',
      'equity_option:create',
    ]);
    expect(clients.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:create',
      'curve:create',
      'vol_surface:create',
      'equity_option:create',
    ]);

    // Stamped UUIDs — no curveSetId (equity has no curve_set).
    expect(result.graph).toEqual<EquityOptionAppGraph>({
      equityOptionId: 'equity-option-uuid',
      volSurfaceId: 'volsurface-uuid',
      curveIds: { discount: 'curve-uuid-1', dividend: 'curve-uuid-2' },
    });
  });

  it('persists the vol_surface with its literal payload (parity with Thin-A, BlackVolSpec)', async () => {
    const clients = makeClients();
    await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    const volCall = clients.calls.find((c) => c.entity === 'vol_surface');
    const volBody = volCall?.body as Record<string, unknown>;
    expect(volBody.kind).toBe('BlackVolSpec');
    expect((volBody.payload as Record<string, unknown>).base).toBeDefined();
  });

  it('pins pricing.curves (role-tagged list) + vol_surface_id + inline spot — NOT curve_set_id/discount_curve_id', async () => {
    const clients = makeClients();
    await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    const eoCall = clients.calls.find((c) => c.entity === 'equity_option');
    const body = eoCall?.body as { name: string; request: Record<string, unknown> };
    expect(body.name).toBe('AAPL Call 100 2026-01-15');

    // Exactly the refs the equity_options assembler reads (verified against the backend read-path,
    // a role-tagged pricing.curves list, pricing.vol_surface_id, and the
    // inline pricing.spot. No curve_set_id (swaption shape) and no
    // discount_curve_id (cds shape).
    const pricing = body.request.pricing as Record<string, unknown>;
    expect(pricing.curves).toEqual([
      { curve_id: 'curve-uuid-1', role: 'discount' },
      { curve_id: 'curve-uuid-2', role: 'dividend' },
    ]);
    expect(pricing.vol_surface_id).toBe('volsurface-uuid');
    expect(pricing.spot).toEqual({ value: 100.0, canonical_id: 'AAPL' });
    expect(pricing.curve_set_id).toBeUndefined();
    expect(pricing.discount_curve_id).toBeUndefined();

    // Flat trade levers preserved alongside the pinned refs (engine_io reads
    // these off the equity_option root).
    expect(body.request.option_type).toBe('Call');
    expect(body.request.strike).toBe(100.0);
    expect(body.request.expiry_date).toBe('2026-01-15');
  });
});

// persistEquityOptionGraph — idempotent re-save (PATCH by id)

describe('persistEquityOptionGraph — idempotent re-save', () => {
  const prior: EquityOptionAppGraph = {
    equityOptionId: 'equity-option-uuid',
    volSurfaceId: 'volsurface-uuid',
    curveIds: { discount: 'curve-uuid-1', dividend: 'curve-uuid-2' },
  };

  it('PATCHes every entity by its prior UUID — no duplicate creates', async () => {
    const clients = makeClients();
    const result = await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, prior, clients);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.calls.map((c) => `${c.entity}:${c.op}`)).toEqual([
      'curve:patch',
      'curve:patch',
      'vol_surface:patch',
      'equity_option:patch',
    ]);
    // No create was issued on any client.
    expect(clients.curves.create).not.toHaveBeenCalled();
    expect(clients.volSurfaces.create).not.toHaveBeenCalled();
    expect(clients.equityOptions.create).not.toHaveBeenCalled();
    // PATCH targets are the prior ids; the returned graph is stable.
    expect(clients.calls.find((c) => c.entity === 'curve' && c.id === 'curve-uuid-1')).toBeDefined();
    expect(clients.calls.find((c) => c.entity === 'vol_surface')?.id).toBe('volsurface-uuid');
    expect(clients.calls.find((c) => c.entity === 'equity_option')?.id).toBe('equity-option-uuid');
    expect(result.graph).toEqual(prior);
  });
});

// persistEquityOptionGraph — error branches (branch on code)

describe('persistEquityOptionGraph — error branches', () => {
  it('short-circuits on a curve failure and surfaces the error envelope + stage', async () => {
    const clients = makeClients({ curveCreate: () => fail('name_conflict', 409) });
    const result = await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.envelope.code).toBe('name_conflict');
    expect(result.httpStatus).toBe(409);
    expect(result.stage).toEqual({ entity: 'curve', op: 'create', key: 'discount' });
    // Downstream entities were never touched.
    expect(clients.volSurfaces.create).not.toHaveBeenCalled();
    expect(clients.equityOptions.create).not.toHaveBeenCalled();
  });

  it('short-circuits on a vol_surface failure (the equity-specific leaf)', async () => {
    const clients = makeClients({ volSurfaceCreate: () => fail('validation_error', 422) });
    const result = await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('vol_surface');
    // The equity_option (root) was never created.
    expect(clients.equityOptions.create).not.toHaveBeenCalled();
  });

  it('short-circuits on an equity_option failure', async () => {
    const clients = makeClients({ equityOptionCreate: () => fail('unauthenticated', 401) });
    const result = await persistEquityOptionGraph({ ...baseInput, curves: twoCurves() }, null, clients);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.stage.entity).toBe('equity_option');
    expect(result.envelope.code).toBe('unauthenticated');
  });
});

// buildEquityOptionPriceArm — price-arm selection

describe('buildEquityOptionPriceArm — price-arm selection', () => {
  const graph: EquityOptionAppGraph = {
    equityOptionId: 'equity-option-uuid',
    volSurfaceId: 'volsurface-uuid',
    curveIds: { discount: 'c1', dividend: 'c2' },
  };

  it('saved → Thin-B by-reference body {equity_option_id, as_of}, no inline', () => {
    const body = buildEquityOptionPriceArm({
      appGraph: graph,
      inlineRequest: { equity_option: { strike: 1 }, curves: [], vol_surface: {}, spot: {}, as_of: '2025-01-15' },
      asOf: '2025-01-15',
    }) as Record<string, unknown>;
    expect(body).toEqual({ equity_option_id: 'equity-option-uuid', as_of: '2025-01-15' });
    expect(body.equity_option).toBeUndefined();
    expect(body.curves).toBeUndefined();
    expect(body.vol_surface).toBeUndefined();
    expect(body.spot).toBeUndefined();
  });

  it('saved + snapshot pin → snapshot_id forwarded', () => {
    const body = buildEquityOptionPriceArm({
      appGraph: graph,
      inlineRequest: undefined,
      asOf: '2025-01-15',
      snapshotId: 'snap-1',
    }) as Record<string, unknown>;
    expect(body).toEqual({ equity_option_id: 'equity-option-uuid', as_of: '2025-01-15', snapshot_id: 'snap-1' });
  });

  it('unsaved → Thin-A inline request passes through verbatim', () => {
    const inline = { equity_option: { strike: 1 }, curves: [{ name: 'd' }], vol_surface: {}, spot: {}, as_of: '2025-01-15' };
    const body = buildEquityOptionPriceArm({ appGraph: null, inlineRequest: inline, asOf: '2025-01-15' });
    expect(body).toBe(inline);
  });
});

// asEquityOptionAppGraph — narrowing

describe('asEquityOptionAppGraph', () => {
  it('returns the graph for a valid record', () => {
    expect(
      asEquityOptionAppGraph({
        equityOptionId: 'eo',
        volSurfaceId: 'vs',
        curveIds: { discount: 'c1', dividend: 'c2', x: 2 },
      }),
    ).toEqual({ equityOptionId: 'eo', volSurfaceId: 'vs', curveIds: { discount: 'c1', dividend: 'c2' } });
  });

  it('returns null for undefined / missing ids / non-object', () => {
    expect(asEquityOptionAppGraph(undefined)).toBeNull();
    expect(asEquityOptionAppGraph(null)).toBeNull();
    expect(asEquityOptionAppGraph({})).toBeNull();
    expect(asEquityOptionAppGraph({ equityOptionId: '', volSurfaceId: 'vs' })).toBeNull();
    expect(asEquityOptionAppGraph({ equityOptionId: 'eo' })).toBeNull();
    expect(asEquityOptionAppGraph('nope')).toBeNull();
  });
});
