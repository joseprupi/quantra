/**
 * CDS save → reference flow.
 *
 * Mirrors ``irSwapSaveGraph`` (the swap_ir template) with the one structural
 * difference CDS adds: a **credit-curve** leaf. "Save" is a real server
 * persist — the entity graph is written to the backend leaf→root through the
 * CRUD wrappers, and the server-minted UUIDs are stamped back onto the local
 * record. Once stamped, the price layer switches that cds to the by-reference
 * arm (a minimal id-only body); an unsaved cds still prices inline (full
 * entities in the request). Both arms coexist — same route, same server-side
 * assembly, same response handling.
 *
 * Leaf→root order (about id-availability, not FK constraints — refs are soft):
 *
 *     curves → curve_set → credit_curve → cds
 *
 * A curve_set needs its curves' UUIDs; the cds needs the curve_set's,
 * discount-curve's and credit-curve's UUIDs pinned into ``request.pricing``.
 *
 * The cds entity graph differs from swap_ir in TWO ways:
 *   1. the extra ``credit_curve`` leaf — persisted as a stored credit-curve
 *      row carrying its literal ``flat_hazard_rate`` / par-spread ``points`` +
 *      ``recovery_rate`` (credit curves bypass market-data resolution; the
 *      by-reference price loads this row server-side). No client-side
 *      credit-curve quote resolution.
 *   2. the backend resolves the cds's single discount curve from
 *      ``request.pricing.discount_curve_id`` (or ``pricing.curves[0]``), NOT a
 *      ``curve_set_id``. So the saved cds pins ``discount_curve_id`` (the
 *      single persisted curve UUID) for the live by-reference chain to
 *      resolve. ``curve_set_id`` is also pinned (grouping + parity with the
 *      swap_ir template), and ``credit_curve_id`` is read from
 *      ``pricing.credit_curve_id``.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by
 * its prior UUID instead of POSTing a duplicate. The prior UUIDs travel on the
 * local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — this module
 * posts saved curve points carrying unresolved ``quote_id`` leaves (discount
 * curve) and inline literal credit-curve inputs, exactly as the inline arm
 * does.
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
   * Stable local key (e.g. the role ``'discount'``) used to map the server
   * UUID back on re-save. Determines curve_set ref order; the cds assembler
   * consumes exactly one discount curve (the first entry).
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** The leaf→root inputs the save flow persists for one cds vertical. */
export interface CdsGraphInput {
  /** Display name applied to all four entities (deriveable upstream). */
  name: string;
  /** Curves to persist, in role order (discount first). CDS uses exactly one. */
  curves: CurveGraphInput[];
  /** Currency stamped on the curve_set row (optional). */
  curveSetCurrency?: string;
  /**
   * The credit-curve body to persist as a stored credit-curve row.
   * Carries the literal ``body.flat_hazard_rate`` or ``body.points`` + the
   * ``recovery_rate`` / ``source`` the backend validates — identical to the
   * inline ``credit_curve`` the inline arm sends, so by-ref ↔ inline price
   * the same credit curve.
   */
  creditCurve: Schemas['CreditCurveCreate'];
  /**
   * The saved cds's flat trade body (side / notional / running_coupon /
   * day_counter / business_day_convention / schedule, plus optional upfront).
   * ``pricing.{curve_set_id,discount_curve_id,credit_curve_id}`` are injected
   * here from the persisted UUIDs — callers MUST NOT pre-set them.
   */
  cdsRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** The persisted server-side UUIDs — stamped onto the local record's appGraph. */
export interface CdsAppGraph {
  /** Saved cds id — the by-reference discriminator + price-arm key. */
  cdsId: string;
  /** Saved curve-set id pinned into ``request.pricing.curve_set_id``. */
  curveSetId: string;
  /** Saved credit-curve id pinned into ``request.pricing.credit_curve_id``. */
  creditCurveId: string;
  /** local curve key → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'curve_set' | 'credit_curve' | 'cds';
  op: 'create' | 'patch';
  key?: string;
}

export type SaveGraphResult =
  | { ok: true; graph: CdsAppGraph; calls: SaveGraphCall[] }
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
export interface CdsCrudClients {
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
  creditCurves: Pick<
    NamedCrudClient<
      Schemas['CreditCurveCreate'],
      Schemas['CreditCurveUpdate'],
      Schemas['CreditCurveResponse'],
      unknown
    >,
    'create' | 'patch'
  >;
  cds: Pick<
    NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
    'create' | 'patch'
  >;
}

const defaultClients: CdsCrudClients = {
  curves: crud.curves,
  curveSets: crud.curveSets,
  creditCurves: crud.creditCurves,
  cds: crud.cds,
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
 * Persist the cds entity graph leaf→root and return the stamped UUIDs.
 *
 * When ``prior`` is supplied (a previous save's appGraph) each entity is
 * PATCHed by its known id; otherwise it is created. On any non-2xx the flow
 * stops at the failing stage and returns the structured error envelope.
 */
export async function persistCdsGraph(
  input: CdsGraphInput,
  prior?: CdsAppGraph | null,
  clients: CdsCrudClients = defaultClients,
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

  // 2. Curve set (groups the curves by reference via ``curve_refs[*].curve_id``).
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

  // 3. Credit curve (the cds-specific leaf). Persisted with its literal
  //    hazard/par-spread body + recovery; the by-reference price loads this row
  //    server-side and never consults market data.
  const creditCurveStage: SaveGraphCall = {
    entity: 'credit_curve',
    op: prior?.creditCurveId ? 'patch' : 'create',
  };
  calls.push(creditCurveStage);
  const creditCurveRes = prior?.creditCurveId
    ? await clients.creditCurves.patch(prior.creditCurveId, input.creditCurve, mutateOpts)
    : await clients.creditCurves.create(input.creditCurve, mutateOpts);
  if (!creditCurveRes.ok) return failure(creditCurveStage, creditCurveRes, calls);
  const creditCurveId = prior?.creditCurveId ?? creditCurveRes.data.id;

  // 4. Cds (root). Pin the persisted refs into ``request.pricing`` so the
  //    backend's by-reference assembly chains trade → discount curve + credit
  //    curve. ``discount_curve_id`` is what the backend actually reads for the
  //    discount curve; ``curve_set_id`` is the grouping ref (parity with the
  //    swap_ir template); ``credit_curve_id`` is read from
  //    ``pricing.credit_curve_id``.
  const discountKey = input.curves[0]?.key;
  const discountCurveId = discountKey ? curveIds[discountKey] : undefined;
  const existingPricing =
    input.cdsRequest.pricing && typeof input.cdsRequest.pricing === 'object'
      ? (input.cdsRequest.pricing as Record<string, unknown>)
      : {};
  const cdsBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.cdsRequest,
      pricing: {
        ...existingPricing,
        curve_set_id: curveSetId,
        ...(discountCurveId ? { discount_curve_id: discountCurveId } : {}),
        credit_curve_id: creditCurveId,
      },
    },
  };
  const cdsStage: SaveGraphCall = {
    entity: 'cds',
    op: prior?.cdsId ? 'patch' : 'create',
  };
  calls.push(cdsStage);
  const cdsRes = prior?.cdsId
    ? await clients.cds.patch(prior.cdsId, cdsBody, mutateOpts)
    : await clients.cds.create(cdsBody, mutateOpts);
  if (!cdsRes.ok) return failure(cdsStage, cdsRes, calls);
  const cdsId = prior?.cdsId ?? cdsRes.data.id;

