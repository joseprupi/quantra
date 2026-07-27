// Points table for "Interpolate given values" curve construction.
//
// Model: a PINNED reference-date header row (the curve anchor: DF fixed at
// 1.0, zero / fwd an editable start value) rendered ABOVE the editable rows.
// Each editable row picks its maturity via a small per-row [Tenor | Date]
// toggle: Tenor mode is a number input + a TimeUnit select (the same control
// pattern as the instrument editor's Tenor fields); Date mode is a date
// input. New rows default to Tenor mode. Tenor rows show their resolved date
// as a grey hint. Rows auto-sort by resolved date on commit (blur / Enter /
// unit change), never mid-keystroke. Values are inline OR an MD quote
// reference. "Paste table…" normalizes a two-column block into this model,
// setting each created row's mode from the parsed line (tenor vs date).
import { Fragment, useState } from 'react';
import { TIME_UNITS, TimeUnit, ValueCurvePoint } from '../../lib/types';
import {
  ValueQuantity,
  parsePastedTable,
  pointValue,
  quantitySpec,
  resolvedPillarIso,
  sortValuePoints,
} from '../../lib/valueCurves';
import { useMdQuoteOptions } from '../../hooks/useMdQuoteOptions';

export interface ValuePointsTableProps {
  quantity: ValueQuantity;
  /** Editable rows only. NEVER contains the pinned reference-date anchor. */
  rows: ValueCurvePoint[];
  onRowsChange: (rows: ValueCurvePoint[]) => void;
  /** Pinned-row value (decimal) for zero / fwd; ignored for DF (fixed 1.0). */
  anchorValue: number | undefined;
  onAnchorValueChange: (value: number | undefined) => void;
  referenceDate: string;
  /** FULL wire-point index -> message: 0 = the pinned anchor, i >= 1 =
   * rows[i - 1], -1 = curve-level (client validation merged with mapped
   * server 422s). */
  rowErrors: Map<number, string>;
}

const inputClass =
  'w-full px-2 py-1.5 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors';

type MaturityMode = 'tenor' | 'date';

/** UI-only per-row control state: which maturity editor a row shows while it
 * has no committed maturity, and the last unit picked before a number is
 * typed. Rows WITH a maturity derive both from the point itself. */
interface RowCtl {
  mode: MaturityMode;
  unit: TimeUnit;
}

