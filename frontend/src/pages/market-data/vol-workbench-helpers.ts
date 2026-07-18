import type { DisplayUnits } from './vol-workbench-types';
import type { VolSurfaceSampleResult } from '../../lib/quantra-types';
import type { VolSurfaceSpec } from '../../lib/storage/volSurfaces';
import type { StoredIndexSpec } from '../../lib/storage/indices';
import { yearsToPeriod } from '../../lib/storage/volSurfacePayload';

/**
 * The subset of registered indices that can legitimately serve as a swap's
 * floating leg: IBOR-family and Overnight rate indices. Inflation indices
 * (e.g. EUHICP_YY) are explicitly excluded — the engine cannot resolve an
 * inflation index as a swap float leg ("Unknown index id: …") and silently
 * returns an EMPTY vol surface (HTTP 200, 0 cells). Returns the ids only.
 */
export function rateFloatIndexOptions(indices: StoredIndexSpec[]): string[] {
  return indices
    .filter(i => i.type === 'IBOR' || i.type === 'Overnight')
    .map(i => i.id);
}

/**
 * Derives the swap-index id for a registered rate index, mirroring the
 * QuantLib-style naming the workbench uses to populate the Swap Index
 * dropdown. IBOR indices map to `${CCY}_SWAP_${n}${code}` (e.g. EURIBOR_6M →
 * EUR_SWAP_6M); everything else (Overnight) maps to `${id}_OIS`. This is the
 * SINGLE source of the derivation — both `swapIndexOptions` and the
 * coherent float-leg auto-select reuse it so they can never drift apart.
 *
 * The currency is taken from `idx.currency` (e.g. EURIBOR_6M ⇒ "EUR") and
 * only falls back to the id prefix when absent. Using the id prefix alone is
 * wrong for family-named ids: `EURIBOR_6M`.split('_')[0] === "EURIBOR" would
 * yield `EURIBOR_SWAP_6M`, which does NOT match the seeded/default swap index
 * `EUR_SWAP_6M` — so the coherent auto-select would miss and fall back to the
 * wrong-tenor first IBOR (EURIBOR_1M → empty surface).
 */
export function swapIndexIdForIndex(idx: StoredIndexSpec): string {
  const ccy = (idx.currency || idx.id.split('_')[0] || 'USD').toUpperCase();
  if (idx.type === 'IBOR') {
    const n = idx.tenor_number ?? 6;
    const u = (idx.tenor_time_unit || 'Months').toString();
    const code = u.startsWith('Year') ? 'Y' : u.startsWith('Week') ? 'W' : u.startsWith('Day') ? 'D' : 'M';
    return `${ccy}_SWAP_${n}${code}`;
  }
  return `${idx.id}_OIS`;
}

/**
 * Picks the default float index for the swaption underlying's floating leg.
 *
 * The float leg MUST be COHERENT with the currently-selected swap index:
 * `EUR_SWAP_6M` needs `EURIBOR_6M`, not just any IBOR. Picking the wrong
 * tenor (e.g. the first IBOR `EURIBOR_1M`) yields an index with no forwarding
 * curve → engine "Unknown index id" → empty surface. So when `swapIndexId`
 * is given we first prefer the IBOR whose derived swap-index id matches it.
 *
 * Fallback (no swap-index match, or none supplied): prefer any IBOR-family
 * index, then Overnight / the first available rate option. `options` MUST
 * already be the rate-only list from `rateFloatIndexOptions` so the result is
 * never an inflation index. Returns `undefined` when no rate index exists.
 */
export function pickDefaultFloatIndex(
  indices: StoredIndexSpec[],
  options: string[],
  swapIndexId?: string,
): string | undefined {
  if (swapIndexId) {
    const coherent = indices.find(
      i => options.includes(i.id) && swapIndexIdForIndex(i) === swapIndexId,
    )?.id;
    if (coherent) return coherent;
  }
  const firstIbor = indices.find(i => i.type === 'IBOR' && options.includes(i.id))?.id;
  return firstIbor ?? options[0];
}

