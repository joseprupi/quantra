// Backend-backed credit-curve store.
//
// `app.credit_curves` is the single source of truth (scalar columns `name` /
// `reference_entity` / `currency` / `seniority` / `source` / `recovery_rate`;
// `points` / `flat_hazard_rate` ride in the JSONB `body`). Local business ids
// stay the entity key (curve sets carry `credit_curve_ids`) and map onto the
// backend row via the shared cache. NO localStorage.
import { TimeUnit } from '../types';
import { getQuoteBook, getResolutionMode, resolveQuoteValue } from './quoteBook';
import { creditCurves as creditCurvesApi } from '../api/crud';
import { createBackendEntityCache } from './backendEntityCache';

export type CreditCurveSource = 'flat' | 'manual' | 'quote_book';

export interface CreditCurvePoint {
  tenor_number: number;
  tenor_time_unit: TimeUnit;
  spread?: number;
  quote_id?: string;
}

export interface CreditCurveSpec {
  id: string;
  name?: string;
  reference_entity?: string;
  currency?: string;
  seniority?: string;
  source: CreditCurveSource;
  recovery_rate: number;
  flat_hazard_rate?: number;
  points?: CreditCurvePoint[];
  createdAt?: string;
  updatedAt?: string;
}

export interface ResolvedCreditCurve {
  recovery_rate: number;
  flat_hazard_rate?: number;
  quotes?: Array<{
    tenor_number: number;
    tenor_time_unit: TimeUnit;
    quote_type: 'ParSpread';
    quote_id?: string;
    quoted_par_spread: number;
  }>;
}

const cache = createBackendEntityCache<CreditCurveSpec, any>({
  client: creditCurvesApi as any,
  fromApi(row) {
    const body = (row.body ?? {}) as Record<string, any>;
    return {
      id: body.local_id ?? row.name,
      name: row.name,
      reference_entity: row.reference_entity ?? undefined,
      currency: row.currency ?? undefined,
      seniority: row.seniority ?? undefined,
      source: (row.source as CreditCurveSource) || 'manual',
      recovery_rate: row.recovery_rate ?? 0.4,
      ...(body.flat_hazard_rate !== undefined ? { flat_hazard_rate: body.flat_hazard_rate } : {}),
      points: Array.isArray(body.points) ? body.points : [],
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  },
  toApi(curve) {
    return {
      name: curve.name || curve.id,
      reference_entity: curve.reference_entity ?? null,
      currency: curve.currency ?? null,
      seniority: curve.seniority ?? null,
      source: curve.source,
      recovery_rate: curve.recovery_rate,
      body: {
        points: curve.points ?? [],
        ...(curve.flat_hazard_rate !== undefined
          ? { flat_hazard_rate: curve.flat_hazard_rate }
          : {}),
        local_id: curve.id,
      },
    };
  },
  localId(curve) {
    return curve.id;
  },
});

export const refreshCreditCurves = (): Promise<CreditCurveSpec[]> => cache.refresh();
export const ensureCreditCurvesLoaded = (): Promise<CreditCurveSpec[]> => cache.ensureLoaded();

/** Synchronous cache read (empty before the bootstrap preload). */
export function getCreditCurves(): CreditCurveSpec[] {
  return cache.getAll();
}

export function getCreditCurveById(id: string): CreditCurveSpec | null {
  return cache.getById(id);
}

export async function saveCreditCurve(curve: CreditCurveSpec): Promise<CreditCurveSpec> {
  const now = new Date().toISOString();
  return cache.save({
    ...curve,
    createdAt: curve.createdAt || now,
    updatedAt: now,
  });
}

export async function deleteCreditCurve(id: string): Promise<void> {
  await cache.remove(id);
}

/** Deletes every credit curve the caller owns (honest backend semantics). */
export async function clearCreditCurves(): Promise<void> {
  await cache.ensureLoaded();
  await cache.removeAll();
}

/** Backup-import path: upserts each item (no destructive wipe). */
export async function replaceCreditCurves(items: CreditCurveSpec[]): Promise<void> {
  for (const item of items) {
    if (!item?.id) continue;
    await saveCreditCurve(item);
  }
}

export function exportCreditCurves(items: CreditCurveSpec[]) {
  const blob = new Blob([JSON.stringify(items, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `credit-curves-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export async function importCreditCurves(file: File): Promise<CreditCurveSpec[]> {
  const text = await file.text();
  const data = JSON.parse(text);
  const items: CreditCurveSpec[] = Array.isArray(data) ? data : [];
  if (items.length === 0) throw new Error('No valid credit curves found in file');
  await replaceCreditCurves(items);
  return items;
}

export function resolveCreditCurve(spec: CreditCurveSpec, asOfDate: string): ResolvedCreditCurve {
  const recovery_rate = spec.recovery_rate;
  if (spec.source === 'flat') {
    return {
      recovery_rate,
      flat_hazard_rate: spec.flat_hazard_rate ?? 0.0,
    };
  }

  const points = spec.points || [];
  if (points.length === 0) {
    return { recovery_rate, quotes: [] };
  }

  const mode = getResolutionMode();
  const byId = new Map(getQuoteBook().map(e => [e.id, e]));

  const quotes = points
    .map(p => {
      let spread = p.spread;
      if (spec.source === 'quote_book' && p.quote_id) {
        const entry = byId.get(p.quote_id);
        if (entry) {
          const resolved = resolveQuoteValue(entry.series, asOfDate, mode);
          if (resolved !== null) spread = resolved;
        }
      }
      if (spread === undefined || spread === null || Number.isNaN(spread)) return null;
      return {
        tenor_number: p.tenor_number,
        tenor_time_unit: p.tenor_time_unit,
        quote_type: 'ParSpread' as const,
        ...(p.quote_id ? { quote_id: p.quote_id } : {}),
        quoted_par_spread: spread,
      };
    })
    .filter((v): v is {
      tenor_number: number;
      tenor_time_unit: TimeUnit;
      quote_type: 'ParSpread';
      quote_id?: string;
      quoted_par_spread: number;
    } => v !== null);

  return { recovery_rate, quotes };
}
