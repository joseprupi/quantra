import { createWrappedStore, downloadJson, type StoredEntry, type SaveOptions } from './wrappedStore';
import { cds as cdsApi } from '../api/crud';

export interface CdsRequest {
  pricing?: {
    as_of_date?: string;
    indices?: any[];
    curves?: any[];
    credit_curves?: any[];
    models?: any[];
    quotes?: any[];
  };
  cds_list?: any[];
}

const CDS_KEY = 'quantra_cds';

export type StoredCds = StoredEntry<CdsRequest>;

export function deriveCdsName(request: CdsRequest): string {
  const trade = request?.cds_list?.[0] || {};
  const cds = trade?.cds || {};
  const side = cds?.side || 'Buyer';
  const linkedId = trade?.credit_curve_id || 'CDS';
  return `${side} ${linkedId}`;
}

const store = createWrappedStore<CdsRequest>({
  client: cdsApi,
  storageKey: CDS_KEY,
  deriveName: deriveCdsName,
});

export async function getCdsItems(): Promise<StoredCds[]> {
  return store.getAll();
}


export async function getCdsEntryById(id: string): Promise<StoredCds | null> {
  return store.getById(id);
}

export async function saveCds(request: CdsRequest, opts?: SaveOptions): Promise<string> {
  return store.save(request, opts);
}

export async function deleteCds(id: string): Promise<void> {
  return store.delete(id);
}

export async function clearCds(): Promise<void> {
  return store.clear();
}

export async function replaceCds(items: Array<CdsRequest | StoredCds>): Promise<void> {
  return store.replaceAll(items);
}

export function exportCds(items: Array<CdsRequest | StoredCds>) {
  downloadJson(`cds-${new Date().toISOString().split('T')[0]}.json`, items);
}

export async function importCds(file: File): Promise<StoredCds[]> {
  const text = await file.text();
  return store.importJson(text);
}
