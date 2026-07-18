/**
 * IR-swap save → reference flow.
 *
 * "Save" is a real server persist: the entity graph is written to the backend
 * leaf→root through the CRUD wrappers (curves → curve_set → swap_ir), and the
 * server-minted UUIDs are stamped back onto the local record. Once stamped,
 * the price layer switches that swap to the by-reference arm (a minimal
 * id-only body); an unsaved swap still prices inline (full entities in the
 * request). Both arms coexist — same routes, same server-side assembly, same
 * response handling.
 *
 * Persistence order is about id-availability, not FK constraints (refs are
 * soft): a curve_set needs its curves' UUIDs, and the swap needs the
 * curve_set's UUID, so leaf→root is curves → curve_set → swap.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by
 * its prior UUID instead of POSTing a duplicate. The prior UUIDs travel on
 * the local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — this module
 * posts saved curve points carrying unresolved ``quote_id`` leaves, exactly
 * as the inline arm does.
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
   * Stable local key (e.g. the role ``'discount'`` / ``'forward'``) used to
   * map the server UUID back on re-save. Determines curve_set ref order, which
   * the backend reads positionally (curves[0] = discount role).
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** The leaf→root inputs the save flow persists for one swap_ir vertical. */
export interface IrSwapGraphInput {
  /** Display name applied to all three entities (deriveable upstream). */
  name: string;
  /** Curves to persist, in role order (discount first). */
  curves: CurveGraphInput[];
  /** Currency stamped on the curve_set row (optional). */
  curveSetCurrency?: string;
  /**
   * The saved swap's flat trade body (notional / fixed_rate / swap_type /
   * effective_date / termination_date). ``pricing.curve_set_id`` is injected
   * here from the persisted curve_set UUID — callers MUST NOT pre-set it.
   */
  swapRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (curves / curve_set / swap — one user action, one reason;
   * the shared request id groups them server-side anyway).
   */
  changeReason?: string;
}

/** The persisted server-side UUIDs — stamped onto the local record's appGraph. */
export interface IrSwapAppGraph {
  /** Saved swap id — the by-reference discriminator + price-arm key. */
  swapId: string;
  /** Saved curve-set id pinned into ``request.pricing.curve_set_id``. */
  curveSetId: string;
  /** local curve key → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'curve_set' | 'swap_ir';
  op: 'create' | 'patch';
  key?: string;
}

export type SaveGraphResult =
  | { ok: true; graph: IrSwapAppGraph; calls: SaveGraphCall[] }
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
export interface IrSwapCrudClients {
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
  swapsIr: Pick<
    NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
    'create' | 'patch'
  >;
}

const defaultClients: IrSwapCrudClients = {
  curves: crud.curves,
  curveSets: crud.curveSets,
  swapsIr: crud.swapsIr,
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
 * Persist the swap_ir entity graph leaf→root and return the stamped UUIDs.
 *
 * When ``prior`` is supplied (a previous save's appGraph) each entity is
 * PATCHed by its known id; otherwise it is created. On any non-2xx the flow
 * stops at the failing stage and returns the structured error envelope.
 */
export async function persistIrSwapGraph(
  input: IrSwapGraphInput,
  prior?: IrSwapAppGraph | null,
  clients: IrSwapCrudClients = defaultClients,
): Promise<SaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const curveIds: Record<string, string> = {};
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;

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
      : await clients.curves.create(
          graphCurveCreateBody(curve.body, input.name, curve.key),
          mutateOpts,
        );
    if (!res.ok) return failure(stage, res, calls);
    curveIds[curve.key] = priorId ?? res.data.id;
  }

  // 2. Curve set (groups the curves by reference, D9 ``curve_refs[*].curve_id``).
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

  // 3. Swap (root). Pin the persisted curve_set into ``pricing.curve_set_id``
  //    so the by-reference assembler chains trade → curve_set → curves.
  const existingPricing =
    input.swapRequest.pricing && typeof input.swapRequest.pricing === 'object'
      ? (input.swapRequest.pricing as Record<string, unknown>)
      : {};
  const swapBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.swapRequest,
      pricing: { ...existingPricing, curve_set_id: curveSetId },
    },
  };
  const swapStage: SaveGraphCall = {
    entity: 'swap_ir',
    op: prior?.swapId ? 'patch' : 'create',
  };
  calls.push(swapStage);
  const swapRes = prior?.swapId
    ? await clients.swapsIr.patch(prior.swapId, swapBody, mutateOpts)
    : await clients.swapsIr.create(swapBody, mutateOpts);
  if (!swapRes.ok) return failure(swapStage, swapRes, calls);
  const swapId = prior?.swapId ?? swapRes.data.id;

  return { ok: true, graph: { swapId, curveSetId, curveIds }, calls };
}

/**
 * Decide which price arm to use for this swap.
 *
 *   • saved (appGraph present) → by-reference: a minimal
 *     ``{ swap_id, as_of, snapshot_id? }`` body; the orchestrator loads the
 *     trade + curve chain from storage.
 *   • unsaved → inline: the request the caller already built, verbatim.
 *
 * ``priceIrSwap``/``buildIrSwapPriceBody`` then key off the presence of
 * ``swap_id`` — this only chooses what to hand it.
 */
export function buildIrSwapPriceArm(opts: {
  appGraph: IrSwapAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.swapId) {
    const body: Record<string, unknown> = { swap_id: opts.appGraph.swapId, as_of: opts.asOf };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Narrow an untyped ``appGraph`` (from a StoredEntry) to IrSwapAppGraph. */
export function asIrSwapAppGraph(raw: unknown): IrSwapAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.swapId !== 'string' || g.swapId.length === 0) return null;
  if (typeof g.curveSetId !== 'string') return null;
  const curveIds: Record<string, string> = {};
  if (g.curveIds && typeof g.curveIds === 'object') {
    for (const [k, v] of Object.entries(g.curveIds as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return { swapId: g.swapId, curveSetId: g.curveSetId, curveIds };
}
