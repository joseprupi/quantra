/**
 * Equity-option save → reference flow.
 *
 * Mirrors ``swaptionSaveGraph`` / ``cdsSaveGraph`` / ``bondSaveGraph`` /
 * ``irSwapSaveGraph``. "Save" is a real server persist — the entity graph is
 * written to the backend leaf→root through the CRUD wrappers, and the
 * server-minted UUIDs are stamped back onto the local record. Once stamped, the
 * price layer switches that equity option to the by-reference arm (a minimal
 * id-only body); an unsaved option still prices inline (full entities in the
 * request). Both arms coexist — same route, same server-side assembly, same
 * response handling.
 *
 * Leaf→root order (about id-availability, not FK constraints — refs are soft):
 *
 *     curves → vol_surface → equity_option
 *
 * The equity option needs its curves' + vol-surface's UUIDs pinned into
 * ``request.pricing`` before it can be persisted.
 *
 * The save shape matches what the backend's equity-option assembler actually
 * reads (verified against the equity path, not assumed from swaption):
 *   • curves: ``pricing.curves`` is a **role-tagged LIST of curve refs**
 *     ``[{curve_id, role}]`` read directly from the saved trade. Equity options
 *     do NOT use a ``curve_set_id`` (swaption) and do NOT use a
 *     ``discount_curve_id`` (cds); the assembler buckets the list into
 *     discount/dividend by the per-entry ``role`` and resolves each
 *     ``curve_id`` against the stored curves. So we persist the curves and pin
 *     ``pricing.curves`` with explicit roles — NO curve_set is created (it
 *     would be a dead, never-read row).
 *   • vol surface: the resolver follows ``pricing.vol_surface_id`` to the
 *     stored surface row (kind enforced to ``BlackVolSpec``). We pin
 *     ``pricing.vol_surface_id``.
 *   • spot / underlier: ``pricing.spot`` (``{canonical_id, value}``) is read
 *     **inline off the saved trade** — spot is the one equity market-data leaf
 *     that is NOT a vendor-style table row. There is no separate underlier
 *     entity yet, so the underlier id derives from ``spot.canonical_id``
 *     server-side. We persist ``pricing.spot`` faithfully inline on the
 *     equity option; there is NO separate spot/underlier CRUD entity to create.
 *
 * The equity model defaults to ``DEFAULT_EQUITY_MODEL_ID`` (analytic
 * Black-Scholes) server-side — no model entity is persisted (unlike swaption's
 * swaption_model). The BlackVolSpec surface payload is base-value-only.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by its
 * prior UUID instead of POSTing a duplicate. The prior UUIDs travel on the
 * local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — curve points
 * carry unresolved ``quote_id`` leaves exactly as the inline arm does, the
 * vol-surface payload carries pure config (base-value-only), and the spot rides
 * as an inline value + canonical_id.
 */

import * as crud from './crud';
import type { NamedCrudClient } from './crud';
import type { components } from './_generated/orchestrator';
import type { ApiErrorEnvelope, OrchestratorResult } from './types';
import { graphCurveCreateBody, graphCurvePatchBody } from './saveGraphNaming';

type Schemas = components['schemas'];

