import type { MatrixCell } from '../types/matrix2d';
import type { Period } from '../types';
import { volSurfaces as volSurfacesApi } from '../api/crud';
import { createBackendEntityCache } from './backendEntityCache';

export type VolatilityType = 'Normal' | 'Lognormal' | 'ShiftedLognormal';
export type VolPayloadType = 'SwaptionVolSpec' | 'OptionletVolSpec' | 'BlackVolSpec';
export type VolShape =
  | 'Constant'
  | 'AtmMatrix2D'
  | 'SmileCube3D'
  | 'SurfaceFromPrices'
  | 'SabrParams'
  | 'SabrCalibrate';

export interface VolSurfaceBase {
  reference_date?: string;
  calendar?: string;
  business_day_convention?: string;
  day_counter?: string;
  shape?: VolShape;
  volatility_type?: VolatilityType;
  displacement?: number;
  constant_vol?: number;
  quote_id?: string;
}

export interface VolCubeTensor {
  n_1: number;
  n_2: number;
  n_3: number;
  values: number[];
}

export interface VolSurfaceSnapshot {
  date: string;
  base?: VolSurfaceBase;
  expiries?: number[];
  tenors?: number[];
  strikes?: number[];
  strike_kind?: 'Absolute' | 'SpreadFromATM';
  cube_vols?: VolCubeTensor;
  // Each cell is a literal number or a {quoteId} reference. Legacy data
  // serialized as number[][] is structurally a MatrixCell[][] and continues
  // to load unchanged.
  grid?: MatrixCell[][];
}

export interface VolSurfaceSpec {
  id: string;
  payload_type: VolPayloadType;
  base: VolSurfaceBase;
  // TODO(step-2-cleanup): retire `expiries`/`tenors: number[]` (years) once
  // Swaption.tsx, IrSwap.tsx, and SwaptionModels.tsx are routed through
  // buildSwaptionVolWirePayload and use `axes_expiries`/`axes_tenors`. Kept
  // for one step so those pages keep doing closest-match year lookups.
  expiries?: number[]; // years
  tenors?: number[];   // years
  strikes?: number[];  // decimal strike/spread grid
  strike_kind?: 'Absolute' | 'SpreadFromATM';
  cube_vols?: VolCubeTensor; // flattened 3D vols tensor
  // Surface-owned period axes used by AtmMatrix2D and SabrParams payloads.
  // The wire helper sources expiries/tenors from these so the saved grid
  // dimensions cannot drift from what the sampler emits. Falls back to
  // converting legacy `expiries: number[]` (years) to Period[] when absent.
  axes_expiries?: Period[];
  axes_tenors?: Period[];
  // [expiry][tenor], decimal vols. Each cell is a literal number or a
  // {quoteId} reference; legacy number[][] data is a structural subtype.
  grid?: MatrixCell[][];
  // SABR-params: per-node grids, all shaped (axes_expiries × axes_tenors).
  // Each cell is a literal number or a {quoteId} reference.
  sabr_alpha?: MatrixCell[][];
  sabr_beta?: MatrixCell[][];
  sabr_rho?: MatrixCell[][];
  sabr_nu?: MatrixCell[][];
  // SabrCalibrate: market vol cube indexed [expiry][tenor][strike]. Each cell
  // is a literal number or a {quoteId} reference. Strike axis lives on
  // `axes_strikes` as absolute spreads from per-node ATM forward (rate units,
  // e.g. -0.02 = -200bp), matching quantra_SwaptionSabrCalibrateSpec.
  sabr_market_vols?: MatrixCell[][][];
  axes_strikes?: number[];
  sabr_calibration_options?: {
    beta_fixed?: boolean;
    beta_value?: number;
    // QuantLib 1.41 exposes vega-weighted smile fit only as a cube-level
    // boolean. Per-strike weights are NOT supported (the engine rejects a
    // non-empty `weights` field on SwaptionSabrCalibrateSpec).
    vega_weighted_smile_fit?: boolean;
  };
  // Cached SABR calibration diagnostics, populated after a /calibrate-swaption-vol
  // success. Field names mirror quantra_SwaptionVolDiagnostics so the cache
  // is byte-for-byte the response payload (modulo `cached_at`).
  sabr_last_calibration?: {
    cached_at: string;
    n_expiries: number;
    n_tenors: number;
    alpha_per_node: number[];
    beta_per_node: number[];
    rho_per_node: number[];
    nu_per_node: number[];
    forward_per_node?: number[];
    atm_vol_per_node?: number[];
    time_to_expiry_per_node?: number[];
    per_node_rmse: number[];
    per_node_max_abs_error?: number[];
    overall_rmse: number;
    converged?: boolean;
    strikes?: number[];
    per_strike_fit_error?: number[];
    warnings?: string[];
  };
  series?: VolSurfaceSnapshot[];
  quote_source?: 'quote_book' | 'manual';
  quote_template?: string;
  quote_ids?: string[][];
  // EquityBlack SurfaceFromPrices support
  surface_prices?: { n_rows: number; n_cols: number; values: number[] };
  surface_price_quote_ids?: string[]; // row-major optional quote ids for surface_prices
  price_expiries?: string[];
  price_strikes?: number[];
  price_option_type?: 'Call' | 'Put';
  spot?: number;
  spot_quote_id?: string;
  discount_curve_id?: string;
  use_flat_discount_rate?: boolean;
  flat_discount_rate?: number;
  dividend_curve_id?: string;
  use_flat_dividend_rate?: boolean;
  flat_dividend_rate?: number;
  surface_interpolator?: 'Bilinear' | 'Bicubic';
  createdAt?: string;
  updatedAt?: string;
}