  return { ok: true, graph: { cdsId, curveSetId, creditCurveId, curveIds }, calls };
}

/**
 * Decide which price arm to use for this cds.
 *
 *   • saved (appGraph present) → by-reference: a minimal
 *     ``{ cds_id, as_of, snapshot_id? }`` body; the orchestrator loads the
 *     trade + discount/credit curves from storage.
 *   • unsaved → inline: the request the caller already built, verbatim.
 *
 * ``priceCds``/``buildCdsPriceBody`` then key off the presence of ``cds_id`` —
 * this only chooses what to hand it.
 */
export function buildCdsPriceArm(opts: {
  appGraph: CdsAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.cdsId) {
    const body: Record<string, unknown> = { cds_id: opts.appGraph.cdsId, as_of: opts.asOf };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Narrow an untyped ``appGraph`` (from a StoredEntry) to CdsAppGraph. */
export function asCdsAppGraph(raw: unknown): CdsAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.cdsId !== 'string' || g.cdsId.length === 0) return null;
  if (typeof g.curveSetId !== 'string') return null;
  if (typeof g.creditCurveId !== 'string') return null;
  const curveIds: Record<string, string> = {};
  if (g.curveIds && typeof g.curveIds === 'object') {
    for (const [k, v] of Object.entries(g.curveIds as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return { cdsId: g.cdsId, curveSetId: g.curveSetId, creditCurveId: g.creditCurveId, curveIds };
}
