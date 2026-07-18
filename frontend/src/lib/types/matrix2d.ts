/**
 * Shared 2D matrix cell types and helpers used by the Matrix2DEditor UI
 * component and by VolSurfaceSpec storage. Lives in lib/types/ so that
 * storage code does not need to import from components/ui/.
 */

export type MatrixCell = number | { quoteId: string };
export type Matrix2D = MatrixCell[][];

export const isQuoteCell = (c: MatrixCell): c is { quoteId: string } =>
  typeof c === 'object' && c !== null && 'quoteId' in c;

/**
 * Resolve a Matrix2D (which may contain literal numbers or quote-cell objects)
 * to a dense number[][] of size nRows × nCols, suitable for handing to the
 * existing toQuoteMatrix2D wire helper. Quote cells are resolved through the
 * supplied callback; missing/null/non-finite values fall back to 0, matching
 * the pre-existing behaviour of `grid[i]?.[j] ?? 0` in VolWorkbench.
 *
 * For a legacy number[][] input this returns a fresh number[][] with bit-equal
 * values, so the resulting wire payload is identical to the pre-change one.
 */
export function flattenMatrixForWire(
  grid: ReadonlyArray<ReadonlyArray<MatrixCell>> | undefined,
  nRows: number,
  nCols: number,
  resolveQuoteValue: (quoteId: string) => number | null,
): number[][] {
  const out: number[][] = [];
  for (let i = 0; i < nRows; i += 1) {
    const row: number[] = [];
    for (let j = 0; j < nCols; j += 1) {
      const cell = grid?.[i]?.[j];
      if (typeof cell === 'number') {
        row.push(Number.isFinite(cell) ? cell : 0);
      } else if (cell && typeof cell === 'object' && 'quoteId' in cell) {
        const v = resolveQuoteValue(cell.quoteId);
        row.push(v !== null && Number.isFinite(v) ? v : 0);
      } else {
        row.push(0);
      }
    }
    out.push(row);
  }
  return out;
}
