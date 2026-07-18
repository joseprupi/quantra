import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  getVersion,
  listVersions,
  restoreEntityVersion,
  type EntityVersionDetail,
  type EntityVersionSummary,
} from '../../lib/api/crud';
import {
  diffSnapshots,
  flattenPayload,
  restoreBodyFromSnapshot,
  type DiffRow,
} from '../../lib/versionDiff';

/**
 * HistoryPanel — the audit-trail UI over the entity versioning API.
 *
 * Given an entity's API base path + id it renders a collapsed "History"
 * section that, when opened, shows the full amendment timeline (newest
 * first): version badge, change-type chip, actor, relative + absolute time,
 * the optional X-Change-Reason and the grouping request id.
 *
 * Click one version → read-only snapshot view with a "Restore this version"
 * action (issues the entity PATCH with the snapshot-derived editable body and
 * `X-Change-Reason: restored to v{n}`, then refreshes the timeline). Select a
 * second version → side-by-side field diff (client-side; dotted-path leaves,
 * only changed / added / removed keys).
 */

export interface HistoryPanelProps {
  /** Entity API base path, e.g. `/v1/swaps/ir` or `/v1/curves`. */
  entityPath: string;
  /** Server UUID of the saved entity. */
  entityId: string;
  /**
   * Client-owned keys forming the restore PATCH body (the same fields the
   * save flow sends). Defaults to the seven product entities' shape.
   */
  restoreKeys?: readonly string[];
  /** Called after a successful restore (timeline already refreshed). */
  onRestored?: (versionNo: number) => void;
  /** Change to force a timeline refetch (e.g. pass the save status). */
  refreshKey?: unknown;
  /** Open the section on first render (default collapsed). */
  defaultOpen?: boolean;
}

// Distinct muted chip styles per change type.
const CHIP_STYLES: Record<string, string> = {
  create: 'bg-[#f0fdf4] text-[#166534] border-[#bbf7d0]',
  amend: 'bg-[#eff6ff] text-[#1e40af] border-[#bfdbfe]',
  delete: 'bg-[#fef2f2] text-[#991b1b] border-[#fecaca]',
  restore: 'bg-[#fffbeb] text-[#92400e] border-[#fde68a]',
};

const CHIP_FALLBACK = 'bg-[#f5f5f5] text-[#525252] border-[#e5e5e5]';

