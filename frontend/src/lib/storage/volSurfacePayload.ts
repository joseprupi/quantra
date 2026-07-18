/**
 * Shared wire-payload builder for swaption volatility surfaces.
 *
 * Historically VolWorkbench.runSample and pages/products/{Swaption,IrSwap}.tsx
 * each built their own `SwaptionVolSpec` wire envelope inline, with subtle
 * differences (notably: sourcing axes from the sampling-grid React state
 * rather than from the saved surface, which caused
 * `Empty list of points for term structure` errors and silent dimensional
 * mismatches).
 *
 * This module centralizes the envelope so the surface owns its axes, the
 * sampling grid is independent, and dimensional checks happen before emit.
 */

import type { MatrixCell } from '../types/matrix2d';
import type { Period, TimeUnit } from '../types';
import { flattenMatrixForWire } from '../types/matrix2d';
import type { VolSurfaceSpec, VolShape } from './volSurfaces';

export type ResolveQuoteFn = (quoteId: string) => number | null;

export class VolSurfacePayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'VolSurfacePayloadError';
  }
}

export type QuoteMatrix2D = { n_rows: number; n_cols: number; values: number[] };

export type QuoteTensor3D = { n_1: number; n_2: number; n_3: number; values: number[] };

export type SwaptionInnerSpec =
  | {
      payload_type: 'SwaptionVolConstantSpec';
      payload: { base: Record<string, unknown> };
    }
  | {
      payload_type: 'SwaptionVolAtmMatrixSpec';
      payload: {
        base: Record<string, unknown>;
        expiries: Period[];
        tenors: Period[];
        vols: QuoteMatrix2D;
      };
    }
  | {
      payload_type: 'SwaptionSabrParamsSpec';
      payload: {
        base: Record<string, unknown>;
        expiries: Period[];
        tenors: Period[];
        alpha: QuoteMatrix2D;
        beta: QuoteMatrix2D;
        rho: QuoteMatrix2D;
        nu: QuoteMatrix2D;
      };
    }
  | {
      payload_type: 'SwaptionSabrCalibrateSpec';
      payload: {
        base: Record<string, unknown>;
        expiries: Period[];
        tenors: Period[];
        strikes: number[];
        vols: QuoteTensor3D;
        beta_fixed: boolean;
        beta_value?: number;
        vega_weighted_smile_fit?: boolean;
      };
    };

export type SwaptionVolWirePayload = {
  id: string;
  payload_type: 'SwaptionVolSpec';
  payload: { swap_index_id: string } & SwaptionInnerSpec;
};

/**
 * Build a dense `{n_rows, n_cols, values}` envelope from a row-major number
 * matrix. Lifted out of VolWorkbench.tsx where it lived as a private helper
 * for the AtmMatrix branch; also reused for SabrParams.
 *
 * Caller is responsible for resolving quote cells beforehand via
 * `flattenMatrixForWire`.
 */
export function toQuoteMatrix2D(grid: number[][], n1: number, n2: number): QuoteMatrix2D {
  const values: number[] = [];
  for (let i = 0; i < n1; i += 1) {
    for (let j = 0; j < n2; j += 1) {
      values.push(grid[i]?.[j] ?? 0);
    }
  }
  return { n_rows: n1, n_cols: n2, values };
}

/**
 * Resolve a possibly-heterogeneous 3D MatrixCell cube (`[expiry][tenor][strike]`)
 * to a dense number tensor of size `nE × nT × nS`. Quote cells are resolved
 * through the supplied callback; missing/non-finite values fall back to 0.
 *
 * Used by the SabrCalibrate wire branch — the SwaptionSabrCalibrateSpec payload
 * is row-major `n_1*n_2*n_3` with `n_1=nExpiries, n_2=nTenors, n_3=nStrikes`
 * (per `quantra_QuoteTensor3D` in OpenAPI: "Row-major, length = n_1*n_2*n_3").
 */
export function flattenCubeForWire(
  cube: ReadonlyArray<ReadonlyArray<ReadonlyArray<MatrixCell>>> | undefined,
  nE: number,
  nT: number,
  nS: number,
  resolveQuoteValue: ResolveQuoteFn,
): number[] {
  const values: number[] = [];
  for (let i = 0; i < nE; i += 1) {
    for (let j = 0; j < nT; j += 1) {
      for (let k = 0; k < nS; k += 1) {
        const cell = cube?.[i]?.[j]?.[k];
        if (typeof cell === 'number') {
          values.push(Number.isFinite(cell) ? cell : 0);
        } else if (cell && typeof cell === 'object' && 'quoteId' in cell) {
          const v = resolveQuoteValue(cell.quoteId);
          values.push(v !== null && Number.isFinite(v) ? v : 0);
        } else {
          values.push(0);
        }
      }
    }
  }
  return values;
}