/**
 * Editable Swaption surface kinds — i.e. those that render a Matrix2DEditor
 * over `surface.grid` (or the four SABR-parameter grids, or the 3D SABR
 * market-vol cube). Constant has no matrix.
 */
export type EditableSwaptionKind = 'AtmMatrix' | 'SmileCube' | 'SabrParams' | 'SabrCalibrate';

/**
 * Returns a backfilled `VolSurfaceSpec` whose `axes_expiries` / `axes_tenors`
 * are seeded from the legacy `expiries` / `tenors: number[]` (years) when
 * either axis is empty, or `null` if no backfill is needed (the surface is
 * already on the period-axes schema).
 *
 * Why this exists: surfaces saved before Portal Step 1 introduced the
 * `axes_expiries`/`axes_tenors` fields (notably `swaptvol_dense` shipped in
 * `public/example-market-data.json`) carry only the legacy fields. Without
 * backfill, the Matrix2DEditor mounted by Step 1.5 for AtmMatrix /
 * SmileCube renders its empty-state ("Add at least one row and one column…")
 * because it reads `axes_expiries.length === 0`, even though the saved
 * `grid` is fully populated.
 *
 * `yearsToPeriod` ensures fractional years like `1/12` become integer
 * Periods (`{n: 1, unit: 'Months'}`) per the `quantra_Period.n: int`
 * OpenAPI contract — same conversion `applyKindChangeToSurface` uses for
 * kind-transition seeding, so opening a legacy surface and changing its
 * kind produce identical axes.
 */
export function backfillSurfaceAxesFromLegacy(
  surface: VolSurfaceSpec,
  kind: EditableSwaptionKind,
): VolSurfaceSpec | null {
  void kind;
  const needsExpiries = (!surface.axes_expiries || surface.axes_expiries.length === 0)
    && Array.isArray(surface.expiries) && surface.expiries.length > 0;
  const needsTenors = (!surface.axes_tenors || surface.axes_tenors.length === 0)
    && Array.isArray(surface.tenors) && surface.tenors.length > 0;
  if (!needsExpiries && !needsTenors) return null;
  const next: VolSurfaceSpec = { ...surface };
  if (needsExpiries) next.axes_expiries = surface.expiries!.map(yearsToPeriod);
  if (needsTenors) next.axes_tenors = surface.tenors!.map(yearsToPeriod);
  return next;
}

export function expiryLabelToYears(expiryLabel: string | undefined, asOfDate: string): number {
  if (!expiryLabel) return 0;
  const raw = expiryLabel.trim();
  const upper = raw.toUpperCase();
  const periodMatch = upper.match(/^(\d+(?:\.\d+)?)([DWMY])$/);
  if (periodMatch) {
    const n = Number(periodMatch[1]);
    const u = periodMatch[2];
    if (!Number.isFinite(n)) return 0;
    if (u === 'D') return n / 365;
    if (u === 'W') return (n * 7) / 365;
    if (u === 'M') return n / 12;
    return n;
  }

  const isoMatch = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const asOfMatch = asOfDate.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!isoMatch || !asOfMatch) return 0;

  const y = Number(isoMatch[1]);
  const m = Number(isoMatch[2]);
  const d = Number(isoMatch[3]);
  const ay = Number(asOfMatch[1]);
  const am = Number(asOfMatch[2]);
  const ad = Number(asOfMatch[3]);
  if (![y, m, d, ay, am, ad].every(Number.isFinite)) return 0;

  const expiryTs = Date.UTC(y, m - 1, d);
  const asOfTs = Date.UTC(ay, am - 1, ad);
  return (expiryTs - asOfTs) / (365 * 24 * 60 * 60 * 1000);
}

export function isStrictlyIncreasing(values: number[]): boolean {
  for (let i = 1; i < values.length; i += 1) {
    if (!(values[i] > values[i - 1])) return false;
  }
  return true;
}

function toDisplayVol(v: number, units: DisplayUnits): number {
  if (units === 'bps') return v * 10000;
  if (units === 'pct') return v * 100;
  return v;
}

