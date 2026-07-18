// Quote Book — ONE unified master-detail table.
//
// Everything about a quote lives in its row. A single table lists the
// authoritative `md.canonical_ids` catalog (from the orchestrator, the CRUD
// source of truth) enriched with each series' LATEST value + provenance read
// from the market-data read path (same-origin `/_md` proxy). Each row
// carries its own edit (PATCH) / delete (cascade-warned) controls, and clicks
// open IN PLACE to show that series' value history (+ sparkline) and an inline
// add-value form.
//
// Two write paths, one panel:
//   • series definition CRUD   → orchestrator `/v1/market-data/series`
//   • add a single quote value → orchestrator `/v1/market-data/import`
//     (a one-row manual batch; the server tags it `source=manual`) — the very
//     same endpoint the bulk Import screen uses.
// Value READS (latest + history) stay on the MD read path (invariant-#8
// "show me the quote" carve-out).
import { useState, useEffect, useCallback, Fragment } from 'react';
import { Link } from 'react-router-dom';
import Header from '../../components/Header';
import {
  getMdBackendSettings,
  resolveCatalogValuesAt,
  getSeriesPoints,
  type MdResolvedValue,
  type MdSeriesPoint,
} from '../../lib/marketDataBackend';
import {
  listSeries,
  createSeries,
  updateSeries,
  deleteSeries,
  importMarketDataJson,
  type MarketDataSeries,
  type MarketDataSeriesCreate,
  type MarketDataSeriesPatch,
} from '../../lib/api/orchestrator';
import type { ApiErrorEnvelope } from '../../lib/api/types';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import SourceChip from '../../components/market-data/SourceChip';

// Far-future sentinel: "latest value at or before this" == the genuine latest
// point, whatever its date. Used to batch-resolve every row's latest in one
// POST /quotes/resolved.
const LATEST_AS_OF = '2999-12-31';
const HISTORY_START = '1900-01-01';

// The optional structural/metadata fields, in form order. `canonical_id`,
// `asset_class` and `currency` are handled separately (required).
const OPTIONAL_FIELDS = [
  { key: 'family', label: 'Family', placeholder: 'IRS, SOFR, HICP…' },
  { key: 'instrument', label: 'Instrument', placeholder: 'derived from id if blank' },
  { key: 'tenor', label: 'Tenor', placeholder: '5Y, 3M…' },
  { key: 'field', label: 'Field', placeholder: 'RATE, VOL, YIELD…' },
  { key: 'frequency', label: 'Frequency', placeholder: 'daily (default)' },
  { key: 'units', label: 'Units', placeholder: 'decimal_rate (default)' },
  { key: 'description', label: 'Description', placeholder: 'free-form label' },
] as const;

type OptionalKey = (typeof OPTIONAL_FIELDS)[number]['key'];

interface FormState {
  canonical_id: string;
  asset_class: string;
  currency: string;
  family: string;
  instrument: string;
  tenor: string;
  field: string;
  frequency: string;
  units: string;
  description: string;
}

function emptyForm(): FormState {
  return {
    canonical_id: '', asset_class: '', currency: '', family: '', instrument: '',
    tenor: '', field: '', frequency: '', units: '', description: '',
  };
}

function formFromSeries(s: MarketDataSeries): FormState {
  return {
    canonical_id: s.canonical_id,
    asset_class: s.asset_class,
    currency: s.currency,
    family: s.family ?? '',
    instrument: s.instrument ?? '',
    tenor: s.tenor ?? '',
    field: s.field ?? '',
    frequency: s.frequency ?? '',
    units: s.units ?? '',
    description: s.description ?? '',
  };
}

function isRateLike(s: { units?: string | null; field?: string | null }): boolean {
  if (s.units === 'decimal_rate') return true;
  const field = (s.field || '').toUpperCase();
  return field.includes('RATE') || field.includes('YIELD');
}

function formatValue(s: { units?: string | null; field?: string | null }, value: number): string {
  if (isRateLike(s)) return `${(value * 100).toFixed(4)}%`;
  return value.toFixed(6);
}

