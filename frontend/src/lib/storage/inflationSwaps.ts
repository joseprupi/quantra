import { createWrappedStore, downloadJson, type StoredEntry, type SaveOptions } from './wrappedStore';
import { swapsInflation as swapsInflationApi } from '../api/crud';
import { saveInflationIndexSpecs } from './indices';

export interface InflationSwapRequest {
  pricing?: {
    as_of_date?: string;
    settlement_date?: string;
    indices?: any[];
    curves?: any[];
    inflation_indices?: any[];
    inflation_curves?: any[];
    inflation?: {
      inflation_indices?: any[];
      inflation_curves?: any[];
    };
  };
  swaps?: any[];
  include_flows?: boolean;
}

const INFLATION_SWAPS_KEY = 'quantra_inflation_swaps';

export type StoredInflationSwap = StoredEntry<InflationSwapRequest>;

export function deriveInflationSwapName(request: InflationSwapRequest): string {
  const trade = request?.swaps?.[0] as any;
  const yoy = trade?.year_on_year_inflation_swap;
  const zc = trade?.zero_coupon_inflation_swap;
  const side = (yoy || zc)?.swap_type || 'Payer';
  const pricing = request?.pricing as Record<string, any> | undefined;
  const inflation = pricing?.inflation || {
    inflation_curves: pricing?.inflation_curves,
    inflation_indices: pricing?.inflation_indices,
  };
  const indexId =
    inflation?.inflation_indices?.[0]?.id ||
    inflation?.inflation_curves?.[0]?.id ||
    'Inflation';
  if (yoy) return `YoY ${side} ${indexId}`;
  return `ZC ${side} ${indexId}`;
}

function extractInflationIndices(entries: StoredInflationSwap[]) {
  return entries.flatMap(({ request }) => {
    const pricing = request?.pricing;
    const nested = pricing?.inflation?.inflation_indices;
    const direct = pricing?.inflation_indices;
    return Array.isArray(nested) ? nested : (Array.isArray(direct) ? direct : []);
  }).filter((spec) => spec?.id);
}

const store = createWrappedStore<InflationSwapRequest>({
  client: swapsInflationApi,
  storageKey: INFLATION_SWAPS_KEY,
  deriveName: deriveInflationSwapName,
  onChange: async (entries) => {
    try {
      await saveInflationIndexSpecs(extractInflationIndices(entries) as any[]);
    } catch (error) {
      console.warn('Failed to backfill saved inflation indices from swaps', error);
    }
  },
});

export async function getInflationSwaps(): Promise<StoredInflationSwap[]> {
  return store.getAll();
}


export async function getInflationSwapEntryById(id: string): Promise<StoredInflationSwap | null> {
  return store.getById(id);
}

export async function saveInflationSwap(request: InflationSwapRequest, opts?: SaveOptions): Promise<string> {
  return store.save(request, opts);
}

export async function deleteInflationSwap(id: string): Promise<void> {
  return store.delete(id);
}

export async function replaceInflationSwaps(items: Array<InflationSwapRequest | StoredInflationSwap>): Promise<void> {
  return store.replaceAll(items);
}

export async function clearInflationSwaps(): Promise<void> {
  return store.clear();
}

export function exportInflationSwaps(items: Array<InflationSwapRequest | StoredInflationSwap>) {
  downloadJson(`inflation-swaps-${new Date().toISOString().split('T')[0]}.json`, items);
}

export async function importInflationSwaps(file: File): Promise<StoredInflationSwap[]> {
  const text = await file.text();
  return store.importJson(text);
}