export type CubeGeometry = {
  ok: boolean;
  error?: string;
  x: number[];
  y: number[];
  z: number[][];
  nE: number;
  nT: number;
  nS: number;
  k: number;
  expiryLabels: string[];
  tenorLabels: string[];
  zMin: number;
  zMax: number;
  xStrictlyIncreasing: boolean;
  yStrictlyIncreasing: boolean;
  yAllZero: boolean;
  yHasDuplicates: boolean;
};

export type EquityGeometry = {
  ok: boolean;
  error?: string;
  x: number[];
  y: number[];
  z: number[][];
  nE: number;
  nS: number;
  expiryLabels: string[];
  strikeLabels: string[];
  zMin: number;
  zMax: number;
  xStrictlyIncreasing: boolean;
  yStrictlyIncreasing: boolean;
  yAllZero: boolean;
  yHasDuplicates: boolean;
};

export function buildCubeGeometry(
  sample: VolSurfaceSampleResult,
  selectedStrikeIdx: number,
  displayUnits: DisplayUnits,
  asOfDate: string
): CubeGeometry {
  const nE = sample.n_expiries || sample.expiries?.length || 0;
  const nT = sample.n_tenors || sample.tenors?.length || 0;
  const nS = sample.n_strikes || sample.strikes?.length || 0;
  const vols = sample.vols || [];
  const expiryLabels = sample.expiries || [];
  const tenorLabels = (sample.tenors || []).map(t => `${t.n}${t.unit?.[0] || ''}`);

  if (!nE || !nT || !nS || vols.length === 0) {
    return {
      ok: false,
      error: 'No cube values',
      x: [],
      y: [],
      z: [],
      nE, nT, nS, k: 0,
      expiryLabels,
      tenorLabels,
      zMin: 0,
      zMax: 0,
      xStrictlyIncreasing: false,
      yStrictlyIncreasing: false,
      yAllZero: true,
      yHasDuplicates: false,
    };
  }
  if ((nE * nT * nS) !== vols.length) {
    return {
      ok: false,
      error: 'Shape mismatch: n_expiries*n_tenors*n_strikes does not match vols length.',
      x: [],
      y: [],
      z: [],
      nE, nT, nS, k: 0,
      expiryLabels,
      tenorLabels,
      zMin: 0,
      zMax: 0,
      xStrictlyIncreasing: false,
      yStrictlyIncreasing: false,
      yAllZero: true,
      yHasDuplicates: false,
    };
  }
  if (!sample.tenors?.length || !sample.expiries?.length) {
    return {
      ok: false,
      error: 'No data: missing expiries or tenors in sample response.',
      x: [],
      y: [],
      z: [],
      nE, nT, nS, k: 0,
      expiryLabels,
      tenorLabels,
      zMin: 0,
      zMax: 0,
      xStrictlyIncreasing: false,
      yStrictlyIncreasing: false,
      yAllZero: true,
      yHasDuplicates: false,
    };
  }

  const x = (sample.tenors || []).map(t => {
    if (t.unit === 'Days') return t.n / 365;
    if (t.unit === 'Weeks') return (t.n * 7) / 365;
    if (t.unit === 'Months') return t.n / 12;
    return t.n;
  });
  const y = (sample.expiries || []).map(v => expiryLabelToYears(v, asOfDate));
  const k = Math.max(0, Math.min(selectedStrikeIdx, nS - 1));
  const z: number[][] = Array.from({ length: nE }, () => Array.from({ length: nT }, () => NaN));
  let zMin = Number.POSITIVE_INFINITY;
  let zMax = Number.NEGATIVE_INFINITY;
  for (let i = 0; i < nE; i += 1) {
    for (let j = 0; j < nT; j += 1) {
      const value = toDisplayVol(vols[(i * nT + j) * nS + k] ?? NaN, displayUnits);
      z[i][j] = value;
      if (Number.isFinite(value)) {
        if (value < zMin) zMin = value;
        if (value > zMax) zMax = value;
      }
    }
  }
  if (!Number.isFinite(zMin) || !Number.isFinite(zMax)) {
    zMin = 0;
    zMax = 0;
  }
  const ySet = new Set(y.map(v => v.toFixed(12)));
  return {
    ok: true,
    x,
    y,
    z,
    nE,
    nT,
    nS,
    k,
    expiryLabels,
    tenorLabels,
    zMin,
    zMax,
    xStrictlyIncreasing: isStrictlyIncreasing(x),
    yStrictlyIncreasing: isStrictlyIncreasing(y),
    yAllZero: y.every(v => Math.abs(v) < 1e-12),
    yHasDuplicates: ySet.size !== y.length,
  };
}

