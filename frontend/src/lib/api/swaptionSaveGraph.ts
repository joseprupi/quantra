/**
 * Swaption save → reference flow.
 *
 * Mirrors ``cdsSaveGraph`` / ``bondSaveGraph`` / ``irSwapSaveGraph``. "Save" is
 * a real server persist — the entity graph is written to the backend leaf→root
 * through the CRUD wrappers, and the server-minted UUIDs are stamped back onto
 * the local record. Once stamped, the price layer switches that swaption to the
 * by-reference arm (a minimal id-only body); an unsaved swaption still prices
 * inline (full entities in the request). Both arms coexist — same route, same
 * server-side assembly, same response handling.
 *
 * Leaf→root order (about id-availability, not FK constraints — refs are soft):
 *
 *     curves → curve_set → vol_surface → swaption_model → swaption
 *
 * A curve_set needs its curves' UUIDs; the swaption needs the curve_set's,
 * vol-surface's and swaption-model's UUIDs pinned into ``request.pricing``.
 *
 * The save shape matches what the backend's swaption assembler actually reads
 * (verified against the swaption path, not assumed from swap_ir):
 *   • curves: the resolver follows ``pricing.curve_set_id`` to the curve-set
 *     row, then each ``curve_refs[*].curve_id`` to its curve row. It reads
 *     curves ONLY via ``curve_set_id`` (there is no ``discount_curve_id``);
 *     discount/forwarding *roles* are assigned positionally off the resolved
 *     curve list (curves[0]=discount, curves[1]=forwarding). So we pin
 *     ``pricing.curve_set_id`` and persist the curve_refs discount-first
 *     (role-tagged for symmetry with the curve_set).
 *   • vol surface: the resolver follows ``pricing.vol_surface_id`` to the
 *     stored surface row. We pin ``pricing.vol_surface_id``.
 *   • swaption model: the resolver follows ``pricing.swaption_model_id`` to the
 *     stored model row. We pin ``pricing.swaption_model_id``.
 *
 * Model note: the production swaption path is HullWhiteLattice/Explicit, which
 * prices off ``hw_sigma`` and IGNORES the BlackVol surface forwarded alongside.
 * We persist BOTH the vol_surface and the swaption_model faithfully (the same
 * payloads the inline arm sends), so the by-reference price equals the inline
 * price for the same model. Wiring the surface to drive the HW calibration is
 * deferred — not expanded here.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by
 * its prior UUID instead of POSTing a duplicate. The prior UUIDs travel on the
 * local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — curve points
 * carry unresolved ``quote_id`` leaves exactly as the inline arm does, and the
 * vol-surface / model payloads carry pure config (base-value-only).
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
   * Stable local key (the role: ``'discount'`` / ``'forwarding'``). Determines
   * curve_set ref order (discount first → the assembler's positional role
   * assignment) and the idempotent re-save curve map.
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** The leaf→root inputs the save flow persists for one swaption vertical. */
export interface SwaptionGraphInput {
  /** Display name applied to all five entities (deriveable upstream). */
  name: string;
  /** Curves to persist, in role order (discount first; forwarding optional). */
  curves: CurveGraphInput[];
  /** Currency stamped on the curve_set row (optional). */
  curveSetCurrency?: string;
  /**
   * The vol-surface body to persist as a stored vol-surface row. Carries the
   * same ``{name, kind, payload}`` the inline ``vol_surface`` sends
   * (base-value-only ``SwaptionVolSpec``), so by-ref ↔ inline resolve the
   * same surface.
   */
  volSurface: Schemas['VolSurfaceCreate'];
  /**
   * The swaption-model body to persist as a stored swaption-model row
   * (production default ``HullWhiteLattice``). Same ``{name, kind,
   * payload}`` the inline ``swaption_model`` sends.
   */
  swaptionModel: Schemas['SwaptionModelCreate'];
  /**
   * The saved swaption's flat trade body (notional / strike / swap_type /
   * exercise|effective|termination dates …). ``pricing.{curve_set_id,
   * vol_surface_id, swaption_model_id}`` are injected here from the persisted
   * UUIDs — callers MUST NOT pre-set them.
   */
  swaptionRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** The persisted server-side UUIDs — stamped onto the local record's appGraph. */
export interface SwaptionAppGraph {
  /** Saved swaption id — the by-reference discriminator + price-arm key. */
  swaptionId: string;
  /** Saved curve-set id pinned into ``request.pricing.curve_set_id``. */
  curveSetId: string;
  /** Saved vol-surface id pinned into ``request.pricing.vol_surface_id``. */
  volSurfaceId: string;
  /** Saved swaption-model id pinned into ``request.pricing.swaption_model_id``. */
  swaptionModelId: string;
  /** local curve key → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'curve_set' | 'vol_surface' | 'swaption_model' | 'swaption';
  op: 'create' | 'patch';
  key?: string;
}

export type SaveGraphResult =
  | { ok: true; graph: SwaptionAppGraph; calls: SaveGraphCall[] }
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
 * ``crud.*`` exports satisfy this by construction.
 */
export interface SwaptionCrudClients {
  curves: Pick<
    NamedCrudClient<Schemas['CurveCreate'], Schemas['CurveUpdate'], Schemas['CurveResponse'], unknown>,
    'create' | 'patch'
  >;
  curveSets: Pick<
    NamedCrudClient<
      Schemas['CurveSetCreate'],
      Schemas['CurveSetUpdate'],
      Schemas['CurveSetResponse'],
      unknown
    >,
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
  swaptionModels: Pick<
    NamedCrudClient<
      Schemas['SwaptionModelCreate'],
      Schemas['SwaptionModelUpdate'],
      Schemas['SwaptionModelResponse'],
      unknown
    >,
    'create' | 'patch'
  >;
  swaptions: Pick<
    NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
    'create' | 'patch'
  >;
}

const defaultClients: SwaptionCrudClients = {
  curves: crud.curves,
  curveSets: crud.curveSets,
  volSurfaces: crud.volSurfaces,
  swaptionModels: crud.swaptionModels,
  swaptions: crud.swaptions,
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
 * Persist the swaption entity graph leaf→root and return the stamped UUIDs.
 *
 * When ``prior`` is supplied (a previous save's appGraph) each entity is
 * PATCHed by its known id; otherwise it is created. On any non-2xx the flow
 * stops at the failing stage and returns the structured error envelope.
 */
export async function persistSwaptionGraph(
  input: SwaptionGraphInput,
  prior?: SwaptionAppGraph | null,
  clients: SwaptionCrudClients = defaultClients,
): Promise<SaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;
  const curveIds: Record<string, string> = {};

  // 1. Curves (leaf). PATCH a known id, else POST a fresh one.
  // Identity model (see saveGraphNaming): CREATE persists under a unique
  // product-derived name (never the wire's constant role id); re-save PATCHes
  // the REMEMBERED UUID with a name-less body — by-name matching never
  // happens, so an unrelated user curve can never be touched.
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

  // 2. Curve set (groups the curves by reference via ``curve_refs[*].curve_id``;
  //    role-tagged discount-first so the assembler's positional role
  //    assignment lines up).
  const curveSetBody: Schemas['CurveSetCreate'] = {
    name: input.name,
    ...(input.curveSetCurrency ? { currency: input.curveSetCurrency } : {}),
    body: {
      curve_refs: input.curves.map(curve => ({ curve_id: curveIds[curve.key], role: curve.key })),
    },
  };
  const curveSetStage: SaveGraphCall = {
    entity: 'curve_set',
    op: prior?.curveSetId ? 'patch' : 'create',
  };
  calls.push(curveSetStage);
  const curveSetRes = prior?.curveSetId
    ? await clients.curveSets.patch(prior.curveSetId, curveSetBody, mutateOpts)
    : await clients.curveSets.create(curveSetBody, mutateOpts);
  if (!curveSetRes.ok) return failure(curveSetStage, curveSetRes, calls);
  const curveSetId = prior?.curveSetId ?? curveSetRes.data.id;

  // 3. Vol surface (swaption-specific leaf). Persisted with its literal
  //    ``{name, kind, payload}`` (base-value-only); the by-reference price
  //    loads this row server-side via ``pricing.vol_surface_id``.
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

  // 4. Swaption model (swaption-specific leaf, HullWhiteLattice default).
  //    Pure config (not MD-resolved); the by-reference price loads it via
  //    ``pricing.swaption_model_id``.
  const swaptionModelStage: SaveGraphCall = {
    entity: 'swaption_model',
    op: prior?.swaptionModelId ? 'patch' : 'create',
  };
  calls.push(swaptionModelStage);
  const swaptionModelRes = prior?.swaptionModelId
    ? await clients.swaptionModels.patch(prior.swaptionModelId, input.swaptionModel, mutateOpts)
    : await clients.swaptionModels.create(input.swaptionModel, mutateOpts);
  if (!swaptionModelRes.ok) return failure(swaptionModelStage, swaptionModelRes, calls);
  const swaptionModelId = prior?.swaptionModelId ?? swaptionModelRes.data.id;

  // 5. Swaption (root). Pin the persisted refs into ``request.pricing`` so the
  //    backend's by-reference assembly chains trade → curve_set → vol_surface →
  //    swaption_model.
  const existingPricing =
    input.swaptionRequest.pricing && typeof input.swaptionRequest.pricing === 'object'
      ? (input.swaptionRequest.pricing as Record<string, unknown>)
      : {};
  const swaptionBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.swaptionRequest,
      pricing: {
        ...existingPricing,
        curve_set_id: curveSetId,
        vol_surface_id: volSurfaceId,
        swaption_model_id: swaptionModelId,
      },
    },
  };
  const swaptionStage: SaveGraphCall = {
    entity: 'swaption',
    op: prior?.swaptionId ? 'patch' : 'create',
  };
  calls.push(swaptionStage);
  const swaptionRes = prior?.swaptionId
    ? await clients.swaptions.patch(prior.swaptionId, swaptionBody, mutateOpts)
    : await clients.swaptions.create(swaptionBody, mutateOpts);
  if (!swaptionRes.ok) return failure(swaptionStage, swaptionRes, calls);
  const swaptionId = prior?.swaptionId ?? swaptionRes.data.id;

