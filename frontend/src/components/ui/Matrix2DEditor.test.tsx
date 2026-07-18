import { describe, expect, it } from 'vitest';
import {
  applyPasteAt,
  fillAll,
  fillColumn,
  fillRow,
  getCellPresentation,
  isQuoteCell,
  type Matrix2D,
  type MatrixCell,
  nextFocusFromKey,
  normalizeMatrix2D,
  parsePastedMatrix,
  resizeMatrixPreserving,
  toggleCellMode,
} from './Matrix2DEditor';
import { flattenMatrixForWire } from '../../lib/types/matrix2d';

describe('parsePastedMatrix', () => {
  it('parses TSV (Excel default)', () => {
    const input = '0.20\t0.21\t0.22\n0.23\t0.24\t0.25\n0.26\t0.27\t0.28';
    const result = parsePastedMatrix(input);
    expect(result).toEqual([
      [0.20, 0.21, 0.22],
      [0.23, 0.24, 0.25],
      [0.26, 0.27, 0.28],
    ]);
  });

  it('parses CSV when no tabs are present', () => {
    const result = parsePastedMatrix('1,2,3\n4,5,6');
    expect(result).toEqual([[1, 2, 3], [4, 5, 6]]);
  });

  it('uses TSV whenever any tab appears, even alongside commas', () => {
    const result = parsePastedMatrix('1,2\t3,4\n5,6\t7,8');
    // Tabs split each line into 2 tokens; "1,2" and "3,4" are not finite numbers.
    expect(result.length).toBe(2);
    expect(result[0].length).toBe(2);
    expect(result[0].every(v => Number.isNaN(v as number))).toBe(true);
  });

  it('treats empty and non-numeric tokens as NaN literals', () => {
    const result = parsePastedMatrix('1\t\tfoo\n,2,');
    // First line is TSV: ['1', '', 'foo'] → [1, NaN, NaN]
    expect(result[0][0]).toBe(1);
    expect(Number.isNaN(result[0][1] as number)).toBe(true);
    expect(Number.isNaN(result[0][2] as number)).toBe(true);
  });

  it('strips trailing newlines and CRLFs', () => {
    expect(parsePastedMatrix('1\t2\r\n3\t4\r\n')).toEqual([[1, 2], [3, 4]]);
  });

  it('does not produce quote cells (paste stays unsurprising)', () => {
    const result = parsePastedMatrix('@QUOTE_A\t1\n2\t@QUOTE_B');
    for (const row of result) {
      for (const cell of row) {
        expect(isQuoteCell(cell)).toBe(false);
      }
    }
  });

  it('returns empty array for empty input', () => {
    expect(parsePastedMatrix('')).toEqual([]);
    expect(parsePastedMatrix('\n\n')).toEqual([]);
  });
});

describe('applyPasteAt', () => {
  const m: Matrix2D = [
    [1, 2, 3],
    [4, 5, 6],
    [7, 8, 9],
  ];

  it('overlays at anchor', () => {
    const result = applyPasteAt(m, 0, 0, [[10, 20], [30, 40]]);
    expect(result).toEqual([
      [10, 20, 3],
      [30, 40, 6],
      [7, 8, 9],
    ]);
  });

  it('clips at bounds', () => {
    const result = applyPasteAt(m, 2, 2, [[100, 200], [300, 400]]);
    expect(result).toEqual([
      [1, 2, 3],
      [4, 5, 6],
      [7, 8, 100],
    ]);
  });

  it('does not mutate the input', () => {
    const original = m.map(r => r.slice());
    applyPasteAt(m, 0, 0, [[99]]);
    expect(m).toEqual(original);
  });

  it('returns the matrix unchanged when paste is empty', () => {
    expect(applyPasteAt(m, 0, 0, [])).toEqual(m);
  });

  it('handles negative or out-of-bounds anchors by clipping', () => {
    const result = applyPasteAt(m, -1, -1, [[10, 20], [30, 40]]);
    expect(result).toEqual([
      [40, 2, 3],
      [4, 5, 6],
      [7, 8, 9],
    ]);
  });
});

describe('resizeMatrixPreserving', () => {
  it('grows with NaN literals', () => {
    const m: Matrix2D = [[1, 2], [3, 4]];
    const result = resizeMatrixPreserving(m, 3, 3);
    expect(result[0][0]).toBe(1);
    expect(result[0][1]).toBe(2);
    expect(Number.isNaN(result[0][2] as number)).toBe(true);
    expect(Number.isNaN(result[2][2] as number)).toBe(true);
  });

  it('shrinks by dropping cells', () => {
    const m: Matrix2D = [[1, 2, 3], [4, 5, 6], [7, 8, 9]];
    expect(resizeMatrixPreserving(m, 2, 2)).toEqual([[1, 2], [4, 5]]);
  });

  it('handles undefined input', () => {
    const result = resizeMatrixPreserving(undefined, 2, 2);
    expect(result.length).toBe(2);
    expect(result[0].length).toBe(2);
    expect(result.every(row => row.every(c => Number.isNaN(c as number)))).toBe(true);
  });

  it('preserves quote cells when resizing', () => {
    const m: Matrix2D = [[{ quoteId: 'A' }, 1], [2, 3]];
    const result = resizeMatrixPreserving(m, 3, 3);
    expect(result[0][0]).toEqual({ quoteId: 'A' });
    expect(result[0][1]).toBe(1);
  });
});