export function buildEquityGeometry(
  sample: VolSurfaceSampleResult,
  displayUnits: DisplayUnits,
  asOfDate: string,
  strikeFallback: number[] = []
): EquityGeometry {
  const nE = sample.n_expiries || sample.expiries?.length || 0;
  const vols = sample.vols || [];
  const expiryLabels = sample.expiries || [];
  const inferredNS = nE > 0 && vols.length >= nE ? Math.floor(vols.length / nE) : 0;
  const declaredNS = sample.n_strikes || sample.strikes?.length || 0;
  const nS = declaredNS > 1
    ? declaredNS
    : (strikeFallback.length > 1
      ? strikeFallback.length
      : (inferredNS > 1 ? inferredNS : Math.max(declaredNS, inferredNS)));
  const x = sample.strikes?.length === nS
    ? sample.strikes
    : (strikeFallback.length === nS
      ? strikeFallback
      : Array.from({ length: Math.max(nS, 0) }, (_, i) => i + 1));
  const strikeLabels = x.map(v => Number(v).toFixed(4));

  if (!nE || !nS || vols.length === 0) {
    return {
      ok: false,
      error: 'No equity surface values',
      x: [],
      y: [],
      z: [],
      nE,
      nS,
      expiryLabels,
      strikeLabels,
      zMin: 0,
      zMax: 0,
      xStrictlyIncreasing: false,
      yStrictlyIncreasing: false,
      yAllZero: true,
      yHasDuplicates: false,
    };
  }

  if (nS <= 0 || (nE * nS) !== vols.length) {
    return {
      ok: false,
      error: 'Shape mismatch: n_expiries*n_strikes does not match vols length.',
      x: [],
      y: [],
      z: [],
      nE,
      nS,
      expiryLabels,
      strikeLabels,
      zMin: 0,
      zMax: 0,
      xStrictlyIncreasing: false,
      yStrictlyIncreasing: false,
      yAllZero: true,
      yHasDuplicates: false,
    };
  }

  const y = (sample.expiries || []).map(v => expiryLabelToYears(v, asOfDate));
  const z: number[][] = Array.from({ length: nE }, () => Array.from({ length: nS }, () => NaN));
  let zMin = Number.POSITIVE_INFINITY;
  let zMax = Number.NEGATIVE_INFINITY;

  for (let i = 0; i < nE; i += 1) {
    for (let k = 0; k < nS; k += 1) {
      const value = toDisplayVol(vols[i * nS + k] ?? NaN, displayUnits);
      z[i][k] = value;
      if (Number.isFinite(value)) {
        if (value < zMin) zMin = value;
        if (value > zMax) zMax = value;
      }
    }
  }

  if (!Number.isFinite(zMin) || !Number.isFinite(zMax)) {
    zMin = 0;
    zMax = 0;
  }
  const ySet = new Set(y.map(v => v.toFixed(12)));

  return {
    ok: true,
    x,
    y,
    z,
    nE,
    nS,
    expiryLabels,
    strikeLabels,
    zMin,
    zMax,
    xStrictlyIncreasing: isStrictlyIncreasing(x),
    yStrictlyIncreasing: isStrictlyIncreasing(y),
    yAllZero: y.every(v => Math.abs(v) < 1e-12),
    yHasDuplicates: ySet.size !== y.length,
  };
}
