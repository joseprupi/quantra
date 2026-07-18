/**
 * Inflation-swap save → reference flow.
 *
 * Mirrors ``equityOptionSaveGraph`` / ``swaptionSaveGraph`` / ``cdsSaveGraph`` /
 * ``bondSaveGraph`` / ``irSwapSaveGraph``. "Save" is a real server persist —
 * the entity graph is written to the backend leaf→root through the CRUD
 * wrappers, and the server-minted UUIDs are stamped back onto the local record.
 * Once stamped, the price layer switches that inflation swap to the
 * by-reference arm (a minimal id-only body); an unsaved swap still prices
 * inline (full entities in the request). Both arms coexist — same route, same
 * server-side assembly, same response handling.
 *
 * Leaf→root order (about id-availability, not FK constraints — refs are soft):
 *
 *     curves → index → swaps_inflation
 *
 * The swap needs its curves' + inflation index's UUIDs pinned into
 * ``request.pricing`` before it can be persisted.
 *
 * The save shape matches what the backend's inflation-swap assembler actually
 * reads (verified against the inflation path, not assumed from a sibling):
 *   • curves: ``pricing.curves`` is a **role-tagged LIST of curve refs**
 *     ``[{curve_id, role}]`` read directly off the saved trade. Inflation swaps
 *     do NOT use a ``curve_set_id`` (swaption) and do NOT use a
 *     ``discount_curve_id`` (cds); the assembler buckets the list into
 *     nominal/inflation by the per-entry ``role`` and resolves each
 *     ``curve_id`` against the stored curves. Both curves are required (no
 *     default for either). Note the **nominal** curve MUST span the trade
 *     maturity or the engine ABORTs "past max curve time" — we persist whatever
 *     span the page supplies, so the page is responsible for a long-enough
 *     nominal curve.
 *   • inflation index: the resolver accepts an inline ``pricing.inflation_index``
 *     block or the ``pricing.inflation_index_id`` scalar, which it resolves
 *     against the stored indices (kind enforced ``Inflation``). We pin the
 *     **scalar** ``pricing.inflation_index_id`` (the cleanest by-ref form).
 *   • swap_kind: the resolver reads **top-level** ``swap_request.swap_kind``,
 *     falling back to nested ``swaps[*]`` detection, then to the default
 *     ``"zero_coupon"``. We persist ``swap_kind`` at the **top level** of the
 *     saved trade ``request`` (ZCIIS / YYIIS faithfully round-tripped), NOT
 *     under ``pricing`` (the assembler reads it off the request root).
 *
 * Engine caveat: ZCIIS is the live-good path; YYIIS still 502s on the engine
 * (a known engine abort). The save flow is kind-agnostic — it persists both
 * faithfully; the 502 (if any) surfaces cleanly on the by-ref price call,
 * identical to inline.
 *
 * Idempotency: a re-save of an already-stamped record PATCHes each entity by its
 * prior UUID instead of POSTing a duplicate. The prior UUIDs travel on the
 * local record's ``appGraph``.
 *
 * Errors branch on ``envelope.code``; the first failing CRUD call
 * short-circuits and is surfaced verbatim with the stage that failed. The
 * frontend only ever sends ids / quote-ids, never engine bytes — curve points
 * carry unresolved ``quote_id`` leaves exactly as the inline arm does, and the
 * inflation index body carries pure config + historical fixings (the index
 * body is consumed verbatim by the engine, never market-data-resolved).
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
   * Stable local key (the role: ``'nominal'`` / ``'inflation'``). Becomes the
   * ``role`` on the ``pricing.curves`` ref and the idempotent re-save curve
   * map key.
   */
  key: string;
  body: Schemas['CurveCreate'];
}