describe('fill helpers', () => {
  const m: Matrix2D = [[1, 2, 3], [4, 5, 6]];

  it('fillRow replaces every cell in the target row', () => {
    expect(fillRow(m, 0, 9)).toEqual([[9, 9, 9], [4, 5, 6]]);
  });

  it('fillColumn replaces every cell in the target column', () => {
    expect(fillColumn(m, 1, 0)).toEqual([[1, 0, 3], [4, 0, 6]]);
  });

  it('fillAll yields a fresh nRows × nCols grid', () => {
    expect(fillAll(m, 2, 3, 0.5)).toEqual([[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]]);
  });

  it('fillRow / fillColumn no-op on out-of-range index', () => {
    expect(fillRow(m, 5, 9)).toEqual(m);
    expect(fillColumn(m, 5, 9)).toEqual(m);
  });

  it('does not mutate the original matrix', () => {
    const snapshot = m.map(r => r.slice());
    fillRow(m, 0, 99);
    fillColumn(m, 0, 99);
    fillAll(m, 5, 5, 99);
    expect(m).toEqual(snapshot);
  });
});

describe('nextFocusFromKey', () => {
  // 3x3 grid for navigation tests
  const N = 3;

  it('Tab advances to next column, wraps to next row at end', () => {
    expect(nextFocusFromKey('Tab', 0, 0, N, N, false, true)).toEqual({ row: 0, col: 1 });
    expect(nextFocusFromKey('Tab', 0, 2, N, N, false, true)).toEqual({ row: 1, col: 0 });
    expect(nextFocusFromKey('Tab', 2, 2, N, N, false, true)).toBeNull();
  });

  it('Shift+Tab moves backward and wraps at start', () => {
    expect(nextFocusFromKey('Tab', 0, 1, N, N, true, true)).toEqual({ row: 0, col: 0 });
    expect(nextFocusFromKey('Tab', 1, 0, N, N, true, true)).toEqual({ row: 0, col: 2 });
    expect(nextFocusFromKey('Tab', 0, 0, N, N, true, true)).toBeNull();
  });

  it('Enter moves down, Shift+Enter moves up', () => {
    expect(nextFocusFromKey('Enter', 0, 1, N, N, false, true)).toEqual({ row: 1, col: 1 });
    expect(nextFocusFromKey('Enter', 1, 1, N, N, true, true)).toEqual({ row: 0, col: 1 });
    expect(nextFocusFromKey('Enter', 2, 0, N, N, false, true)).toBeNull();
  });

  it('Up/Down always navigate (caretAtEdge ignored)', () => {
    expect(nextFocusFromKey('ArrowUp', 1, 0, N, N, false, false)).toEqual({ row: 0, col: 0 });
    expect(nextFocusFromKey('ArrowDown', 1, 0, N, N, false, false)).toEqual({ row: 2, col: 0 });
    expect(nextFocusFromKey('ArrowUp', 0, 0, N, N, false, false)).toBeNull();
    expect(nextFocusFromKey('ArrowDown', 2, 0, N, N, false, false)).toBeNull();
  });

  it('ArrowRight on empty cell (caretAtEdge=true) navigates', () => {
    expect(nextFocusFromKey('ArrowRight', 0, 0, N, N, false, true)).toEqual({ row: 0, col: 1 });
  });

  it('ArrowRight at end of text-cell navigates', () => {
    // Caller computes caretAtEdge=true when caret is at end.
    expect(nextFocusFromKey('ArrowRight', 0, 0, N, N, false, true)).toEqual({ row: 0, col: 1 });
  });

  it('ArrowRight in middle of text-cell does NOT navigate', () => {
    // Caller computes caretAtEdge=false when caret is mid-string.
    expect(nextFocusFromKey('ArrowRight', 0, 0, N, N, false, false)).toBeNull();
  });

  it('ArrowLeft mirrors right behaviour', () => {
    expect(nextFocusFromKey('ArrowLeft', 0, 1, N, N, false, true)).toEqual({ row: 0, col: 0 });
    expect(nextFocusFromKey('ArrowLeft', 0, 1, N, N, false, false)).toBeNull();
    expect(nextFocusFromKey('ArrowLeft', 0, 0, N, N, false, true)).toBeNull();
  });

  it('returns null for empty grids', () => {
    expect(nextFocusFromKey('Tab', 0, 0, 0, 0, false, true)).toBeNull();
  });

  it('returns null for unrecognised keys', () => {
    expect(nextFocusFromKey('a', 0, 0, N, N, false, true)).toBeNull();
  });
});

