// Quote Book — time-series quote storage with as-of date resolution
// Each quote id maps to an array of dated {date, value} entries.
// Resolution: for a given as_of_date, pick the appropriate value.
import { QuoteSpec, QuoteType } from '../types';

export interface DatedValue {
  date: string;   // YYYY-MM-DD
  value: number;
}

export type QuoteResolutionMode = 'previous' | 'exact';

export interface QuoteBookEntry {
  id: string;
  kind: string;          // 'Rate' | 'Spread' | 'Price' | etc.
  quote_type?: QuoteType;
  resolution_mode?: QuoteResolutionMode;
  label?: string;
  currency?: string;
  description?: string;
  series: DatedValue[];  // sorted by date ascending
}

const QUOTE_BOOK_KEY = 'quantra_quote_book';
const RESOLUTION_MODE_KEY = 'quantra_quote_resolution_mode';

// Legacy flat-quote fallback
// The "Market Data → Quotes" authoring app and its storage module
// (`lib/storage/quotes.ts`) were removed. Pre-existing `quantra_quotes` data
// stays READABLE so quote-id labels/pickers keep working (curve previews are
// routed through the orchestrator). Read-only: nothing writes this key
// anymore.
const LEGACY_FLAT_QUOTES_KEY = 'quantra_quotes';

export function getLegacyFlatQuotes(): QuoteSpec[] {
  try {
    const data = localStorage.getItem(LEGACY_FLAT_QUOTES_KEY);
    return data ? (JSON.parse(data) as QuoteSpec[]) : [];
  } catch {
    return [];
  }
}

// ============================================================================
// Resolution logic
// ============================================================================

/**
 * Resolve a single quote's value for a given as-of date.
 * "previous": last value with date <= asOfDate
 * "exact": value on exactly asOfDate
 */
export function resolveQuoteValue(
  series: DatedValue[],
  asOfDate: string,
  mode: QuoteResolutionMode = 'previous'
): number | null {
  if (!series || series.length === 0) return null;

  if (mode === 'exact') {
    const match = series.find(s => s.date === asOfDate);
    return match ? match.value : null;
  }

  // "previous" mode: find last entry with date <= asOfDate
  // Series should be sorted ascending
  let result: number | null = null;
  for (const entry of series) {
    if (entry.date <= asOfDate) {
      result = entry.value;
    } else {
      break;
    }
  }
  return result;
}

// ============================================================================
// Persistence
// ============================================================================

function sortSeries(series: DatedValue[]): DatedValue[] {
  return [...series].sort((a, b) => a.date.localeCompare(b.date));
}

export function getQuoteBook(): QuoteBookEntry[] {
  try {
    const raw = localStorage.getItem(QUOTE_BOOK_KEY);
    if (!raw) return [];
    const entries: QuoteBookEntry[] = JSON.parse(raw);
    // Ensure series are sorted
    return entries.map(e => ({ ...e, series: sortSeries(e.series || []) }));
  } catch {
    return [];
  }
}

export function saveQuoteBook(entries: QuoteBookEntry[]): void {
  // Ensure sorted before saving
  const sorted = entries.map(e => ({ ...e, series: sortSeries(e.series || []) }));
  localStorage.setItem(QUOTE_BOOK_KEY, JSON.stringify(sorted));
}

// ============================================================================
// Resolution mode
// ============================================================================

export function getResolutionMode(): QuoteResolutionMode {
  try {
    const mode = localStorage.getItem(RESOLUTION_MODE_KEY);
    if (mode === 'exact' || mode === 'previous') return mode;
    return 'previous';
  } catch {
    return 'previous';
  }
}
