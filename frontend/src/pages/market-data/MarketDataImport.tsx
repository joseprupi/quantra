// Market Data → Import — load user-supplied quotes into the
// shared md.* catalog via the orchestrator's POST /v1/market-data/import.
//
// Two modes on ONE endpoint:
//   • Manual entry — an editable table of rows → JSON batch (source="manual").
//   • CSV upload — a `.csv` file part → multipart (source="csv").
//
// Per-row failures come back SOFT in the report's `errors[]` with HTTP 200, so
// a 200 is NOT treated as total success — we always render the per-row report.
// Hard failures (structured envelope) render as an error banner keyed on `code`.
//
// Honesty: imported quotes are USER-SUPPLIED and provenance-tagged
// (`source` = manual/csv), sitting alongside the real public market-data feeds
// (Bank of England, US Treasury, FRED, ECB).
import { useState, useMemo, useEffect } from 'react';
import { Link } from 'react-router-dom';
import Header from '../../components/Header';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import {
  importMarketDataJson,
  importMarketDataCsv,
  listSeries,
  type MarketDataImportQuote,
  type MarketDataImportReport,
} from '../../lib/api/orchestrator';
import type { ApiErrorEnvelope } from '../../lib/api/types';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

interface ManualRow {
  canonical_id: string;
  as_of: string;
  value: string;
  source: string;
}

function emptyRow(asOf: string): ManualRow {
  return { canonical_id: '', as_of: asOf, value: '', source: '' };
}

interface RowValidation {
  ok: boolean;
  reason?: string;
}

function validateManualRow(row: ManualRow): RowValidation {
  if (!row.canonical_id.trim()) return { ok: false, reason: 'canonical_id is required' };
  if (!ISO_DATE.test(row.as_of.trim())) return { ok: false, reason: 'as_of must be YYYY-MM-DD' };
  if (row.value.trim() === '' || !Number.isFinite(Number(row.value)))
    return { ok: false, reason: 'value must be numeric' };
  return { ok: true };
}

