import { CSSProperties, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { isQuoteCell, type Matrix2D, type MatrixCell } from '../../lib/types/matrix2d';

export type { Matrix2D, MatrixCell };
export { isQuoteCell };

// ---------------------------------------------------------------------------
// Pure helpers (exported for unit tests)
// ---------------------------------------------------------------------------

/**
 * Parse a clipboard string (TSV or CSV from Excel/Sheets) into a 2D array of
 * MatrixCells. Tokens that parse as finite numbers become number cells; all
 * other tokens (including empty strings) become NaN literals (rendered empty).
 *
 * Quote cells are intentionally never produced by paste — quote-cell mode is
 * settable only via the UI toggle, so paste behaviour stays unsurprising.
 */
export function parsePastedMatrix(text: string): MatrixCell[][] {
  if (typeof text !== 'string' || text.length === 0) return [];
  const trimmed = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n+$/, '');
  if (trimmed.length === 0) return [];
  const lines = trimmed.split('\n');
  // Excel/Sheets default copy is TSV. Treat any tab in the first line as a
  // signal to use TSV; otherwise fall back to CSV. This avoids ambiguity when
  // numeric content happens to contain commas (e.g. European thousands).
  const firstLine = lines[0] || '';
  const sep = firstLine.includes('\t') ? '\t' : ',';
  return lines.map(line => line.split(sep).map(tok => {
    const t = tok.trim();
    if (t.length === 0) return NaN as MatrixCell;
    const n = Number(t);
    if (!Number.isFinite(n)) return NaN as MatrixCell;
    return n as MatrixCell;
  }));
}

/**
 * Overlay `paste` onto `matrix` starting at (anchorRow, anchorCol). Cells that
 * fall outside the existing matrix dimensions are dropped (matrix size is
 * parent-owned). Cells outside the paste rectangle are left untouched.
 */
export function applyPasteAt(
  matrix: Matrix2D,
  anchorRow: number,
  anchorCol: number,
  paste: MatrixCell[][],
): Matrix2D {
  if (paste.length === 0) return matrix;
  const nRows = matrix.length;
  const nCols = matrix[0]?.length ?? 0;
  if (nRows === 0 || nCols === 0) return matrix;
  const next = matrix.map(row => row.slice());
  for (let pi = 0; pi < paste.length; pi += 1) {
    const r = anchorRow + pi;
    if (r < 0 || r >= nRows) continue;
    const pasteRow = paste[pi] || [];
    for (let pj = 0; pj < pasteRow.length; pj += 1) {
      const c = anchorCol + pj;
      if (c < 0 || c >= nCols) continue;
      next[r][c] = pasteRow[pj];
    }
  }
  return next;
}

/**
 * Resize matrix to (nRows × nCols), preserving overlapping cells and filling
 * new cells with NaN literals (rendered as empty inputs). Shrinking drops
 * cells beyond the new bounds.
 */
export function resizeMatrixPreserving(
  matrix: Matrix2D | undefined,
  nRows: number,
  nCols: number,
): Matrix2D {
  const out: Matrix2D = [];
  for (let i = 0; i < nRows; i += 1) {
    const row: MatrixCell[] = [];
    for (let j = 0; j < nCols; j += 1) {
      const existing = matrix?.[i]?.[j];
      row.push(existing === undefined ? (NaN as MatrixCell) : existing);
    }
    out.push(row);
  }
  return out;
}

export function fillRow(matrix: Matrix2D, row: number, value: MatrixCell): Matrix2D {
  if (row < 0 || row >= matrix.length) return matrix;
  return matrix.map((r, i) => i === row ? r.map(() => value) : r.slice());
}

export function fillColumn(matrix: Matrix2D, col: number, value: MatrixCell): Matrix2D {
  return matrix.map(r => {
    if (col < 0 || col >= r.length) return r.slice();
    const next = r.slice();
    next[col] = value;
    return next;
  });
}

