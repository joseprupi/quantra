// Time Series Lab — smooth multi-series charting over the REAL market-data path.
//
// Data path (identical to the unified Quote Book):
//   • Catalog (the series universe): orchestrator `listSeries()`
//     → GET /v1/market-data/series (the authoritative md.canonical_ids catalog).
//   • Latest value + provenance per series: `resolveCatalogValuesAt()` on the
//     MD read path (same-origin `/_md`, POST /quotes/resolved) — one batch call.
//   • Value history per SELECTED series: `getSeriesPoints()` on the MD read path
//     (GET /series/{id}) — fetched lazily when a series is charted.
//
// This replaces the retired `localStorage['quantra_quote_book']` read (the
// market-data unification removed that browser-side store). The platform
// serves real public market data plus user imports, so the old
// "My Data vs Quantra Data" split is obsolete — series are
// grouped/filtered by their PROVIDER (BoE / US Treasury / FRED / ECB /
// Imported) via `providerLabel` below.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ReactECharts from 'echarts-for-react';
import Header from '../../components/Header';
import {
  getMdBackendSettings,
  resolveCatalogValuesAt,
  getSeriesPoints,
} from '../../lib/marketDataBackend';
import { listSeries, type MarketDataSeries } from '../../lib/api/orchestrator';
import type { ApiErrorEnvelope } from '../../lib/api/types';
import { providerLabel } from '../../lib/provenance';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';

type TransformMode = 'raw' | 'rebase100' | 'pctChange' | 'zscore';
type TimeRangePreset = '1m' | '3m' | '6m' | 'ytd' | '1y' | '3y' | '5y' | 'all';
type AxisSide = 'left' | 'right';

// Far-future sentinel → "latest value at or before this" == the genuine latest
// point. HISTORY_START pulls the whole series. Both mirror QuoteBook.tsx.
const LATEST_AS_OF = '2999-12-31';
const HISTORY_START = '1900-01-01';
const HISTORY_MAX_POINTS = 2000;
const FIXED_POINTS_PER_VIEW = 400;

// Provider attribution: each quote point carries a `source` tag identifying the
// real public feed that published it (BoE / US Treasury / FRED / ECB) or, for
// user-supplied values, `manual` / `csv` → "Imported". We attribute a SERIES by
// the source of its LATEST point (the one value resolveCatalogValuesAt returns
// per series in a single batch call — the same Source column the Quote Book
// shows). `providerLabel` maps the raw tag to the friendly provider name.
const UNKNOWN_PROVIDER = 'Unknown';

function classifyProvider(source: string | null | undefined): string {
  if (!source) return UNKNOWN_PROVIDER;
  return providerLabel(source);
}

interface PlotSeries {
  id: string;
  label: string;
  kind: string;
  currency?: string;
  provider: string;
  latestSource: string | null;
  points: Array<{ ts: number; value: number }>;
}

interface ZoomWindow {
  startPct: number;
  endPct: number;
}

// Axis default: rates/spreads on the left, prices on the right. Derived from the
// catalog metadata (units/field), same rate-like test as the Quote Book.
function seriesKind(s: MarketDataSeries): string {
  const units = (s.units || '').toLowerCase();
  const field = (s.field || '').toUpperCase();
  if (units === 'decimal_rate' || field.includes('RATE') || field.includes('YIELD')) return 'Rate';
  if (field.includes('SPREAD')) return 'Spread';
  return 'Price';
}

// Human message keyed on the envelope `code` (never on prose).
function describeSeriesError(envelope: ApiErrorEnvelope): string {
  switch (envelope.code) {
    case 'network_error':
      return 'Could not reach the orchestrator to load the series catalog.';
    case 'unauthenticated':
      return 'You are not signed in. Please sign in to load the series catalog.';
    default:
      return envelope.error || 'The request failed. Please try again.';
  }
}

function cutoffTimestamp(preset: TimeRangePreset, maxTs: number): number | null {
  if (preset === 'all') return null;
  const d = new Date(maxTs);
  if (preset === 'ytd') {
    return Date.UTC(d.getUTCFullYear(), 0, 1);
  }

  if (preset === '1m') d.setUTCMonth(d.getUTCMonth() - 1);
  if (preset === '3m') d.setUTCMonth(d.getUTCMonth() - 3);
  if (preset === '6m') d.setUTCMonth(d.getUTCMonth() - 6);
  if (preset === '1y') d.setUTCFullYear(d.getUTCFullYear() - 1);
  if (preset === '3y') d.setUTCFullYear(d.getUTCFullYear() - 3);
  if (preset === '5y') d.setUTCFullYear(d.getUTCFullYear() - 5);
  return d.getTime();
}

