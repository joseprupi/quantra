// Backend-backed curve store.
//
// The orchestrator's `app.curves` is the SINGLE source of truth; this module
// is a thin adapter over `lib/api/crud.ts#curves` with an in-memory cache so
// the many synchronous call sites (`getSavedCurves()`, `getCurveById()`)
// keep working unchanged. There is NO localStorage persistence here anymore —
// the cache is process-local, non-durable, and refreshed from the API.
//
// Loading contract: pages/flows that read curves must first await
// `ensureCurvesLoaded()` (list pages do this in their load effect). Sync
// reads before the first load resolve to an empty list, exactly like an
// empty localStorage did.
//
// Mapping: scalar columns `name` / `currency` / `day_counter` /
// `reference_date` / `points` live top-level on the backend row; everything
// else the portal `Curve` carries (`role`, `description`, `interpolator`,
// `bootstrap_trait`, `discount_curve_id`, `inflation_curve`) rides in the
// JSONB `body` and is flattened back here.
import { Curve } from '../types';
import { syncInflationConfig } from '../inflation-curves';
import { curves as curvesApi } from '../api/crud';
import type { components } from '../api/_generated/orchestrator';

type CurveResponse = components['schemas']['CurveResponse'];
type CurveCreate = components['schemas']['CurveCreate'];
type CurveUpdate = components['schemas']['CurveUpdate'];

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PAGE_LIMIT = 200;

let cache: Curve[] = [];
let loaded = false;
let inflight: Promise<Curve[]> | null = null;

