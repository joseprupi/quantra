// Pricing-trace investigation page — general pipeline inspector.
//
// Enter (or deep-link via ?request_id=…) a request id → calls the orchestrator
// GET /v1/traces/{request_id} and renders the pipeline as a header bar (status,
// headline result) plus one card per stage: clean structured summary first,
// raw JSON on demand. Product-agnostic across all six products; degrades
// gracefully on older / partial / error traces. The orchestrator is the sole
// writer of these traces; the page is a thin reader over the typed API client.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Header from '../components/Header';
import { getTrace } from '../lib/api/orchestrator';
import type { PricingTrace, TraceStage } from '../lib/api/orchestrator';
import {
  deriveRequestMode,
  deriveTraceHeader,
  flowColumns,
  formatFlowCell,
  formatNumber,
  formatValue,
  isObj,
  normalizeEngineRequest,
  normalizeEngineResponse,
  normalizeMdResolve,
  prettyJson,
  splitEngineRequestViews,
} from './investigate/normalize';
import type { FlowsSection, KeyTerm } from './investigate/normalize';

type LoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; trace: PricingTrace }
  | { status: 'not_found'; requestId: string }
  | { status: 'error'; message: string; code: string };

// Shared bits

const LEVEL_DOT: Record<string, string> = {
  info: 'bg-emerald-500',
  warn: 'bg-amber-500',
  error: 'bg-red-500',
};

const STAGE_TITLES: Record<string, string> = {
  input: 'Request received',
  load_entities: 'Entities loaded',
  md_resolve: 'Market data',
  engine_request: 'Engine request',
  engine_response: 'Engine response',
  history_write: 'History write',
  error: 'Error',
};

function useCopy(): [copied: boolean, copy: (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const copy = useCallback((text: string) => {
    const done = () => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    };
    try {
      if (navigator.clipboard?.writeText) {
        void navigator.clipboard.writeText(text).then(done, done);
      } else {
        done();
      }
    } catch {
      done();
    }
  }, []);
  return [copied, copy];
}

function CopyButton({ text, label, testId }: { text: string; label: string; testId?: string }) {
  const [copied, copy] = useCopy();
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={() => copy(text)}
      className="text-xs px-2 py-1 rounded border border-[#e5e5e5] bg-white text-[#525252] hover:bg-[#fafafa] hover:text-[#0a0a0a]"
    >
      {copied ? 'Copied ✓' : label}
    </button>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] font-medium uppercase tracking-wide text-[#a3a3a3] mb-1">{children}</p>
  );
}