// Backend-backed persistence: the backend vol-surface table is the single
// source of truth (scalar columns `name` / `kind`; the full spec rides in the
// JSONB `payload`). Local business ids stay the entity key (swaption models
// reference `vol_surface_id`, sourceRefs carry surface ids) and double as the
// backend row `name`. Resolution mode below remains a localStorage UI pref.
const VOL_SURFACE_RES_MODE_KEY = 'quantra_vol_surface_resolution_mode';

export type VolSurfaceResolutionMode = 'previous' | 'exact';

const cache = createBackendEntityCache<VolSurfaceSpec, any>({
  client: volSurfacesApi as any,
  fromApi(row) {
    const payload = (row.payload ?? {}) as Record<string, any>;
    const spec: VolSurfaceSpec = {
      ...payload,
      id: payload.local_id ?? row.name,
      payload_type: (row.kind as VolPayloadType) || payload.payload_type || 'BlackVolSpec',
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    } as VolSurfaceSpec;
    delete (spec as unknown as Record<string, unknown>).local_id;
    return spec;
  },
  toApi(surface) {
    return {
      name: surface.id,
      kind: surface.payload_type || 'BlackVolSpec',
      payload: { ...surface, local_id: surface.id },
    };
  },
  localId(surface) {
    return surface.id;
  },
});

export const refreshVolSurfaces = (): Promise<VolSurfaceSpec[]> => cache.refresh();
export const ensureVolSurfacesLoaded = (): Promise<VolSurfaceSpec[]> => cache.ensureLoaded();

/** Synchronous cache read (empty before the bootstrap preload). */
export function getVolSurfaces(): VolSurfaceSpec[] {
  return cache.getAll();
}


export async function saveVolSurface(surface: VolSurfaceSpec): Promise<VolSurfaceSpec> {
  return cache.save(surface);
}

export async function deleteVolSurface(id: string): Promise<void> {
  await cache.remove(id);
}

/** Delete every vol surface the caller owns from the backend (owner-scoped CRUD). */
export async function clearVolSurfaces(): Promise<void> {
  await cache.removeAll();
}

export function exportVolSurfaces(surfaces: VolSurfaceSpec[]) {
  const blob = new Blob([JSON.stringify(surfaces, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `vol-surfaces-${new Date().toISOString().split('T')[0]}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export async function importVolSurfaces(file: File): Promise<VolSurfaceSpec[]> {
  const text = await file.text();
  const data = JSON.parse(text);
  const surfaces: VolSurfaceSpec[] = Array.isArray(data) ? data : [];
  if (surfaces.length === 0) throw new Error('No valid volatility surfaces found in file');
  for (const surface of surfaces) {
    if (!surface?.id) continue;
    await saveVolSurface(surface);
  }
  return surfaces;
}

export function getVolSurfaceResolutionMode(): VolSurfaceResolutionMode {
  try {
    const mode = localStorage.getItem(VOL_SURFACE_RES_MODE_KEY);
    if (mode === 'exact' || mode === 'previous') return mode;
    return 'previous';
  } catch {
    return 'previous';
  }
}


export function resolveVolSurfaceSnapshot(
  surface: VolSurfaceSpec,
  asOfDate: string,
  mode: VolSurfaceResolutionMode = 'previous'
): VolSurfaceSnapshot | null {
  const series = surface.series || [];
  if (series.length === 0) return null;
  if (mode === 'exact') {
    return series.find(s => s.date === asOfDate) || null;
  }
  let result: VolSurfaceSnapshot | null = null;
  for (const snap of series.sort((a, b) => a.date.localeCompare(b.date))) {
    if (snap.date <= asOfDate) {
      result = snap;
    } else {
      break;
    }
  }
  return result;
}