// Human message keyed on the envelope `code` (never on prose).
function describeSeriesError(envelope: ApiErrorEnvelope): string {
  switch (envelope.code) {
    case 'market_data_series_exists':
      return 'A series with that canonical ID already exists. Edit or delete the existing one instead.';
    case 'market_data_series_invalid':
      return envelope.error || 'Invalid canonical ID or a required field was left blank.';
    case 'market_data_series_not_found':
      return 'That series no longer exists — it may have been deleted. Refresh the list.';
    default:
      return envelope.error || 'The request failed. Please try again.';
  }
}

export default function QuoteBook() {
  const md = getMdBackendSettings();
  const baseUrl = md.baseUrl;

  const [series, setSeries] = useState<MarketDataSeries[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [reachError, setReachError] = useState<string | null>(null);

  // Latest value + provenance per canonical_id, batch-resolved from the MD read
  // path. Absent id = "—" (unresolved / no value / read path unreachable).
  const [latestById, setLatestById] = useState<Map<string, MdResolvedValue>>(new Map());

  const [filterCurrency, setFilterCurrency] = useState('');
  const [filterAssetClass, setFilterAssetClass] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Create / edit modal: null = closed.
  const [form, setForm] = useState<
    | { mode: 'create'; state: FormState }
    | { mode: 'edit'; original: MarketDataSeries; state: FormState }
    | null
  >(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Delete confirm.
  const [deleteTarget, setDeleteTarget] = useState<MarketDataSeries | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);

  // Load the authoritative catalog (orchestrator)
  const reload = useCallback(async () => {
    setLoading(true);
    const result = await listSeries();
    if (result.ok) {
      setSeries([...result.data.series].sort((a, b) => a.canonical_id.localeCompare(b.canonical_id)));
      setListError(null);
    } else {
      setListError(describeSeriesError(result.envelope));
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Batch-resolve each row's LATEST value + source (MD read path)
  const refreshLatest = useCallback(
    async (ids: string[]) => {
      if (ids.length === 0) return;
      try {
        const resolved = await resolveCatalogValuesAt(baseUrl, ids, LATEST_AS_OF);
        setLatestById((prev) => {
          const next = new Map(prev);
          for (const r of resolved) next.set(r.canonical_id, r);
          return next;
        });
        setReachError(null);
      } catch (err) {
        setReachError(err instanceof Error ? err.message : 'Failed to reach the market-data service');
      }
    },
    [baseUrl],
  );

  useEffect(() => {
    if (series.length === 0) return;
    void refreshLatest(series.map((s) => s.canonical_id));
  }, [series, refreshLatest]);

  // Series CRUD
  function openCreate() {
    setFormError(null);
    setForm({ mode: 'create', state: emptyForm() });
  }
  function openEdit(s: MarketDataSeries) {
    setFormError(null);
    setForm({ mode: 'edit', original: s, state: formFromSeries(s) });
  }
  function setField(key: keyof FormState, value: string) {
    setForm((prev) => (prev ? { ...prev, state: { ...prev.state, [key]: value } } : prev));
  }

  async function submitForm() {
    if (!form) return;
    const s = form.state;
    setFormError(null);
    if (!s.canonical_id.trim() || !s.asset_class.trim() || !s.currency.trim()) {
      setFormError('Canonical ID, asset class and currency are required.');
      return;
    }
    setSaving(true);
    try {
      if (form.mode === 'create') {
        const body: MarketDataSeriesCreate = {
          canonical_id: s.canonical_id.trim(),
          asset_class: s.asset_class.trim(),
          currency: s.currency.trim(),
        };
        for (const { key } of OPTIONAL_FIELDS) {
          const v = s[key as OptionalKey].trim();
          if (v) body[key as OptionalKey] = v;
        }
        const result = await createSeries(body);
        if (!result.ok) {
          setFormError(describeSeriesError(result.envelope));
          return;
        }
        setNotice(`Created series ${result.data.canonical_id}.`);
      } else {
        const orig = form.original;
        const patch: MarketDataSeriesPatch = {};
        if (s.asset_class.trim() !== orig.asset_class) patch.asset_class = s.asset_class.trim();
        if (s.currency.trim() !== orig.currency) patch.currency = s.currency.trim();
        for (const { key } of OPTIONAL_FIELDS) {
          const next = s[key as OptionalKey].trim();
          const before = (orig[key as OptionalKey] ?? '') as string;
          if (next !== before) patch[key as OptionalKey] = next;
        }
        if (Object.keys(patch).length === 0) {
          setForm(null);
          return;
        }
        const result = await updateSeries(orig.canonical_id, patch);
        if (!result.ok) {
          setFormError(describeSeriesError(result.envelope));
          return;
        }
        setNotice(`Updated series ${orig.canonical_id}.`);
      }
      setForm(null);
      await reload();
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleteError(null);
    setDeleting(true);
    try {
      const result = await deleteSeries(deleteTarget.canonical_id);
      if (!result.ok) {
        setDeleteError(describeSeriesError(result.envelope));
        return;
      }
      setNotice(
        `Deleted series ${result.data.canonical_id} and ${result.data.deleted_quote_points} quote value${
          result.data.deleted_quote_points === 1 ? '' : 's'
        }.`,
      );
      if (expandedId === deleteTarget.canonical_id) setExpandedId(null);
      setDeleteTarget(null);
      await reload();
    } finally {
      setDeleting(false);
    }
  }

  // Derived
  const currencies = Array.from(new Set(series.map((s) => s.currency).filter(Boolean))).sort();
  const assetClasses = Array.from(new Set(series.map((s) => s.asset_class).filter(Boolean))).sort();
  const filtered = series.filter((s) => {
    if (filterCurrency && s.currency !== filterCurrency) return false;
    if (filterAssetClass && s.asset_class !== filterAssetClass) return false;
    return true;
  });

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        {/* Title row */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-semibold text-[#0a0a0a]">Quote Book</h1>
            <p className="text-[#737373] mt-1">
              Every quote series, its latest value and its history — browse, define, edit and add values in one place.
            </p>
          </div>
          <button
            onClick={openCreate}
            className="px-3 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28] whitespace-nowrap"
          >
            + New series
          </button>
        </div>

        {notice && (
          <div className="mb-4 px-4 py-2 rounded-lg bg-green-50 border border-green-200 text-sm text-green-700 flex items-center justify-between">
            <span>{notice}</span>
            <button onClick={() => setNotice(null)} className="text-green-600 hover:text-green-800 text-lg leading-none" aria-label="dismiss notice">×</button>
          </div>
        )}
        {listError && (
          <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{listError}</div>
        )}
        {reachError && (
          <div className="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-800">
            Latest values unavailable — could not reach the market-data read service at {baseUrl}: {reachError}
          </div>
        )}

        {/* Controls bar */}
        <div className="bg-white border border-[#e5e5e5] rounded-xl p-4 mb-4">
          <div className="flex gap-3 items-center flex-wrap">
            <select value={filterCurrency} onChange={(e) => setFilterCurrency(e.target.value)}
              className="px-3 py-1.5 text-sm border border-[#d4d4d4] rounded-lg">
              <option value="">All currencies</option>
              {currencies.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={filterAssetClass} onChange={(e) => setFilterAssetClass(e.target.value)}
              className="px-3 py-1.5 text-sm border border-[#d4d4d4] rounded-lg">
              <option value="">All asset classes</option>
              {assetClasses.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
            <span className="text-xs text-[#a3a3a3] ml-auto">{filtered.length} series</span>
            <Link to="/market-data/import" className="text-xs font-medium text-[#8a6a2f] hover:underline whitespace-nowrap">
              Bulk CSV import →
            </Link>
          </div>
          <div className="mt-3 pt-3 border-t border-[#f5f5f5] text-xs text-[#a3a3a3] flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="inline-flex items-center gap-1">
              <span aria-hidden="true" className="text-[#1f9d55]">&#9679;</span>
              <span className="text-[#525252] font-medium">Real public market data</span>
            </span>
            <span>— Bank of England, US Treasury, FRED, ECB (updated daily).</span>
            <span className="inline-flex items-center gap-1">
              <span className="px-1.5 py-0.5 rounded bg-[#f5f5f5] text-[#525252]">Imported</span>
              <span>= values you added via Import.</span>
            </span>
          </div>
        </div>

        {/* Unified table */}
        <div className="bg-white border border-[#e5e5e5] rounded-xl overflow-hidden">
          {loading ? (
            <div className="p-6 text-sm text-[#737373]">Loading catalog…</div>
          ) : filtered.length === 0 ? (
            <div className="p-6">
              <PanelPlaceholder
                icon="timeSeries"
                title="No series in the catalog"
                description="Use “+ New series” to define one, then add values to it."
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px]">
                <thead>
                  <tr className="bg-[#fafafa] border-b border-[#e5e5e5]">
                    <th className="w-6 px-2 py-3"></th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Canonical ID</th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Class</th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Ccy</th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Tenor</th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Field</th>
                    <th className="text-right px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Latest</th>
                    <th className="text-left px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Source</th>
                    <th className="text-right px-3 py-3 text-xs font-semibold text-[#737373] uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((s) => {
                    const isExpanded = expandedId === s.canonical_id;
                    const latest = latestById.get(s.canonical_id);
                    const hasValue = !!latest && latest.found && latest.value != null;
                    return (
                      <Fragment key={s.canonical_id}>
                        <tr
                          className={`border-b border-[#f5f5f5] cursor-pointer transition-colors ${isExpanded ? 'qb-selected bg-[#f7f4ed]' : 'hover:bg-[#fafafa]'}`}
                          onClick={() => setExpandedId(isExpanded ? null : s.canonical_id)}
                        >
                          <td className="px-2 py-3 text-center text-[#a3a3a3] select-none">{isExpanded ? '▾' : '▸'}</td>
                          <td className="px-3 py-3">
                            <code className="text-sm font-mono text-[#0a0a0a] block max-w-[18rem] truncate" title={s.description || s.canonical_id}>
                              {s.canonical_id}
                            </code>
                          </td>
                          <td className="px-3 py-3 text-sm text-[#525252]">{s.asset_class || '—'}</td>
                          <td className="px-3 py-3 text-sm text-[#525252]">{s.currency || '—'}</td>
                          <td className="px-3 py-3 text-sm text-[#525252]">{s.tenor || '—'}</td>
                          <td className="px-3 py-3 text-sm text-[#525252]">{s.field || '—'}</td>
                          <td className="px-3 py-3 text-right">
                            {hasValue ? (
                              <span className={`font-mono text-sm ${isExpanded ? 'text-[#3a2c05]' : 'text-[#0a0a0a]'}`}>{formatValue(s, latest!.value as number)}</span>
                            ) : (
                              <span className="text-xs text-[#a3a3a3]">—</span>
                            )}
                          </td>
                          <td className="px-3 py-3">
                            {hasValue && latest!.source ? (
                              <SourceChip source={latest!.source} />
                            ) : (
                              <span className="text-xs text-[#a3a3a3]">—</span>
                            )}
                          </td>
                          <td className="px-3 py-3 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                            <button
                              onClick={() => openEdit(s)}
                              aria-label={`edit ${s.canonical_id}`}
                              className="px-2 py-1 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded hover:bg-[#f5f5f5] mr-2"
                            >
                              Edit
                            </button>
                            <button
                              onClick={() => { setDeleteError(null); setDeleteTarget(s); }}
                              aria-label={`delete ${s.canonical_id}`}
                              className="px-2 py-1 text-xs font-medium text-red-600 bg-white border border-red-200 rounded hover:bg-red-50"
                            >
                              Delete
                            </button>
                          </td>
                        </tr>
                        {isExpanded && (
                          <tr className="qb-selected bg-[#faf9f6] border-b border-[#e5e5e5]">
                            <td colSpan={9} className="px-4 py-4">
                              <ExpandedSeries
                                series={s}
                                baseUrl={baseUrl}
                                onValueAdded={() => refreshLatest([s.canonical_id])}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>

      {/* Create / edit modal */}
      {form && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={form.mode === 'create' ? 'New series' : 'Edit series'}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
            <div className="px-5 py-4 border-b border-[#e5e5e5]">
              <h3 className="text-base font-semibold text-[#0a0a0a]">
                {form.mode === 'create' ? 'New series' : `Edit ${form.original.canonical_id}`}
              </h3>
            </div>
            <div className="px-5 py-4 space-y-3">
              <div>
                <label className="block text-xs font-medium text-[#525252] mb-1">
                  Canonical ID <span className="text-red-500">*</span>
                </label>
                <input
                  aria-label="canonical_id"
                  value={form.state.canonical_id}
                  onChange={(e) => setField('canonical_id', e.target.value)}
                  disabled={form.mode === 'edit'}
                  placeholder="USD.MYDESK.7Y"
                  className="w-full px-3 py-1.5 text-sm font-mono border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f] disabled:bg-[#f5f5f5] disabled:text-[#a3a3a3]"
                />
                {form.mode === 'edit' && (
                  <p className="text-[10px] text-[#a3a3a3] mt-0.5">The canonical ID cannot be changed.</p>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-[#525252] mb-1">
                    Asset class <span className="text-red-500">*</span>
                  </label>
                  <input
                    aria-label="asset_class"
                    value={form.state.asset_class}
                    onChange={(e) => setField('asset_class', e.target.value)}
                    placeholder="RATES, INFLATION, VOL…"
                    className="w-full px-3 py-1.5 text-sm border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#525252] mb-1">
                    Currency <span className="text-red-500">*</span>
                  </label>
                  <input
                    aria-label="currency"
                    value={form.state.currency}
                    onChange={(e) => setField('currency', e.target.value)}
                    placeholder="USD, EUR…"
                    className="w-full px-3 py-1.5 text-sm border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                {OPTIONAL_FIELDS.map(({ key, label, placeholder }) => (
                  <div key={key} className={key === 'description' ? 'col-span-2' : ''}>
                    <label className="block text-xs font-medium text-[#525252] mb-1">{label}</label>
                    <input
                      aria-label={key}
                      value={form.state[key]}
                      onChange={(e) => setField(key, e.target.value)}
                      placeholder={placeholder}
                      className="w-full px-3 py-1.5 text-sm border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
                    />
                  </div>
                ))}
              </div>
              {formError && (
                <div className="p-2 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{formError}</div>
              )}
            </div>
            <div className="px-5 py-3 border-t border-[#e5e5e5] flex items-center justify-end gap-2">
              <button
                onClick={() => setForm(null)}
                disabled={saving}
                className="px-3 py-1.5 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
              >
                Cancel
              </button>
              <button
                onClick={submitForm}
                disabled={saving}
                className="px-4 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28] disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {saving ? 'Saving…' : form.mode === 'create' ? 'Create series' : 'Save changes'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirm dialog */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Delete series">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md">
            <div className="px-5 py-4 border-b border-[#e5e5e5]">
              <h3 className="text-base font-semibold text-[#0a0a0a]">Delete series?</h3>
            </div>
            <div className="px-5 py-4 space-y-2">
              <p className="text-sm text-[#525252]">
                Delete <code className="font-mono text-[#0a0a0a]">{deleteTarget.canonical_id}</code>?
              </p>
              <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg p-2">
                Warning: this also permanently deletes <span className="font-semibold">all of this series' quote values</span>. This cannot be undone.
              </p>
              {deleteError && (
                <div className="p-2 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{deleteError}</div>
              )}
            </div>
            <div className="px-5 py-3 border-t border-[#e5e5e5] flex items-center justify-end gap-2">
              <button
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
                className="px-3 py-1.5 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                disabled={deleting}
                aria-label="confirm delete"
                className="px-4 py-1.5 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {deleting ? 'Deleting…' : 'Delete series and values'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Expanded row — value history (+ sparkline) + inline add-value.
// Reads history from the MD read path; writes a single manual quote through the
// orchestrator import endpoint (server tags source=manual).
// ============================================================================
function ExpandedSeries({
  series,
  baseUrl,
  onValueAdded,
}: {
  series: MarketDataSeries;
  baseUrl: string;
  onValueAdded: () => void;
}) {
  const [points, setPoints] = useState<MdSeriesPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [addDate, setAddDate] = useState('');
  const [addValue, setAddValue] = useState('');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [addNotice, setAddNotice] = useState<string | null>(null);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const history = await getSeriesPoints(baseUrl, series.canonical_id, HISTORY_START, LATEST_AS_OF, 400);
      setPoints([...history].sort((a, b) => a.as_of.localeCompare(b.as_of)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load value history');
    } finally {
      setLoading(false);
    }
  }, [baseUrl, series.canonical_id]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  async function submitValue() {
    setAddError(null);
    setAddNotice(null);
    const date = addDate.trim();
    const raw = addValue.trim();
    if (!date || !raw) {
      setAddError('A date and a value are both required.');
      return;
    }
    const num = Number(raw);
    if (!Number.isFinite(num)) {
      setAddError('Value must be a number.');
      return;
    }
    setAdding(true);
    try {
      const result = await importMarketDataJson([
        { canonical_id: series.canonical_id, as_of: date, value: num },
      ]);
      if (!result.ok) {
        setAddError(result.envelope.error || 'The add failed. Please try again.');
        return;
      }
      const report = result.data;
      if (report.imported === 0) {
        const reason = report.errors[0]?.reason;
        setAddError(reason ? `Rejected: ${reason}` : 'The value was not imported.');
        return;
      }
      setAddNotice(`Added value for ${date} (source ${report.source}).`);
      setAddValue('');
      await loadHistory();
      onValueAdded();
    } finally {
      setAdding(false);
    }
  }

  const renderChart = () => {
    if (points.length < 2) return <div className="text-xs text-[#a3a3a3]">Not enough points to chart</div>;
    const values = points.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;

    // Clean, technical 2D line chart. Fixed, UNDISTORTED coordinate space (no
    // preserveAspectRatio="none" — that non-uniform stretch is what fattened the
    // old stroke into a soft, variable-width "3D" line). No area fill, no
    // gradient, no shadow/blur: just a thin flat solid stroke over a faint axis.
    const W = 640;
    const H = 168;
    const padL = 6;
    const padR = 6;
    const padT = 8;
    const baseY = 132; // x-axis / baseline (bottom of plot area)
    const plotW = W - padL - padR;
    const plotH = baseY - padT;

    const xAt = (i: number) => padL + (i / (points.length - 1)) * plotW;
    const yAt = (v: number) => padT + (1 - (v - min) / range) * plotH;

    const line = points.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.value).toFixed(1)}`).join(' ');

    // 3–4 evenly spaced date ticks (first, thirds, last), de-duplicated.
    const tickCount = Math.min(4, points.length);
    const tickIdx = Array.from(
      new Set(Array.from({ length: tickCount }, (_, k) => Math.round((k / (tickCount - 1)) * (points.length - 1)))),
    );

    // Dark amber tone (~11:1 on the yellow/cream panel rgb(247,244,237)).
    const ink = '#453510';

    return (
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img" aria-label="Value history line chart">
        {/* faint horizontal gridlines (top / mid / baseline) */}
        {[0, 0.5, 1].map((g) => (
          <line
            key={`grid-${g}`}
            x1={padL}
            y1={padT + g * plotH}
            x2={W - padR}
            y2={padT + g * plotH}
            stroke={ink}
            strokeOpacity={0.15}
            strokeWidth={1}
          />
        ))}
        {/* baseline / x-axis */}
        <line x1={padL} y1={baseY} x2={W - padR} y2={baseY} stroke={ink} strokeOpacity={0.55} strokeWidth={1} />
        {/* value line — thin, flat, solid; no fill/gradient/shadow */}
        <polyline fill="none" stroke={ink} strokeWidth={1.3} strokeLinejoin="round" strokeLinecap="round" points={line} />
        {/* x-axis date ticks + labels */}
        {tickIdx.map((idx, k) => {
          const isFirst = k === 0;
          const isLast = k === tickIdx.length - 1;
          const anchor = isFirst ? 'start' : isLast ? 'end' : 'middle';
          const tx = isFirst ? padL : isLast ? W - padR : xAt(idx);
          return (
            <g key={`tick-${idx}`}>
              <line x1={xAt(idx)} y1={baseY} x2={xAt(idx)} y2={baseY + 4} stroke={ink} strokeOpacity={0.7} strokeWidth={1} />
              <text
                x={tx}
                y={baseY + 19}
                fontSize={13}
                fontFamily="ui-monospace, 'JetBrains Mono', monospace"
                fill={ink}
                textAnchor={anchor}
              >
                {points[idx].as_of.slice(0, 10)}
              </text>
            </g>
          );
        })}
      </svg>
    );
  };

  const recent = [...points].reverse().slice(0, 12);

  return (
    <div className="grid lg:grid-cols-3 gap-4">
      {/* History + chart */}
      <div className="lg:col-span-2">
        <div className="text-xs font-medium text-[#737373] mb-2">
          Value history — {series.description || series.canonical_id}
        </div>
        {loading ? (
          <div className="text-xs text-[#a3a3a3]">Loading history…</div>
        ) : error ? (
          <div className="text-xs text-red-500">{error}</div>
        ) : points.length === 0 ? (
          <div className="text-xs text-[#a3a3a3]">No values yet. Add one on the right.</div>
        ) : (
          <>
            <div className="mb-2">{renderChart()}</div>
            <div className="overflow-x-auto max-h-56 overflow-y-auto border border-[#ece7dc] rounded-lg">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[#f9f8f5]">
                  <tr>
                    <th className="text-left px-3 py-1.5 text-xs font-semibold text-[#737373] uppercase tracking-wider">As-of</th>
                    <th className="text-right px-3 py-1.5 text-xs font-semibold text-[#737373] uppercase tracking-wider">Value</th>
                    <th className="text-left px-3 py-1.5 text-xs font-semibold text-[#737373] uppercase tracking-wider">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((p) => (
                    <tr key={p.as_of} className="border-t border-[#f5f5f5]">
                      <td className="px-3 py-1.5 font-mono text-xs text-[#525252]">{p.as_of.slice(0, 10)}</td>
                      <td className="px-3 py-1.5 text-right font-mono text-xs text-[#0a0a0a]">{formatValue(series, p.value)}</td>
                      <td className="px-3 py-1.5">
                        <SourceChip source={p.source} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-1 text-[10px] text-[#a3a3a3]">
              Showing {recent.length} of {points.length} points ({points[0].as_of.slice(0, 10)} → {points[points.length - 1].as_of.slice(0, 10)}).
            </div>
          </>
        )}
      </div>

      {/* Inline add-value */}
      <div className="bg-white border border-[#ece7dc] rounded-lg p-3 h-fit">
        <div className="text-xs font-medium text-[#525252] mb-2">Add a value</div>
        <div className="space-y-2">
          <div>
            <label className="block text-[10px] font-medium text-[#737373] mb-0.5">Date</label>
            <input
              type="date"
              aria-label="add value date"
              value={addDate}
              onChange={(e) => setAddDate(e.target.value)}
              className="w-full px-2 py-1 text-sm border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
            />
          </div>
          <div>
            <label className="block text-[10px] font-medium text-[#737373] mb-0.5">Value</label>
            <input
              type="text"
              inputMode="decimal"
              aria-label="add value amount"
              value={addValue}
              onChange={(e) => setAddValue(e.target.value)}
              placeholder="0.0375"
              className="w-full px-2 py-1 text-sm font-mono border border-[#d4d4d4] rounded-lg focus:outline-none focus:border-[#8a6a2f]"
            />
          </div>
          <button
            onClick={submitValue}
            disabled={adding}
            className="w-full px-3 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {adding ? 'Adding…' : '+ Add value'}
          </button>
          {addError && (
            <div className="p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">{addError}</div>
          )}
          {addNotice && (
            <div className="p-2 rounded-lg bg-green-50 border border-green-200 text-xs text-green-700">{addNotice}</div>
          )}
          <p className="text-[10px] text-[#a3a3a3]">
            Added values are tagged <span className="font-medium">manual</span> (via the same import path as the bulk screen).
          </p>
        </div>
      </div>
    </div>
  );
}