/**
 * Build the row-major `{n_1, n_2, n_3, values}` 3D quote tensor envelope used
 * by SwaptionSabrCalibrateSpec.vols. Caller is responsible for resolving
 * quote cells via `flattenCubeForWire` first.
 */
export function toQuoteTensor3D(
  values: number[],
  nE: number,
  nT: number,
  nS: number,
): QuoteTensor3D {
  return { n_1: nE, n_2: nT, n_3: nS, values };
}

/**
 * Resolve the surface's own period axes. Prefers `axes_expiries`/`axes_tenors`;
 * falls back to converting the legacy `expiries`/`tenors: number[]` (years)
 * fields into Period objects with unit:'Years' so imports created before the
 * Period axes were added continue to wire correctly.
 *
 * Throws `VolSurfacePayloadError` when either axis is empty after fallback —
 * this replaces the silent zero-pad / `Empty list of points for term
 * structure` failure mode of the previous inline branch.
 */
export function getSurfaceAxes(surface: VolSurfaceSpec): { expiries: Period[]; tenors: Period[] } {
  const expiries = surface.axes_expiries && surface.axes_expiries.length > 0
    ? surface.axes_expiries.map(coercePeriodToInt)
    : legacyYearsToPeriods(surface.expiries);
  const tenors = surface.axes_tenors && surface.axes_tenors.length > 0
    ? surface.axes_tenors.map(coercePeriodToInt)
    : legacyYearsToPeriods(surface.tenors);
  if (expiries.length === 0) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" has no expiries on its axes. Populate axes_expiries before sampling/pricing.`
    );
  }
  if (tenors.length === 0) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" has no tenors on its axes. Populate axes_tenors before sampling/pricing.`
    );
  }
  return { expiries, tenors };
}

/**
 * Convert a fractional year-count into the smallest-unit integer Period that
 * represents it exactly (within 1e-6). Preference: Years > Months > Weeks >
 * Days, with a final fallback to nearest-day rounding.
 *
 * The engine's OpenAPI contract (`quantra_Period.n`) requires `n` to be a
 * 32-bit signed integer, mirroring
 * the FlatBuffers schema (`table Period { n: int; ... }`). This helper
 * exists so the legacy `expiries/tenors: number[]` (years) field — which can
 * carry fractional values like `1/12` for "1 month" — never produces a wire
 * payload with a fractional `n`. The previous direct `{ n: years, unit:
 * 'Years' }` mapping caused backend rejections.
 */
export function yearsToPeriod(years: number): Period {
  if (!Number.isFinite(years) || years <= 0) {
    return { n: 1, unit: 'Days' };
  }
  if (Number.isInteger(years)) return { n: years, unit: 'Years' };
  const candidates: Array<{ factor: number; unit: TimeUnit }> = [
    { factor: 12, unit: 'Months' },
    { factor: 52, unit: 'Weeks' },
    { factor: 365, unit: 'Days' },
  ];
  for (const c of candidates) {
    const v = years * c.factor;
    const rounded = Math.round(v);
    if (rounded > 0 && Math.abs(v - rounded) < 1e-6) {
      return { n: rounded, unit: c.unit };
    }
  }
  return { n: Math.max(1, Math.round(years * 365)), unit: 'Days' };
}

/**
 * Defensive `n: int` coercion for axes already typed as Period[]. A surface
 * imported from a JSON file with a fractional `n` (e.g. legacy `1/12`-year
 * entry that bypassed the editor) would otherwise wire through unchanged.
 * Always round to the nearest integer so the wire matches the OpenAPI
 * contract for `quantra_Period`.
 */
function coercePeriodToInt(p: Period): Period {
  if (Number.isInteger(p.n)) return p;
  // If the period is in Years and the value looks like a sub-year fraction
  // (e.g. someone dropped 0.25 in directly), promote to the matching
  // smaller unit instead of losing resolution by rounding to 0 / 1 Year.
  if (p.unit === 'Years' && p.n > 0 && p.n < 1) {
    return yearsToPeriod(p.n);
  }
  return { n: Math.max(1, Math.round(p.n)), unit: p.unit };
}

