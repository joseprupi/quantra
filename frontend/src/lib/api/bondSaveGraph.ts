/**
 * Bond (fixed + floating) save → reference flow.
 *
 * Mirrors ``cdsSaveGraph`` / ``irSwapSaveGraph``. "Save" is a real server
 * persist: the entity graph is written to the backend leaf→root through the
 * CRUD wrappers, and the server-minted UUIDs are stamped back onto the local
 * record. Once stamped, the price layer switches that bond to the by-reference
 * arm (a minimal id-only body); an unsaved bond still prices inline (full
 * entities in the request). Both arms coexist — same routes, same server-side
 * assembly, same response handling.
 *
 * Leaf→root order (about id-availability, not FK constraints — refs are soft):
 *   • fixed:    curves → curve_set → bonds_fixed
 *   • floating: curves → curve_set → index → bonds_floating
 *
 * The save shape matches what the backend's bond assembler actually reads
 * (verified against the bonds path, not assumed from swap_ir):
 *   • fixed: the resolver reads, in priority order, an inline
 *     ``pricing.curves[0]``, then ``pricing.discount_curve_id``, then the
 *     ``pricing.curve_set_id`` grouping ref. We pin
 *     ``pricing.{curve_set_id, discount_curve_id}`` (discount_curve_id is the
 *     single-curve read; curve_set_id is the grouping ref and the
 *     ``group_by_curve_set`` key).
 *   • floating: the resolver gathers per-role
 *     ``pricing.discount_curve_id`` + ``pricing.forecast_curve_id``
 *     (role-tagged), with a curve_set fallback, and resolves the projection
 *     index via ``pricing.index_id`` against the stored indices. We pin
 *     ``pricing.{curve_set_id, discount_curve_id, forecast_curve_id,
 *     index_id}``.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by
 * its prior UUID instead of POSTing a duplicate. The prior UUIDs travel on the
 * local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — curve points
 * carry unresolved ``quote_id`` leaves exactly as the inline arm does.
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
   * Stable local key (the role: ``'discount'`` / ``'projection'``). Determines
   * curve_set ref order and the idempotent re-save curve map; the backend
   * reads role markers off the curve_set / pricing block.
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** Leaf→root inputs for one fixed-rate bond vertical. */
export interface BondFixedGraphInput {
  name: string;
  /** Curves to persist, role order (discount first). Fixed uses exactly one. */
  curves: CurveGraphInput[];
  curveSetCurrency?: string;
  /**
   * The saved bond's flat trade body (face_amount / coupon_rate / dates …).
   * ``pricing.{curve_set_id, discount_curve_id}`` are injected here from the
   * persisted UUIDs — callers MUST NOT pre-set them.
   */
  bondRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** Leaf→root inputs for one floating-rate bond vertical. */
export interface BondFloatingGraphInput {
  name: string;
  /** Curves to persist, role order (discount, projection). */
  curves: CurveGraphInput[];
  curveSetCurrency?: string;
  /**
   * The projection index to persist as a stored index row. Same
   * ``{name, kind, currency, calendar, day_counter, body}`` shape the
   * inline ``index`` carries, so by-ref ↔ inline resolve the same index.
   */
  index: Schemas['IndexCreate'];
  /**
   * The saved bond's flat trade body (face_amount / spread / dates …).
   * ``pricing.{curve_set_id, discount_curve_id, forecast_curve_id, index_id}``
   * are injected here — callers MUST NOT pre-set them.
   */
  bondRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** Persisted server-side UUIDs for a fixed bond — stamped onto the local record. */
export interface BondFixedAppGraph {
  /** Saved fixed-bond id — the by-reference discriminator + price-arm key. */
  bondId: string;
  /** Saved curve-set id pinned into ``request.pricing.curve_set_id``. */
  curveSetId: string;
  /** local curve key → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** Persisted server-side UUIDs for a floating bond. */
export interface BondFloatingAppGraph {
  /** Saved floating-bond id — the by-reference discriminator + price-arm key. */
  bondId: string;
  /** Saved curve-set id pinned into ``request.pricing.curve_set_id``. */
  curveSetId: string;
  /** Saved index id pinned into ``request.pricing.index_id``. */
  indexId: string;
  /** local curve key (discount / projection) → saved curve id. */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'curve_set' | 'index' | 'bond_fixed' | 'bond_floating';
  op: 'create' | 'patch';
  key?: string;
}

export type BondFixedSaveGraphResult =
  | { ok: true; graph: BondFixedAppGraph; calls: SaveGraphCall[] }
  | { ok: false; envelope: ApiErrorEnvelope; httpStatus: number; stage: SaveGraphCall; calls: SaveGraphCall[] };

export type BondFloatingSaveGraphResult =
  | { ok: true; graph: BondFloatingAppGraph; calls: SaveGraphCall[] }
  | { ok: false; envelope: ApiErrorEnvelope; httpStatus: number; stage: SaveGraphCall; calls: SaveGraphCall[] };

type CurveClient = Pick<
  NamedCrudClient<Schemas['CurveCreate'], Schemas['CurveUpdate'], Schemas['CurveResponse'], unknown>,
  'create' | 'patch'
>;
type CurveSetClient = Pick<
  NamedCrudClient<Schemas['CurveSetCreate'], Schemas['CurveSetUpdate'], Schemas['CurveSetResponse'], unknown>,
  'create' | 'patch'
>;
type IndexClient = Pick<
  NamedCrudClient<Schemas['IndexCreate'], Schemas['IndexUpdate'], Schemas['IndexResponse'], unknown>,
  'create' | 'patch'
>;
type ProductClient = Pick<
  NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
  'create' | 'patch'
>;

/** CRUD slice the fixed-bond flow needs (create/patch only). */
export interface BondFixedCrudClients {
  curves: CurveClient;
  curveSets: CurveSetClient;
  bondsFixed: ProductClient;
}

/** CRUD slice the floating-bond flow needs (adds indices + bonds_floating). */
export interface BondFloatingCrudClients {
  curves: CurveClient;
  curveSets: CurveSetClient;
  indices: IndexClient;
  bondsFloating: ProductClient;
}

const defaultFixedClients: BondFixedCrudClients = {
  curves: crud.curves,
  curveSets: crud.curveSets,
  bondsFixed: crud.bondsFixed,
};

const defaultFloatingClients: BondFloatingCrudClients = {
  curves: crud.curves,
  curveSets: crud.curveSets,
  indices: crud.indices,
  bondsFloating: crud.bondsFloating,
};

interface CurvesFailure {
  ok: false;
  envelope: ApiErrorEnvelope;
  httpStatus: number;
  stage: SaveGraphCall;
}
interface CurvesOk {
  ok: true;
  curveIds: Record<string, string>;
  curveSetId: string;
}

function failureFrom(
  result: Extract<OrchestratorResult<unknown>, { ok: false }>,
  stage: SaveGraphCall,
): CurvesFailure {
  return { ok: false, envelope: result.envelope, httpStatus: result.httpStatus, stage };
}

/**
 * Shared leaf→root persist of the curves + curve_set (common to both bond
 * routes). Pushes each call onto ``calls`` and returns the stamped curve ids +
 * curve_set id, or the first failing stage.
 */
async function persistCurvesAndSet(
  input: {
    name: string;
    curves: CurveGraphInput[];
    curveSetCurrency?: string;
    changeReason?: string;
  },
  prior: { curveSetId?: string; curveIds?: Record<string, string> } | null | undefined,
  clients: { curves: CurveClient; curveSets: CurveSetClient },
  calls: SaveGraphCall[],
): Promise<CurvesOk | CurvesFailure> {
  const curveIds: Record<string, string> = {};
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;

  // Identity model (see saveGraphNaming): CREATE persists under a unique
  // product-derived name (never the wire's constant role id); re-save PATCHes
  // the REMEMBERED UUID with a name-less body — by-name matching never happens,
  // so an unrelated user curve can never be touched.
  for (const curve of input.curves) {
    const priorId = prior?.curveIds?.[curve.key];
    const stage: SaveGraphCall = { entity: 'curve', op: priorId ? 'patch' : 'create', key: curve.key };
    calls.push(stage);
    const res = priorId
      ? await clients.curves.patch(priorId, graphCurvePatchBody(curve.body), mutateOpts)
      : await clients.curves.create(graphCurveCreateBody(curve.body, input.name, curve.key), mutateOpts);
    if (!res.ok) return failureFrom(res, stage);
    curveIds[curve.key] = priorId ?? res.data.id;
  }

  const curveSetBody: Schemas['CurveSetCreate'] = {
    name: input.name,
    ...(input.curveSetCurrency ? { currency: input.curveSetCurrency } : {}),
    body: {
      curve_refs: input.curves.map(curve => ({ curve_id: curveIds[curve.key], role: curve.key })),
    },
  };
  const curveSetStage: SaveGraphCall = { entity: 'curve_set', op: prior?.curveSetId ? 'patch' : 'create' };
  calls.push(curveSetStage);
  const curveSetRes = prior?.curveSetId
    ? await clients.curveSets.patch(prior.curveSetId, curveSetBody, mutateOpts)
    : await clients.curveSets.create(curveSetBody, mutateOpts);
  if (!curveSetRes.ok) return failureFrom(curveSetRes, curveSetStage);

  return { ok: true, curveIds, curveSetId: prior?.curveSetId ?? curveSetRes.data.id };
}

/**
 * Persist the fixed-rate bond entity graph leaf→root (curves → curve_set →
 * bonds_fixed) and return the stamped UUIDs. PATCHes by prior id when
 * ``prior`` is supplied; short-circuits on the first non-2xx.
 */
export async function persistBondFixedGraph(
  input: BondFixedGraphInput,
  prior?: BondFixedAppGraph | null,
  clients: BondFixedCrudClients = defaultFixedClients,
): Promise<BondFixedSaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;
  const curvesRes = await persistCurvesAndSet(input, prior, clients, calls);
  if (!curvesRes.ok) {
    return { ok: false, envelope: curvesRes.envelope, httpStatus: curvesRes.httpStatus, stage: curvesRes.stage, calls };
  }
  const { curveIds, curveSetId } = curvesRes;
  const discountKey = input.curves[0]?.key;
  const discountCurveId = discountKey ? curveIds[discountKey] : undefined;