export function fillAll(matrix: Matrix2D, nRows: number, nCols: number, value: MatrixCell): Matrix2D {
  const out: Matrix2D = [];
  for (let i = 0; i < nRows; i += 1) {
    const row: MatrixCell[] = [];
    for (let j = 0; j < nCols; j += 1) row.push(value);
    out.push(row);
  }
  // Preserve any pre-existing cells beyond (nRows × nCols) is unnecessary —
  // fillAll's contract is "every cell is set to value". Caller passes the
  // current dimensions. Any extra rows/cols already present are dropped.
  void matrix;
  return out;
}

export type FocusPos = { row: number; col: number };

/**
 * Decide the next focus position for a navigation keypress. Pure function so
 * the navigation policy is fully unit-testable.
 *
 * `caretAtEdge` is true when the cursor in the input is at the boundary that
 * makes a directional arrow safe to consume (left arrow + caret at start;
 * right arrow + caret at end). For empty cells, callers should pass true for
 * both. Up/Down always navigate (caretAtEdge is ignored).
 */
export function nextFocusFromKey(
  key: string,
  row: number,
  col: number,
  nRows: number,
  nCols: number,
  shift: boolean,
  caretAtEdge: boolean,
): FocusPos | null {
  if (nRows <= 0 || nCols <= 0) return null;
  const clamp = (r: number, c: number): FocusPos | null => {
    if (r < 0 || r >= nRows || c < 0 || c >= nCols) return null;
    return { row: r, col: c };
  };
  if (key === 'Tab') {
    if (shift) {
      if (col > 0) return clamp(row, col - 1);
      if (row > 0) return clamp(row - 1, nCols - 1);
      return null;
    }
    if (col < nCols - 1) return clamp(row, col + 1);
    if (row < nRows - 1) return clamp(row + 1, 0);
    return null;
  }
  if (key === 'Enter') {
    if (shift) return clamp(row - 1, col);
    return clamp(row + 1, col);
  }
  if (key === 'ArrowUp') return clamp(row - 1, col);
  if (key === 'ArrowDown') return clamp(row + 1, col);
  if (key === 'ArrowLeft') {
    if (!caretAtEdge) return null;
    return clamp(row, col - 1);
  }
  if (key === 'ArrowRight') {
    if (!caretAtEdge) return null;
    return clamp(row, col + 1);
  }
  return null;
}

/** Flip a literal cell to a quote cell and vice versa, preserving any prior
 * literal value as an empty quote id (and dropping the quote id back to NaN
 * when going the other way). */
export function toggleCellMode(cell: MatrixCell): MatrixCell {
  if (isQuoteCell(cell)) return NaN as MatrixCell;
  return { quoteId: '' };
}

export interface CellPresentation {
  className: string;
  title?: string;
  ariaInvalid: boolean;
}

/**
 * Compute presentation props (className, tooltip, aria-invalid) for a cell,
 * given an optional validate callback. Pure function for testability.
 */
export function getCellPresentation(
  cell: MatrixCell,
  row: number,
  col: number,
  validate?: (cell: MatrixCell, row: number, col: number) => string | null,
): CellPresentation {
  const error = validate ? validate(cell, row, col) : null;
  const baseClass = 'w-full px-2 py-1 text-xs font-mono border rounded outline-none focus:ring-1';
  if (error) {
    return {
      className: `${baseClass} border-red-500 bg-red-50 text-red-700 focus:ring-red-300`,
      title: error,
      ariaInvalid: true,
    };
  }
  return {
    className: `${baseClass} border-[#d4d4d4] bg-white text-[#0a0a0a] focus:ring-[#d4b87b] focus:border-[#8a6a2f]`,
    ariaInvalid: false,
  };
}

/**
 * Defensive load-time normalizer. Accepts any unknown JSON-shaped input and
 * coerces it into a Matrix2D of exactly nRows × nCols. Unknown cells become
 * NaN literals.
 */
