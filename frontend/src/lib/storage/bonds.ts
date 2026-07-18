// Bonds storage — backend-backed: saved bond configs are rows
// in app.bonds_fixed / app.bonds_floating, marked `request.__wrapper__` so
// they coexist with (and stay distinct from) the save-graph rows in
// the same tables. The DataStore interface is unchanged for consumers; the
// business id stays the entity key and doubles as the backend row `name`.
import { generateId, DataStore } from './dataStore';
import { bondsFixed as bondsFixedApi, bondsFloating as bondsFloatingApi } from '../api/crud';
import { createBackendEntityCache } from './backendEntityCache';

function makeBondStore<T extends { id: string; createdAt?: string; updatedAt?: string }>(
  client: any,
): DataStore<T> {
  const cache = createBackendEntityCache<T, any>({
    client,
    fromApi(row) {
      const payload = (row.request ?? {}) as Record<string, any>;
      if (payload.__wrapper__ !== true) return null; // save-graph row
      const item = { ...payload } as Record<string, any>;
      delete item.__wrapper__;
      item.id = payload.local_id ?? row.name;
      delete item.local_id;
      item.createdAt = payload.createdAt || row.created_at;
      item.updatedAt = payload.updatedAt || row.updated_at;
      return item as T;
    },
    toApi(item) {
      return { name: item.id, request: { ...item, __wrapper__: true, local_id: item.id } };
    },
    localId(item) {
      return item.id;
    },
  });

  return {
    async getAll(): Promise<T[]> {
      await cache.ensureLoaded();
      return cache.getAll();
    },
    async getById(id: string): Promise<T | null> {
      await cache.ensureLoaded();
      return cache.getById(id);
    },
    async save(item: T): Promise<void> {
      await cache.ensureLoaded();
      await cache.save(item);
    },
    async delete(id: string): Promise<void> {
      await cache.ensureLoaded();
      await cache.remove(id);
    },
    async clear(): Promise<void> {
      await cache.ensureLoaded();
      await cache.removeAll();
    },
  };
}

// Fixed Rate Bond saved configuration
export interface SavedFixedRateBond {
  id: string;
  name: string;
  description?: string;
  // Bond params
  settlementDays: number;
  faceAmount: number;
  couponRate: number;
  accrualDayCounter: string;
  paymentConvention: string;
  redemption: number;
  issueDate: string;
  effectiveDate: string;
  terminationDate: string;
  frequency: string;
  calendar: string;
  convention: string;
  dateGenerationRule: string;
  // Curve reference
  discountCurveId: string;
  // Yield params
  yieldDayCounter: string;
  yieldCompounding: string;
  yieldFrequency: string;
  // id↔uuid bridge. When persisted server-side via the CRUD surface,
  // appId holds the server-minted app.bonds_fixed UUID and appGraph the full
  // {bondId, curveSetId, curveIds} so re-saves PATCH and pricing switches to
  // the by-reference arm. Absent ⇒ inline.
  appId?: string;
  appGraph?: Record<string, unknown>;
  // Timestamps
  createdAt: string;
  updatedAt: string;
}

// Floating Rate Bond saved configuration
export interface SavedFloatingRateBond {
  id: string;
  name: string;
  description?: string;
  // Bond params
  settlementDays: number;
  faceAmount: number;
  spread: number;
  accrualDayCounter: string;
  paymentConvention: string;
  fixingDays: number;
  inArrears: boolean;
  redemption: number;
  issueDate: string;
  effectiveDate: string;
  terminationDate: string;
  frequency: string;
  calendar: string;
  convention: string;
  dateGenerationRule: string;
  // Index — new: just a ref id
  indexRefId?: string;
  // Index params (legacy — kept for backward compat with existing saved data)
  indexPeriodNumber: number;
  indexPeriodTimeUnit: string;
  indexSettlementDays: number;
  indexCalendar: string;
  indexBusinessDayConvention: string;
  indexEndOfMonth: boolean;
  indexDayCounter: string;
  // Curve references
  discountCurveId: string;
  forecastCurveId: string;
  useSameCurve: boolean;
  // id↔uuid bridge. appId = the server-minted floating-bond UUID;
  // appGraph = {bondId, curveSetId, indexId, curveIds}. Present ⇒
  // by-reference pricing; absent ⇒ inline.
  appId?: string;
  appGraph?: Record<string, unknown>;
  // Timestamps
  createdAt: string;
  updatedAt: string;
}

// Create stores (backend-backed)
export const fixedBondStore: DataStore<SavedFixedRateBond> = makeBondStore<SavedFixedRateBond>(bondsFixedApi);
export const floatingBondStore: DataStore<SavedFloatingRateBond> = makeBondStore<SavedFloatingRateBond>(bondsFloatingApi);

// Re-export generateId
export { generateId };

// Export/Import helpers for Fixed Rate Bonds
export function exportFixedBonds(bonds: SavedFixedRateBond[]) {
  const blob = new Blob([JSON.stringify(bonds, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `fixed-bonds-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export async function importFixedBonds(file: File): Promise<SavedFixedRateBond[]> {
  const text = await file.text();
  const data = JSON.parse(text);
  const bonds: SavedFixedRateBond[] = Array.isArray(data) ? data : [];
  if (bonds.length === 0) throw new Error('No valid bonds found in file');
  for (const bond of bonds) {
    if (!bond.id) bond.id = generateId();
    await fixedBondStore.save(bond);
  }
  return bonds;
}

// Export/Import helpers for Floating Rate Bonds
export function exportFloatingBonds(bonds: SavedFloatingRateBond[]) {
  const blob = new Blob([JSON.stringify(bonds, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `floating-bonds-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export async function importFloatingBonds(file: File): Promise<SavedFloatingRateBond[]> {
  const text = await file.text();
  const data = JSON.parse(text);
  const bonds: SavedFloatingRateBond[] = Array.isArray(data) ? data : [];
  if (bonds.length === 0) throw new Error('No valid bonds found in file');
  for (const bond of bonds) {
    if (!bond.id) bond.id = generateId();
    await floatingBondStore.save(bond);
  }
  return bonds;
}