function legacyYearsToPeriods(years: number[] | undefined): Period[] {
  if (!years || years.length === 0) return [];
  return years
    .filter((y) => Number.isFinite(y) && y > 0)
    .map(yearsToPeriod);
}

/**
 * Build the `base` envelope sent inside a SwaptionVolSpec, mirroring the
 * defaults used by the prior inline VolWorkbench branch so byte-equality is
 * preserved for unchanged shapes.
 */
function buildBase(surface: VolSurfaceSpec, asOfDate: string, shape: VolShape): Record<string, unknown> {
  const base = surface.base || {};
  // Property insertion order matches the pre-refactor inline `{ ...base,
  // shape }` spread so JSON.stringify produces a byte-equal envelope for
  // unchanged shapes (regression guarded by volSurfacePayload.test.ts).
  const out: Record<string, unknown> = {
    reference_date: base.reference_date || asOfDate,
    calendar: base.calendar || 'TARGET',
    business_day_convention: base.business_day_convention || 'ModifiedFollowing',
    day_counter: base.day_counter || 'Actual365Fixed',
    constant_vol: base.constant_vol ?? 0.01,
    volatility_type: base.volatility_type || 'Normal',
  };
  if (base.displacement !== undefined) out.displacement = base.displacement;
  out.shape = shape;
  return out;
}

/**
 * Verify a MatrixCell[][] grid has exactly `nE` rows and `nT` columns. Returns
 * a short label like "alpha (2×3, expected 2×2)" when the shape is wrong,
 * otherwise null. Pure so the per-matrix error aggregator stays simple.
 */
export function checkMatrixShape(
  grid: MatrixCell[][] | undefined,
  nE: number,
  nT: number,
  label: string,
): string | null {
  if (!grid || grid.length === 0) return `${label} (missing)`;
  const rows = grid.length;
  const cols = grid[0]?.length ?? 0;
  if (rows !== nE || cols !== nT) return `${label} (${rows}×${cols}, expected ${nE}×${nT})`;
  for (let i = 0; i < rows; i += 1) {
    if ((grid[i]?.length ?? 0) !== nT) {
      return `${label} (ragged row ${i} of length ${grid[i]?.length ?? 0}, expected ${nT})`;
    }
  }
  return null;
}

/**
 * Verify every literal cell in the grid is finite. Quote cells are accepted
 * (they're resolved at wire-emit time). Returns the row/col of the first NaN
 * literal, or null if the grid is fully populated.
 */
function findNonFiniteLiteral(grid: MatrixCell[][]): { row: number; col: number } | null {
  for (let i = 0; i < grid.length; i += 1) {
    const row = grid[i] || [];
    for (let j = 0; j < row.length; j += 1) {
      const cell = row[j];
      if (typeof cell === 'number' && !Number.isFinite(cell)) {
        return { row: i, col: j };
      }
    }
  }
  return null;
}

/**
 * Verify a 3D MatrixCell cube has exactly nE × nT × nS shape. Returns a short
 * label like "vols (2×3×5, expected 2×3×4)" when the shape is wrong.
 */
export function checkCubeShape(
  cube: MatrixCell[][][] | undefined,
  nE: number,
  nT: number,
  nS: number,
  label: string,
): string | null {
  if (!cube || cube.length === 0) return `${label} (missing)`;
  const eDim = cube.length;
  const tDim = cube[0]?.length ?? 0;
  const sDim = cube[0]?.[0]?.length ?? 0;
  if (eDim !== nE || tDim !== nT || sDim !== nS) {
    return `${label} (${eDim}×${tDim}×${sDim}, expected ${nE}×${nT}×${nS})`;
  }
  for (let i = 0; i < eDim; i += 1) {
    if ((cube[i]?.length ?? 0) !== nT) {
      return `${label} (ragged expiry-row ${i} has ${cube[i]?.length ?? 0} tenor entries, expected ${nT})`;
    }
    for (let j = 0; j < tDim; j += 1) {
      if ((cube[i]?.[j]?.length ?? 0) !== nS) {
        return `${label} (ragged tenor-row at expiry ${i}, tenor ${j} has ${cube[i]?.[j]?.length ?? 0} strike entries, expected ${nS})`;
      }
    }
  }
  return null;
}

