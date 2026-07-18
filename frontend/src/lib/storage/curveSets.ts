// Backend-backed curve-set store.
//
// `app.curve_sets` is the single source of truth. Backend scalar columns are
// `name` / `currency`; everything else (`description`, `as_of_date`,
// `curve_refs`, `credit_curve_ids`, `quote_ids`) lives in the JSONB `body`
// and is flattened back into the portal `CurveSet` shape here. Curve refs are
// soft references (D9) carrying backend curve UUIDs.
//
// Same loading contract as `storage/curves.ts`: await
// `ensureCurveSetsLoaded()` before synchronous reads.
import { Curve, CurveSet, CurveSetCurveRef } from '../types';
import { curveSets as curveSetsApi } from '../api/crud';
import type { components } from '../api/_generated/orchestrator';
import { getSavedCurves } from './curves';

type CurveSetResponse = components['schemas']['CurveSetResponse'];
type CurveSetCreate = components['schemas']['CurveSetCreate'];
type CurveSetUpdate = components['schemas']['CurveSetUpdate'];

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PAGE_LIMIT = 200;

let cache: CurveSet[] = [];
let loaded = false;
let inflight: Promise<CurveSet[]> | null = null;

function generateCurveSetRefId(): string {
  return `csref_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`;
}

export function generateCurveSetId(): string {
  // Draft-only id; the backend mints the durable UUID on create.
  return `cset_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`;
}

function unwrap<T>(result: { ok: true; data: T } | { ok: false; envelope: { error: string; code: string } }): T {
  if (!result.ok) {
    const err = new Error(result.envelope.error) as Error & { code?: string };
    err.code = result.envelope.code;
    throw err;
  }
  return result.data;
}

function normalizeRefs(raw: unknown): CurveSetCurveRef[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((ref: any) => ref?.curve_id)
    .map((ref: any) => ({
      id: ref.id || generateCurveSetRefId(),
      curve_id: ref.curve_id,
      role: ref.role || 'discount',
      ...(ref.label || ref.name ? { label: ref.label || ref.name } : {}),
    }));
}

function fromApi(row: CurveSetResponse): CurveSet {
  const body = (row.body ?? {}) as Record<string, any>;
  return {
    id: row.id as string,
    name: row.name,
    description: body.description,
    currency: (row.currency as string) || 'EUR',
    as_of_date: body.as_of_date || '',
    curve_refs: normalizeRefs(body.curve_refs),
    credit_curve_ids: Array.isArray(body.credit_curve_ids)
      ? body.credit_curve_ids.filter(Boolean)
      : [],
    quote_ids: Array.isArray(body.quote_ids) ? body.quote_ids : [],
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function toApiBody(cs: CurveSet): CurveSetCreate & CurveSetUpdate {
  return {
    name: cs.name,
    currency: cs.currency,
    body: {
      ...(cs.description ? { description: cs.description } : {}),
      ...(cs.as_of_date ? { as_of_date: cs.as_of_date } : {}),
      curve_refs: normalizeRefs(cs.curve_refs),
      credit_curve_ids: Array.isArray(cs.credit_curve_ids)
        ? cs.credit_curve_ids.filter(Boolean)
        : [],
      quote_ids: Array.isArray(cs.quote_ids) ? cs.quote_ids : [],
    },
  };
}

export async function refreshCurveSets(): Promise<CurveSet[]> {
  if (!inflight) {
    inflight = (async () => {
      const rows: CurveSetResponse[] = [];
      let offset = 0;
      for (;;) {
        const page = unwrap(await curveSetsApi.list({ limit: PAGE_LIMIT, offset }));
        rows.push(...(page.items as CurveSetResponse[]));
        if (!page.page.has_more) break;
        offset += PAGE_LIMIT;
      }
      cache = rows.map(fromApi);
      loaded = true;
      return cache;
    })().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

export async function ensureCurveSetsLoaded(): Promise<CurveSet[]> {
  if (loaded) return cache;
  return refreshCurveSets();
}

/** Synchronous read of the cached list (empty before the first load). */
export function getSavedCurveSets(): CurveSet[] {
  return cache;
}

export async function saveCurveSet(cs: CurveSet): Promise<CurveSet> {
  if (cs.id && UUID_RE.test(cs.id)) {
    const row = unwrap(await curveSetsApi.patch(cs.id, toApiBody(cs)));
    const updated = fromApi(row as CurveSetResponse);
    cache = cache.map(s => (s.id === updated.id ? updated : s));
    return updated;
  }
  const row = unwrap(await curveSetsApi.create(toApiBody(cs)));
  const created = fromApi(row as CurveSetResponse);
  cache = [...cache.filter(s => s.id !== cs.id), created];
  return created;
}

export async function deleteCurveSet(id: string): Promise<void> {
  if (UUID_RE.test(id)) {
    unwrap(await curveSetsApi.delete(id));
  }
  cache = cache.filter(s => s.id !== id);
}

export function getCurveSetById(id: string): CurveSet | null {
  return cache.find(s => s.id === id) || null;
}

/** Delete every curve set the caller owns from the backend (owner-scoped CRUD). */
export async function clearCurveSets(): Promise<void> {
  for (const cs of [...cache]) {
    await deleteCurveSet(cs.id);
  }
}

/** TEST-ONLY seam: replace the in-memory cache without touching the API. */
export function __setCurveSetsCacheForTests(sets: CurveSet[]): void {
  cache = [...sets];
  loaded = true;
}

export interface ResolvedCurveSetRef extends CurveSetCurveRef {
  curve: Curve | null;
}

export function resolveCurveSetRefs(cs: CurveSet): ResolvedCurveSetRef[] {
  const savedCurves = getSavedCurves();
  return (cs.curve_refs || []).map(ref => ({
    ...ref,
    curve: savedCurves.find(curve => curve.id === ref.curve_id) || null,
  }));
}

export function resolveCurveSetCurves(cs: CurveSet): Curve[] {
  return resolveCurveSetRefs(cs)
    .map(ref => ref.curve)
    .filter((curve): curve is Curve => curve !== null);
}
