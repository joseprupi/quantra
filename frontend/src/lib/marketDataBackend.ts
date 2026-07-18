import { QuoteKind, QuoteType } from './types';
import { getQuoteBook, getResolutionMode, resolveQuoteValue, saveQuoteBook } from './storage/quoteBook';
import { getMarketDataUrl, runtimeConfig } from './runtimeConfig';

const MD_ENABLED_KEY = 'quantra_md_backend_enabled';
const MD_URL_KEY = 'quantra_md_backend_url';


export interface MdBackendSettings {
  enabled: boolean;
  baseUrl: string;
}

interface MdResolvedItemResponse {
  canonical_id: string;
  requested_as_of: string;
  found: boolean;
  is_exact: boolean;
  resolved_as_of?: string | null;
  value?: number | null;
  source?: string | null;
  vendor_id?: string | null;
}

interface MdResolvedBatchResponse {
  items: MdResolvedItemResponse[];
}

export interface PricingQuoteSnapshotItem {
  id: string;
  kind: string;
  value: number;
  quote_type?: QuoteType;
}

export interface MdPricingResolveResult {
  quotes: PricingQuoteSnapshotItem[];
  localHits: number;
  backendHits: number;
  missingIds: string[];
}

function normalizeBaseUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim();
  const noSlash = trimmed.endsWith('/') ? trimmed.slice(0, -1) : trimmed;
  // Prevent mixed-content failures when app runs on HTTPS.
  if (noSlash.startsWith('http://')) {
    const host = noSlash.slice('http://'.length).split('/')[0].toLowerCase();
    const isLocal =
      host.startsWith('localhost') ||
      host.startsWith('127.0.0.1') ||
      host.startsWith('10.') ||
      host.startsWith('192.168.') ||
      /^172\.(1[6-9]|2[0-9]|3[0-1])\./.test(host);
    if (!isLocal) return `https://${noSlash.slice('http://'.length)}`;
  }
  return noSlash;
}

async function fetchJson<T>(baseUrl: string, path: string): Promise<T> {
  const response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`);
  if (!response.ok) {
    throw new Error(`Market data backend request failed (${response.status}) for ${path}`);
  }
  return (await response.json()) as T;
}

async function postJson<T>(baseUrl: string, path: string, payload: unknown): Promise<T> {
  const response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Market data backend request failed (${response.status}) for ${path}`);
  }
  return (await response.json()) as T;
}

function inferQuoteKind(canonicalId: string): QuoteKind {
  const upper = canonicalId.toUpperCase();
  if (upper.includes('RATE') || upper.includes('YIELD')) return 'Rate';
  if (upper.includes('SPREAD')) return 'Spread';
  return 'Price';
}

function normalizeQuoteKind(raw: string | undefined, canonicalId: string): QuoteKind {
  if (raw === 'Rate' || raw === 'Spread' || raw === 'Price' || raw === 'FxSpot' || raw === 'FxPoints') {
    return raw;
  }
  return inferQuoteKind(canonicalId);
}

function normalizeSyncedValue(kind: QuoteKind, value: number): number {
  // Defensive normalization: if backend snapshot still contains percent values
  // (e.g. 3.65 for 3.65%), convert to decimal for portal conventions.
  if ((kind === 'Rate' || kind === 'Spread') && Math.abs(value) > 1) {
    return value / 100.0;
  }
  return value;
}

