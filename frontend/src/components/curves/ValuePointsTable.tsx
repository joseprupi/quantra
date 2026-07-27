// Points table for "Interpolate given values" curve construction.
//
// Model: a PINNED reference-date header row (the curve anchor: DF fixed at
// 1.0, zero / fwd an editable short-end value) rendered ABOVE the editable
// rows. Each editable row has ONE free-text Maturity field accepting a tenor
// (1Y, 18M, 2W, 30D) or an ISO date (2033-01-15), parsed on blur, with the
// resolved date always shown next to it. Rows auto-sort by resolved date on
// add / blur (never mid-keystroke). Values are inline OR an MD quote
// reference. "Paste table…" normalizes a two-column block into this model.
import { Fragment, useState } from 'react';
import { ValueCurvePoint } from '../../lib/types';
import {
  ValueQuantity,
  parsePastedTable,
  parsePillarToken,
  pillarLabel,
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

interface MaturityDraft {
  text: string;
  invalid: boolean;
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
  // Uncommitted maturity edit strings, keyed by row index. Invalid drafts
  // stay visible with a per-row error until fixed.
  const [maturityDrafts, setMaturityDrafts] = useState<Record<number, MaturityDraft>>({});

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

  /** Sort rows by resolved date and remap in-flight drafts (row objects keep
   * identity through the sort, so drafts follow their rows). */
  const sortAndCommit = (next: ValueCurvePoint[], drafts: Record<number, MaturityDraft>) => {
    const sorted = sortValuePoints(next, referenceDate);
    const remapped: Record<number, MaturityDraft> = {};
    for (const [key, draft] of Object.entries(drafts)) {
      const row = next[Number(key)];
      const ni = sorted.indexOf(row);
      if (ni >= 0) remapped[ni] = draft;
    }
    setMaturityDrafts(remapped);
    onRowsChange(sorted);
  };

  const commitMaturityDraft = (index: number, raw: string) => {
    const text = raw.trim();
    const otherDrafts = { ...maturityDrafts };
    delete otherDrafts[index];
    if (text === '') {
      setMaturityDrafts(otherDrafts);
      updateRow(index, {}, ['date', 'tenor_number', 'tenor_time_unit']);
      return;
    }
    const parsed = parsePillarToken(text);
    if (!parsed) {
      // Keep the raw text visible with a per-row error; the maturity unsets.
      setMaturityDrafts({ ...otherDrafts, [index]: { text: raw, invalid: true } });
      updateRow(index, {}, ['date', 'tenor_number', 'tenor_time_unit']);
      return;
    }
    const next = rows.map((pt, i) => {
      if (i !== index) return pt;
      const inner: Record<string, unknown> = { ...pt.point };
      delete inner.date;
      delete inner.tenor_number;
      delete inner.tenor_time_unit;
      if (parsed.kind === 'date') {
        inner.date = parsed.iso;
      } else {
        inner.tenor_number = parsed.n;
        inner.tenor_time_unit = parsed.unit;
      }
      return { ...pt, point: inner } as ValueCurvePoint;
    });
    sortAndCommit(next, otherDrafts);
  };

  const addRow = () => {
    // New rows start empty (maturity typed by the user) and sit at the end
    // until their maturity resolves.
    onRowsChange([...rows, { point_type: spec.pointType, point: {} } as ValueCurvePoint]);
  };

  const deleteRow = (index: number) => {
    const remaining: Record<number, MaturityDraft> = {};
    for (const [key, draft] of Object.entries(maturityDrafts)) {
      const i = Number(key);
      if (i === index) continue;
      remaining[i > index ? i - 1 : i] = draft;
    }
    setMaturityDrafts(remaining);
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
      setMaturityDrafts({});
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
            <th className="pb-2 font-medium w-44">Maturity</th>
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
                Reference date · {referenceDate}
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
                  placeholder="short end"
                  onChange={e => {
                    const raw = parseFloat(e.target.value);
                    onAnchorValueChange(Number.isNaN(raw) ? undefined : raw / 100);
                  }}
                  className={inputClass}
                  aria-label="Reference date value"
                />
              )}
            </td>
            <td colSpan={2} className="py-1.5 pr-2 align-middle">
              <span className="text-[10px] text-[#a3a3a3]">
                {quantity === 'df'
                  ? 'Fixed: a discount curve starts at 1.0 on its reference date.'
                  : 'The curve value at its start date (the short end).'}
              </span>
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
              const draft = maturityDrafts[i];
              const resolved = resolvedPillarIso(pt, referenceDate);
              const error =
                draft?.invalid
                  ? `Unrecognized maturity "${draft.text.trim()}". Use a tenor like 1Y, 18M, 2W, 30D or a date like 2033-01-15.`
                  : rowErrors.get(i + 1);
              return (
                <Fragment key={i}>
                  <tr className="border-t border-[#f0f0f0]" data-testid="value-point-row">
                    <td className="py-1.5 pr-2 align-top">
                      <input
                        type="text"
                        value={draft?.text ?? pillarLabel(pt)}
                        placeholder="1Y / 18M / 2033-01-15"
                        onChange={e =>
                          setMaturityDrafts(prev => ({
                            ...prev,
                            [i]: { text: e.target.value, invalid: false },
                          }))
                        }
                        onBlur={e => commitMaturityDraft(i, e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') {
                            commitMaturityDraft(i, (e.target as HTMLInputElement).value);
                          }
                        }}
                        className={inputClass}
                        aria-label={`Maturity ${i + 1}`}
                      />
                      {resolved && !draft?.invalid && (
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