/**
 * Find the first cube cell that's a non-positive literal (NaN, -Inf, ≤ 0).
 * Quote cells are skipped (their resolved values are checked at wire-emit
 * time via the resolver). Mirrors the backend's
 * `_RejectsNonPositiveVols` test — every market vol must be > 0 because the
 * Hagan SABR formula's lognormal vol is positive by construction.
 */
function findNonPositiveCubeLiteral(
  cube: MatrixCell[][][],
): { e: number; t: number; s: number; value: number } | null {
  for (let i = 0; i < cube.length; i += 1) {
    const eRow = cube[i] || [];
    for (let j = 0; j < eRow.length; j += 1) {
      const tRow = eRow[j] || [];
      for (let k = 0; k < tRow.length; k += 1) {
        const cell = tRow[k];
        if (typeof cell === 'number') {
          if (!Number.isFinite(cell) || !(cell > 0)) {
            return { e: i, t: j, s: k, value: cell };
          }
        }
      }
    }
  }
  return null;
}

function buildAtmMatrixInner(
  surface: VolSurfaceSpec,
  asOfDate: string,
  resolveQuoteValue: ResolveQuoteFn,
): SwaptionInnerSpec {
  const { expiries, tenors } = getSurfaceAxes(surface);
  const nE = expiries.length;
  const nT = tenors.length;
  const shapeErr = checkMatrixShape(surface.grid, nE, nT, 'grid');
  if (shapeErr) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" AtmMatrix dimension mismatch: ${shapeErr}.`
    );
  }
  return {
    payload_type: 'SwaptionVolAtmMatrixSpec',
    payload: {
      base: buildBase(surface, asOfDate, 'AtmMatrix2D'),
      expiries,
      tenors,
      vols: toQuoteMatrix2D(
        flattenMatrixForWire(surface.grid, nE, nT, resolveQuoteValue),
        nE,
        nT,
      ),
    },
  };
}