function TermGrid({ terms, testId }: { terms: KeyTerm[]; testId?: string }) {
  if (terms.length === 0) return null;
  return (
    <dl data-testid={testId} className="grid grid-cols-[minmax(90px,auto)_1fr] gap-x-4 gap-y-1 text-[13px]">
      {terms.map(t => (
        <div key={t.label} className="contents">
          <dt className="text-[#737373]">{t.label}</dt>
          <dd className="text-[#0a0a0a] font-mono break-words">{t.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Collapsed raw-JSON expander + copy button, shared by every stage card. */
function RawJson({ payload, label = 'Raw JSON' }: { payload: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  const json = prettyJson(payload);
  return (
    <div className="border-t border-[#f5f5f5] px-4 py-2 flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen(v => !v)}
          aria-expanded={open}
          data-testid="raw-json-toggle"
          className="text-xs text-[#737373] hover:text-[#0a0a0a] flex items-center gap-1"
        >
          <svg
            className={`w-3 h-3 transition-transform ${open ? 'rotate-90' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          {label}
        </button>
        <CopyButton text={json} label="Copy raw JSON" testId="copy-raw-json" />
      </div>
      {open && (
        <pre
          data-testid="raw-json"
          className="text-[12px] leading-relaxed text-[#525252] font-mono bg-[#f5f5f5] rounded-md px-3 py-2 overflow-auto max-h-96 whitespace-pre-wrap break-words"
        >
          {json}
        </pre>
      )}
    </div>
  );
}

// Per-stage structured summaries

function scalarTerms(payload: unknown, skip: string[] = []): KeyTerm[] {
  if (!isObj(payload)) return [];
  const terms: KeyTerm[] = [];
  for (const [k, v] of Object.entries(payload)) {
    if (skip.includes(k) || k.startsWith('_')) continue;
    if (v === null || v === undefined) continue;
    if (typeof v === 'number') terms.push({ label: k, value: formatValue(v) });
    else if (typeof v === 'string' || typeof v === 'boolean') terms.push({ label: k, value: String(v) });
    else if (Array.isArray(v) && v.every(x => typeof x === 'string') && v.length > 0) {
      terms.push({ label: k, value: v.join(', ') });
    }
  }
  return terms;
}

function InputSummary({ payload }: { payload: unknown }) {
  const mode = deriveRequestMode(payload);
  const product = isObj(payload) && typeof payload.product === 'string' ? payload.product : null;
  const terms = scalarTerms(payload, ['product']);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 text-[13px]">
        {product && (
          <span className="font-mono font-medium text-[#0a0a0a] bg-[#f5f5f5] border border-[#e5e5e5] rounded px-1.5 py-0.5">
            {product}
          </span>
        )}
        {mode && (
          <span data-testid="request-mode" className="text-[#525252]">
            mode: <span className="font-medium">{mode}</span>
          </span>
        )}
      </div>
      <TermGrid terms={terms} testId="input-terms" />
    </div>
  );
}

function MdResolveSummary({ payload }: { payload: unknown }) {
  const md = normalizeMdResolve(payload);
  if (md.resolved.length === 0 && md.misses.length === 0) {
    return (
      <p className="text-[13px] text-[#737373]">
        No market-data quotes resolved — inputs were inline, nothing pulled from the market-data service.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {(md.liveCount != null || md.snapshotCount != null) && (
        <p className="text-[13px] text-[#525252]">
          {md.resolved.length} resolved
          {md.liveCount != null && <> · {md.liveCount} live</>}
          {md.snapshotCount != null && <> · {md.snapshotCount} from snapshot</>}
          {md.misses.length > 0 && <> · {md.misses.length} missed</>}
        </p>
      )}
      {md.resolved.length > 0 && (
        <table data-testid="resolved-quotes-table" className="text-[13px] w-full">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide text-[#a3a3a3]">
              <th className="pr-4 pb-1 font-medium">Canonical id</th>
              <th className="pr-4 pb-1 font-medium text-right">Value</th>
              <th className="pr-4 pb-1 font-medium">Source</th>
              <th className="pb-1 font-medium">As of</th>
            </tr>
          </thead>
          <tbody>
            {md.resolved.map(q => (
              <tr key={q.canonicalId} className="border-t border-[#f5f5f5]">
                <td className="pr-4 py-1 font-mono">{q.canonicalId}</td>
                <td className="pr-4 py-1 font-mono text-right tabular-nums">{q.value ?? '—'}</td>
                <td className="pr-4 py-1 text-[#525252]">
                  {q.fromSnapshot ? 'snapshot' : (q.source ?? 'live')}
                </td>
                <td className="py-1 text-[#525252]">{q.asOf ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {md.misses.length > 0 && (
        <p data-testid="md-misses" className="text-[13px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
          Missed: <span className="font-mono">{md.misses.join(', ')}</span>
        </p>
      )}
    </div>
  );
}

const WIRE_TOOLTIP =
  'Wire = exact FlatBuffers sent to the engine; internal = orchestrator pre-encoding inputs, not transmitted.';

function EngineRequestSummary({ payload }: { payload: unknown }) {
  const views = useMemo(() => splitEngineRequestViews(payload), [payload]);
  const [view, setView] = useState<'wire' | 'internal'>(views.wire ? 'wire' : 'internal');
  const selected = view === 'wire' ? views.wire : views.internal;
  const summary = useMemo(() => normalizeEngineRequest(selected), [selected]);

  const toggleBtn = (which: 'wire' | 'internal', label: string, available: boolean) => (
    <button
      type="button"
      data-testid={`view-toggle-${which}`}
      aria-pressed={view === which}
      disabled={!available}
      onClick={() => available && setView(which)}
      title={available ? undefined : which === 'wire' ? 'No wire capture on this trace' : 'No internal capture on this trace'}
      className={`text-xs px-2.5 py-1 rounded-md border ${
        view === which
          ? 'bg-[#0a0a0a] text-white border-[#e5e5e5] font-medium'
          : 'bg-white text-[#525252] border-[#e5e5e5] hover:bg-[#fafafa]'
      } disabled:opacity-40 disabled:cursor-not-allowed`}
    >
      {label}
    </button>
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 flex-wrap">
        {toggleBtn('wire', 'Sent to engine (wire)', views.wire != null)}
        {toggleBtn('internal', 'Orchestrator internal', views.internal != null)}
        <span
          data-testid="view-toggle-tooltip"
          title={WIRE_TOOLTIP}
          aria-label={WIRE_TOOLTIP}
          className="text-[#a3a3a3] text-sm cursor-help select-none"
        >
          ⓘ
        </span>
        {views.rpc && view === 'wire' && (
          <span className="text-[12px] text-[#737373] font-mono ml-auto">
            {views.rpc}
            {views.bytesLen != null && ` · ${formatNumber(views.bytesLen)} bytes`}
            {views.sent === false && ' · not sent'}
          </span>
        )}
      </div>

      {selected == null ? (
        <p className="text-[13px] text-[#737373]">
          No {view === 'wire' ? 'wire capture' : 'request object'} recorded on this trace.
        </p>
      ) : (
        <>
          {summary.trade.length > 0 && (
            <div>
              <SectionLabel>Trade</SectionLabel>
              <TermGrid terms={summary.trade} testId="engine-request-trade" />
            </div>
          )}

          {summary.curves.length > 0 && (
            <div>
              <SectionLabel>Curves</SectionLabel>
              <ul data-testid="engine-request-curves" className="flex flex-wrap gap-2">
                {summary.curves.map(c => (
                  <li
                    key={c.id}
                    className="text-[13px] font-mono bg-[#f5f5f5] border border-[#e5e5e5] rounded px-2 py-0.5"
                  >
                    {c.id}
                    {c.role && <span className="text-[#8a6a2f] font-sans"> · {c.role}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {summary.indices.length > 0 && (
            <div>
              <SectionLabel>Indices</SectionLabel>
              <table data-testid="engine-request-indices" className="text-[13px] w-full">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-[#a3a3a3]">
                    <th className="pr-4 pb-1 font-medium">Index</th>
                    <th className="pr-4 pb-1 font-medium">Tenor</th>
                    <th className="pr-4 pb-1 font-medium">Day count</th>
                    <th className="pb-1 font-medium">Used by</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.indices.map((idx, i) => (
                    <tr key={idx.id ?? i} data-testid="index-row" className="border-t border-[#f5f5f5]">
                      <td className="pr-4 py-1">
                        <span className="font-mono">{idx.name}</span>
                        {idx.id && idx.id !== idx.name && (
                          <span className="text-[#a3a3a3] font-mono text-[12px]"> ({idx.id})</span>
                        )}
                        {idx.isDefaultCatalog && (
                          <span
                            data-testid="default-catalog-badge"
                            title="Default curve-bootstrap catalog entry"
                            className="ml-1.5 text-[11px] text-[#8a6a2f] bg-[#f5f5f5] border border-[#e5e5e5] rounded px-1 py-px align-middle"
                          >
                            default catalog
                          </span>
                        )}
                      </td>
                      <td className="pr-4 py-1 font-mono tabular-nums">{idx.tenor ?? '—'}</td>
                      <td className="pr-4 py-1 font-mono">{idx.dayCount ?? '—'}</td>
                      <td className="py-1">
                        {idx.boundTo.length > 0 ? (
                          <span
                            data-testid="index-bound-leg"
                            className="text-[12px] text-emerald-800 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-px"
                          >
                            {idx.boundTo.join(', ')}
                          </span>
                        ) : (
                          <span className="text-[#a3a3a3]">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {summary.schedules.length > 0 && (
            <div>
              <SectionLabel>Schedules</SectionLabel>
              <ul data-testid="engine-request-schedules" className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
                {summary.schedules.map(s => (
                  <li key={s.label}>
                    <span className="text-[#737373]">{s.label}:</span>{' '}
                    <span className="font-mono">{s.frequency}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {summary.trade.length === 0 &&
            summary.curves.length === 0 &&
            summary.indices.length === 0 &&
            summary.schedules.length === 0 && (
              <p className="text-[13px] text-[#737373]">
                No structured summary available for this payload — see the raw JSON below.
              </p>
            )}
        </>
      )}

      <RawJson payload={selected ?? payload} label={view === 'wire' ? 'Raw JSON (wire, decoded)' : 'Raw JSON (internal)'} />
    </div>
  );
}

function FlowsTable({ section }: { section: FlowsSection }) {
  const [open, setOpen] = useState(false);
  const columns = flowColumns(section.rows);
  return (
    <div className="border border-[#e5e5e5] rounded-md overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        data-testid="flows-toggle"
        className="w-full flex items-center gap-2 px-3 py-2 text-left text-[13px] text-[#525252] hover:bg-[#fafafa]"
      >
        <svg
          className={`w-3 h-3 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        <span className="font-medium">{section.label} flows</span>
        <span className="text-[#a3a3a3]">({section.rows.length})</span>
      </button>
      {open && (
        <div className="overflow-x-auto border-t border-[#f5f5f5]">
          <table data-testid="flows-table" className="text-[12px] w-full">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wide text-[#a3a3a3]">
                {columns.map(c => (
                  <th key={c} className="px-2 py-1 font-medium whitespace-nowrap">
                    {c.replace(/_/g, ' ')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {section.rows.map((row, i) => (
                <tr key={i} className="border-t border-[#f5f5f5]">
                  {columns.map(c => (
                    <td key={c} className="px-2 py-1 font-mono tabular-nums whitespace-nowrap">
                      {formatFlowCell(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ErrorBlock({
  code,
  message,
  details,
}: {
  code: string | null;
  message: string | null;
  details?: unknown;
}) {
  return (
    <div data-testid="error-block" className="bg-red-50 border border-red-200 rounded-lg px-4 py-3">
      {code && <p className="text-sm font-mono font-semibold text-red-700">{code}</p>}
      <p className="text-sm text-red-800 mt-1 break-words whitespace-pre-wrap">
        {message ?? 'The pipeline failed; see the raw payload for detail.'}
      </p>
      {details != null && (
        <pre className="mt-2 text-[12px] font-mono text-red-700/80 overflow-auto max-h-48 whitespace-pre-wrap break-words">
          {prettyJson(details)}
        </pre>
      )}
    </div>
  );
}

function EngineResponseSummaryView({ payload }: { payload: unknown }) {
  const resp = useMemo(() => normalizeEngineResponse(payload), [payload]);
  if (resp.error) {
    return <ErrorBlock code={resp.error.code} message={resp.error.message} details={resp.error.details} />;
  }
  return (
    <div className="flex flex-col gap-3">
      {resp.npv != null && (
        <p className="text-[15px]">
          <span className="text-[#737373]">NPV</span>{' '}
          <span data-testid="engine-response-npv" className="font-mono font-semibold text-[#0a0a0a] tabular-nums">
            {formatNumber(resp.npv)}
          </span>
        </p>
      )}
      {resp.legNpvs.length > 0 && (
        <ul data-testid="leg-npvs" className="flex flex-wrap gap-2 text-[13px]">
          {resp.legNpvs.map((leg, i) => (
            <li key={`${leg.role}-${i}`} className="bg-[#f5f5f5] border border-[#e5e5e5] rounded px-2 py-0.5">
              <span className="text-[#737373]">{leg.role}</span>{' '}
              <span className="font-mono tabular-nums">{formatNumber(leg.npv)}</span>
            </li>
          ))}
        </ul>
      )}
      {resp.metrics.length > 0 && <TermGrid terms={resp.metrics} testId="engine-response-metrics" />}
      {resp.flows.map(section => (
        <FlowsTable key={section.label} section={section} />
      ))}
    </div>
  );
}

function HistoryWriteSummary({ payload }: { payload: unknown }) {
  if (!isObj(payload)) return null;
  const recorded = payload.recorded === true;
  const id = payload.pricing_history_id;
  return (
    <p className="text-[13px]" data-testid="history-write-summary">
      {recorded && id != null ? (
        <>
          Persisted as <span className="font-mono">pricing_history_id={String(id)}</span>.
        </>
      ) : (
        <span className="text-amber-700">Not persisted (pricing_history_id is null).</span>
      )}
    </p>
  );
}

function ErrorStageSummary({ payload }: { payload: unknown }) {
  const e = isObj(payload) && isObj(payload.error) ? payload.error : isObj(payload) ? payload : null;
  return (
    <ErrorBlock
      code={e && typeof e.code === 'string' ? e.code : null}
      message={
        e && typeof e.error === 'string'
          ? e.error
          : e && typeof e.message === 'string'
            ? e.message
            : e && typeof e.detail === 'string'
              ? e.detail
              : null
      }
      details={e && e.details != null ? e.details : undefined}
    />
  );
}

// Stage card

function StageBody({ stage }: { stage: TraceStage }) {
  switch (stage.stage) {
    case 'input':
      return <InputSummary payload={stage.payload} />;
    case 'md_resolve':
      return <MdResolveSummary payload={stage.payload} />;
    case 'engine_request':
      return <EngineRequestSummary payload={stage.payload} />;
    case 'engine_response':
      return <EngineResponseSummaryView payload={stage.payload} />;
    case 'history_write':
      return <HistoryWriteSummary payload={stage.payload} />;
    case 'error':
      return <ErrorStageSummary payload={stage.payload} />;
    case 'load_entities':
    default: {
      const terms = scalarTerms(stage.payload);
      return terms.length > 0 ? <TermGrid terms={terms} /> : null;
    }
  }
}

function StageCard({ stage, index }: { stage: TraceStage; index: number }) {
  const dot = LEVEL_DOT[stage.level] ?? 'bg-slate-400';
  const title = STAGE_TITLES[stage.stage] ?? stage.stage;
  const summary = stage.summary;
  // engine_request renders its own raw viewer (wire ⟷ internal aware).
  const ownRaw = stage.stage === 'engine_request';
  return (
    <div data-testid="stage-card" className="border border-[#e5e5e5] rounded-lg overflow-hidden bg-white">
      <div className="flex items-center gap-3 px-4 py-2.5 bg-[#f5f5f5] border-b border-[#f5f5f5]">
        <span data-testid="stage-dot" className={`flex-shrink-0 w-2.5 h-2.5 rounded-full ${dot}`} aria-hidden />
        <span className="flex-shrink-0 w-5 text-xs font-mono text-[#a3a3a3] tabular-nums">{index + 1}</span>
        <span data-testid="stage-title" className="text-sm font-semibold text-[#0a0a0a]">
          {title}
        </span>
        <span data-testid="stage-name" className="text-[11px] font-mono text-[#a3a3a3]">
          {stage.stage}
        </span>
        <span className="ml-auto flex items-center gap-3">
          {typeof stage.duration_ms === 'number' && (
            <span className="text-xs font-mono text-[#737373] tabular-nums">{stage.duration_ms} ms</span>
          )}
          <span className="text-[11px] text-[#a3a3a3] truncate max-w-[180px]" title={stage.ts}>
            {stage.ts}
          </span>
        </span>
      </div>
      <div className="px-4 py-3 flex flex-col gap-2">
        {typeof summary === 'string' && summary.length > 0 && stage.stage !== 'error' && (
          <p data-testid="stage-summary" className="text-[13px] text-[#525252]">
            {summary}
          </p>
        )}
        <StageBody stage={stage} />
      </div>
      {!ownRaw && <RawJson payload={stage.payload} />}
    </div>
  );
}

// Header bar

function TraceHeaderBar({ trace }: { trace: PricingTrace }) {
  const header = useMemo(() => deriveTraceHeader(trace.stages), [trace.stages]);
  return (
    <div
      data-testid="trace-header"
      className="bg-white border border-[#e5e5e5] rounded-xl px-4 py-3 mb-4 flex items-center gap-x-4 gap-y-2 flex-wrap"
    >
      <span className="flex items-center gap-1.5 min-w-0">
        <span data-testid="trace-request-id" className="font-mono text-sm text-[#0a0a0a] truncate">
          {trace.request_id}
        </span>
        <CopyButton text={trace.request_id} label="Copy" testId="copy-request-id" />
      </span>
      {header.product && (
        <span data-testid="trace-product" className="text-sm font-mono bg-[#f5f5f5] border border-[#e5e5e5] rounded px-1.5 py-0.5">
          {header.product}
        </span>
      )}
      {header.timestamp && (
        <span className="text-xs text-[#737373]" title={header.timestamp}>
          {header.timestamp}
        </span>
      )}
      <span
        data-testid="trace-status"
        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${
          header.isError
            ? 'bg-red-50 text-red-700 border-red-200'
            : 'bg-emerald-50 text-emerald-700 border-emerald-200'
        }`}
      >
        {header.isError ? 'ERROR' : 'OK'}
      </span>
      {header.totalDurationMs != null && (
        <span className="text-xs font-mono text-[#737373] tabular-nums">{formatNumber(header.totalDurationMs)} ms</span>
      )}
      <span className="ml-auto text-sm">
        {header.isError ? (
          <span data-testid="trace-headline" className="font-mono font-semibold text-red-700">
            {header.errorCode ?? 'failed'}
          </span>
        ) : header.npv != null ? (
          <span data-testid="trace-headline" className="font-mono font-semibold text-[#0a0a0a] tabular-nums">
            NPV {formatNumber(header.npv)}
          </span>
        ) : null}
      </span>
    </div>
  );
}

// Page

export default function Investigate() {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRequestId = searchParams.get('request_id') ?? '';
  const [input, setInput] = useState(urlRequestId);
  const [state, setState] = useState<LoadState>({ status: 'idle' });

  const runLookup = useCallback(async (requestId: string) => {
    const id = requestId.trim();
    if (!id) {
      setState({ status: 'idle' });
      return;
    }
    setState({ status: 'loading' });
    const result = await getTrace(id);
    if (result.ok) {
      setState({ status: 'loaded', trace: result.data });
      return;
    }
    if (result.envelope.code === 'trace_not_found' || result.httpStatus === 404) {
      setState({ status: 'not_found', requestId: id });
      return;
    }
    setState({ status: 'error', message: result.envelope.error, code: result.envelope.code });
  }, []);

  // Auto-run when the page is opened with ?request_id=… (the deep-link path).
  useEffect(() => {
    if (urlRequestId) {
      setInput(urlRequestId);
      void runLookup(urlRequestId);
    }
  }, [urlRequestId, runLookup]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const id = input.trim();
    setSearchParams(id ? { request_id: id } : {});
    void runLookup(id);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <div className="max-w-5xl mx-auto px-6 py-8">
        <div className="mb-6">
          <h1 className="text-xl font-semibold text-[#0a0a0a]">Investigate pricing call</h1>
          <p className="text-sm text-[#737373] mt-1">
            Paste a <span className="font-mono">request_id</span> to inspect the pricing pipeline — the request,
            entities loaded, market data resolved, the engine request and response, and the history write.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="flex gap-2 mb-8">
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            placeholder="request_id (e.g. 81327e7f…)"
            aria-label="request_id"
            className="flex-1 px-3 py-2 text-sm font-mono border border-[#e5e5e5] rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-[#8a6a2f]/30 focus:border-[#8a6a2f]"
          />
          <button
            type="submit"
            disabled={!input.trim()}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-[#171717] text-white hover:bg-[#404040] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Investigate
          </button>
        </form>

        {state.status === 'loading' && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-[#e5e5e5] border-t-[#8a6a2f] rounded-full animate-spin" />
            <span className="ml-3 text-sm text-[#737373]">Loading trace…</span>
          </div>
        )}

        {state.status === 'idle' && (
          <p className="text-sm text-[#a3a3a3] text-center py-12">
            Enter a request id above to load its trace.
          </p>
        )}

        {state.status === 'not_found' && (
          <div className="bg-white border border-amber-200 rounded-xl p-6">
            <p className="text-sm font-semibold text-amber-700">No trace for this id</p>
            <p className="text-sm mt-1 text-amber-800">
              There is no pricing trace for{' '}
              <span className="font-mono break-all">{state.requestId}</span> (it may not exist, or it may belong to
              another user). Check the id and try again.
            </p>
          </div>
        )}

        {state.status === 'error' && (
          <div className="bg-white border border-red-200 rounded-xl p-6">
            <p className="text-sm font-semibold text-red-600">Could not load trace</p>
            <p className="text-sm mt-1 text-red-500 break-words">{state.message}</p>
            <p className="mt-2 text-xs font-mono text-red-500">{state.code}</p>
          </div>
        )}

        {state.status === 'loaded' && (
          <div>
            <TraceHeaderBar trace={state.trace} />
            {state.trace.stages.length === 0 ? (
              <p className="text-sm text-[#a3a3a3] py-8 text-center">This trace has no stages.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {state.trace.stages.map((stage, i) => (
                  <StageCard key={`${stage.stage}-${i}`} stage={stage} index={i} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