export function normalizeMatrix2D(input: unknown, nRows: number, nCols: number): Matrix2D {
  const rows = Array.isArray(input) ? input : [];
  const out: Matrix2D = [];
  for (let i = 0; i < nRows; i += 1) {
    const row: MatrixCell[] = [];
    const src = Array.isArray(rows[i]) ? (rows[i] as unknown[]) : [];
    for (let j = 0; j < nCols; j += 1) {
      const cell = src[j];
      if (typeof cell === 'number') row.push(cell);
      else if (cell && typeof cell === 'object' && 'quoteId' in (cell as Record<string, unknown>) && typeof (cell as { quoteId: unknown }).quoteId === 'string') {
        row.push({ quoteId: (cell as { quoteId: string }).quoteId });
      } else row.push(NaN);
    }
    out.push(row);
  }
  return out;
}

// ---------------------------------------------------------------------------
// React component
// ---------------------------------------------------------------------------

export interface Matrix2DEditorProps {
  value: MatrixCell[][];
  onChange: (next: MatrixCell[][]) => void;
  rowLabels: string[];
  colLabels: string[];
  rowAxisLabel?: string;
  colAxisLabel?: string;
  validateCell?: (cell: MatrixCell, row: number, col: number) => string | null;
  colorScale?: (value: number) => string;
  allowQuoteCells?: boolean;
  quoteIdOptions?: string[];
  step?: number;
  ariaLabel?: string;
  className?: string;
}

function formatCellInputValue(cell: MatrixCell): string {
  if (isQuoteCell(cell)) return cell.quoteId;
  if (typeof cell === 'number' && Number.isFinite(cell)) return String(cell);
  return '';
}

