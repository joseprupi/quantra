// Index registry storage — rate and inflation index definitions.
//
// Backend-backed: the backend index table is the single source of
// truth (scalar columns `name` / `kind` / `currency` / `calendar` /
// `day_counter`; everything else in the JSONB `body`). The local business id
// (e.g. "ESTR", "EURIBOR_6M", user-created ids) stays the entity key — curve
// points and saved products reference indices by this id — and doubles as
// the backend row `name`. The `indexStore` DataStore interface is unchanged;
// only its implementation moved off localStorage.
import type { InflationIndexSpec, IndexDef, TimeUnit } from '../types';
import { generateId, DataStore } from './dataStore';
import { indices as indicesApi } from '../api/crud';
import { createBackendEntityCache } from './backendEntityCache';

export type StoredIndexType = 'IBOR' | 'Overnight' | 'Inflation';

export interface StoredIndexSpec {
  id: string;
  type: StoredIndexType;
  // IBOR fields
  family?: string;           // Euribor, USDLibor, etc.
  tenor_number?: number;
  tenor_time_unit?: string;
  // Overnight fields
  overnight_name?: string;   // SOFR, ESTR, SONIA, etc.
  // Inflation fields
  family_name?: string;
  currency?: string;
  frequency?: string;
  availability_lag?: { n: number; unit: string };
  observation_lag?: { n: number; unit: string };
  interpolated?: boolean;
  revised?: boolean;
  kind?: 'ZeroInflation' | 'YoYInflation';
  underlying_zero_index_id?: string;
  // Common fields
  fixing_days: number;
  calendar: string;
  business_day_convention?: string;
  day_counter: string;
  end_of_month?: boolean;
  description?: string;
  fixings?: { date: string; value: number }[];
  createdAt: string;
  updatedAt: string;
}

export { generateId as generateIndexId };

function normalizeStoredIndexType(raw: any): StoredIndexType {
  const explicitType = String(raw?.type || '').toLowerCase();
  if (explicitType === 'ibor') return 'IBOR';
  if (explicitType === 'overnight') return 'Overnight';
  if (explicitType === 'inflation') return 'Inflation';

  const legacyIndexType = String(raw?.index_type || '').toLowerCase();
  if (legacyIndexType === 'ibor') return 'IBOR';
  if (legacyIndexType === 'overnight') return 'Overnight';

  if (raw?.family_name || raw?.frequency || raw?.observation_lag || raw?.availability_lag || raw?.kind === 'ZeroInflation' || raw?.kind === 'YoYInflation') {
    return 'Inflation';
  }
  if (raw?.overnight_name) return 'Overnight';
  return 'IBOR';
}

function normalizeStoredIndexSpec(raw: any): StoredIndexSpec | null {
  if (!raw || typeof raw !== 'object' || !raw.id) return null;

  const type = normalizeStoredIndexType(raw);
  const tenor = raw.tenor && typeof raw.tenor === 'object' ? raw.tenor : undefined;
  const now = new Date().toISOString();

  return {
    id: raw.id,
    type,
    family: type === 'IBOR' ? (raw.family || raw.name || raw.family_name || raw.id) : undefined,
    tenor_number: type === 'IBOR' ? (raw.tenor_number ?? tenor?.n ?? 0) : undefined,
    tenor_time_unit: type === 'IBOR' ? (raw.tenor_time_unit ?? tenor?.unit ?? 'Months') : undefined,
    overnight_name: type === 'Overnight' ? (raw.overnight_name || raw.name || raw.id) : undefined,
    family_name: type === 'Inflation' ? (raw.family_name || raw.name || raw.id) : undefined,
    currency: raw.currency,
    frequency: type === 'Inflation' ? (raw.frequency || 'Monthly') : undefined,
    availability_lag: type === 'Inflation'
      ? {
          n: raw.availability_lag?.n ?? 2,
          unit: raw.availability_lag?.unit || 'Months',
        }
      : undefined,
    observation_lag: type === 'Inflation'
      ? {
          n: raw.observation_lag?.n ?? 3,
          unit: raw.observation_lag?.unit || 'Months',
        }
      : undefined,
    interpolated: type === 'Inflation' ? (raw.interpolated ?? true) : undefined,
    revised: type === 'Inflation' ? (raw.revised ?? false) : undefined,
    kind: type === 'Inflation' ? (raw.kind || 'ZeroInflation') : undefined,
    underlying_zero_index_id: type === 'Inflation' ? raw.underlying_zero_index_id : undefined,
    fixing_days: raw.fixing_days ?? (type === 'Inflation' ? 0 : 2),
    calendar: raw.calendar || 'TARGET',
    business_day_convention: type === 'Inflation' ? undefined : raw.business_day_convention,
    day_counter: raw.day_counter || (type === 'Inflation' ? 'Actual365Fixed' : 'Actual360'),
    end_of_month: type === 'IBOR' ? raw.end_of_month : undefined,
    description: raw.description,
    fixings: Array.isArray(raw.fixings) ? raw.fixings.filter((fixing: any) => fixing?.date && Number.isFinite(fixing?.value)) : undefined,
    createdAt: raw.createdAt || now,
    updatedAt: raw.updatedAt || now,
  };
}