  const existingPricing =
    input.bondRequest.pricing && typeof input.bondRequest.pricing === 'object'
      ? (input.bondRequest.pricing as Record<string, unknown>)
      : {};
  const bondBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.bondRequest,
      pricing: {
        ...existingPricing,
        curve_set_id: curveSetId,
        ...(discountCurveId ? { discount_curve_id: discountCurveId } : {}),
      },
    },
  };
  const bondStage: SaveGraphCall = { entity: 'bond_fixed', op: prior?.bondId ? 'patch' : 'create' };
  calls.push(bondStage);
  const bondRes = prior?.bondId
    ? await clients.bondsFixed.patch(prior.bondId, bondBody, mutateOpts)
    : await clients.bondsFixed.create(bondBody, mutateOpts);
  if (!bondRes.ok) {
    return { ok: false, envelope: bondRes.envelope, httpStatus: bondRes.httpStatus, stage: bondStage, calls };
  }
  return { ok: true, graph: { bondId: prior?.bondId ?? bondRes.data.id, curveSetId, curveIds }, calls };
}

/**
 * Persist the floating-rate bond entity graph leaf→root (curves → curve_set →
 * index → bonds_floating) and return the stamped UUIDs. The index persists as
 * a stored index row; the bond pins discount + projection curves + index.
 */