function toDateInputValue(ts: number): string {
  return new Date(ts).toISOString().slice(0, 10);
}

function parseDateInput(value: string): number | null {
  if (!value) return null;
  const ts = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(ts) ? ts : null;
}

function transformedValues(values: number[], mode: TransformMode): number[] {
  if (values.length === 0) return values;
  if (mode === 'raw') return values;

  const first = values[0];
  if (mode === 'rebase100') {
    if (first === 0) return values.map(() => NaN);
    return values.map((v) => (v / first) * 100);
  }
  if (mode === 'pctChange') {
    if (first === 0) return values.map(() => NaN);
    return values.map((v) => ((v / first) - 1) * 100);
  }

  const mean = values.reduce((acc, v) => acc + v, 0) / values.length;
  const variance = values.reduce((acc, v) => acc + (v - mean) ** 2, 0) / values.length;
  const std = Math.sqrt(variance);
  if (std === 0) return values.map(() => 0);
  return values.map((v) => (v - mean) / std);
}

function defaultAxisSide(kind: string): AxisSide {
  return kind === 'Rate' || kind === 'Spread' ? 'left' : 'right';
}

function downsampleMinMax(
  points: Array<{ ts: number; value: number }>,
  maxPoints: number
): Array<{ ts: number; value: number }> {
  if (points.length <= maxPoints || maxPoints < 8) return points;

  const bucketCount = Math.max(1, Math.floor((maxPoints - 2) / 2));
  const first = points[0];
  const last = points[points.length - 1];
  const interior = points.slice(1, -1);
  const bucketSize = Math.ceil(interior.length / bucketCount);
  const out: Array<{ ts: number; value: number }> = [first];

  for (let i = 0; i < interior.length; i += bucketSize) {
    const bucket = interior.slice(i, i + bucketSize);
    if (bucket.length === 0) continue;
    let min = bucket[0];
    let max = bucket[0];
    for (const p of bucket) {
      if (p.value < min.value) min = p;
      if (p.value > max.value) max = p;
    }
    if (min.ts <= max.ts) {
      out.push(min);
      if (max.ts !== min.ts) out.push(max);
    } else {
      out.push(max);
      if (max.ts !== min.ts) out.push(min);
    }
  }

  out.push(last);
  return out.sort((a, b) => a.ts - b.ts);
}

function zoomTimeBounds(
  points: Array<{ ts: number; value: number }>,
  startPct: number,
  endPct: number
): { startTs: number; endTs: number } | null {
  if (points.length === 0) return null;
  if (points.length === 1) {
    const only = points[0].ts;
    return { startTs: only, endTs: only };
  }
  const clampedStart = Math.max(0, Math.min(100, startPct));
  const clampedEnd = Math.max(clampedStart, Math.min(100, endPct));
  const minTs = points[0].ts;
  const maxTs = points[points.length - 1].ts;
  const span = Math.max(1, maxTs - minTs);
  const startTs = minTs + (clampedStart / 100) * span;
  const endTs = minTs + (clampedEnd / 100) * span;
  return { startTs, endTs };
}

function sliceByTimeBounds(
  points: Array<{ ts: number; value: number }>,
  startTs: number,
  endTs: number
): Array<{ ts: number; value: number }> {
  if (points.length === 0) return points;
  return points.filter((p) => p.ts >= startTs && p.ts <= endTs);
}