export default function ValuePointsTable({
  quantity,
  rows,
  onRowsChange,
  anchorValue,
  onAnchorValueChange,
  referenceDate,
  rowErrors,
}: ValuePointsTableProps) {
  const spec = quantitySpec(quantity);
  const { quotes, quotesError, asOfDate } = useMdQuoteOptions();
  const [showPaste, setShowPaste] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteErrors, setPasteErrors] = useState<string[]>([]);
  const [pasteNote, setPasteNote] = useState<string | null>(null);
  const [rowCtls, setRowCtls] = useState<Record<number, RowCtl>>({});

  const setRowCtl = (index: number, ctl: RowCtl) =>
    setRowCtls(prev => ({ ...prev, [index]: ctl }));

  const updateRow = (index: number, patch: Record<string, unknown>, drop: string[] = []) => {
    const next = rows.map((pt, i) => {
      if (i !== index) return pt;
      const inner: Record<string, unknown> = { ...pt.point, ...patch };
      for (const key of drop) delete inner[key];
      return { ...pt, point: inner } as ValueCurvePoint;
    });
    onRowsChange(next);
    return next;
  };

  /** Sort rows by resolved date and remap per-row control state (row objects
   * keep identity through the sort, so the state follows its row). */
  const sortAndCommit = (next: ValueCurvePoint[]) => {
    const sorted = sortValuePoints(next, referenceDate);
    setRowCtls(prev => {
      const remapped: Record<number, RowCtl> = {};
      for (const [key, ctl] of Object.entries(prev)) {
        const row = next[Number(key)];
        const ni = sorted.indexOf(row);
        if (ni >= 0) remapped[ni] = ctl;
      }
      return remapped;
    });
    onRowsChange(sorted);
  };

  /** Switch a row between Tenor and Date mode. A committed tenor converts to
   * its resolved date; a committed date cannot become a tenor and clears. */
  const setRowMode = (index: number, mode: MaturityMode, current: MaturityMode, unit: TimeUnit) => {
    if (mode === current) return;
    setRowCtl(index, { mode, unit });
    const pt = rows[index];
    if (mode === 'date') {
      if (pt.point.tenor_number !== undefined && pt.point.tenor_number !== null) {
        const resolved = resolvedPillarIso(pt, referenceDate);
        updateRow(index, resolved ? { date: resolved } : {}, ['tenor_number', 'tenor_time_unit']);
      }
    } else if (pt.point.date !== undefined) {
      updateRow(index, {}, ['date']);
    }
  };

  const addRow = () => {
    // New rows start empty (Tenor mode by default) and sit at the end until
    // their maturity is committed.
    onRowsChange([...rows, { point_type: spec.pointType, point: {} } as ValueCurvePoint]);
  };

  const deleteRow = (index: number) => {
    setRowCtls(prev => {
      const remaining: Record<number, RowCtl> = {};
      for (const [key, ctl] of Object.entries(prev)) {
        const i = Number(key);
        if (i === index) continue;
        remaining[i > index ? i - 1 : i] = ctl;
      }
      return remaining;
    });
    onRowsChange(rows.filter((_, i) => i !== index));
  };

  const applyPaste = () => {
    const { rows: parsed, anchorValue: pastedAnchor, anchorNote, errors } = parsePastedTable(
      pasteText,
      quantity,
      referenceDate,
    );
    setPasteErrors(errors);
    setPasteNote(anchorNote ?? null);
    if (parsed.length > 0 || pastedAnchor !== undefined) {
      setRowCtls({});
      onRowsChange(parsed);
      if (pastedAnchor !== undefined && quantity !== 'df') {
        onAnchorValueChange(pastedAnchor);
      }
      if (errors.length === 0) {
        setShowPaste(false);
        setPasteText('');
      }
    }
  };

  const valueDisplay = (v: number | undefined): string => {
    if (v === undefined) return '';
    return spec.percent ? String(parseFloat((v * 100).toPrecision(12))) : String(v);
  };

  const anchorError = rowErrors.get(0);

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-2 mb-3">
        <button
          onClick={addRow}
          className="px-3 py-1.5 text-xs font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#a67c3a] transition-colors"
        >
          + Add row
        </button>
        <button
          onClick={() => setShowPaste(s => !s)}
          className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors"
        >
          Paste table…
        </button>
      </div>

      {/* Paste panel */}
      {showPaste && (
        <div className="mb-4 p-3 bg-[#f5f5f5] border border-[#e5e5e5] rounded-lg">
          <textarea
            value={pasteText}
            onChange={e => setPasteText(e.target.value)}
            placeholder={
              spec.percent
                ? '6M\t2.05\n1Y\t2.20\n10Y\t3.10'
                : '1Y\t0.97\n5Y\t0.85\n10Y\t0.72'
            }
            rows={6}
            className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm font-mono text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f]"
            data-testid="paste-table-input"
          />
          <p className="text-[11px] text-[#737373] mt-1">
            One maturity + value per line (tab, comma or space separated). Maturities: tenors like{' '}
            <code>6M</code> / <code>10Y</code> or ISO dates like <code>2027-01-15</code>. Values:{' '}
            {spec.percent ? 'in percent (2.05 = 2.05%)' : 'raw discount factors (0.97)'}. Applying
            replaces the rows below the pinned reference-date row.
            {quantity === 'df'
              ? ' A line at the reference date is ignored: the discount factor there is fixed at 1.0.'
              : ' A line at the reference date fills the pinned row.'}
          </p>
          {pasteErrors.length > 0 && (
            <ul className="mt-2 text-xs text-red-600 list-disc pl-4">
              {pasteErrors.map((err, i) => (
                <li key={i}>{err}</li>
              ))}
            </ul>
          )}
          <div className="flex gap-2 mt-2">
            <button
              onClick={applyPaste}
              className="px-3 py-1.5 text-xs font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626]"
            >
              Apply
            </button>
            <button
              onClick={() => {
                setShowPaste(false);
                setPasteErrors([]);
              }}
              className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {pasteNote && (
        <p className="text-xs text-amber-600 mb-2" data-testid="paste-note">
          {pasteNote}
        </p>
      )}

      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-[#737373]">
            <th className="pb-2 font-medium w-52">Maturity</th>
            <th className="pb-2 font-medium w-36">
              {spec.percent ? 'Value (%)' : 'Discount factor'}
            </th>
            <th className="pb-2 font-medium">Source</th>
            <th className="pb-2 w-8" />
          </tr>
        </thead>
        <tbody>
          {/* Pinned reference-date anchor: always the curve's first point on
              the wire, never part of the editable row list. */}
          <tr
            className="bg-[#f5f0e6] border-t border-[#e5d9c3]"
            data-testid="value-anchor-row"
          >
            <td className="py-2 pr-2 pl-2 align-middle">
              <span className="text-xs font-medium text-[#8a6a2f]">
                Start · {referenceDate}
              </span>
            </td>
            <td className="py-1.5 pr-2 align-middle">
              {quantity === 'df' ? (
                <div className="px-2 py-1.5 text-sm font-medium text-[#8a6a2f]">1.0000</div>
              ) : (
                <input
                  type="number"
                  step="0.001"
                  value={valueDisplay(anchorValue)}
                  placeholder="start value"
                  onChange={e => {
                    const raw = parseFloat(e.target.value);
                    onAnchorValueChange(Number.isNaN(raw) ? undefined : raw / 100);
                  }}
                  className={inputClass}
                  aria-label="Start value at the reference date"
                />
              )}
            </td>
            <td colSpan={2} className="py-1.5 pr-2 align-middle">
              {quantity === 'df' && (
                <span className="text-[10px] text-[#a3a3a3]">Always 1.0</span>
              )}
            </td>
          </tr>
          {anchorError && (
            <tr className="bg-[#f5f0e6]">
              <td colSpan={4} className="pb-1.5 px-2">
                <p className="text-xs text-red-600" data-testid="value-point-row-error">
                  {anchorError}
                </p>
              </td>
            </tr>
          )}

          {rows.length === 0 ? (
            <tr>
              <td colSpan={4} className="py-8 text-center text-[#a3a3a3]">
                <p className="text-sm">No points yet</p>
                <p className="text-xs mt-1">Add rows or paste a published curve with “Paste table…”</p>
              </td>
            </tr>
          ) : (
            rows.map((pt, i) => {
              // '' = quote mode selected but no id picked yet (still quote mode).
              const usesQuote = pt.point.quote_id !== undefined;
              const p = pt.point;
              const ctl = rowCtls[i];
              const hasTenor = p.tenor_number !== undefined && p.tenor_number !== null;
              const mode: MaturityMode =
                p.date !== undefined ? 'date' : hasTenor ? 'tenor' : ctl?.mode ?? 'tenor';
              const unit: TimeUnit =
                (hasTenor ? (p.tenor_time_unit as TimeUnit | undefined) : undefined) ??
                ctl?.unit ??
                'Years';
              const resolved = resolvedPillarIso(pt, referenceDate);
              const error = rowErrors.get(i + 1);
              return (
                <Fragment key={i}>
                  <tr className="border-t border-[#f0f0f0]" data-testid="value-point-row">
                    <td className="py-1.5 pr-2 align-top">
                      <div
                        className="inline-flex rounded-md bg-[#f5f5f5] p-0.5 mb-1"
                        role="group"
                        aria-label={`Maturity mode ${i + 1}`}
                      >
                        {(['tenor', 'date'] as const).map(m => (
                          <button
                            key={m}
                            type="button"
                            aria-pressed={mode === m}
                            onClick={() => setRowMode(i, m, mode, unit)}
                            className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${
                              mode === m
                                ? 'bg-[#8a6a2f] text-white'
                                : 'text-[#525252] hover:bg-[#e5e5e5]'
                            }`}
                          >
                            {m === 'tenor' ? 'Tenor' : 'Date'}
                          </button>
                        ))}
                      </div>
                      {mode === 'tenor' ? (
                        <div className="flex gap-1">
                          <input
                            type="number"
                            min={1}
                            value={hasTenor ? p.tenor_number : ''}
                            onChange={e => {
                              const raw = parseInt(e.target.value, 10);
                              if (!Number.isFinite(raw) || raw <= 0) {
                                setRowCtl(i, { mode: 'tenor', unit });
                                updateRow(i, {}, ['tenor_number', 'tenor_time_unit']);
                              } else {
                                updateRow(
                                  i,
                                  { tenor_number: raw, tenor_time_unit: unit },
                                  ['date'],
                                );
                              }
                            }}
                            onBlur={() => sortAndCommit(rows)}
                            onKeyDown={e => {
                              if (e.key === 'Enter') sortAndCommit(rows);
                            }}
                            className={`${inputClass} w-16`}
                            aria-label={`Tenor number ${i + 1}`}
                          />
                          <select
                            value={unit}
                            onChange={e => {
                              const nextUnit = e.target.value as TimeUnit;
                              setRowCtl(i, { mode: 'tenor', unit: nextUnit });
                              if (hasTenor) {
                                sortAndCommit(updateRow(i, { tenor_time_unit: nextUnit }));
                              }
                            }}
                            className={inputClass}
                            aria-label={`Tenor unit ${i + 1}`}
                          >
                            {TIME_UNITS.map(u => (
                              <option key={u}>{u}</option>
                            ))}
                          </select>
                        </div>
                      ) : (
                        <input
                          type="date"
                          value={p.date ?? ''}
                          onChange={e => {
                            const value = e.target.value;
                            if (!value) {
                              setRowCtl(i, { mode: 'date', unit });
                              updateRow(i, {}, ['date']);
                            } else {
                              updateRow(i, { date: value }, ['tenor_number', 'tenor_time_unit']);
                            }
                          }}
                          onBlur={() => sortAndCommit(rows)}
                          onKeyDown={e => {
                            if (e.key === 'Enter') sortAndCommit(rows);
                          }}
                          className={inputClass}
                          aria-label={`Maturity date ${i + 1}`}
                        />
                      )}
                      {mode === 'tenor' && resolved && (
                        <p className="text-[10px] text-[#a3a3a3] mt-0.5 px-2" data-testid="resolved-date">
                          → {resolved}
                        </p>
                      )}
                    </td>
                    <td className="py-1.5 pr-2 align-top">
                      {usesQuote ? (
                        <div className="px-2 py-1.5 text-xs text-[#a3a3a3]">from quote</div>
                      ) : (
                        <input
                          type="number"
                          step={spec.percent ? '0.001' : '0.0001'}
                          value={valueDisplay(pointValue(pt))}
                          onChange={e => {
                            const raw = parseFloat(e.target.value);
                            if (Number.isNaN(raw)) {
                              updateRow(i, {}, [spec.valueKey]);
                            } else {
                              updateRow(i, { [spec.valueKey]: spec.percent ? raw / 100 : raw });
                            }
                          }}
                          className={inputClass}
                          aria-label={`Value ${i + 1}`}
                        />
                      )}
                    </td>
                    <td className="py-1.5 pr-2 align-top">
                      <div className="flex gap-2 items-start">
                        <select
                          value={usesQuote ? 'quote' : 'inline'}
                          onChange={e => {
                            if (e.target.value === 'quote') {
                              updateRow(i, { quote_id: '' }, [spec.valueKey]);
                            } else {
                              updateRow(i, {}, ['quote_id']);
                            }
                          }}
                          className={`${inputClass} w-24`}
                          aria-label={`Source ${i + 1}`}
                        >
                          <option value="inline">Inline</option>
                          <option value="quote">Quote</option>
                        </select>
                        {usesQuote && (
                          <select
                            value={pt.point.quote_id || ''}
                            onChange={e => updateRow(i, { quote_id: e.target.value })}
                            className={inputClass}
                            aria-label={`Quote ${i + 1}`}
                          >
                            <option value="">— Select quote —</option>
                            {quotes.map(q => (
                              <option key={q.id} value={q.id}>
                                {q.id}
                                {q.value !== null
                                  ? ` — ${spec.percent ? `${(q.value * 100).toFixed(3)}%` : q.value.toFixed(6)}`
                                  : ' — no value'}
                                {q.resolvedAsOf ? ` @${q.resolvedAsOf}` : ''}
                              </option>
                            ))}
                          </select>
                        )}
                      </div>
                    </td>
                    <td className="py-1.5 align-top text-right">
                      <button
                        onClick={() => deleteRow(i)}
                        className="p-1.5 text-[#a3a3a3] hover:text-red-500"
                        aria-label={`Delete row ${i + 1}`}
                      >
                        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                  {error && (
                    <tr>
                      <td colSpan={4} className="pb-1.5">
                        <p className="text-xs text-red-600" data-testid="value-point-row-error">
                          {error}
                        </p>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>

      <p className="text-[11px] text-[#a3a3a3] mt-2">
        Quote values from the MD server, resolved at the global As-Of ({asOfDate}). The id is
        stored on the curve; pricing re-resolves it server-side.
      </p>
      {quotesError && <p className="text-xs text-amber-600 mt-1">{quotesError}</p>}
      {rowErrors.get(-1) && (
        <p className="text-xs text-red-600 mt-2" data-testid="value-points-error">
          {rowErrors.get(-1)}
        </p>
      )}
    </div>
  );
}