export async function persistBondFloatingGraph(
  input: BondFloatingGraphInput,
  prior?: BondFloatingAppGraph | null,
  clients: BondFloatingCrudClients = defaultFloatingClients,
): Promise<BondFloatingSaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;
  const curvesRes = await persistCurvesAndSet(input, prior, clients, calls);
  if (!curvesRes.ok) {
    return { ok: false, envelope: curvesRes.envelope, httpStatus: curvesRes.httpStatus, stage: curvesRes.stage, calls };
  }
  const { curveIds, curveSetId } = curvesRes;

  // Index leaf (the floating-specific entity).
  const indexStage: SaveGraphCall = { entity: 'index', op: prior?.indexId ? 'patch' : 'create' };
  calls.push(indexStage);
  const indexRes = prior?.indexId
    ? await clients.indices.patch(prior.indexId, input.index, mutateOpts)
    : await clients.indices.create(input.index, mutateOpts);
  if (!indexRes.ok) {
    return { ok: false, envelope: indexRes.envelope, httpStatus: indexRes.httpStatus, stage: indexStage, calls };
  }
  const indexId = prior?.indexId ?? indexRes.data.id;

  const discountCurveId = curveIds['discount'];
  const forecastCurveId = curveIds['projection'] ?? curveIds['discount'];
  const existingPricing =
    input.bondRequest.pricing && typeof input.bondRequest.pricing === 'object'
      ? (input.bondRequest.pricing as Record<string, unknown>)
      : {};
  const bondBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.bondRequest,
      pricing: {
        ...existingPricing,
        curve_set_id: curveSetId,
        ...(discountCurveId ? { discount_curve_id: discountCurveId } : {}),
        ...(forecastCurveId ? { forecast_curve_id: forecastCurveId } : {}),
        index_id: indexId,
      },
    },
  };
  const bondStage: SaveGraphCall = { entity: 'bond_floating', op: prior?.bondId ? 'patch' : 'create' };
  calls.push(bondStage);
  const bondRes = prior?.bondId
    ? await clients.bondsFloating.patch(prior.bondId, bondBody, mutateOpts)
    : await clients.bondsFloating.create(bondBody, mutateOpts);
  if (!bondRes.ok) {
    return { ok: false, envelope: bondRes.envelope, httpStatus: bondRes.httpStatus, stage: bondStage, calls };
  }
  return {
    ok: true,
    graph: { bondId: prior?.bondId ?? bondRes.data.id, curveSetId, indexId, curveIds },
    calls,
  };
}