function normalizeStoredIndexArray(rawItems: any[]): StoredIndexSpec[] {
  const normalized = rawItems
    .map(normalizeStoredIndexSpec)
    .filter((item): item is StoredIndexSpec => !!item);
  const deduped = new Map<string, StoredIndexSpec>();
  for (const item of normalized) deduped.set(item.id, item);
  return Array.from(deduped.values());
}

const indexCache = createBackendEntityCache<StoredIndexSpec, any>({
  client: indicesApi as any,
  fromApi(row) {
    const body = (row.body ?? {}) as Record<string, any>;
    // `source_name` preserves the human family name (e.g. "Euribor") when
    // the unique backend `name` carries the business id ("EURIBOR_6M").
    return normalizeStoredIndexSpec({
      ...body,
      id: body.local_id ?? row.name,
      name: body.source_name ?? body.name,
      type: row.kind,
      currency: row.currency ?? body.currency,
      calendar: row.calendar ?? body.calendar,
      day_counter: row.day_counter ?? body.day_counter,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    });
  },
  toApi(item) {
    return {
      name: item.id,
      kind: item.type,
      currency: item.currency ?? null,
      calendar: item.calendar ?? null,
      day_counter: item.day_counter ?? null,
      body: { ...item, local_id: item.id },
    };
  },
  localId(item) {
    return item.id;
  },
});

export const refreshIndices = (): Promise<StoredIndexSpec[]> => indexCache.refresh();
export const ensureIndicesLoaded = (): Promise<StoredIndexSpec[]> => indexCache.ensureLoaded();

async function readNormalizedIndices(): Promise<StoredIndexSpec[]> {
  await indexCache.ensureLoaded();
  return indexCache.getAll();
}

function inflationIndexSpecToStored(spec: InflationIndexSpec): StoredIndexSpec {
  const now = new Date().toISOString();
  return {
    id: spec.id,
    type: 'Inflation',
    family_name: spec.family_name || spec.id,
    currency: spec.currency || 'EUR',
    frequency: spec.frequency || 'Monthly',
    availability_lag: spec.availability_lag,
    observation_lag: spec.observation_lag,
    interpolated: spec.interpolated,
    revised: spec.revised,
    kind: spec.kind,
    underlying_zero_index_id: spec.underlying_zero_index_id,
    fixing_days: 0,
    calendar: spec.calendar,
    day_counter: spec.day_counter,
    description: undefined,
    fixings: spec.fixings,
    createdAt: now,
    updatedAt: now,
  };
}

export const indexStore: DataStore<StoredIndexSpec> = {
  async getAll(): Promise<StoredIndexSpec[]> {
    return readNormalizedIndices();
  },

  async getById(id: string): Promise<StoredIndexSpec | null> {
    const all = await readNormalizedIndices();
    return all.find(item => item.id === id) || null;
  },

  async save(item: StoredIndexSpec): Promise<void> {
    const normalized = normalizeStoredIndexSpec(item);
    if (!normalized) return;
    await indexCache.ensureLoaded();
    await indexCache.save(normalized);
  },

  async delete(id: string): Promise<void> {
    await indexCache.ensureLoaded();
    await indexCache.remove(id);
  },

  async clear(): Promise<void> {
    await indexCache.ensureLoaded();
    await indexCache.removeAll();
  },
};

