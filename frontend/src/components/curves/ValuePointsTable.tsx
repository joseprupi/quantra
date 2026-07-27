// Points table for "Interpolate given values" curve construction: one row
// per pillar with an inline value OR an MD quote reference. Includes
// add/delete/sort and the primary "Paste table…" workflow (reproduce a
// published curve from a pasted two-column block).
import { Fragment, useMemo, useState } from 'react';
import { ValueCurvePoint } from '../../lib/types';
import {
  ValueQuantity,
  makeValuePoint,
  parsePastedTable,
  parsePillarToken,
  pillarLabel,
  pointValue,
  quantitySpec,
  sortValuePoints,
} from '../../lib/valueCurves';
import { useMdQuoteOptions } from '../../hooks/useMdQuoteOptions';

export interface ValuePointsTableProps {
  quantity: ValueQuantity;
  points: ValueCurvePoint[];
  onChange: (points: ValueCurvePoint[]) => void;
  referenceDate: string;
  /** Preferred pillar entry style for NEW rows (existing rows keep theirs). */
  pillarStyle: 'tenor' | 'date';
  /** index -> message (client validation merged with mapped server 422s). */
  rowErrors: Map<number, string>;
}

const inputClass =
  'w-full px-2 py-1.5 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors';

export default function ValuePointsTable({
  quantity,
  points,
  onChange,
  referenceDate,
  pillarStyle,
  rowErrors,
}: ValuePointsTableProps) {
  const spec = quantitySpec(quantity);
  const { quotes, quotesError, asOfDate } = useMdQuoteOptions();
  const [showPaste, setShowPaste] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteErrors, setPasteErrors] = useState<string[]>([]);
  // Uncommitted tenor-pillar edit strings, keyed by row index.
  const [pillarDrafts, setPillarDrafts] = useState<Record<number, string>>({});

  // Row 0 in DF mode is the mandatory reference-date DF=1.0 pillar — pinned.
  const isPinned = (index: number): boolean => {
    if (quantity !== 'df' || index !== 0) return false;
    const p = points[0];
    return !!p && p.point.date === referenceDate && pointValue(p) === 1.0 && !p.point.quote_id;
  };

  const updatePoint = (index: number, patch: Record<string, unknown>, drop: string[] = []) => {
    const next = points.map((pt, i) => {
      if (i !== index) return pt;
      const inner: Record<string, unknown> = { ...pt.point, ...patch };
      for (const key of drop) delete inner[key];
      return { ...pt, point: inner } as ValueCurvePoint;
    });
    onChange(next);
  };

  const commitPillarDraft = (index: number, raw: string) => {
    setPillarDrafts(prev => {
      const next = { ...prev };
      delete next[index];
      return next;
    });
    const parsed = parsePillarToken(raw);
    if (!parsed) {
      // Keep the raw text visible via validation (pillar stays unset).
      if (raw.trim() === '') updatePoint(index, {}, ['date', 'tenor_number', 'tenor_time_unit']);
      return;
    }
    if (parsed.kind === 'date') {
      updatePoint(index, { date: parsed.iso }, ['tenor_number', 'tenor_time_unit']);
    } else {
      updatePoint(index, { tenor_number: parsed.n, tenor_time_unit: parsed.unit }, ['date']);
    }
  };

  const addRow = () => {
    // The interpolated curve is anchored at its FIRST pillar, so the first
    // row of an empty table always starts at the reference date.
    const pillar =
      points.length === 0 || pillarStyle === 'date'
        ? ({ kind: 'date', iso: referenceDate } as const)
        : ({ kind: 'tenor', n: 1, unit: 'Years' } as const);
    onChange([...points, makeValuePoint(quantity, pillar, {})]);
  };

  const deleteRow = (index: number) => {
    onChange(points.filter((_, i) => i !== index));
  };

  const sortRows = () => {
    if (quantity === 'df' && isPinned(0)) {
      onChange([points[0], ...sortValuePoints(points.slice(1), referenceDate)]);
    } else {
      onChange(sortValuePoints(points, referenceDate));
    }
  };

  const applyPaste = () => {
    const { points: parsed, errors } = parsePastedTable(pasteText, quantity, referenceDate);
    setPasteErrors(errors);
    if (parsed.length > 0) {
      onChange(parsed);
      if (errors.length === 0) {
        setShowPaste(false);
        setPasteText('');
      }
    }
  };

  const valueDisplay = (pt: ValueCurvePoint): string => {
    const v = pointValue(pt);
    if (v === undefined) return '';
    return spec.percent ? String(parseFloat((v * 100).toPrecision(12))) : String(v);
  };

  const quoteRows = useMemo(() => quotes, [quotes]);

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
          onClick={sortRows}
          className="px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors"
        >
          Sort by pillar
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
            One pillar + value per line (tab, comma or space separated). Pillars: tenors like{' '}
            <code>6M</code> / <code>10Y</code> or ISO dates like <code>2027-01-15</code>. Values:{' '}
            {spec.percent ? 'in percent (2.05 = 2.05%)' : 'raw discount factors (0.97)'}. Applying
            replaces the current table
            {quantity === 'df'
              ? ' (the mandatory 1.0 reference-date row is added automatically)'
              : ' (a reference-date anchor row is added automatically — the curve starts there)'}.
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

      {/* Table */}
      {points.length === 0 ? (
        <div className="text-center py-8 text-[#a3a3a3]">
          <p className="text-sm">No points yet</p>
          <p className="text-xs mt-1">Add rows or paste a published curve with “Paste table…”</p>
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-[#737373]">
              <th className="pb-2 font-medium w-40">Pillar</th>
              <th className="pb-2 font-medium w-36">
                {spec.percent ? 'Value (%)' : 'Discount factor'}
              </th>
              <th className="pb-2 font-medium">Source</th>
              <th className="pb-2 w-8" />
            </tr>
          </thead>
          <tbody>
            {points.map((pt, i) => {
              const pinned = isPinned(i);
              // '' = quote mode selected but no id picked yet (still quote mode).
              const usesQuote = pt.point.quote_id !== undefined;
              const error = rowErrors.get(i);
              const hasDatePillar = !!pt.point.date;
              return (
                <Fragment key={i}>
                  <tr className="border-t border-[#f0f0f0]" data-testid="value-point-row">
                    <td className="py-1.5 pr-2 align-top">
                      {pinned ? (
                        <div className="px-2 py-1.5 text-sm text-[#737373]">
                          {referenceDate}
                          <span className="ml-1 text-[10px] text-[#a3a3a3]">(reference)</span>
                        </div>
                      ) : hasDatePillar ? (
                        <input
                          type="date"
                          value={pt.point.date || ''}
                          onChange={e =>
                            updatePoint(i, { date: e.target.value }, ['tenor_number', 'tenor_time_unit'])
                          }
                          className={inputClass}
                          aria-label={`Pillar ${i + 1}`}
                        />
                      ) : (
                        <input
                          type="text"
                          value={pillarDrafts[i] ?? pillarLabel(pt)}
                          placeholder="6M / 10Y / 2027-01-15"
                          onChange={e => setPillarDrafts(prev => ({ ...prev, [i]: e.target.value }))}
                          onBlur={e => commitPillarDraft(i, e.target.value)}
                          onKeyDown={e => {
                            if (e.key === 'Enter') commitPillarDraft(i, (e.target as HTMLInputElement).value);
                          }}
                          className={inputClass}
                          aria-label={`Pillar ${i + 1}`}
                        />
                      )}
                    </td>
                    <td className="py-1.5 pr-2 align-top">
                      {pinned ? (
                        <div className="px-2 py-1.5 text-sm text-[#737373]">1.0</div>
                      ) : usesQuote ? (
                        <div className="px-2 py-1.5 text-xs text-[#a3a3a3]">from quote</div>
                      ) : (
                        <input
                          type="number"
                          step={spec.percent ? '0.001' : '0.0001'}
                          value={valueDisplay(pt)}
                          onChange={e => {
                            const raw = parseFloat(e.target.value);
                            if (Number.isNaN(raw)) {
                              updatePoint(i, {}, [spec.valueKey]);
                            } else {
                              updatePoint(i, { [spec.valueKey]: spec.percent ? raw / 100 : raw });
                            }
                          }}
                          className={inputClass}
                          aria-label={`Value ${i + 1}`}
                        />
                      )}
                    </td>
                    <td className="py-1.5 pr-2 align-top">
                      {pinned ? (
                        <div className="px-2 py-1.5 text-[10px] text-[#a3a3a3]">
                          pinned — the curve starts at 1.0 on the reference date
                        </div>
                      ) : (
                        <div className="flex gap-2 items-start">
                          <select
                            value={usesQuote ? 'quote' : 'inline'}
                            onChange={e => {
                              if (e.target.value === 'quote') {
                                updatePoint(i, { quote_id: '' }, [spec.valueKey]);
                              } else {
                                updatePoint(i, {}, ['quote_id']);
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
                              onChange={e => updatePoint(i, { quote_id: e.target.value })}
                              className={inputClass}
                              aria-label={`Quote ${i + 1}`}
                            >
                              <option value="">— Select quote —</option>
                              {quoteRows.map(q => (
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
                      )}
                    </td>
                    <td className="py-1.5 align-top text-right">
                      {!pinned && (
                        <button
                          onClick={() => deleteRow(i)}
                          className="p-1.5 text-[#a3a3a3] hover:text-red-500"
                          aria-label={`Delete row ${i + 1}`}
                        >
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      )}
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
            })}
          </tbody>
        </table>
      )}

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