/** Saved → by-reference ``{ bond_id, as_of, snapshot_id? }``; unsaved → inline verbatim. */
export function buildBondFixedPriceArm(opts: {
  appGraph: BondFixedAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.bondId) {
    const body: Record<string, unknown> = { bond_id: opts.appGraph.bondId, as_of: opts.asOf };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Saved → by-reference ``{ bond_id, as_of, snapshot_id? }``; unsaved → inline verbatim. */
export function buildBondFloatingPriceArm(opts: {
  appGraph: BondFloatingAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.bondId) {
    const body: Record<string, unknown> = { bond_id: opts.appGraph.bondId, as_of: opts.asOf };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

function narrowCurveIds(raw: unknown): Record<string, string> {
  const curveIds: Record<string, string> = {};
  if (raw && typeof raw === 'object') {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return curveIds;
}

/** Narrow an untyped ``appGraph`` (from a saved bond) to BondFixedAppGraph. */
export function asBondFixedAppGraph(raw: unknown): BondFixedAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.bondId !== 'string' || g.bondId.length === 0) return null;
  if (typeof g.curveSetId !== 'string') return null;
  return { bondId: g.bondId, curveSetId: g.curveSetId, curveIds: narrowCurveIds(g.curveIds) };
}

/** Narrow an untyped ``appGraph`` (from a saved bond) to BondFloatingAppGraph. */
export function asBondFloatingAppGraph(raw: unknown): BondFloatingAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.bondId !== 'string' || g.bondId.length === 0) return null;
  if (typeof g.curveSetId !== 'string') return null;
  if (typeof g.indexId !== 'string') return null;
  return {
    bondId: g.bondId,
    curveSetId: g.curveSetId,
    indexId: g.indexId,
    curveIds: narrowCurveIds(g.curveIds),
  };
}