/** The leaf→root inputs the save flow persists for one inflation-swap vertical. */
export interface SwapsInflationGraphInput {
  /** Display name applied to all entities (deriveable upstream). */
  name: string;
  /** Curves to persist, in role order (nominal first; inflation second). */
  curves: CurveGraphInput[];
  /**
   * The inflation-index body to persist as a stored index row. Carries the
   * same ``{name, kind:'Inflation', currency?, day_counter?, body}`` the
   * inline ``inflation_index`` sends (the body holds the historical CPI fixings
   * + engine-side conventions), so by-ref ↔ inline resolve the same index.
   */
  index: Schemas['IndexCreate'];
  /**
   * The swap-kind discriminator persisted at the **top level** of the saved
   * trade request (``"zero_coupon"`` = ZCIIS, ``"year_on_year"`` = YYIIS). The
   * backend reads it off the request root.
   */
  swapKind: 'zero_coupon' | 'year_on_year';
  /**
   * The saved swap's flat trade body (``swaps[]`` + ``include_flows`` …).
   * ``swap_kind`` (top-level) and ``pricing.{curves, inflation_index_id}`` are
   * injected here from the persisted UUIDs — callers MUST NOT pre-set them.
   */
  swapsInflationRequest: Record<string, unknown>;
  /**
   * Optional audit reason. Sent as ``X-Change-Reason`` on EVERY write this
   * save performs (one user action, one reason; the shared request id
   * groups them server-side anyway).
   */
  changeReason?: string;
}

/** The persisted server-side UUIDs — stamped onto the local record's appGraph. */
export interface SwapsInflationAppGraph {
  /** Saved inflation-swap id — the by-reference discriminator + price-arm key. */
  swapsInflationId: string;
  /** Saved index id pinned into ``request.pricing.inflation_index_id``. */
  indexId: string;
  /** local curve key (role) → saved curve id (idempotent re-save map). */
  curveIds: Record<string, string>;
}

/** One CRUD call the save flow made, in order — for assertion + telemetry. */
export interface SaveGraphCall {
  entity: 'curve' | 'index' | 'swaps_inflation';
  op: 'create' | 'patch';
  key?: string;
}

export type SaveGraphResult =
  | { ok: true; graph: SwapsInflationAppGraph; calls: SaveGraphCall[] }
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
 * ``crud.*`` exports satisfy this by construction. NB: no ``curveSets`` (inflation
 * reads ``pricing.curves`` directly) and the index is a first-class stored
 * entity, not inline-only.
 */
export interface SwapsInflationCrudClients {
  curves: Pick<
    NamedCrudClient<Schemas['CurveCreate'], Schemas['CurveUpdate'], Schemas['CurveResponse'], unknown>,
    'create' | 'patch'
  >;
  indices: Pick<
    NamedCrudClient<Schemas['IndexCreate'], Schemas['IndexUpdate'], Schemas['IndexResponse'], unknown>,
    'create' | 'patch'
  >;
  swapsInflation: Pick<
    NamedCrudClient<Schemas['ProductCreate'], Schemas['ProductUpdate'], Schemas['ProductResponse'], unknown>,
    'create' | 'patch'
  >;
}