export default function TimeSeriesLab() {
  const md = getMdBackendSettings();

  const [catalog, setCatalog] = useState<MarketDataSeries[]>([]);
  // Latest point's source per canonical_id (from the MD read path batch resolve).
  // Absent id = unresolved / no value → provider shown as "Unknown".
  const [sourceById, setSourceById] = useState<Map<string, string | null>>(new Map());
  // Fetched value history (ts/value) per SELECTED series.
  const [pointsById, setPointsById] = useState<Map<string, Array<{ ts: number; value: number }>>>(new Map());

  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [reachError, setReachError] = useState<string | null>(null);
  const [loadingPoints, setLoadingPoints] = useState(false);

  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState<string>('all');
  const [transformMode, setTransformMode] = useState<TransformMode>('raw');
  const [rangePreset, setRangePreset] = useState<TimeRangePreset>('1y');
  const [minDateInput, setMinDateInput] = useState('');
  const [maxDateInput, setMaxDateInput] = useState('');
  const [useLogScale, setUseLogScale] = useState(false);
  const [axisById, setAxisById] = useState<Record<string, AxisSide>>({});
  const [zoomWindow, setZoomWindow] = useState<ZoomWindow>({ startPct: 0, endPct: 100 });

  const inFlight = useRef<Set<string>>(new Set());
  const didInitSelection = useRef(false);

  // Load catalog (orchestrator) + latest value/provenance (MD read path)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const result = await listSeries();
      if (cancelled) return;
      if (!result.ok) {
        setListError(describeSeriesError(result.envelope));
        setLoading(false);
        return;
      }
      const cat = [...result.data.series].sort((a, b) => a.canonical_id.localeCompare(b.canonical_id));
      setCatalog(cat);
      setListError(null);
      setLoading(false);

      if (cat.length === 0 || !md.enabled || !md.baseUrl) return;
      try {
        const resolved = await resolveCatalogValuesAt(md.baseUrl, cat.map((s) => s.canonical_id), LATEST_AS_OF);
        if (cancelled) return;
        const m = new Map<string, string | null>();
        for (const r of resolved) m.set(r.canonical_id, r.found ? (r.source ?? null) : null);
        setSourceById(m);
        setReachError(null);
        if (!didInitSelection.current) {
          const withData = resolved.filter((r) => r.found).map((r) => r.canonical_id);
          const pick = (withData.length > 0 ? withData : cat.map((s) => s.canonical_id)).slice(0, 8);
          setSelectedIds(pick);
          didInitSelection.current = true;
        }
      } catch (err) {
        if (!cancelled) {
          setReachError(err instanceof Error ? err.message : 'Failed to reach the market-data service');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The full series universe (catalog + provenance). Points are merged in as
  //    they arrive for selected series.
  const allSeries = useMemo<PlotSeries[]>(() => {
    return catalog.map((s) => {
      const latestSource = sourceById.get(s.canonical_id) ?? null;
      return {
        id: s.canonical_id,
        label: s.description || s.canonical_id,
        kind: seriesKind(s),
        currency: s.currency || undefined,
        provider: classifyProvider(latestSource),
        latestSource,
        points: pointsById.get(s.canonical_id) ?? [],
      };
    });
  }, [catalog, sourceById, pointsById]);

  // Distinct providers present in the universe → the filter dropdown options.
  const providers = useMemo(() => {
    const set = new Set<string>();
    for (const s of allSeries) set.add(s.provider);
    return Array.from(set).sort();
  }, [allSeries]);

  const filteredUniverse = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allSeries.filter((s) => {
      if (providerFilter !== 'all' && s.provider !== providerFilter) return false;
      if (!q) return true;
      return (
        s.id.toLowerCase().includes(q) ||
        s.label.toLowerCase().includes(q) ||
        s.kind.toLowerCase().includes(q) ||
        s.provider.toLowerCase().includes(q) ||
        (s.currency || '').toLowerCase().includes(q)
      );
    });
  }, [allSeries, search, providerFilter]);

  const selectedSeries = useMemo(
    () => allSeries.filter((s) => selectedIds.includes(s.id)),
    [allSeries, selectedIds]
  );

  // Lazily fetch value history for selected series (MD read path)
  useEffect(() => {
    if (!md.enabled || !md.baseUrl) return;
    const toFetch = selectedIds.filter((id) => !pointsById.has(id) && !inFlight.current.has(id));
    if (toFetch.length === 0) return;

    let cancelled = false;
    toFetch.forEach((id) => inFlight.current.add(id));
    setLoadingPoints(true);

    void Promise.all(
      toFetch.map(async (id) => {
        try {
          const history = await getSeriesPoints(md.baseUrl, id, HISTORY_START, LATEST_AS_OF, HISTORY_MAX_POINTS);
          const pts = history
            .map((p) => ({ ts: Date.parse(p.as_of), value: p.value }))
            .filter((p) => Number.isFinite(p.ts) && Number.isFinite(p.value))
            .sort((a, b) => a.ts - b.ts);
          return [id, pts] as const;
        } catch (err) {
          if (!cancelled) {
            setReachError(err instanceof Error ? err.message : 'Failed to load value history');
          }
          return [id, [] as Array<{ ts: number; value: number }>] as const;
        }
      })
    ).then((results) => {
      toFetch.forEach((id) => inFlight.current.delete(id));
      if (cancelled) return;
      setPointsById((prev) => {
        const next = new Map(prev);
        for (const [id, pts] of results) next.set(id, pts);
        return next;
      });
      setLoadingPoints(false);
    });

    return () => {
      cancelled = true;
    };
  }, [selectedIds, pointsById]);

  const maxTs = useMemo(() => {
    let m = 0;
    for (const s of selectedSeries) {
      const last = s.points[s.points.length - 1]?.ts || 0;
      if (last > m) m = last;
    }
    return m;
  }, [selectedSeries]);

  const minTs = useMemo(() => {
    let m = Number.POSITIVE_INFINITY;
    for (const s of selectedSeries) {
      const first = s.points[0]?.ts;
      if (typeof first === 'number' && first < m) m = first;
    }
    return Number.isFinite(m) ? m : 0;
  }, [selectedSeries]);

  useEffect(() => {
    if (maxTs === 0) return;
    const presetStart = rangePreset === 'all'
      ? Date.parse('1900-01-01T00:00:00.000Z')
      : (cutoffTimestamp(rangePreset, maxTs) ?? minTs);
    const boundedStart = rangePreset === 'all'
      ? presetStart
      : Math.max(minTs || presetStart, presetStart);
    setMinDateInput(toDateInputValue(boundedStart));
    setMaxDateInput(toDateInputValue(maxTs));
  }, [rangePreset, maxTs, minTs]);

  const chartSeries = useMemo(() => {
    if (selectedSeries.length === 0 || maxTs === 0) return [];
    const parsedMinTs = parseDateInput(minDateInput);
    const parsedMaxTs = parseDateInput(maxDateInput);
    const fallbackMin = rangePreset === 'all'
      ? Date.parse('1900-01-01T00:00:00.000Z')
      : (cutoffTimestamp(rangePreset, maxTs) ?? minTs);
    const rangeStartTs = parsedMinTs ?? fallbackMin;
    const rangeEndTs = parsedMaxTs ?? maxTs;

    return selectedSeries
      .map((s) => {
        const slicedRaw = s.points.filter((p) => p.ts >= rangeStartTs && p.ts <= rangeEndTs);
        const bounds = zoomTimeBounds(slicedRaw, zoomWindow.startPct, zoomWindow.endPct);
        const windowedRaw = bounds ? sliceByTimeBounds(slicedRaw, bounds.startTs, bounds.endTs) : slicedRaw;
        const sliced = downsampleMinMax(windowedRaw, FIXED_POINTS_PER_VIEW);
        if (sliced.length < 2) return null;
        const rawValues = sliced.map((p) => p.value);
        const yVals = transformedValues(rawValues, transformMode);
        const data = sliced
          .map((p, idx) => [p.ts, yVals[idx]] as [number, number])
          .filter((pt) => Number.isFinite(pt[1]))
          .filter((pt) => (useLogScale ? pt[1] > 0 : true));

        if (data.length < 2) return null;
        const axisSide = axisById[s.id] || defaultAxisSide(s.kind);

        return {
          name: `${s.label} (${s.provider})`,
          type: 'line',
          smooth: true,
          showSymbol: false,
          sampling: 'none',
          progressive: 5000,
          progressiveThreshold: 8000,
          yAxisIndex: axisSide === 'left' ? 0 : 1,
          emphasis: { focus: 'series' },
          lineStyle: { width: 1.75 },
          data,
        };
      })
      .filter((s) => s !== null);
  }, [selectedSeries, maxTs, minTs, rangePreset, minDateInput, maxDateInput, transformMode, useLogScale, axisById, zoomWindow.startPct, zoomWindow.endPct]);

  const option = useMemo(() => {
    const yLabel =
      transformMode === 'raw'
        ? 'Raw value'
        : transformMode === 'rebase100'
          ? 'Rebased (100=start)'
          : transformMode === 'pctChange'
            ? '% change'
            : 'Z-score';

    return {
      animation: true,
      backgroundColor: '#0b1220',
      textStyle: { color: '#d4d4d8' },
      color: [
        '#1d4ed8',
        '#d97706',
        '#059669',
        '#7c3aed',
        '#dc2626',
        '#0891b2',
        '#4338ca',
        '#65a30d',
      ],
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(10, 10, 10, 0.9)',
        borderColor: '#3f3f46',
        textStyle: { color: '#f4f4f5' },
        valueFormatter: (value: number) => (Number.isFinite(value) ? value.toFixed(6) : 'n/a'),
      },
      legend: {
        type: 'scroll',
        top: 0,
        textStyle: { color: '#d4d4d8' },
      },
      grid: { left: 60, right: 60, top: 48, bottom: 72 },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#52525b' } },
        axisLabel: { color: '#d4d4d8' },
        splitLine: { lineStyle: { color: '#27272a' } },
      },
      yAxis: [
        {
          type: useLogScale ? 'log' : 'value',
          name: `${yLabel} (L)`,
          nameTextStyle: { color: '#a1a1aa' },
          axisLine: { lineStyle: { color: '#52525b' } },
          axisLabel: { color: '#d4d4d8' },
          splitLine: { lineStyle: { color: '#27272a' } },
          scale: true,
        },
        {
          type: useLogScale ? 'log' : 'value',
          name: `${yLabel} (R)`,
          nameTextStyle: { color: '#a1a1aa' },
          axisLine: { lineStyle: { color: '#52525b' } },
          axisLabel: { color: '#d4d4d8' },
          splitLine: { show: false },
          scale: true,
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, filterMode: 'none', start: zoomWindow.startPct, end: zoomWindow.endPct },
        { type: 'slider', xAxisIndex: 0, height: 24, bottom: 20, filterMode: 'none', start: zoomWindow.startPct, end: zoomWindow.endPct },
      ],
      series: chartSeries,
    };
  }, [chartSeries, transformMode, useLogScale, zoomWindow.startPct, zoomWindow.endPct]);

  useEffect(() => {
    setZoomWindow({ startPct: 0, endPct: 100 });
  }, [rangePreset, selectedIds.join('|')]);

  const toggleSeries = (id: string) => {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const chartEvents = useMemo(
    () => ({
      datazoom: (event: any) => {
        const batch = Array.isArray(event?.batch) ? event.batch[0] : event;
        const start = Number(batch?.start);
        const end = Number(batch?.end);
        if (Number.isFinite(start) && Number.isFinite(end)) {
          const clampedStart = Math.max(0, Math.min(100, start));
          const clampedEnd = Math.max(0, Math.min(100, end));
          setZoomWindow({ startPct: clampedStart, endPct: Math.max(clampedStart + 0.1, clampedEnd) });
        }
      },
    }),
    []
  );

  // Empty state: the catalog itself is empty (no series defined anywhere).
  const catalogEmpty = !loading && !listError && catalog.length === 0;

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-[1480px] mx-auto px-4 sm:px-6 pt-24 pb-10">
        <div className="mb-5">
          <h1 className="text-2xl font-semibold text-[#0a0a0a]">Time Series Lab</h1>
          <p className="text-[#737373] mt-1">
            All series are real public market data (Bank of England, US Treasury, FRED, ECB), updated daily. Smooth multi-series charting with normalization and scale controls.
          </p>
        </div>

        {listError && (
          <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{listError}</div>
        )}
        {reachError && (
          <div className="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-800">
            Some values are unavailable — could not reach the market-data read service at {md.baseUrl}: {reachError}
          </div>
        )}

        {loading ? (
          <div className="bg-white border border-[#e5e5e5] rounded-xl p-6 text-sm text-[#737373]">Loading catalog…</div>
        ) : catalogEmpty ? (
          <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
            <PanelPlaceholder
              icon="timeSeries"
              title="No series yet"
              description="There are no quote series to chart. Create one in the Quote Book, then add values to it."
            >
              <Link
                to="/quote-book"
                className="inline-block px-4 py-2 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28]"
              >
                Go to the Quote Book →
              </Link>
            </PanelPlaceholder>
          </div>
        ) : (
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
          <section className="xl:col-span-4 bg-white border border-[#e5e5e5] rounded-xl p-4">
            <h2 className="text-sm font-semibold text-[#0a0a0a] mb-3">Series Selection</h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search series..."
                className="px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
              />
              <select
                value={providerFilter}
                onChange={(e) => setProviderFilter(e.target.value)}
                className="px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              >
                <option value="all">All providers</option>
                {providers.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>

            <div className="max-h-[420px] overflow-auto border border-[#e5e5e5] rounded-lg">
              {filteredUniverse.length === 0 ? (
                <div className="p-3">
                  <PanelPlaceholder
                    compact
                    icon="timeSeries"
                    title="No matching series"
                    description="Adjust the filters or search for a different series."
                  />
                </div>
              ) : (
                filteredUniverse.map((s) => {
                  const checked = selectedIds.includes(s.id);
                  return (
                    <label
                      key={s.id}
                      className="flex items-start gap-3 p-3 border-b border-[#f5f5f5] hover:bg-[#fafafa] cursor-pointer"
                    >
                      <input type="checkbox" checked={checked} onChange={() => toggleSeries(s.id)} className="mt-1" />
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-[#0a0a0a] truncate">{s.label}</div>
                        <div className="text-xs text-[#737373] truncate">{s.id}</div>
                        <div className="text-[11px] text-[#a3a3a3] mt-0.5">
                          {s.kind} {s.currency ? `- ${s.currency}` : ''} - {s.provider}
                        </div>
                      </div>
                    </label>
                  );
                })
              )}
            </div>

            {selectedSeries.length > 0 && (
              <div className="mt-3 border border-[#e5e5e5] rounded-lg p-3">
                <div className="text-xs font-medium text-[#525252] mb-2">Axis Assignment</div>
                <div className="max-h-40 overflow-auto space-y-1">
                  {selectedSeries.map((s) => {
                    const side = axisById[s.id] || defaultAxisSide(s.kind);
                    return (
                      <div key={s.id} className="flex items-center justify-between gap-2">
                        <span className="text-xs text-[#525252] truncate">{s.label}</span>
                        <select
                          value={side}
                          onChange={(e) =>
                            setAxisById((prev) => ({ ...prev, [s.id]: e.target.value as AxisSide }))
                          }
                          className="px-2 py-1 text-xs border border-[#d4d4d4] rounded-lg"
                        >
                          <option value="left">Left</option>
                          <option value="right">Right</option>
                        </select>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </section>

          <section className="xl:col-span-8 bg-white border border-[#e5e5e5] rounded-xl p-4">
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <select
                value={transformMode}
                onChange={(e) => setTransformMode(e.target.value as TransformMode)}
                className="px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              >
                <option value="raw">Raw</option>
                <option value="rebase100">Rebased to 100</option>
                <option value="pctChange">% Change</option>
                <option value="zscore">Z-Score</option>
              </select>
              <select
                value={rangePreset}
                onChange={(e) => setRangePreset(e.target.value as TimeRangePreset)}
                className="px-3 py-2 text-sm border border-[#d4d4d4] rounded-lg"
              >
                <option value="1m">1M</option>
                <option value="3m">3M</option>
                <option value="6m">6M</option>
                <option value="ytd">YTD</option>
                <option value="1y">1Y</option>
                <option value="3y">3Y</option>
                <option value="5y">5Y</option>
                <option value="all">All</option>
              </select>
              <div className="flex items-center gap-1.5">
                <label className="text-xs text-[#737373]">Min</label>
                <input
                  type="date"
                  value={minDateInput}
                  onChange={(e) => setMinDateInput(e.target.value)}
                  className="px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg"
                />
              </div>
              <div className="flex items-center gap-1.5">
                <label className="text-xs text-[#737373]">Max</label>
                <input
                  type="date"
                  value={maxDateInput}
                  onChange={(e) => setMaxDateInput(e.target.value)}
                  className="px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg"
                />
              </div>
              <label className="flex items-center gap-2 text-sm text-[#525252] px-2">
                <input type="checkbox" checked={useLogScale} onChange={(e) => setUseLogScale(e.target.checked)} />
                Log scale
              </label>
              <span className="ml-auto text-xs text-[#a3a3a3]">
                Showing {chartSeries.length} plotted / {selectedSeries.length} selected series
              </span>
              {loadingPoints && (
                <span className="text-xs text-[#737373] inline-flex items-center gap-1">
                  <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                    <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                  </svg>
                  Loading value history...
                </span>
              )}
            </div>

            {chartSeries.length === 0 ? (
              <div className="h-[520px] border border-[#e5e5e5] rounded-lg flex items-center justify-center p-6">
                <div className="w-full max-w-md">
                  <PanelPlaceholder
                    compact
                    icon="timeSeries"
                    title={loadingPoints ? 'Loading value history…' : 'No chartable series selected'}
                    description={
                      loadingPoints
                        ? 'Fetching points for the selected series.'
                        : 'Select at least one series that has at least two data points.'
                    }
                  />
                </div>
              </div>
            ) : (
              <ReactECharts
                option={option}
                onEvents={chartEvents}
                style={{ height: 520, width: '100%' }}
                notMerge
                lazyUpdate
              />
            )}
          </section>
        </div>
        )}
      </main>
    </div>
  );
}