export default function MarketDataImport() {
  const { asOfDate } = useAsOfDate();
  const [tab, setTab] = useState<'manual' | 'csv'>('manual');

  // Shared result state (whichever mode last submitted).
  const [report, setReport] = useState<MarketDataImportReport | null>(null);
  const [hardError, setHardError] = useState<ApiErrorEnvelope | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Existing series — the canonical_id dropdown source for manual entry. Import
  // now rejects unknown series, so the manual form steers the operator to pick
  // one that exists (defined via the Quote Book's series manager).
  const [seriesIds, setSeriesIds] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    void listSeries().then((result) => {
      if (cancelled || !result.ok) return;
      setSeriesIds(
        result.data.series.map((s) => s.canonical_id).sort((a, b) => a.localeCompare(b)),
      );
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function resetResult() {
    setReport(null);
    setHardError(null);
  }

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-4xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-semibold text-[#0a0a0a]">Import Market Data</h1>
            <p className="text-[#737373] mt-1">
              Load your own quotes into the shared catalog — manual rows or a CSV upload. Imported
              values are <span className="font-medium text-[#525252]">user-supplied</span> and
              tagged by source, alongside the real public market-data feeds.
            </p>
          </div>
          <span className="px-3 py-1.5 text-xs font-medium text-[#8a6a2f] bg-[#f7f4ed] border border-[#ece7dc] rounded-lg whitespace-nowrap">
            Writes md.*
          </span>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 mb-4 border-b border-[#e5e5e5]">
          <button
            onClick={() => setTab('manual')}
            className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors ${
              tab === 'manual'
                ? 'border-[#8a6a2f] text-[#8a6a2f]'
                : 'border-transparent text-[#737373] hover:text-[#0a0a0a]'
            }`}
          >
            Manual entry
          </button>
          <button
            onClick={() => setTab('csv')}
            className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors ${
              tab === 'csv'
                ? 'border-[#8a6a2f] text-[#8a6a2f]'
                : 'border-transparent text-[#737373] hover:text-[#0a0a0a]'
            }`}
          >
            CSV upload
          </button>
        </div>

        {tab === 'manual' ? (
          <ManualEntry
            defaultAsOf={asOfDate}
            seriesIds={seriesIds}
            submitting={submitting}
            setSubmitting={setSubmitting}
            onResult={(rep, err) => {
              setReport(rep);
              setHardError(err);
            }}
            onReset={resetResult}
          />
        ) : (
          <CsvUpload
            submitting={submitting}
            setSubmitting={setSubmitting}
            onResult={(rep, err) => {
              setReport(rep);
              setHardError(err);
            }}
            onReset={resetResult}
          />
        )}

        {/* Result region — shared across both tabs */}
        {hardError && <HardErrorBanner envelope={hardError} />}
        {report && <ImportReportView report={report} />}
      </main>
    </div>
  );
}

// ============================================================================
// Manual entry — editable rows → JSON batch
// ============================================================================
function ManualEntry({
  defaultAsOf,
  seriesIds,
  submitting,
  setSubmitting,
  onResult,
  onReset,
}: {
  defaultAsOf: string;
  seriesIds: string[];
  submitting: boolean;
  setSubmitting: (v: boolean) => void;
  onResult: (report: MarketDataImportReport | null, hardError: ApiErrorEnvelope | null) => void;
  onReset: () => void;
}) {
  const [rows, setRows] = useState<ManualRow[]>(() => [emptyRow(defaultAsOf)]);

  const validations = useMemo(() => rows.map(validateManualRow), [rows]);
  const anyValid = validations.some((v) => v.ok);

  function updateRow(idx: number, patch: Partial<ManualRow>) {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }
  function addRow() {
    setRows((prev) => [...prev, emptyRow(defaultAsOf)]);
  }
  function removeRow(idx: number) {
    setRows((prev) => (prev.length === 1 ? prev : prev.filter((_, i) => i !== idx)));
  }

  async function submit() {
    onReset();
    // Client validation gates the submit, but the SERVER report is the
    // authoritative result — we only send rows that pass the basic checks.
    const quotes: MarketDataImportQuote[] = rows
      .filter((_, i) => validations[i].ok)
      .map((r) => {
        const q: MarketDataImportQuote = {
          canonical_id: r.canonical_id.trim(),
          as_of: r.as_of.trim(),
          value: Number(r.value),
        };
        if (r.source.trim()) q.source = r.source.trim();
        return q;
      });
    if (quotes.length === 0) return;
    setSubmitting(true);
    try {
      const result = await importMarketDataJson(quotes);
      if (result.ok) onResult(result.data, null);
      else onResult(null, result.envelope);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
      <div className="mb-3 text-xs text-[#737373]">
        {seriesIds.length === 0 ? (
          <>
            No series defined yet — import only accepts existing series.{' '}
            <Link to="/quote-book" className="font-medium text-[#8a6a2f] hover:underline">
              Define a new series in the Quote Book first →
            </Link>
          </>
        ) : (
          <>
            Pick an existing series for each row. Need a new one?{' '}
            <Link to="/quote-book" className="font-medium text-[#8a6a2f] hover:underline">
              Define it in the Quote Book first →
            </Link>
          </>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px]">
          <thead>
            <tr className="bg-[#fafafa]">
              <th className="text-left px-3 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider">
                Canonical ID
              </th>
              <th className="text-left px-3 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider">
                As-Of
              </th>
              <th className="text-left px-3 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider">
                Value
              </th>
              <th className="text-left px-3 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider">
                Source <span className="normal-case font-normal text-[#a3a3a3]">(optional)</span>
              </th>
              <th className="w-10" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => {
              const v = validations[idx];
              const touched = row.canonical_id || row.value;
              return (
                <tr key={idx} className="border-b border-[#f5f5f5]">
                  <td className="px-2 py-1.5">
                    <select
                      aria-label={`canonical_id row ${idx + 1}`}
                      value={row.canonical_id}
                      onChange={(e) => updateRow(idx, { canonical_id: e.target.value })}
                      className="w-full px-2 py-1 text-sm font-mono bg-white border border-[#d4d4d4] rounded focus:outline-none focus:border-[#8a6a2f]"
                    >
                      <option value="">Select a series…</option>
                      {seriesIds.map((id) => (
                        <option key={id} value={id}>{id}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      type="date"
                      aria-label={`as_of row ${idx + 1}`}
                      value={row.as_of}
                      onChange={(e) => updateRow(idx, { as_of: e.target.value })}
                      className="w-full px-2 py-1 text-sm border border-[#d4d4d4] rounded focus:outline-none focus:border-[#8a6a2f]"
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      aria-label={`value row ${idx + 1}`}
                      value={row.value}
                      onChange={(e) => updateRow(idx, { value: e.target.value })}
                      placeholder="0.032"
                      inputMode="decimal"
                      className="w-full px-2 py-1 text-sm font-mono border border-[#d4d4d4] rounded focus:outline-none focus:border-[#8a6a2f]"
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      aria-label={`source row ${idx + 1}`}
                      value={row.source}
                      onChange={(e) => updateRow(idx, { source: e.target.value })}
                      placeholder="manual"
                      className="w-full px-2 py-1 text-sm border border-[#d4d4d4] rounded focus:outline-none focus:border-[#8a6a2f]"
                    />
                  </td>
                  <td className="px-1 py-1.5 text-center align-middle">
                    <button
                      onClick={() => removeRow(idx)}
                      disabled={rows.length === 1}
                      aria-label={`remove row ${idx + 1}`}
                      className="text-[#a3a3a3] hover:text-red-600 disabled:opacity-30 disabled:cursor-not-allowed text-lg leading-none"
                      title="Remove row"
                    >
                      ×
                    </button>
                    {touched && !v.ok && (
                      <div className="text-[10px] text-red-500 mt-0.5 whitespace-nowrap">{v.reason}</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-3 mt-4">
        <button
          onClick={addRow}
          className="px-3 py-1.5 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
        >
          + Add row
        </button>
        <button
          onClick={submit}
          disabled={!anyValid || submitting}
          className="px-4 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28] disabled:opacity-40 disabled:cursor-not-allowed ml-auto"
        >
          {submitting ? 'Importing…' : 'Import rows'}
        </button>
      </div>
    </div>
  );
}

// ============================================================================
// CSV upload — file part → multipart
// ============================================================================
function CsvUpload({
  submitting,
  setSubmitting,
  onResult,
  onReset,
}: {
  submitting: boolean;
  setSubmitting: (v: boolean) => void;
  onResult: (report: MarketDataImportReport | null, hardError: ApiErrorEnvelope | null) => void;
  onReset: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [previewRows, setPreviewRows] = useState<string[]>([]);

  function onPick(f: File | null) {
    onReset();
    setFile(f);
    setPreviewRows([]);
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : '';
      const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '');
      setPreviewRows(lines.slice(0, 5));
    };
    reader.readAsText(f);
  }

  async function submit() {
    if (!file) return;
    onReset();
    setSubmitting(true);
    try {
      const result = await importMarketDataCsv(file);
      if (result.ok) onResult(result.data, null);
      else onResult(null, result.envelope);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
      <div className="mb-4 p-3 rounded-lg bg-[#f9f8f5] border border-[#ece7dc] text-sm text-[#525252]">
        <div className="font-medium text-[#0a0a0a] mb-1">Expected CSV columns</div>
        <code className="text-xs font-mono text-[#8a6a2f]">
          canonical_id, as_of (YYYY-MM-DD), value[, source, meta_json]
        </code>
        <p className="text-xs text-[#737373] mt-1">
          A header row is optional (auto-detected). <code className="font-mono">source</code> and{' '}
          <code className="font-mono">meta_json</code> are optional per row.
        </p>
      </div>

      <input
        type="file"
        accept=".csv,text/csv"
        aria-label="CSV file"
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        className="block w-full text-sm text-[#525252] file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border file:border-[#d4d4d4] file:text-sm file:font-medium file:bg-white file:text-[#525252] hover:file:bg-[#f5f5f5]"
      />

      {previewRows.length > 0 && (
        <div className="mt-4">
          <div className="text-xs font-medium text-[#737373] mb-1">
            Preview — first {previewRows.length} line{previewRows.length === 1 ? '' : 's'}
          </div>
          <pre className="text-xs font-mono bg-[#fafafa] border border-[#e5e5e5] rounded-lg p-3 overflow-x-auto text-[#525252]">
            {previewRows.join('\n')}
          </pre>
        </div>
      )}

      <div className="flex mt-4">
        <button
          onClick={submit}
          disabled={!file || submitting}
          className="px-4 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#755a28] disabled:opacity-40 disabled:cursor-not-allowed ml-auto"
        >
          {submitting ? 'Uploading…' : 'Upload CSV'}
        </button>
      </div>
    </div>
  );
}

// ============================================================================
// Result rendering — report (soft) + hard-error banner
// ============================================================================
function HardErrorBanner({ envelope }: { envelope: ApiErrorEnvelope }) {
  return (
    <div className="mt-6 p-4 rounded-xl bg-red-50 border border-red-200">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-red-700">Import failed</span>
        <code className="text-xs font-mono px-1.5 py-0.5 rounded bg-red-100 text-red-700">
          {envelope.code}
        </code>
      </div>
      <p className="text-sm text-red-700 mt-1">{envelope.error}</p>
      {envelope.request_id && (
        <p className="text-xs text-red-400 mt-1 font-mono">request-id: {envelope.request_id}</p>
      )}
    </div>
  );
}

function ImportReportView({ report }: { report: MarketDataImportReport }) {
  const allFailed = report.imported === 0 && (report.skipped > 0 || report.errors.length > 0);
  const partial = report.imported > 0 && report.errors.length > 0;
  const tone = allFailed
    ? { bg: 'bg-red-50', border: 'border-red-200', text: 'text-red-700', label: 'No rows imported' }
    : partial
      ? { bg: 'bg-amber-50', border: 'border-amber-200', text: 'text-amber-800', label: 'Partial import' }
      : { bg: 'bg-green-50', border: 'border-green-200', text: 'text-green-700', label: 'Import complete' };

  return (
    <div className={`mt-6 rounded-xl border ${tone.border} ${tone.bg} overflow-hidden`}>
      <div className="px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className={`text-sm font-semibold ${tone.text}`}>{tone.label}</span>
        <span className="text-sm text-[#525252]">
          Imported <span className="font-semibold text-[#0a0a0a]">{report.imported}</span>
        </span>
        <span className="text-sm text-[#525252]">
          Skipped <span className="font-semibold text-[#0a0a0a]">{report.skipped}</span>
        </span>
        <span className="text-xs text-[#737373]">
          source <code className="font-mono text-[#8a6a2f]">{report.source}</code>
        </span>
        {report.imported > 0 && (
          <Link to="/quote-book" className="text-sm font-medium text-[#8a6a2f] hover:underline ml-auto">
            View in Quote Book →
          </Link>
        )}
      </div>

      {report.errors.length > 0 && (
        <div className="bg-white border-t border-[#e5e5e5]">
          <table className="w-full">
            <thead>
              <tr className="bg-[#fafafa]">
                <th className="text-left px-4 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider w-20">
                  Row
                </th>
                <th className="text-left px-4 py-2 text-xs font-semibold text-[#737373] uppercase tracking-wider">
                  Reason
                </th>
              </tr>
            </thead>
            <tbody>
              {report.errors.map((e, i) => (
                <tr key={i} className="border-b border-[#f5f5f5]">
                  <td className="px-4 py-2 text-sm font-mono text-[#525252]">{e.row}</td>
                  <td className="px-4 py-2 text-sm text-red-600">{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