const defaultClients: SwapsInflationCrudClients = {
  curves: crud.curves,
  indices: crud.indices,
  swapsInflation: crud.swapsInflation,
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
 * Persist the inflation-swap entity graph leaf→root and return the stamped UUIDs.
 *
 * When ``prior`` is supplied (a previous save's appGraph) each entity is PATCHed
 * by its known id; otherwise it is created. On any non-2xx the flow stops at the
 * failing stage and returns the structured error envelope.
 */
export async function persistSwapsInflationGraph(
  input: SwapsInflationGraphInput,
  prior?: SwapsInflationAppGraph | null,
  clients: SwapsInflationCrudClients = defaultClients,
): Promise<SaveGraphResult> {
  const calls: SaveGraphCall[] = [];
  const mutateOpts = input.changeReason ? { changeReason: input.changeReason } : undefined;
  const curveIds: Record<string, string> = {};

  // 1. Curves (leaf). PATCH a known id, else POST a fresh one. The nominal
  //    curve body is persisted with whatever span the caller supplied — a too-short
  //    nominal curve surfaces later as an engine ABORT, not a save error.
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

  // 2. Inflation index (inflation-specific leaf). Persisted with its literal
  //    ``{name, kind:'Inflation', …, body}`` (the body carries historical CPI
  //    fixings + conventions, consumed verbatim by the engine); the by-reference
  //    price loads this row server-side via ``pricing.inflation_index_id``.
  const indexStage: SaveGraphCall = {
    entity: 'index',
    op: prior?.indexId ? 'patch' : 'create',
  };
  calls.push(indexStage);
  const indexRes = prior?.indexId
    ? await clients.indices.patch(prior.indexId, input.index, mutateOpts)
    : await clients.indices.create(input.index, mutateOpts);
  if (!indexRes.ok) return failure(indexStage, indexRes, calls);
  const indexId = prior?.indexId ?? indexRes.data.id;

  // 3. Swaps_inflation (root). Pin the persisted refs into ``request.pricing``
  //    and ``swap_kind`` at the request root so the backend's by-reference
  //    assembly chains trade → curves → index. Inflation reads ``pricing.curves``
  //    (a role-tagged list), ``pricing.inflation_index_id`` and top-level
  //    ``swap_kind``. NO curve_set_id / discount_curve_id (those are swaption /
  //    cds shapes — verified against the backend's read path).
  const pricingCurves = input.curves.map(curve => ({
    curve_id: curveIds[curve.key],
    role: curve.key,
  }));
  const existingPricing =
    input.swapsInflationRequest.pricing && typeof input.swapsInflationRequest.pricing === 'object'
      ? (input.swapsInflationRequest.pricing as Record<string, unknown>)
      : {};
  const swapBody: Schemas['ProductCreate'] = {
    name: input.name,
    request: {
      ...input.swapsInflationRequest,
      swap_kind: input.swapKind,
      pricing: {
        ...existingPricing,
        curves: pricingCurves,
        inflation_index_id: indexId,
      },
    },
  };
  const swapStage: SaveGraphCall = {
    entity: 'swaps_inflation',
    op: prior?.swapsInflationId ? 'patch' : 'create',
  };
  calls.push(swapStage);
  const swapRes = prior?.swapsInflationId
    ? await clients.swapsInflation.patch(prior.swapsInflationId, swapBody, mutateOpts)
    : await clients.swapsInflation.create(swapBody, mutateOpts);
  if (!swapRes.ok) return failure(swapStage, swapRes, calls);
  const swapsInflationId = prior?.swapsInflationId ?? swapRes.data.id;

  return {
    ok: true,
    graph: { swapsInflationId, indexId, curveIds },
    calls,
  };
}

/**
 * Decide which price arm to use for this inflation swap.
 *
 *   • saved (appGraph present) → by-reference: a minimal
 *     ``{ swap_id, as_of, snapshot_id? }`` body; the orchestrator loads the trade
 *     + curves + index from storage.
 *   • unsaved → inline: the request the caller already built, verbatim.
 *
 * ``priceSwapsInflation``/``buildSwapsInflationPriceBody`` then key off the
 * presence of ``swap_id`` — this only chooses what to hand it.
 */
export function buildSwapsInflationPriceArm(opts: {
  appGraph: SwapsInflationAppGraph | null | undefined;
  inlineRequest: unknown;
  asOf: string;
  snapshotId?: string;
}): unknown {
  if (opts.appGraph?.swapsInflationId) {
    const body: Record<string, unknown> = {
      swap_id: opts.appGraph.swapsInflationId,
      as_of: opts.asOf,
    };
    if (opts.snapshotId) body.snapshot_id = opts.snapshotId;
    return body;
  }
  return opts.inlineRequest;
}

/** Narrow an untyped ``appGraph`` (from a StoredEntry) to SwapsInflationAppGraph. */
export function asSwapsInflationAppGraph(raw: unknown): SwapsInflationAppGraph | null {
  if (!raw || typeof raw !== 'object') return null;
  const g = raw as Record<string, unknown>;
  if (typeof g.swapsInflationId !== 'string' || g.swapsInflationId.length === 0) return null;
  if (typeof g.indexId !== 'string') return null;
  const curveIds: Record<string, string> = {};
  if (g.curveIds && typeof g.curveIds === 'object') {
    for (const [k, v] of Object.entries(g.curveIds as Record<string, unknown>)) {
      if (typeof v === 'string') curveIds[k] = v;
    }
  }
  return { swapsInflationId: g.swapsInflationId, indexId: g.indexId, curveIds };
}