/** A curve to persist, tagged with a stable local key for the id↔uuid map. */
export interface CurveGraphInput {
  /**
   * Stable local key (the role: ``'discount'`` / ``'dividend'``). Becomes the
   * ``role`` on the ``pricing.curves`` ref and the idempotent re-save curve
   * map key.
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** The leaf→root inputs the save flow persists for one equity-option vertical. */
export interface EquityOptionGraphInput {
  /** Display name applied to all entities (deriveable upstream). */
  name: string;
  /** Curves to persist, in role order (discount first; dividend second). */
  curves: CurveGraphInput[];
  /**
   * The vol-surface body to persist as a stored vol-surface row. Carries the
   * same ``{name, kind, payload}`` the inline ``vol_surface`` sends
   * (base-value-only ``BlackVolSpec``), so by-ref ↔ inline resolve the same
   * surface.
   */
  volSurface: Schemas['VolSurfaceCreate'];
  /**
   * The inline spot / underlier reference. ``{canonical_id?, value?}`` —
   * pinned into ``request.pricing.spot`` on the saved trade (no separate
   * underlier entity exists yet). Callers MUST NOT pre-set ``pricing.spot``.
   */
  spot: { canonical_id?: string; value?: number };
  /**
   * The saved option's flat trade body (option_type / strike / quantity /
   * expiry_date / settlement / trade_id …). ``pricing.{curves, vol_surface_id,
   * spot}`` are injected here from the persisted UUIDs + the inline spot —
   * callers MUST NOT pre-set them.
   */
  equityOptionRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** The persisted server-side UUIDs — stamped onto the local record's appGraph. */
export interface EquityOptionAppGraph {
  /** Saved equity-option id — the by-reference discriminator + price-arm key. */
  equityOptionId: string;
  /** Saved vol-surface id pinned into ``request.pricing.vol_surface_id``. */
  volSurfaceId: string;
  /** local curve key (role) → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'vol_surface' | 'equity_option';
  op: 'create' | 'patch';
  key?: string;
}

export type SaveGraphResult =
  | { ok: true; graph: EquityOptionAppGraph; calls: SaveGraphCall[] }
  | {
      ok: false;
      envelope: ApiErrorEnvelope;
      httpStatus: number;
      stage: SaveGraphCall;
      calls: SaveGraphCall[];
    };

/**
 * The slice of the CRUD surface this flow needs, narrowed to create/patch so
 * tests can inject a mock without standing up the whole client. The real
 * ``crud.*`` exports satisfy this by construction. NB: no ``curveSets`` and no
 * ``swaptionModels`` — equity reads ``pricing.curves`` directly and defaults its
 * model server-side.
 */
export interface EquityOptionCrudClients {
  curves: Pick<
    NamedCrudClient<Schemas['CurveCreate'], Schemas['CurveUpdate'], Schemas['CurveResponse'], unknown>,
    'create' | 'patch'
  >;
  volSurfaces: Pick<
    NamedCrudClient<
      Schemas['VolSurfaceCreate'],
      Schemas['VolSurfaceUpdate'],
      Schemas['VolSurfaceResponse'],
      unknown
    >,
    'create' | 'patch'
  >;
  equityOptions: Pick<
    NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
    'create' | 'patch'
  >;
}

const defaultClients: EquityOptionCrudClients = {
  curves: crud.curves,
  volSurfaces: crud.volSurfaces,
  equityOptions: crud.equityOptions,
};

function failure(
  stage: SaveGraphCall,
  result: Extract<OrchestratorResult<unknown>, { ok: false }>,
  calls: SaveGraphCall[],
): SaveGraphResult {
  return {
    ok: false,
    envelope: result.envelope,
    httpStatus: result.httpStatus,
    stage,
    calls,
  };
}

/**
 * Persist the equity-option entity graph leaf→root and return the stamped UUIDs.
 *
 * When ``prior`` is supplied (a previous save's appGraph) each entity is PATCHed
 * by its known id; otherwise it is created. On any non-2xx the flow stops at the
 * failing stage and returns the structured error envelope.
 */
export async function persistEquityOptionGraph(
  input: EquityOptionGraphInput,
  prior?: EquityOptionAppGraph | null,
  clients: EquityOptionCrudClients = defaultClients,
): Promise<SaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;
  const curveIds: Record<string, string> = {};

  // 1. Curves (leaf). PATCH a known id, else POST a fresh one.
  for (const curve of input.curves) {
    const priorId = prior?.curveIds?.[curve.key];
    const stage: SaveGraphCall = {
      entity: 'curve',
      op: priorId ? 'patch' : 'create',
      key: curve.key,
    };
    calls.push(stage);
    const res = priorId
      ? await clients.curves.patch(priorId, graphCurvePatchBody(curve.body), mutateOpts)
      : await clients.curves.create(graphCurveCreateBody(curve.body, input.name, curve.key), mutateOpts);
    if (!res.ok) return failure(stage, res, calls);
    curveIds[curve.key] = priorId ?? res.data.id;
  }

