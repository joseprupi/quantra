// MD-catalog quote options for quote-reference pickers: the canonical ids
// plus their values resolved at the app's global As-Of date ('previous'
// semantics — the latest point at or before that date). Shared by the
// instrument point editor and the value-curve points table so every quote
// dropdown shows the same catalog the same way.
import { useEffect, useState } from 'react';
import {
  getMdBackendSettings,
  listCatalogSeries,
  resolveCatalogValuesAt,
} from '../lib/marketDataBackend';
import { useAsOfDate } from './useAsOfDate';

export interface MdQuoteOption {
  id: string;
  value: number | null;
  resolvedAsOf: string | null;
}

export interface MdQuoteOptionsState {
  quotes: MdQuoteOption[];
  quotesError: string | null;
  asOfDate: string;
}

export function useMdQuoteOptions(): MdQuoteOptionsState {
  const [quotes, setQuotes] = useState<MdQuoteOption[]>([]);
  const [quotesError, setQuotesError] = useState<string | null>(null);
  const { asOfDate } = useAsOfDate();

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const md = getMdBackendSettings();
        const series = await listCatalogSeries(md.baseUrl);
        const ids = series.map(s => s.canonical_id);
        const resolved = await resolveCatalogValuesAt(md.baseUrl, ids, asOfDate);
        if (!alive) return;
        const valueById = new Map(resolved.map(r => [r.canonical_id, r]));
        setQuotes(
          ids.map(id => ({
            id,
            value: valueById.get(id)?.value ?? null,
            resolvedAsOf: valueById.get(id)?.resolved_as_of?.slice(0, 10) ?? null,
          })),
        );
        setQuotesError(null);
      } catch {
        if (!alive) return;
        setQuotes([]);
        setQuotesError('Market-data server unreachable — quote list unavailable.');
      }
    })();
    return () => {
      alive = false;
    };
  }, [asOfDate]);

  return { quotes, quotesError, asOfDate };
}
