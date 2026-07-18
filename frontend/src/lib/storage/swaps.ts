// IR Swap storage — backed by the shared wrapped store so each saved
// swap carries a user-editable name plus stable UUID.
import { createWrappedStore, downloadJson, type StoredEntry, type SaveOptions } from './wrappedStore';
import { swapsIr as swapsIrApi } from '../api/crud';

export interface IrSwapRequest {
  pricing?: {
    as_of_date?: string;
    settlement_date?: string;
    indices?: any[];
    curves?: any[];
    quotes?: any[];
  };
  swaps?: any[];
  include_flows?: boolean;
}

const IR_SWAPS_KEY = 'quantra_ir_swaps';

export type StoredSwap = StoredEntry<IrSwapRequest>;

export function deriveIrSwapName(request: IrSwapRequest): string {
  const row = request?.swaps?.[0] as any;
  const kind = row?.ois_swap ? 'OIS' : row?.basis_swap ? 'Basis' : row?.vanilla_swap?.cms_leg ? 'CMS' : 'Vanilla';
  const item = row?.vanilla_swap || row?.ois_swap || row?.basis_swap || {};
  const side = item?.swap_type || 'Swap';
  const indexId =
    item?.floating_leg?.index?.id ||
    item?.overnight_leg?.index?.id ||
    item?.cms_leg?.swap_index_id ||
    item?.leg1?.index?.id ||
    'Index';
  return `${kind} ${side} ${indexId} Swap`;
}

const store = createWrappedStore<IrSwapRequest>({
  client: swapsIrApi,
  storageKey: IR_SWAPS_KEY,
  deriveName: deriveIrSwapName,
});

export async function getIrSwaps(): Promise<StoredSwap[]> {
  return store.getAll();
}


export async function getIrSwapEntryById(id: string): Promise<StoredSwap | null> {
  return store.getById(id);
}

export async function saveIrSwap(request: IrSwapRequest, opts?: SaveOptions): Promise<string> {
  return store.save(request, opts);
}

export async function deleteIrSwap(id: string): Promise<void> {
  return store.delete(id);
}

export async function clearIrSwaps(): Promise<void> {
  return store.clear();
}

export async function replaceIrSwaps(items: Array<IrSwapRequest | StoredSwap>): Promise<void> {
  return store.replaceAll(items);
}

export function exportIrSwaps(items: Array<IrSwapRequest | StoredSwap>) {
  downloadJson(`ir-swaps-${new Date().toISOString().split('T')[0]}.json`, items);
}

export async function importIrSwaps(file: File): Promise<StoredSwap[]> {
  const text = await file.text();
  return store.importJson(text);
}
