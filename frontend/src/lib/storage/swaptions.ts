import { createWrappedStore, downloadJson, type StoredEntry, type SaveOptions } from './wrappedStore';
import { swaptions as swaptionsApi } from '../api/crud';

export interface SwaptionRequest {
  pricing?: {
    as_of_date?: string;
    swaption_pricing_rebump?: boolean;
    swaption_pricing_details?: boolean;
    indices?: any[];
    curves?: any[];
    vol_surfaces?: any[];
    models?: any[];
  };
  swaptions?: any[];
}

const SWAPTIONS_KEY = 'quantra_swaptions';

export type StoredSwaption = StoredEntry<SwaptionRequest>;

export function deriveSwaptionName(request: SwaptionRequest): string {
  const trade = request?.swaptions?.[0]?.swaption || {};
  const exerciseType = trade?.exercise_type || 'European';
  const underlying = trade?.underlying || trade?.underlying_swap || {};
  const swapType = underlying?.swap_type || 'Swap';
  const isOis = trade?.underlying_type === 'OisSwap';
  const indexId = isOis
    ? (underlying?.overnight_leg?.index?.id || 'Overnight')
    : (underlying?.floating_leg?.index?.id || 'Index');
  return `${exerciseType} ${swapType} ${indexId} Swaption`;
}

const store = createWrappedStore<SwaptionRequest>({
  client: swaptionsApi,
  storageKey: SWAPTIONS_KEY,
  deriveName: deriveSwaptionName,
});

export async function getSwaptions(): Promise<StoredSwaption[]> {
  return store.getAll();
}


export async function getSwaptionEntryById(id: string): Promise<StoredSwaption | null> {
  return store.getById(id);
}

export async function saveSwaption(request: SwaptionRequest, opts?: SaveOptions): Promise<string> {
  return store.save(request, opts);
}

export async function deleteSwaption(id: string): Promise<void> {
  return store.delete(id);
}

export async function clearSwaptions(): Promise<void> {
  return store.clear();
}

export async function replaceSwaptions(items: Array<SwaptionRequest | StoredSwaption>): Promise<void> {
  return store.replaceAll(items);
}

export function exportSwaptions(items: Array<SwaptionRequest | StoredSwaption>) {
  downloadJson(`swaptions-${new Date().toISOString().split('T')[0]}.json`, items);
}

export async function importSwaptions(file: File): Promise<StoredSwaption[]> {
  const text = await file.text();
  return store.importJson(text);
}