export function generateId(): string {
  // Only used for local drafts that have not been saved yet; the backend
  // mints the real UUID on create and the draft id is discarded.
  return `curve_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

function isBackendId(id: string | undefined): boolean {
  return !!id && UUID_RE.test(id);
}

function unwrap<T>(result: { ok: true; data: T } | { ok: false; envelope: { error: string; code: string } }): T {
  if (!result.ok) {
    const err = new Error(result.envelope.error) as Error & { code?: string };
    err.code = result.envelope.code;
    throw err;
  }
  return result.data;
}

// Curve points arrive in whatever shape the writing client used: the portal
// stores the flat `tenor_number` / `tenor_time_unit` fields, but rows written
// through the API directly (seeds, scripts, saved pricing graphs) may carry
// the orchestrator's nested `tenor: {n, unit}`. Normalise to the portal-native
// flat shape here so no component ever sees the nested variant (found live:
// a nested-tenor point crashed CurveChart's `tenor_time_unit[0]`).
function normalizePoint(raw: any): any {
  if (!raw || typeof raw !== 'object') return raw;
  const inner = raw.point;
  if (!inner || typeof inner !== 'object') return raw;
  if (inner.tenor_number !== undefined || !inner.tenor) return raw;
  const { tenor, ...rest } = inner;
  return {
    ...raw,
    point: {
      ...rest,
      tenor_number: tenor?.n,
      tenor_time_unit: tenor?.unit,
    },
  };
}

function fromApi(row: CurveResponse): Curve {
  const body = (row.body ?? {}) as Record<string, any>;
  const currency = (row.currency as string) || body.currency || 'EUR';
  return {
    id: row.id as string,
    name: row.name,
    description: body.description,
    currency,
    role: body.role || 'discount',
    day_counter: (row.day_counter as any) || 'Actual365Fixed',
    interpolator: body.interpolator || 'LogLinear',
    bootstrap_trait: body.bootstrap_trait || 'Discount',
    reference_date: (row.reference_date as string) || '',
    points: ((row.points as any[]) || []).map(normalizePoint),
    ...(body.discount_curve_id ? { discount_curve_id: body.discount_curve_id } : {}),
    ...(body.inflation_curve
      ? { inflation_curve: syncInflationConfig(body.inflation_curve, currency) }
      : {}),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function toApiBody(curve: Curve): CurveCreate & CurveUpdate {
  return {
    name: curve.name,
    currency: curve.currency,
    day_counter: curve.day_counter,
    reference_date: curve.reference_date || null,
    points: (curve.points as any[]) || [],
    body: {
      role: curve.role || 'discount',
      interpolator: curve.interpolator || 'LogLinear',
      bootstrap_trait: curve.bootstrap_trait || 'Discount',
      ...(curve.description ? { description: curve.description } : {}),
      ...(curve.discount_curve_id ? { discount_curve_id: curve.discount_curve_id } : {}),
      ...(curve.inflation_curve ? { inflation_curve: curve.inflation_curve } : {}),
    },
  };
}

/** Fetch every page of curves from the backend and replace the cache. */
export async function refreshCurves(): Promise<Curve[]> {
  if (!inflight) {
    inflight = (async () => {
      const rows: CurveResponse[] = [];
      let offset = 0;
      for (;;) {
        const page = unwrap(await curvesApi.list({ limit: PAGE_LIMIT, offset }));
        rows.push(...(page.items as CurveResponse[]));
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

/** Await the first backend load (no-op afterwards). */
export async function ensureCurvesLoaded(): Promise<Curve[]> {
  if (loaded) return cache;
  return refreshCurves();
}

/** Synchronous read of the cached curve list (empty before the first load). */
export function getSavedCurves(): Curve[] {
  return cache;
}

export async function saveCurve(curve: Curve): Promise<Curve> {
  if (isBackendId(curve.id)) {
    const row = unwrap(await curvesApi.patch(curve.id, toApiBody(curve)));
    const updated = fromApi(row as CurveResponse);
    cache = cache.map(c => (c.id === updated.id ? updated : c));
    return updated;
  }
  // Local draft id → create; the backend mints the durable UUID.
  const row = unwrap(await curvesApi.create(toApiBody(curve)));
  const created = fromApi(row as CurveResponse);
  cache = [...cache.filter(c => c.id !== curve.id), created];
  return created;
}

export async function deleteCurve(curveId: string): Promise<void> {
  if (isBackendId(curveId)) {
    unwrap(await curvesApi.delete(curveId));
  }
  cache = cache.filter(c => c.id !== curveId);
}

export function getCurveById(curveId: string): Curve | null {
  return cache.find(c => c.id === curveId) || null;
}

/** Delete every curve the caller owns from the backend (owner-scoped CRUD). */
export async function clearCurves(): Promise<void> {
  for (const curve of [...cache]) {
    await deleteCurve(curve.id);
  }
}

export function exportCurves(curves: Curve[]): void {
  const blob = new Blob([JSON.stringify(curves, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `quantra-curves-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function exportCurve(curve: Curve): void {
  const blob = new Blob([JSON.stringify(curve, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `quantra-curve-${curve.name.replace(/\s+/g, '-').toLowerCase()}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export async function importCurves(file: File): Promise<Curve[]> {
  const content = await file.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    throw new Error('Invalid JSON file');
  }
  const rawCurves: any[] = Array.isArray(parsed) ? parsed : [parsed];
  const saved: Curve[] = [];
  for (const raw of rawCurves) {
    if (!raw || typeof raw !== 'object') continue;
    const currency = raw.currency || 'EUR';
    const candidate: Curve = {
      ...raw,
      // Imported ids are foreign (another browser / a backup) — always
      // create fresh rows; the name-conflict 409 surfaces duplicates.
      id: generateId(),
      role: raw.role || 'discount',
      currency,
      inflation_curve: raw.inflation_curve
        ? syncInflationConfig(raw.inflation_curve, currency)
        : undefined,
      createdAt: raw.createdAt || new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    saved.push(await saveCurve(candidate));
  }
  return saved;
}

/** TEST-ONLY seam: replace the in-memory cache without touching the API. */
export function __setCurvesCacheForTests(curves: Curve[]): void {
  cache = [...curves];
  loaded = true;
}

