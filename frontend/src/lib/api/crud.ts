/**
 * Typed CRUD client wrappers over the orchestrator `app.*` entity surface.
 *
 * Mirrors the `price<Product>` pattern in `orchestrator.ts`: every wrapper is
 * a thin, fully-typed shim over the generic verb helpers, with request and
 * response shapes pulled straight from the generated OpenAPI client
 * (`_generated/orchestrator.d.ts`) — no hand-rolled entity types.
 *
 * The backend exposes three router shapes:
 *
 *   - 14 named entities  → create / list / get / patch / delete (soft) / restore
 *   - `quote_book`       → get / put                                    [singleton]
 *   - `pricing_history`  → list / get                                  [immutable]
 *
 * That asymmetry is intentional and is reflected in the verb sets below.
 *
 * Error handling branches on `envelope.code`, never on prose;
 * the frontend only ever sends ids / quote-ids, never engine bytes.
 * This module is the wrapper layer only — the save→reference→price flow
 * lives in the per-product SaveGraph/PricingService modules.
 */

import {
  orchestratorDelete,
  orchestratorGet,
  orchestratorPatch,
  orchestratorPost,
} from './orchestrator';
import type { components } from './_generated/orchestrator';
import type { OrchestratorResult } from './types';

type Schemas = components['schemas'];

/** Offset/limit pagination params. Both optional; backend defaults apply. */
export interface ListParams {
  limit?: number;
  offset?: number;
}

/**
 * Optional audit metadata accepted by EVERY mutating verb
 * (create / patch / delete / restore). When `changeReason` is non-empty it is
 * sent as the `X-Change-Reason` header; the backend records it verbatim on the
 * entity's audit-trail version row (visible in the History panel). Omitted —
 * the default — no header is sent and behaviour is identical to before.
 */
export interface MutateOptions {
  changeReason?: string;
}

/**
 * Optimistic-concurrency scaffolding — DEFAULT OFF.
 *
 * When `ifMatch` is supplied, the patch wrapper sends an `If-Match: <token>`
 * header (today the entity's `updated_at`, or a future `version_etag`). When
 * omitted — the default — no precondition header is sent and behaviour is
 * identical to today. The orchestrator PATCH does not yet honour `If-Match`
 * (no 412 path server-side); this is the client half only,
 * pending the backend follow-on. Do not make it mandatory.
 */
export interface PatchOptions extends MutateOptions {
  ifMatch?: string;
}

function withListQuery(path: string, params?: ListParams): string {
  if (!params) return path;
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));
  const qs = query.toString();
  return qs ? `${path}?${qs}` : path;
}

// Verb-set shapes

/** Full six-verb client for the 14 named entities (soft-delete + restore). */
export interface NamedCrudClient<TCreate, TUpdate, TResponse, TListPage> {
  create(body: TCreate, opts?: MutateOptions): Promise<OrchestratorResult<TResponse>>;
  list(params?: ListParams): Promise<OrchestratorResult<TListPage>>;
  get(id: string): Promise<OrchestratorResult<TResponse>>;
  patch(id: string, body: TUpdate, opts?: PatchOptions): Promise<OrchestratorResult<TResponse>>;
  delete(id: string, opts?: MutateOptions): Promise<OrchestratorResult<void>>;
  restore(id: string, opts?: MutateOptions): Promise<OrchestratorResult<TResponse>>;
}

// Factories

function makeNamedCrudClient<TCreate, TUpdate, TResponse, TListPage>(
  basePath: string,
): NamedCrudClient<TCreate, TUpdate, TResponse, TListPage> {
  return {
    create: (body, opts) =>
      orchestratorPost<TResponse>(basePath, body, { changeReason: opts?.changeReason }),
    list: (params) => orchestratorGet<TListPage>(withListQuery(basePath, params)),
    get: (id) => orchestratorGet<TResponse>(`${basePath}/${id}`),
    patch: (id, body, opts) => orchestratorPatch<TResponse>(`${basePath}/${id}`, body, opts),
    delete: (id, opts) => orchestratorDelete(`${basePath}/${id}`, opts),
    restore: (id, opts) =>
      orchestratorPost<TResponse>(`${basePath}/${id}:restore`, {}, {
        changeReason: opts?.changeReason,
      }),
  };
}

// Entity audit trail (versions read API)