/** "3m ago"-style relative time; falls back to the raw string on bad input. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((now.getTime() - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

const TRUNCATE_AT = 80;

/** A value cell that truncates long content with an inline expand toggle. */
function ValueCell({ value, tone }: { value: string; tone?: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = value.length > TRUNCATE_AT;
  const shown = expanded || !long ? value : `${value.slice(0, TRUNCATE_AT)}…`;
  return (
    <span className={`font-mono text-xs break-all ${tone ?? 'text-[#0a0a0a]'}`}>
      {shown}
      {long && (
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          className="ml-1 text-[10px] text-[#2563eb] hover:underline align-baseline"
        >
          {expanded ? 'collapse' : 'expand'}
        </button>
      )}
    </span>
  );
}

function CopyRequestId({ requestId }: { requestId: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    // The chip lives inside the clickable timeline row — copying must not
    // toggle the row's selection.
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(requestId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (non-secure context) — silently keep the text.
    }
  };
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10px] text-[#a3a3a3]">
      {requestId.slice(0, 8)}…
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy request id ${requestId}`}
        title={requestId}
        className="text-[#a3a3a3] hover:text-[#0a0a0a]"
      >
        {copied ? '✓' : '⧉'}
      </button>
    </span>
  );
}

type LoadState = 'idle' | 'loading' | 'ready' | 'error';

export default function HistoryPanel({
  entityPath,
  entityId,
  restoreKeys = ['name', 'request'],
  onRestored,
  refreshKey,
  defaultOpen = false,
}: HistoryPanelProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [state, setState] = useState<LoadState>('idle');
  const [versions, setVersions] = useState<EntityVersionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Selection: version numbers in click order (max 2 kept).
  const [selected, setSelected] = useState<number[]>([]);
  const [details, setDetails] = useState<Record<number, EntityVersionDetail>>({});
  const [detailError, setDetailError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const loadTimeline = useCallback(async () => {
    setState('loading');
    setError(null);
    const res = await listVersions(entityPath, entityId);
    if (!res.ok) {
      setState('error');
      setError(`${res.envelope.error} [${res.envelope.code}]`);
      return;
    }
    setVersions(res.data.items ?? []);
    setState('ready');
  }, [entityPath, entityId]);

  // (Re)load whenever open with a fresh entity or refreshKey. The details
  // cache is keyed by version_no; version rows are immutable (append-only
  // audit trail) so the cache survives refreshes safely.
  useEffect(() => {
    if (!open || !entityId) return;
    void loadTimeline();
  }, [open, entityId, refreshKey, loadTimeline]);

  // Reset selection when the entity itself changes.
  useEffect(() => {
    setSelected([]);
    setDetails({});
    setNotice(null);
  }, [entityPath, entityId]);

  const toggleSelect = (versionNo: number) => {
    setNotice(null);
    setSelected(prev => {
      if (prev.includes(versionNo)) return prev.filter(v => v !== versionNo);
      const next = [...prev, versionNo];
      return next.length > 2 ? next.slice(next.length - 2) : next;
    });
  };

  // Fetch snapshot details for selected versions not yet cached.
  useEffect(() => {
    const missing = selected.filter(v => !(v in details));
    if (missing.length === 0) return;
    let cancelled = false;
    void (async () => {
      setDetailError(null);
      for (const versionNo of missing) {
        const res = await getVersion(entityPath, entityId, versionNo);
        if (cancelled) return;
        if (!res.ok) {
          setDetailError(`${res.envelope.error} [${res.envelope.code}]`);
          return;
        }
        setDetails(prev => ({ ...prev, [versionNo]: res.data }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, details, entityPath, entityId]);

  const handleRestore = async (versionNo: number) => {
    const detail = details[versionNo];
    if (!detail) return;
    setRestoring(true);
    setNotice(null);
    const body = restoreBodyFromSnapshot(detail.payload, restoreKeys);
    const res = await restoreEntityVersion(entityPath, entityId, body, versionNo);
    setRestoring(false);
    if (!res.ok) {
      setNotice(`Restore failed: ${res.envelope.error} [${res.envelope.code}]`);
      return;
    }
    setNotice(`Restored to v${versionNo}.`);
    setSelected([]);
    await loadTimeline();
    onRestored?.(versionNo);
  };

  // Diff pair: always older → newer regardless of click order.
  const diffPair = useMemo(() => {
    if (selected.length !== 2) return null;
    const [a, b] = [...selected].sort((x, y) => x - y);
    const older = details[a];
    const newer = details[b];
    if (!older || !newer) return null;
    return { a, b, rows: diffSnapshots(older.payload, newer.payload) };
  }, [selected, details]);

  const snapshotVersion = selected.length === 1 ? selected[0] : null;
  const snapshotDetail = snapshotVersion != null ? details[snapshotVersion] : undefined;
  const latestVersionNo = versions.length > 0 ? versions[0].version_no : null;

  return (
    <div className="bg-white border border-[#e5e5e5] rounded-xl" data-testid="history-panel">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center justify-between px-5 py-4 text-left"
        aria-expanded={open}
      >
        <span className="text-sm font-semibold text-[#0a0a0a]">History</span>
        <span className="text-xs text-[#a3a3a3]">{open ? 'Hide' : 'Show'}</span>
      </button>

      {open && (
        <div className="px-5 pb-5 space-y-4">
          {state === 'loading' && versions.length === 0 && (
            <p className="text-xs text-[#a3a3a3]">Loading history…</p>
          )}
          {state === 'error' && (
            <p className="text-xs text-[#991b1b]" data-testid="history-error">
              Could not load history: {error}
            </p>
          )}
          {state === 'ready' && versions.length === 0 && (
            <p className="text-xs text-[#a3a3a3]">No recorded versions yet.</p>
          )}

          {versions.length > 0 && (
            <>
              <p className="text-[11px] text-[#a3a3a3]">
                Click a version to inspect its snapshot; select two to compare.
              </p>
              <ol className="divide-y divide-[#f0f0f0]" data-testid="history-timeline">
                {versions.map(v => {
                  const isSelected = selected.includes(v.version_no);
                  return (
                    <li key={v.version_no}>
                      {/* A div-with-button-role (not <button>): the row hosts
                          the nested copy-request-id button, and <button> may
                          not contain another button. */}
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => toggleSelect(v.version_no)}
                        onKeyDown={e => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            toggleSelect(v.version_no);
                          }
                        }}
                        data-testid="history-row"
                        data-version={v.version_no}
                        aria-pressed={isSelected}
                        className={`w-full cursor-pointer text-left py-2.5 px-2 rounded-lg flex flex-wrap items-baseline gap-x-3 gap-y-1 ${
                          isSelected ? 'bg-[#eff6ff]' : 'hover:bg-[#fafafa]'
                        }`}
                      >
                        <span className="font-mono text-xs font-semibold text-[#0a0a0a]">
                          v{v.version_no}
                        </span>
                        <span
                          data-testid="history-chip"
                          className={`text-[10px] px-1.5 py-0.5 rounded border ${
                            CHIP_STYLES[v.change_type] ?? CHIP_FALLBACK
                          }`}
                        >
                          {v.change_type}
                        </span>
                        <span className="text-xs text-[#525252]" data-testid="history-actor">
                          {v.changed_by_email || v.changed_by_uid}
                        </span>
                        <span
                          className="text-[11px] text-[#a3a3a3]"
                          title={v.changed_at}
                        >
                          {relativeTime(v.changed_at)} · {v.changed_at}
                        </span>
                        {v.change_reason && (
                          <span
                            className="text-[11px] italic text-[#525252]"
                            data-testid="history-reason"
                          >
                            “{v.change_reason}”
                          </span>
                        )}
                        {v.request_id && <CopyRequestId requestId={v.request_id} />}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </>
          )}

          {detailError && (
            <p className="text-xs text-[#991b1b]">Could not load snapshot: {detailError}</p>
          )}
          {notice && (
            <p className="text-xs text-[#0a0a0a]" data-testid="history-notice">
              {notice}
            </p>
          )}

          {/* Read-only snapshot of ONE selected version */}
          {snapshotVersion != null && snapshotDetail && (
            <div
              className="border border-[#e5e5e5] rounded-lg p-4 space-y-3"
              data-testid="history-snapshot"
            >
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-xs font-semibold text-[#0a0a0a]">
                  Snapshot — v{snapshotVersion}
                  {snapshotVersion === latestVersionNo && (
                    <span className="ml-2 font-normal text-[#a3a3a3]">(current)</span>
                  )}
                </h3>
                <button
                  type="button"
                  onClick={() => void handleRestore(snapshotVersion)}
                  disabled={restoring}
                  data-testid="history-restore"
                  className="text-xs px-3 py-1.5 rounded-lg border border-[#d4d4d4] text-[#0a0a0a] hover:bg-[#f5f5f5] disabled:opacity-50"
                >
                  {restoring ? 'Restoring…' : 'Restore this version'}
                </button>
              </div>
              <dl className="space-y-1">
                {Object.entries(flattenPayload(snapshotDetail.payload)).map(([path, value]) => (
                  <div key={path} className="flex gap-3 items-baseline">
                    <dt className="font-mono text-[11px] text-[#525252] shrink-0">{path}</dt>
                    <dd className="min-w-0">
                      <ValueCell value={value} />
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          )}

          {/* Side-by-side field diff of TWO selected versions */}
          {selected.length === 2 && (
            <div
              className="border border-[#e5e5e5] rounded-lg p-4 space-y-3"
              data-testid="history-diff"
            >
              {diffPair ? (
                <>
                  <h3 className="text-xs font-semibold text-[#0a0a0a]">
                    Changes v{diffPair.a} → v{diffPair.b}
                  </h3>
                  {diffPair.rows.length === 0 ? (
                    <p className="text-xs text-[#a3a3a3]">No field changes between the two versions.</p>
                  ) : (
                    <div className="space-y-1.5">
                      {diffPair.rows.map(row => (
                        <DiffRowView key={`${row.kind}:${row.path}`} row={row} />
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <p className="text-xs text-[#a3a3a3]">Loading snapshots…</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DiffRowView({ row }: { row: DiffRow }) {
  const kindTone =
    row.kind === 'added'
      ? 'text-[#166534]'
      : row.kind === 'removed'
        ? 'text-[#991b1b]'
        : 'text-[#92400e]';
  return (
    <div
      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5"
      data-testid="history-diff-row"
      data-kind={row.kind}
      data-path={row.path}
    >
      <span className={`font-mono text-[11px] font-semibold ${kindTone} bg-[#fafafa] px-1 rounded`}>
        {row.path}
      </span>
      <span className={`text-[10px] uppercase tracking-wide ${kindTone}`}>{row.kind}</span>
      {row.kind !== 'added' && (
        <ValueCell value={row.oldValue ?? ''} tone="text-[#991b1b] line-through decoration-[#fca5a5]" />
      )}
      {row.kind === 'changed' && <span className="text-[10px] text-[#a3a3a3]">→</span>}
      {row.kind !== 'removed' && <ValueCell value={row.newValue ?? ''} tone="text-[#166534]" />}
    </div>
  );
}