export function getMdBackendSettings(): MdBackendSettings {
  const rawEnabled = localStorage.getItem(MD_ENABLED_KEY);
  const enabled = rawEnabled === null ? true : rawEnabled === 'true';

  // Base-URL precedence — MUST mirror getOrchestratorUrl()/getMarketDataUrl():
  //   • When the runtime `config.js` injects `marketDataUrl` (KEY present — the
  //     self-hosted bundle always does, default `/_md`), it is AUTHORITATIVE and
  //     wins over any localStorage value. This is the bug fix: browsers that
  //     used the OLD same-origin portal at app.quantra.io carry a stale
  //     cross-origin `MD_URL_KEY` (e.g. https://market.quantra.io) which is now
  //     dead → CORS. The runtime same-origin value must override it.
  //   • When NO runtime key is injected (dev `npm run dev` / public hosted),
  //     the localStorage override (Settings) still wins, then build-time VITE —
  //     preserving the pre-fix behaviour exactly.
  const runtimeUrl = runtimeConfig().marketDataUrl;
  const runtimePresent = typeof runtimeUrl === 'string';
  const storedUrl = localStorage.getItem(MD_URL_KEY);
  const rawBaseUrl = runtimePresent ? getMarketDataUrl() : storedUrl || getMarketDataUrl();
  const normalizedBaseUrl = normalizeBaseUrl(rawBaseUrl);

  // Self-heal missing/legacy local settings so HTTPS pages can always issue
  // backend fetches (and avoid mixed-content blocks from http URLs).
  if (rawEnabled === null) {
    localStorage.setItem(MD_ENABLED_KEY, 'true');
  }
  if (runtimePresent) {
    // A runtime same-origin URL is authoritative → don't let a stale
    // localStorage value linger (it would still shadow any direct MD_URL_KEY
    // reads). Overwrite it with the runtime-derived value; if that normalizes
    // to an empty base, drop the key entirely.
    if (storedUrl == null || normalizeBaseUrl(storedUrl) !== normalizedBaseUrl) {
      if (normalizedBaseUrl) {
        localStorage.setItem(MD_URL_KEY, normalizedBaseUrl);
      } else {
        localStorage.removeItem(MD_URL_KEY);
      }
    }
  } else if (!storedUrl || normalizedBaseUrl !== storedUrl) {
    // Dev / hosted: keep the existing self-heal (persist a missing value, or an
    // http→https-normalized one). Runtime config is absent, so never touch it
    // for a same-origin-override reason.
    localStorage.setItem(MD_URL_KEY, normalizedBaseUrl);
  }
  return { enabled, baseUrl: normalizedBaseUrl };
}


// ============================================================================
// Read-only market-data catalog viewer API
// The Quote Book page is a read-only frontend of the MD server catalog
// (md.canonical_ids + md.quote_points). These helpers never write to
// localStorage — they are pure display reads against the MD read service.
// ============================================================================

export interface MdCatalogSeries {
  canonical_id: string;
  asset_class?: string | null;
  family?: string | null;
  instrument?: string | null;
  currency?: string | null;
  tenor?: string | null;
  field?: string | null;
  frequency?: string | null;
  units?: string | null;
  description?: string | null;
}

export interface MdSeriesPoint {
  canonical_id: string;
  as_of: string;
  value: number;
  source?: string | null;
  vendor_id?: string | null;
}

export interface MdResolvedValue {
  canonical_id: string;
  found: boolean;
  is_exact: boolean;
  resolved_as_of: string | null;
  value: number | null;
  source?: string | null;
}

export async function listCatalogSeries(baseUrl: string, limit: number = 5000): Promise<MdCatalogSeries[]> {
  return fetchJson<MdCatalogSeries[]>(baseUrl, `/catalog/series?limit=${limit}`);
}

/** Historical points for one series over [start, end] (GET /series/{canonical_id}). */
export async function getSeriesPoints(
  baseUrl: string,
  canonicalId: string,
  startDate: string,
  endDate: string,
  maxPoints: number = 400
): Promise<MdSeriesPoint[]> {
  return fetchJson<MdSeriesPoint[]>(
    baseUrl,
    `/series/${encodeURIComponent(canonicalId)}?start=${encodeURIComponent(`${startDate}T00:00:00Z`)}&end=${encodeURIComponent(
      `${endDate}T23:59:59Z`
    )}&limit=50000&max_points=${maxPoints}`
  );
}

/** Batch value resolution for the catalog table at a display date (POST /quotes/resolved). */
export async function resolveCatalogValuesAt(
  baseUrl: string,
  canonicalIds: string[],
  asOfDate: string
): Promise<MdResolvedValue[]> {
  if (canonicalIds.length === 0) return [];
  const resolved = await postJson<MdResolvedBatchResponse>(baseUrl, '/quotes/resolved', {
    canonical_ids: canonicalIds,
    as_of: `${asOfDate}T00:00:00Z`,
  });
  return resolved.items.map((item) => ({
    canonical_id: item.canonical_id,
    found: item.found,
    is_exact: item.is_exact,
    resolved_as_of: item.resolved_as_of ?? null,
    value: item.value ?? null,
    source: item.source ?? null,
  }));
}