  // 2. Vol surface (equity-specific leaf). Persisted with its literal
  //    ``{name, kind, payload}`` (base-value-only ``BlackVolSpec``); the
  //    by-reference price loads this row server-side via
  //    ``pricing.vol_surface_id``.
  const volSurfaceStage: SaveGraphCall = {
    entity: 'vol_surface',
    op: prior?.volSurfaceId ? 'patch' : 'create',
  };
  calls.push(volSurfaceStage);
  const volSurfaceRes = prior?.volSurfaceId
    ? await clients.volSurfaces.patch(prior.volSurfaceId, input.volSurface, mutateOpts)
    : await clients.volSurfaces.create(input.volSurface, mutateOpts);
  if (!volSurfaceRes.ok) return failure(volSurfaceStage, volSurfaceRes, calls);
  const volSurfaceId = prior?.volSurfaceId ?? volSurfaceRes.data.id;

  // 3. Equity option (root). Pin the persisted refs into ``request.pricing`` so
  //    the backend's by-reference assembly chains trade → curves → vol_surface
  //    and reads the inline spot. Equity reads ``pricing.curves`` (a role-tagged
  //    list), ``pricing.vol_surface_id`` and inline ``pricing.spot``. NO
  //    curve_set_id / discount_curve_id (those are swaption / cds shapes —
  //    verified against the backend's read path).
  const pricingCurves = input.curves.map(curve => ({
    curve_id: curveIds[curve.key],
    role: curve.key,
  }));
  const existingPricing =
    input.equityOptionRequest.pricing && typeof input.equityOptionRequest.pricing === 'object'
      ? (input.equityOptionRequest.pricing as Record<string, unknown>)
      : {};
  const equityOptionBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.equityOptionRequest,
      pricing: {
        ...existingPricing,
        curves: pricingCurves,
        vol_surface_id: volSurfaceId,
        spot: input.spot,
      },
    },
  };
  const equityOptionStage: SaveGraphCall = {
    entity: 'equity_option',
    op: prior?.equityOptionId ? 'patch' : 'create',
  };
  calls.push(equityOptionStage);
  const equityOptionRes = prior?.equityOptionId
    ? await clients.equityOptions.patch(prior.equityOptionId, equityOptionBody, mutateOpts)
    : await clients.equityOptions.create(equityOptionBody, mutateOpts);
  if (!equityOptionRes.ok) return failure(equityOptionStage, equityOptionRes, calls);
  const equityOptionId = prior?.equityOptionId ?? equityOptionRes.data.id;

  return {
    ok: true,
    graph: { equityOptionId, volSurfaceId, curveIds },
    calls,
  };
}

/**
 * Decide which price arm to use for this equity option.
 *
 *   • saved (appGraph present) → by-reference: a minimal
 *     ``{ equity_option_id, as_of, snapshot_id? }`` body; the orchestrator loads
 *     the trade + curves + vol_surface + spot from storage.
 *   • unsaved → inline: the request the caller already built, verbatim.
 *
 * ``priceEquityOption``/``buildEquityOptionPriceBody`` then key off the presence
 * of ``equity_option_id`` — this only chooses what to hand it.
 */
export function buildEquityOptionPriceArm(opts: {
  appGraph: EquityOptionAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.equityOptionId) {
    const body: Record<string, unknown> = {
      equity_option_id: opts.appGraph.equityOptionId,
      as_of: opts.asOf,
    };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Narrow an untyped ``appGraph`` (from a StoredEntry) to EquityOptionAppGraph. */
export function asEquityOptionAppGraph(raw: unknown): EquityOptionAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.equityOptionId !== 'string' || g.equityOptionId.length === 0) return null;
  if (typeof g.volSurfaceId !== 'string') return null;
  const curveIds: Record<string, string> = {};
  if (g.curveIds && typeof g.curveIds === 'object') {
    for (const [k, v] of Object.entries(g.curveIds as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return { equityOptionId: g.equityOptionId, volSurfaceId: g.volSurfaceId, curveIds };
}
