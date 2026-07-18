// Backend-backed swaption-model store.
//
// `app.swaption_models` is the single source of truth (scalar columns
// `name` / `kind`; the full record rides in the JSONB `payload`). The local
// business id stays the entity key (other records reference it, e.g. saved
// swaptions' sourceRefs) and doubles as the backend row `name`; the row UUID
// is tracked inside the shared cache. NO localStorage.
import { swaptionModels as swaptionModelsApi } from '../api/crud';
import { createBackendEntityCache } from './backendEntityCache';

export interface SwaptionModelRecord {
  id: string;
  kind: 'HullWhiteLattice';
  hw_a: number;
  hw_sigma: number;
  rmse?: number;
  num_helpers?: number;
  grid_rows?: number;
  grid_cols?: number;
  grid_points?: number;
  as_of_date?: string;
  vol_surface_id?: string;
  discount_curve_id?: string;
  forwarding_curve_id?: string;
  swap_index_id?: string;
  createdAt: string;
  updatedAt: string;
}

const cache = createBackendEntityCache<SwaptionModelRecord, any>({
  client: swaptionModelsApi as any,
  fromApi(row) {
    const payload = (row.payload ?? {}) as Record<string, any>;
    const record: SwaptionModelRecord = {
      ...payload,
      id: payload.local_id ?? row.name,
      kind: (row.kind as SwaptionModelRecord['kind']) || 'HullWhiteLattice',
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    } as SwaptionModelRecord;
    delete (record as unknown as Record<string, unknown>).local_id;
    return record;
  },
  toApi(model) {
    return {
      name: model.id,
      kind: model.kind || 'HullWhiteLattice',
      payload: { ...model, local_id: model.id },
    };
  },
  localId(model) {
    return model.id;
  },
});

export const refreshSwaptionModels = (): Promise<SwaptionModelRecord[]> => cache.refresh();
export const ensureSwaptionModelsLoaded = (): Promise<SwaptionModelRecord[]> =>
  cache.ensureLoaded();

/** Synchronous cache read (empty before the bootstrap preload). */
export function getSwaptionModels(): SwaptionModelRecord[] {
  return cache.getAll();
}

export function getSwaptionModelById(id: string): SwaptionModelRecord | null {
  return cache.getById(id);
}

export async function saveSwaptionModel(model: SwaptionModelRecord): Promise<SwaptionModelRecord> {
  const now = new Date().toISOString();
  return cache.save({
    ...model,
    createdAt: model.createdAt || now,
    updatedAt: now,
  });
}

export async function deleteSwaptionModel(id: string): Promise<void> {
  await cache.remove(id);
}

/** Delete every swaption model the caller owns from the backend (owner-scoped CRUD). */
export async function clearSwaptionModels(): Promise<void> {
  await cache.removeAll();
}

/** Backup-import path: upserts each record (no destructive wipe). */
export async function replaceSwaptionModels(models: SwaptionModelRecord[]): Promise<void> {
  const now = new Date().toISOString();
  for (const model of models) {
    if (!model || typeof model.id !== 'string' || model.id.trim().length === 0) continue;
    await cache.save({
      ...model,
      createdAt: model.createdAt || now,
      updatedAt: model.updatedAt || now,
    });
  }
}

