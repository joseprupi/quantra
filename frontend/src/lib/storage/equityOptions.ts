import { createWrappedStore, downloadJson, type StoredEntry, type SaveOptions } from './wrappedStore';
import { equityOptions as equityOptionsApi } from '../api/crud';

export interface EquityOptionRequest {
  pricing?: {
    as_of_date?: string;
    settlement_date?: string;
    curves?: any[];
    vol_surfaces?: any[];
    quotes?: any[];
    indices?: any[];
  };
  options?: any[];
}

const EQUITY_OPTIONS_KEY = 'quantra_equity_options';

export type StoredEquityOption = StoredEntry<EquityOptionRequest>;

export function deriveEquityOptionName(request: EquityOptionRequest): string {
  const option = request?.options?.[0] || {};
  const underlyingId = option?.underlying?.underlying_id || option?.underlying_id || 'Asset';
  const side = option?.option_type || 'Call';
  const strike = option?.strike;
  const expiry = option?.expiry_date;
  const strikePart = strike !== undefined && strike !== null ? ` ${strike}` : '';
  const expiryPart = expiry ? ` ${expiry}` : '';
  return `${underlyingId} ${side}${strikePart}${expiryPart}`.trim();
}

const store = createWrappedStore<EquityOptionRequest>({
  client: equityOptionsApi,
  storageKey: EQUITY_OPTIONS_KEY,
  deriveName: deriveEquityOptionName,
});

export async function listEquityOptions(): Promise<StoredEquityOption[]> {
  return store.getAll();
}


export async function getEquityOptionEntryById(id: string): Promise<StoredEquityOption | null> {
  return store.getById(id);
}

export async function saveEquityOption(request: EquityOptionRequest, opts?: SaveOptions): Promise<string> {
  return store.save(request, opts);
}

export async function deleteEquityOption(id: string): Promise<void> {
  return store.delete(id);
}

export async function clearEquityOptions(): Promise<void> {
  return store.clear();
}

export async function replaceEquityOptions(items: Array<EquityOptionRequest | StoredEquityOption>): Promise<void> {
  return store.replaceAll(items);
}

export function exportEquityOptions(items: Array<EquityOptionRequest | StoredEquityOption>) {
  downloadJson(`equity-options-${new Date().toISOString().split('T')[0]}.json`, items);
}

export async function importEquityOptions(file: File): Promise<StoredEquityOption[]> {
  const text = await file.text();
  return store.importJson(text);
}