describe('toggleCellMode', () => {
  it('flips a literal to a quote cell with empty id', () => {
    const result = toggleCellMode(0.5);
    expect(result).toEqual({ quoteId: '' });
  });

  it('flips a quote cell back to NaN literal', () => {
    const result = toggleCellMode({ quoteId: 'X' });
    expect(typeof result === 'number' && Number.isNaN(result)).toBe(true);
  });

  it('round-trip flips: literal → quote → literal', () => {
    const a = toggleCellMode(0.5);
    expect(isQuoteCell(a)).toBe(true);
    const b = toggleCellMode(a);
    expect(isQuoteCell(b)).toBe(false);
  });
});

describe('getCellPresentation', () => {
  it('returns red-border classes and tooltip when validate fails', () => {
    const presentation = getCellPresentation(NaN, 0, 0, () => 'must be > 0');
    expect(presentation.className).toContain('border-red-500');
    expect(presentation.title).toBe('must be > 0');
    expect(presentation.ariaInvalid).toBe(true);
  });

  it('returns the default classes and no title when validate passes', () => {
    const presentation = getCellPresentation(0.5, 0, 0, () => null);
    expect(presentation.className).toContain('border-[#d4d4d4]');
    expect(presentation.title).toBeUndefined();
    expect(presentation.ariaInvalid).toBe(false);
  });

  it('treats absent validator as pass', () => {
    const presentation = getCellPresentation(0.5, 0, 0);
    expect(presentation.ariaInvalid).toBe(false);
    expect(presentation.title).toBeUndefined();
  });
});

describe('normalizeMatrix2D', () => {
  it('coerces unknown shapes to NaN literals at the requested dimensions', () => {
    const result = normalizeMatrix2D({ not: 'a matrix' }, 2, 2);
    expect(result.length).toBe(2);
    expect(result[0].length).toBe(2);
    expect(result.every(r => r.every(c => Number.isNaN(c as number)))).toBe(true);
  });

  it('preserves number cells', () => {
    expect(normalizeMatrix2D([[1, 2], [3, 4]], 2, 2)).toEqual([[1, 2], [3, 4]]);
  });

  it('preserves quote cells', () => {
    const result = normalizeMatrix2D([[{ quoteId: 'A' }, 1]], 1, 2);
    expect(result[0][0]).toEqual({ quoteId: 'A' });
    expect(result[0][1]).toBe(1);
  });

  it('round-trips through JSON for mixed-cell matrices', () => {
    const original: Matrix2D = [
      [0.20, 0.21, { quoteId: 'EUR_SWAPTION_1Y_5Y' }],
      [{ quoteId: 'EUR_SWAPTION_2Y_5Y' }, 0.24, 0.25],
    ];
    const serialized = JSON.parse(JSON.stringify(original));
    const restored = normalizeMatrix2D(serialized, 2, 3);
    expect(restored).toEqual(original);
  });

  it('round-trips through JSON for legacy number[][] matrices', () => {
    const legacy: number[][] = [[0.20, 0.21], [0.22, 0.23]];
    const serialized = JSON.parse(JSON.stringify(legacy));
    const restored = normalizeMatrix2D(serialized, 2, 2);
    expect(restored).toEqual([[0.20, 0.21], [0.22, 0.23]]);
  });
});

describe('flattenMatrixForWire (storage → wire bridge)', () => {
  const noQuotes = (_: string) => null;

  it('is the identity for legacy number[][] data', () => {
    const legacy: number[][] = [[0.20, 0.21, 0.22], [0.23, 0.24, 0.25]];
    expect(flattenMatrixForWire(legacy as MatrixCell[][], 2, 3, noQuotes)).toEqual(legacy);
  });

  it('coerces NaN literals to 0 (matches pre-change `?? 0` fallback)', () => {
    const grid: MatrixCell[][] = [[NaN, 0.21], [0.23, NaN]];
    expect(flattenMatrixForWire(grid, 2, 2, noQuotes)).toEqual([[0, 0.21], [0.23, 0]]);
  });

  it('resolves quote cells via the supplied callback', () => {
    const grid: MatrixCell[][] = [[{ quoteId: 'A' }, 1], [2, { quoteId: 'B' }]];
    const resolver = (id: string) => id === 'A' ? 0.5 : id === 'B' ? 0.6 : null;
    expect(flattenMatrixForWire(grid, 2, 2, resolver)).toEqual([[0.5, 1], [2, 0.6]]);
  });

  it('falls back to 0 when a quote cell cannot be resolved', () => {
    const grid: MatrixCell[][] = [[{ quoteId: 'unknown' }, 1]];
    expect(flattenMatrixForWire(grid, 1, 2, () => null)).toEqual([[0, 1]]);
  });

  it('produces a dense nRows × nCols result regardless of input dimensions', () => {
    const sparse: MatrixCell[][] = [[1]];
    expect(flattenMatrixForWire(sparse, 2, 3, noQuotes)).toEqual([[1, 0, 0], [0, 0, 0]]);
  });
});