function inferRateIndexCurrency(saved: StoredIndexSpec): string {
  if (saved.currency) return saved.currency;
  if (saved.type === 'IBOR') {
    if (saved.family === 'USDLibor') return 'USD';
    if (saved.family === 'GBPLibor') return 'GBP';
    if (saved.family === 'JPYLibor' || saved.family === 'Tibor') return 'JPY';
    return 'EUR';
  }
  if (saved.overnight_name === 'SOFR' || saved.overnight_name === 'FedFunds') return 'USD';
  if (saved.overnight_name === 'SONIA') return 'GBP';
  return 'EUR';
}

function isStoredRateIndex(saved: StoredIndexSpec): boolean {
  return saved.type === 'IBOR' || saved.type === 'Overnight';
}

function isStoredInflationIndex(saved: StoredIndexSpec): boolean {
  return saved.type === 'Inflation';
}

export function storedToRateIndexDef(saved: StoredIndexSpec): IndexDef | null {
  if (!isStoredRateIndex(saved)) return null;

  const isIbor = saved.type === 'IBOR';
  return {
    id: saved.id,
    name: isIbor ? (saved.family || saved.id) : (saved.overnight_name || saved.id),
    index_type: isIbor ? 'Ibor' : 'Overnight',
    currency: inferRateIndexCurrency(saved),
    tenor: {
      n: isIbor ? (saved.tenor_number ?? 0) : 0,
      unit: isIbor ? ((saved.tenor_time_unit as TimeUnit) || 'Months') : 'Days',
    },
    tenor_number: isIbor ? saved.tenor_number : 0,
    tenor_time_unit: isIbor ? (saved.tenor_time_unit as TimeUnit | undefined) : 'Days',
    fixing_days: saved.fixing_days,
    calendar: saved.calendar as IndexDef['calendar'],
    day_counter: saved.day_counter as IndexDef['day_counter'],
    business_day_convention: saved.business_day_convention as IndexDef['business_day_convention'],
    end_of_month: saved.end_of_month,
    fixings: saved.fixings,
  };
}

export function storedToInflationIndexSpec(saved: StoredIndexSpec): InflationIndexSpec | null {
  if (!isStoredInflationIndex(saved)) return null;

  return {
    id: saved.id,
    family_name: saved.family_name || saved.id,
    currency: saved.currency || 'EUR',
    calendar: (saved.calendar || 'TARGET') as InflationIndexSpec['calendar'],
    day_counter: (saved.day_counter || 'Actual365Fixed') as InflationIndexSpec['day_counter'],
    frequency: (saved.frequency || 'Monthly') as InflationIndexSpec['frequency'],
    availability_lag: {
      n: saved.availability_lag?.n ?? 2,
      unit: (saved.availability_lag?.unit || 'Months') as InflationIndexSpec['availability_lag']['unit'],
    },
    observation_lag: {
      n: saved.observation_lag?.n ?? 3,
      unit: (saved.observation_lag?.unit || 'Months') as InflationIndexSpec['observation_lag']['unit'],
    },
    interpolated: saved.interpolated ?? true,
    revised: saved.revised ?? false,
    kind: saved.kind || 'ZeroInflation',
    underlying_zero_index_id: saved.underlying_zero_index_id,
    fixings: saved.fixings,
  };
}

export async function saveInflationIndexSpecs(specs: InflationIndexSpec[]): Promise<void> {
  for (const spec of specs) {
    if (!spec?.id) continue;
    await indexStore.save(inflationIndexSpecToStored(spec));
  }
}

export function exportIndices(indices: StoredIndexSpec[]) {
  const blob = new Blob([JSON.stringify(indices, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `indices-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function importIndices(file: File): Promise<StoredIndexSpec[]> {
  const text = await file.text();
  const data = JSON.parse(text);
  const arr = normalizeStoredIndexArray(Array.isArray(data) ? data : [data]);
  for (const idx of arr) {
    if (!idx.id) idx.id = generateId();
    await indexStore.save(idx);
  }
  return arr;
}