/** One audit-trail entry (metadata only; no snapshot payload). */
export type EntityVersionSummary = Schemas['EntityVersionSummary'];
/** One audit-trail entry including the full post-change row snapshot. */
export type EntityVersionDetail = Schemas['EntityVersionDetail'];
/** Version history for one entity, newest first. */
export type EntityVersionList = Schemas['EntityVersionList'];

/**
 * List the full amendment history of one entity, newest first:
 * `GET {entityPath}/{id}/versions`. `entityPath` is the entity's API base
 * path (e.g. `/v1/swaps/ir`, `/v1/curves`). A foreign / unknown id is a 404
 * `not_found` envelope — ids are owner-scoped and not probeable.
 */
export function listVersions(
  entityPath: string,
  id: string,
): Promise<OrchestratorResult<EntityVersionList>> {
  return orchestratorGet<EntityVersionList>(
    `${entityPath}/${encodeURIComponent(id)}/versions`,
  );
}

/**
 * Fetch ONE version of an entity including its full post-change row snapshot
 * (`payload`): `GET {entityPath}/{id}/versions/{versionNo}`. Diffs between
 * versions are computed client-side — the server stores snapshots only.
 */
export function getVersion(
  entityPath: string,
  id: string,
  versionNo: number,
): Promise<OrchestratorResult<EntityVersionDetail>> {
  return orchestratorGet<EntityVersionDetail>(
    `${entityPath}/${encodeURIComponent(id)}/versions/${versionNo}`,
  );
}

/**
 * Restore an entity to the state captured in one of its version snapshots by
 * issuing the entity's normal PATCH with the editable body the caller derived
 * from that snapshot (see `restoreBodyFromSnapshot` — never server-managed
 * fields). Audited as an amend with `X-Change-Reason: restored to v{n}`, so
 * the restore itself appears as a new version on the timeline.
 */
export function restoreEntityVersion<TResponse = Record<string, unknown>>(
  entityPath: string,
  id: string,
  body: Record<string, unknown>,
  versionNo: number,
): Promise<OrchestratorResult<TResponse>> {
  return orchestratorPatch<TResponse>(`${entityPath}/${encodeURIComponent(id)}`, body, {
    changeReason: `restored to v${versionNo}`,
  });
}

// The entity slots
// Prefixes match the backend's data-router registration; types per the generated client.
//
// Reference data (0003) -------------------------------------------------------

export const indices = makeNamedCrudClient<
  Schemas['IndexCreate'],
  Schemas['IndexUpdate'],
  Schemas['IndexResponse'],
  Schemas['ListPage_IndexResponse']
>('/v1/indices');

export const curves = makeNamedCrudClient<
  Schemas['CurveCreate'],
  Schemas['CurveUpdate'],
  Schemas['CurveResponse'],
  Schemas['ListPage_CurveResponse']
>('/v1/curves');

export const curveSets = makeNamedCrudClient<
  Schemas['CurveSetCreate'],
  Schemas['CurveSetUpdate'],
  Schemas['CurveSetResponse'],
  Schemas['ListPage_CurveSetResponse']
>('/v1/curve-sets');

export const creditCurves = makeNamedCrudClient<
  Schemas['CreditCurveCreate'],
  Schemas['CreditCurveUpdate'],
  Schemas['CreditCurveResponse'],
  Schemas['ListPage_CreditCurveResponse']
>('/v1/credit-curves');

// Vol surfaces + short-rate models (0005) -------------------------------------

export const volSurfaces = makeNamedCrudClient<
  Schemas['VolSurfaceCreate'],
  Schemas['VolSurfaceUpdate'],
  Schemas['VolSurfaceResponse'],
  Schemas['ListPage_VolSurfaceResponse']
>('/v1/vol-surfaces');

export const swaptionModels = makeNamedCrudClient<
  Schemas['SwaptionModelCreate'],
  Schemas['SwaptionModelUpdate'],
  Schemas['SwaptionModelResponse'],
  Schemas['ListPage_SwaptionModelResponse']
>('/v1/swaption-models');

// Products (0006) — all seven share the (name, request) ProductCreate shape ---

export const swapsIr = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/swaps/ir');

export const swapsInflation = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/swaps/inflation');

export const swaptions = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/swaptions');

export const bondsFixed = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/bonds/fixed');

export const bondsFloating = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/bonds/floating');

export const cds = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/cds');

export const equityOptions = makeNamedCrudClient<
  Schemas['ProductCreate'],
  Schemas['ProductUpdate'],
  Schemas['ProductResponse'],
  Schemas['ListPage_ProductResponse']
>('/v1/equity-options');