export function Matrix2DEditor({
  value,
  onChange,
  rowLabels,
  colLabels,
  rowAxisLabel = 'Row',
  colAxisLabel = 'Col',
  validateCell,
  colorScale,
  allowQuoteCells = true,
  quoteIdOptions,
  step = 0.0001,
  ariaLabel = '2D matrix editor',
  className,
}: Matrix2DEditorProps) {
  const nRows = rowLabels.length;
  const nCols = colLabels.length;
  const [focus, setFocus] = useState<FocusPos | null>(null);
  const cellRefs = useRef<Array<Array<HTMLInputElement | HTMLSelectElement | null>>>([]);

  // Keep the refs grid sized to the current dimensions.
  useEffect(() => {
    cellRefs.current = Array.from({ length: nRows }, (_, i) =>
      Array.from({ length: nCols }, (_, j) => cellRefs.current[i]?.[j] ?? null),
    );
  }, [nRows, nCols]);

  // Imperatively focus the cell whenever `focus` changes.
  useEffect(() => {
    if (!focus) return;
    const el = cellRefs.current[focus.row]?.[focus.col];
    if (el) el.focus();
  }, [focus]);

  const setCellRef = useCallback((row: number, col: number, el: HTMLInputElement | HTMLSelectElement | null) => {
    if (!cellRefs.current[row]) cellRefs.current[row] = [];
    cellRefs.current[row][col] = el;
  }, []);

  const safeValue = useMemo(
    () => resizeMatrixPreserving(value, nRows, nCols),
    [value, nRows, nCols],
  );

  const updateCell = useCallback((row: number, col: number, cell: MatrixCell) => {
    const next = safeValue.map(r => r.slice());
    next[row][col] = cell;
    onChange(next);
  }, [safeValue, onChange]);

  const handlePaste = useCallback((row: number, col: number, ev: React.ClipboardEvent<HTMLInputElement>) => {
    const text = ev.clipboardData.getData('text');
    const parsed = parsePastedMatrix(text);
    const isMultiCell = parsed.length > 1 || (parsed[0]?.length ?? 0) > 1;
    if (!isMultiCell) return;
    ev.preventDefault();
    const next = applyPasteAt(safeValue, row, col, parsed);
    onChange(next);
  }, [safeValue, onChange]);

  const handleKeyDown = useCallback((row: number, col: number, ev: KeyboardEvent<HTMLInputElement | HTMLSelectElement>) => {
    const target = ev.target as HTMLInputElement;
    const text = typeof target.value === 'string' ? target.value : '';
    const selStart = typeof target.selectionStart === 'number' ? target.selectionStart : text.length;
    const selEnd = typeof target.selectionEnd === 'number' ? target.selectionEnd : text.length;
    const noSelection = selStart === selEnd;
    const caretAtEdge = ev.key === 'ArrowLeft'
      ? noSelection && selStart === 0
      : ev.key === 'ArrowRight'
        ? noSelection && selStart === text.length
        : true;
    const next = nextFocusFromKey(ev.key, row, col, nRows, nCols, ev.shiftKey, caretAtEdge);
    if (next) {
      ev.preventDefault();
      setFocus(next);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      target.blur();
    }
  }, [nRows, nCols]);

  const handleNumberInput = useCallback((row: number, col: number, raw: string) => {
    const trimmed = raw.trim();
    if (trimmed.length === 0) {
      updateCell(row, col, NaN as MatrixCell);
      return;
    }
    const n = Number(trimmed);
    updateCell(row, col, Number.isFinite(n) ? (n as MatrixCell) : (NaN as MatrixCell));
  }, [updateCell]);

  const handleToggle = useCallback((row: number, col: number) => {
    const cell = safeValue[row]?.[col] ?? (NaN as MatrixCell);
    updateCell(row, col, toggleCellMode(cell));
  }, [safeValue, updateCell]);

  const promptFill = (initial = ''): MatrixCell | null => {
    const raw = window.prompt('Fill value (number, blank to clear, or @QUOTE_ID for a quote reference):', initial);
    if (raw === null) return null;
    const t = raw.trim();
    if (t.length === 0) return NaN as MatrixCell;
    if (t.startsWith('@')) {
      const id = t.slice(1).trim();
      return { quoteId: id };
    }
    const n = Number(t);
    return Number.isFinite(n) ? (n as MatrixCell) : (NaN as MatrixCell);
  };

  const handleFillAll = () => {
    const v = promptFill();
    if (v === null) return;
    onChange(fillAll(safeValue, nRows, nCols, v));
  };

  const handleFillRow = (row: number) => {
    const v = promptFill();
    if (v === null) return;
    onChange(fillRow(safeValue, row, v));
  };

  const handleFillColumn = (col: number) => {
    const v = promptFill();
    if (v === null) return;
    onChange(fillColumn(safeValue, col, v));
  };

  const [heatmapEnabled, setHeatmapEnabled] = useState(false);
  const effectiveColorScale = heatmapEnabled ? colorScale : undefined;

  if (nRows === 0 || nCols === 0) {
    return (
      <div className={`text-xs text-[#a3a3a3] border border-dashed border-[#d4d4d4] rounded-lg p-3 ${className || ''}`}>
        Add at least one row and one column to edit the matrix.
      </div>
    );
  }

  return (
    <div className={className || ''}>
      <div className="flex items-center justify-between mb-2 gap-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleFillAll}
            className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-[#f5f5f5]"
          >
            Fill all…
          </button>
          {colorScale && (
            <label className="text-xs text-[#525252] flex items-center gap-1.5 select-none">
              <input
                type="checkbox"
                checked={heatmapEnabled}
                onChange={e => setHeatmapEnabled(e.target.checked)}
              />
              Heatmap
            </label>
          )}
        </div>
        <div className="text-[11px] text-[#a3a3a3]">
          Tab/Enter/arrows to navigate. Paste TSV/CSV to bulk-fill.
        </div>
      </div>
      <div className="overflow-auto border border-[#e5e5e5] rounded-lg bg-white">
        <table className="text-xs w-full" aria-label={ariaLabel}>
          <thead className="bg-[#fafafa]">
            <tr>
              <th className="px-2 py-1 text-left text-[#737373] font-medium sticky left-0 bg-[#fafafa] z-10">
                {rowAxisLabel} \ {colAxisLabel}
              </th>
              {colLabels.map((label, j) => (
                <th key={`col-${j}`} className="px-2 py-1 text-left text-[#737373] font-medium whitespace-nowrap">
                  <div className="flex items-center gap-1">
                    <span className="font-mono text-[#0a0a0a]">{label}</span>
                    <button
                      type="button"
                      onClick={() => handleFillColumn(j)}
                      title={`Fill column ${label}`}
                      className="text-[10px] text-[#a3a3a3] hover:text-[#525252]"
                    >
                      ⤓
                    </button>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: nRows }, (_, i) => (
              <tr key={`row-${i}`} className="border-t border-[#f5f5f5]">
                <td className="px-2 py-1 text-[#737373] font-mono whitespace-nowrap sticky left-0 bg-white z-10">
                  <div className="flex items-center gap-1">
                    <span className="text-[#0a0a0a]">{rowLabels[i]}</span>
                    <button
                      type="button"
                      onClick={() => handleFillRow(i)}
                      title={`Fill row ${rowLabels[i]}`}
                      className="text-[10px] text-[#a3a3a3] hover:text-[#525252]"
                    >
                      ⤓
                    </button>
                  </div>
                </td>
                {Array.from({ length: nCols }, (_, j) => {
                  const cell = safeValue[i][j];
                  const presentation = getCellPresentation(cell, i, j, validateCell);
                  const isQuote = isQuoteCell(cell);
                  const numericVal = !isQuote && typeof cell === 'number' && Number.isFinite(cell) ? cell : null;
                  const tdStyle: CSSProperties | undefined = effectiveColorScale && numericVal !== null
                    ? { backgroundColor: effectiveColorScale(numericVal) }
                    : undefined;
                  return (
                    <td
                      key={`cell-${i}-${j}`}
                      className="px-1 py-1 align-middle group"
                      style={tdStyle}
                      title={presentation.title}
                    >
                      <div className="flex items-center gap-1">
                        {isQuote ? (
                          quoteIdOptions && quoteIdOptions.length > 0 ? (
                            <select
                              ref={(el) => setCellRef(i, j, el)}
                              className={presentation.className}
                              value={(cell as { quoteId: string }).quoteId}
                              aria-invalid={presentation.ariaInvalid || undefined}
                              onChange={(e) => updateCell(i, j, { quoteId: e.target.value })}
                              onKeyDown={(e) => handleKeyDown(i, j, e)}
                              onFocus={() => setFocus({ row: i, col: j })}
                            >
                              <option value="">Select…</option>
                              {quoteIdOptions.map(id => <option key={id} value={id}>{id}</option>)}
                            </select>
                          ) : (
                            <input
                              ref={(el) => setCellRef(i, j, el)}
                              type="text"
                              className={presentation.className}
                              value={(cell as { quoteId: string }).quoteId}
                              placeholder="quote id"
                              aria-invalid={presentation.ariaInvalid || undefined}
                              onChange={(e) => updateCell(i, j, { quoteId: e.target.value })}
                              onKeyDown={(e) => handleKeyDown(i, j, e)}
                              onPaste={(e) => handlePaste(i, j, e)}
                              onFocus={() => setFocus({ row: i, col: j })}
                            />
                          )
                        ) : (
                          <input
                            ref={(el) => setCellRef(i, j, el)}
                            type="text"
                            inputMode="decimal"
                            step={step}
                            className={presentation.className}
                            value={formatCellInputValue(cell)}
                            aria-invalid={presentation.ariaInvalid || undefined}
                            onChange={(e) => handleNumberInput(i, j, e.target.value)}
                            onKeyDown={(e) => handleKeyDown(i, j, e)}
                            onPaste={(e) => handlePaste(i, j, e)}
                            onFocus={() => setFocus({ row: i, col: j })}
                          />
                        )}
                        {allowQuoteCells && (
                          <button
                            type="button"
                            onClick={() => handleToggle(i, j)}
                            title={isQuote ? 'Switch to literal value' : 'Switch to quote reference'}
                            className="text-[10px] px-1 py-0.5 rounded text-[#a3a3a3] hover:text-[#525252] hover:bg-[#f5f5f5] opacity-0 group-hover:opacity-100 focus:opacity-100"
                            aria-label={isQuote ? 'Switch to literal value' : 'Switch to quote reference'}
                          >
                            {isQuote ? 'Q' : '#'}
                          </button>
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default Matrix2DEditor;