  return {
    ok: true,
    graph: { swaptionId, curveSetId, volSurfaceId, swaptionModelId, curveIds },
    calls,
  };
}

/**
 * Decide which price arm to use for this swaption.
 *
 *   • saved (appGraph present) → by-reference: a minimal
 *     ``{ swaption_id, as_of, snapshot_id? }`` body; the orchestrator loads the
 *     trade + curve_set + vol_surface + swaption_model from storage.
 *   • unsaved → inline: the request the caller already built, verbatim.
 *
 * ``priceSwaption``/``buildSwaptionPriceBody`` then key off the presence of
 * ``swaption_id`` — this only chooses what to hand it.
 */
export function buildSwaptionPriceArm(opts: {
  appGraph: SwaptionAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.swaptionId) {
    const body: Record<string, unknown> = { swaption_id: opts.appGraph.swaptionId, as_of: opts.asOf };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Narrow an untyped ``appGraph`` (from a StoredEntry) to SwaptionAppGraph. */
export function asSwaptionAppGraph(raw: unknown): SwaptionAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.swaptionId !== 'string' || g.swaptionId.length === 0) return null;
  if (typeof g.curveSetId !== 'string') return null;
  if (typeof g.volSurfaceId !== 'string') return null;
  if (typeof g.swaptionModelId !== 'string') return null;
  const curveIds: Record<string, string> = {};
  if (g.curveIds && typeof g.curveIds === 'object') {
    for (const [k, v] of Object.entries(g.curveIds as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return {
    swaptionId: g.swaptionId,
    curveSetId: g.curveSetId,
    volSurfaceId: g.volSurfaceId,
    swaptionModelId: g.swaptionModelId,
    curveIds,
  };
}