export async function buildPricingQuoteSnapshotWithBackend(
  asOfDate: string,
  opts?: {
    quoteType?: QuoteType | 'Any';
    quoteIds?: string[];
    preferBackend?: boolean;
  }
): Promise<MdPricingResolveResult> {
  const entries = getQuoteBook();
  const quoteType = opts?.quoteType || 'Curve';
  const idFilter = opts?.quoteIds ? new Set(opts.quoteIds) : null;
  const globalMode = getResolutionMode();

  const selected = entries.filter((entry) => {
    const effectiveType = entry.quote_type || 'Curve';
    if (quoteType !== 'Any' && effectiveType !== quoteType) return false;
    if (idFilter && !idFilter.has(entry.id)) return false;
    return true;
  });

  const quotes: PricingQuoteSnapshotItem[] = [];
  const selectedById = new Map(selected.map((entry) => [entry.id, entry]));
  let localHits = 0;
  for (const entry of selected) {
    const mode = entry.resolution_mode || globalMode;
    const localValue = resolveQuoteValue(entry.series, asOfDate, mode);
    if (localValue !== null) {
      quotes.push({
        id: entry.id,
        kind: entry.kind || inferQuoteKind(entry.id),
        value: localValue,
        ...(entry.quote_type ? { quote_type: entry.quote_type } : {}),
      });
      localHits += 1;
    }
  }

  const md = getMdBackendSettings();
  const preferBackend = opts?.preferBackend !== false;
  if (!md.enabled || !md.baseUrl) {
    const localIds = new Set(quotes.map((q) => q.id));
    const missingIds = selected.filter((entry) => !localIds.has(entry.id)).map((entry) => entry.id);
    return { quotes, localHits, backendHits: 0, missingIds };
  }

  const asOfIso = `${asOfDate}T00:00:00Z`;
  let resolved: MdResolvedBatchResponse;
  try {
    resolved = await postJson<MdResolvedBatchResponse>(md.baseUrl, '/quotes/resolved', {
      canonical_ids: selected.map((entry) => entry.id),
      as_of: asOfIso,
    });
  } catch (err) {
    if (!preferBackend) {
      const localIds = new Set(quotes.map((q) => q.id));
      const missingIds = selected.filter((entry) => !localIds.has(entry.id)).map((entry) => entry.id);
      return { quotes, localHits, backendHits: 0, missingIds };
    }
    throw err;
  }

  const byId = new Map(getQuoteBook().map((entry) => [entry.id, { ...entry, series: [...entry.series] }]));
  const resolvedQuotes: PricingQuoteSnapshotItem[] = [];
  const missingIds = new Set<string>();
  let backendHits = 0;

  for (const item of resolved.items) {
    if (!item.found || item.value == null || !item.resolved_as_of) {
      missingIds.add(item.canonical_id);
      continue;
    }
    const entry = byId.get(item.canonical_id) || selectedById.get(item.canonical_id);
    const v = normalizeSyncedValue(inferQuoteKind(item.canonical_id), item.value);
    const d = item.resolved_as_of.slice(0, 10);
    if (entry) {
      const idx = entry.series.findIndex((p) => p.date === d);
      if (idx >= 0) entry.series[idx].value = v;
      else entry.series.push({ date: d, value: v });
      entry.series.sort((a, b) => a.date.localeCompare(b.date));
      byId.set(item.canonical_id, entry);
    }

    const resolvedKind = normalizeQuoteKind(entry?.kind, item.canonical_id);

    resolvedQuotes.push({
      id: item.canonical_id,
      kind: resolvedKind,
      value: v,
      ...(entry?.quote_type ? { quote_type: entry.quote_type } : {}),
    });
    backendHits += 1;
  }

  let finalQuotes = resolvedQuotes;
  if (!preferBackend) {
    const byIdMerged = new Map<string, PricingQuoteSnapshotItem>();
    for (const q of resolvedQuotes) byIdMerged.set(q.id, q);
    for (const q of quotes) {
      if (!byIdMerged.has(q.id)) byIdMerged.set(q.id, q);
    }
    finalQuotes = Array.from(byIdMerged.values());
  }

  saveQuoteBook(Array.from(byId.values()));
  return { quotes: finalQuotes, localHits, backendHits, missingIds: Array.from(missingIds) };
}