function buildSabrParamsInner(
  surface: VolSurfaceSpec,
  asOfDate: string,
  resolveQuoteValue: ResolveQuoteFn,
): SwaptionInnerSpec {
  // SABR-params v1 is lognormal-only: the engine rejects Normal vol type
  // explicitly at parse time because the QuantLib
  // SabrSmileSection path is parameterised in lognormal terms. Catch it here
  // so users get a clear error before the request leaves the browser, rather
  // than discovering it via the 200 + per-query error pattern that
  // SampleVolSurfaces uses for top-level parse failures.
  const volType = surface.base?.volatility_type;
  if (volType === 'Normal') {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrParams requires Lognormal or ShiftedLognormal volatility type — Normal SABR is not supported. Switch the surface's volatility_type before sampling.`
    );
  }
  const { expiries, tenors } = getSurfaceAxes(surface);
  const nE = expiries.length;
  const nT = tenors.length;
  const issues: string[] = [];
  const alphaErr = checkMatrixShape(surface.sabr_alpha, nE, nT, 'alpha');
  const betaErr = checkMatrixShape(surface.sabr_beta, nE, nT, 'beta');
  const rhoErr = checkMatrixShape(surface.sabr_rho, nE, nT, 'rho');
  const nuErr = checkMatrixShape(surface.sabr_nu, nE, nT, 'nu');
  if (alphaErr) issues.push(alphaErr);
  if (betaErr) issues.push(betaErr);
  if (rhoErr) issues.push(rhoErr);
  if (nuErr) issues.push(nuErr);
  if (issues.length > 0) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrParams dimension mismatch: ${issues.join('; ')}.`
    );
  }
  const checkFinite = (grid: MatrixCell[][] | undefined, name: string) => {
    const nan = findNonFiniteLiteral(grid as MatrixCell[][]);
    if (nan) {
      throw new VolSurfacePayloadError(
        `Surface "${surface.id}" SabrParams "${name}" has a non-finite literal at row ${nan.row}, col ${nan.col}. Fill or switch the cell to a quote reference before emitting.`
      );
    }
  };
  checkFinite(surface.sabr_alpha, 'alpha');
  checkFinite(surface.sabr_beta, 'beta');
  checkFinite(surface.sabr_rho, 'rho');
  checkFinite(surface.sabr_nu, 'nu');
  return {
    payload_type: 'SwaptionSabrParamsSpec',
    payload: {
      base: buildBase(surface, asOfDate, 'SabrParams'),
      expiries,
      tenors,
      alpha: toQuoteMatrix2D(
        flattenMatrixForWire(surface.sabr_alpha, nE, nT, resolveQuoteValue),
        nE,
        nT,
      ),
      beta: toQuoteMatrix2D(
        flattenMatrixForWire(surface.sabr_beta, nE, nT, resolveQuoteValue),
        nE,
        nT,
      ),
      rho: toQuoteMatrix2D(
        flattenMatrixForWire(surface.sabr_rho, nE, nT, resolveQuoteValue),
        nE,
        nT,
      ),
      nu: toQuoteMatrix2D(
        flattenMatrixForWire(surface.sabr_nu, nE, nT, resolveQuoteValue),
        nE,
        nT,
      ),
    },
  };
}

/**
 * Build the SwaptionSabrCalibrateSpec inner envelope. Mirrors the shape of
 * `buildSabrParamsInner` but with the 3D market-vol cube and the calibration
 * options panel. Pre-flight rejections mirror the engine's parse-time guards
 * so users get clear errors before the network round-trip:
 *
 *  - Lognormal/ShiftedLognormal only (engine test
 *    `Swaption_SabrCalibrate_RejectsNormalVolType`).
 *  - OIS swap index rejected in v1 (engine test
 *    `_RejectsOisSwapIndex`). Detected here by the conventional `_OIS` suffix
 *    on `swapIndexId`; callers should also pre-check the underlying type.
 *  - Cube dimensions match axes_expiries × axes_tenors × axes_strikes
 *    (`_RejectsMismatchedDimensions`).
 *  - Strikes axis non-empty.
 *  - Every literal vol > 0 (`_RejectsNonPositiveVols`).
 *  - `weights` field is NOT exposed at the API level (backend test
 *    `_RejectsNonEmptyWeights`); we use `vega_weighted_smile_fit` flag instead.
 *
 * Note: the backend also enforces a minimum grid size (test
 * `_RejectsTooSmallGrid`). The exact threshold isn't in the OpenAPI; it
 * surfaces as a backend error if someone supplies a 1×1×1 cube. We don't
 * pre-check it client-side beyond requiring at least one strike, so the
 * backend message bubbles through unchanged.
 */
function buildSabrCalibrateInner(
  surface: VolSurfaceSpec,
  swapIndexId: string,
  asOfDate: string,
  resolveQuoteValue: ResolveQuoteFn,
): SwaptionInnerSpec {
  const volType = surface.base?.volatility_type;
  if (volType === 'Normal') {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrCalibrate requires Lognormal or ShiftedLognormal volatility type — Normal SABR is not supported. Switch the surface's volatility_type before calibrating.`
    );
  }
  // OIS swap-index rejection. The engine rejects SabrCalibrate against any
  // OisSwapIndex at parse time. The wire
  // helper only sees the swap-index id; the IborSwap/OisSwap distinction is
  // resolved in the swap-indices registry by kind. We use the conventional
  // `_OIS` suffix the existing portal code stamps onto OIS swap-index ids
  // (see VolWorkbench.swapIndexOptions: `${idx.id}_OIS` for Overnight indices)
  // as a cheap, reliable client-side guard. Callers that build their own
  // swap-index ids should also pre-check the underlying-leg type and surface
  // the rejection in the UI (Swaption.tsx OIS-leg case).
  if (typeof swapIndexId === 'string' && /_OIS$/i.test(swapIndexId)) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrCalibrate cannot calibrate against an OIS swap index (got "${swapIndexId}"). The backend rejects OIS underlyings for SabrCalibrate in v1; switch to an IborSwapIndex (e.g. EUR_SWAP_6M) or use a SabrParams surface.`
    );
  }
  const { expiries, tenors } = getSurfaceAxes(surface);
  const nE = expiries.length;
  const nT = tenors.length;
  const strikes = (surface.axes_strikes || []).filter((s) => Number.isFinite(s));
  if (strikes.length === 0) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrCalibrate has no strikes. Populate axes_strikes (spreads from per-node ATM forward, rate units) before calibrating.`
    );
  }
  // Strict-monotone strike check — the backend's XabrSwaptionVolatilityCube
  // assumes a sorted strike grid and trips an internal QL precondition when
  // strikes are duplicated or out of order.
  const sortedStrikes = [...strikes].sort((a, b) => a - b);
  for (let i = 1; i < sortedStrikes.length; i += 1) {
    if (!(sortedStrikes[i] > sortedStrikes[i - 1])) {
      throw new VolSurfacePayloadError(
        `Surface "${surface.id}" SabrCalibrate axes_strikes must be strictly increasing (found duplicate or out-of-order pair near ${sortedStrikes[i - 1]}, ${sortedStrikes[i]}).`
      );
    }
  }
  const nS = strikes.length;
  const cube = surface.sabr_market_vols;
  const cubeErr = checkCubeShape(cube, nE, nT, nS, 'vols');
  if (cubeErr) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrCalibrate dimension mismatch: ${cubeErr}.`
    );
  }
  const nonPos = findNonPositiveCubeLiteral(cube as MatrixCell[][][]);
  if (nonPos) {
    throw new VolSurfacePayloadError(
      `Surface "${surface.id}" SabrCalibrate has a non-positive market vol literal at expiry ${nonPos.e}, tenor ${nonPos.t}, strike ${nonPos.s} (value ${nonPos.value}). Every market vol must be > 0 (Hagan formula yields positive lognormal σ).`
    );
  }
  const opts = surface.sabr_calibration_options || {};
  const beta_fixed = opts.beta_fixed === true;
  const flatValues = flattenCubeForWire(cube, nE, nT, nS, resolveQuoteValue);
  const payload: {
    base: Record<string, unknown>;
    expiries: Period[];
    tenors: Period[];
    strikes: number[];
    vols: QuoteTensor3D;
    beta_fixed: boolean;
    beta_value?: number;
    vega_weighted_smile_fit?: boolean;
  } = {
    base: buildBase(surface, asOfDate, 'SabrCalibrate'),
    expiries,
    tenors,
    strikes: sortedStrikes,
    vols: toQuoteTensor3D(flatValues, nE, nT, nS),
    beta_fixed,
  };
  if (beta_fixed && opts.beta_value !== undefined) {
    payload.beta_value = opts.beta_value;
  }
  if (opts.vega_weighted_smile_fit === true) {
    payload.vega_weighted_smile_fit = true;
  }
  return {
    payload_type: 'SwaptionSabrCalibrateSpec',
    payload,
  };
}

/**
 * Build the wire payload for a swaption volatility surface, branching on
 * `surface.base.shape`. Constant shape (and any unknown shape) emits the
 * SwaptionVolConstantSpec envelope, matching the prior inline default.
 *
 * SmileCube3D currently masks as AtmMatrix2D on the wire (the stored
 * surface is a model spec; the cube is generated by the sampler) — same
 * behaviour as the pre-refactor inline branch.
 *
 * SabrCalibrate emits SwaptionSabrCalibrateSpec with the market vol cube and
 * calibration options. Pre-flight guards reject Normal vol, OIS swap index,
 * dimension mismatches, non-positive vols, and missing strikes axis before
 * the request leaves the browser.
 */
export function buildSwaptionVolWirePayload(
  surface: VolSurfaceSpec,
  swapIndexId: string,
  asOfDate: string,
  resolveQuoteValue: ResolveQuoteFn,
): SwaptionVolWirePayload {
  const shape: VolShape = (surface.base?.shape as VolShape) || 'Constant';
  let inner: SwaptionInnerSpec;
  if (shape === 'AtmMatrix2D') {
    inner = buildAtmMatrixInner(surface, asOfDate, resolveQuoteValue);
  } else if (shape === 'SmileCube3D') {
    // Stored surface is a model spec; sampler generates the cube. Mask as
    // AtmMatrix on the wire (existing behaviour, kept to avoid touching the
    // smile-cube pipeline in Step 1).
    inner = buildAtmMatrixInner(surface, asOfDate, resolveQuoteValue);
  } else if (shape === 'SabrParams') {
    inner = buildSabrParamsInner(surface, asOfDate, resolveQuoteValue);
  } else if (shape === 'SabrCalibrate') {
    inner = buildSabrCalibrateInner(surface, swapIndexId, asOfDate, resolveQuoteValue);
  } else {
    inner = {
      payload_type: 'SwaptionVolConstantSpec',
      payload: { base: buildBase(surface, asOfDate, 'Constant') },
    };
  }
  return {
    id: surface.id,
    payload_type: 'SwaptionVolSpec',
    payload: { swap_index_id: swapIndexId, ...inner },
  };
}
