// Shared Quantra domain types + product/calendar constants.
//
// These were formerly exported from `quantra-api.ts` alongside the legacy cloud
// `quantraApi` client. That client (Firebase-auth, `api.quantra.io`) has been
// removed — every feature now talks to the orchestrator (`src/lib/api/`). The
// pure types and constants it carried live on here so the pricing services,
// result/error cards, curve charts, and feature pages keep a stable import.

// ============================================================================
// Error / result envelopes
// ============================================================================

// Structured error info surfaced to the UI. The orchestrator path builds this
// from the error envelope via `src/lib/api/errorEnvelope.ts` (branch on `code`,
// never on the prose `error` string).
export interface PricingErrorInfo {
  message: string;
  httpStatus: number;
  code?: number;
  codeName?: string;
  category: 'validation' | 'not_found' | 'unavailable' | 'auth' | 'server' | 'network' | 'client';
  raw?: unknown;
  // A short actionable "what to do next" derived from the envelope code
  // (never the prose), and the correlation id (support handle, greppable across
  // the stack). Both optional — populated by the orchestrator-path mapper.
  suggestion?: string;
  requestId?: string;
}

export interface PricingResult<T = unknown> {
  success: boolean;
  data?: T;
  error?: string;
  errorInfo?: PricingErrorInfo;
  duration_ms: number;
  // The correlation id the orchestrator client sent as `X-Request-ID`
  // for this call. Surfaced on the success path so a result that "works" (even
  // one with no cashflows) is still one click from its in-app trace.
  requestId?: string;
}

// ============================================================================
// Per-product result shapes
// ============================================================================

// Bootstrap / curve-preview response types
export interface CurveSeries {
  measure: 'DF' | 'ZERO' | 'FWD' | 'ZeroRate' | 'YoYRate' | string;
  values: number[];
}

export interface BootstrapCurveResult {
  id: string;
  reference_date: string;
  grid_dates: string[];
  series: CurveSeries[];
  pillar_dates?: string[];
  error?: { error_message: string };
}

export interface VolSurfaceSampleResult {
  vol_id: string;
  reference_date?: string;
  ql_vol_type?: string;
  requested_strike_axis?: 'AbsoluteStrike' | 'SpreadFromATM';
  canonical_strike_kind?: string;
  allow_extrapolation_used?: boolean;
  calendar_used?: string;
  business_day_convention_used?: string;
  expiry_kind?: string;
  expiries?: string[];
  requested_expiry_grid_points?: string[];
  tenors?: Array<{ n: number; unit: string }>;
  effective_swap_starts?: string[];
  effective_swap_ends?: string[];
  strikes?: number[];
  vols?: number[];
  n_expiries?: number;
  n_tenors?: number;
  n_strikes?: number;
  atm_levels?: number[];
  error?: { error_message?: string };
}

export interface SampleVolSurfacesResponse {
  results: VolSurfaceSampleResult[];
}

export interface CalibrateSwaptionModelResult {
  model_id: string;
  hw_a?: number;
  hw_sigma?: number;
  rmse?: number;
  num_helpers?: number;
  grid_rows?: number;
  grid_cols?: number;
  grid_points?: number;
}

/**
 * Calibration-specific diagnostics (quantra_SabrCalibrationDiagnostics).
 * Populated only for SabrCalibrate surfaces; absent for SabrParams.
 */
export interface SabrCalibrationDiagnostics {
  per_node_rmse?: number[];
  per_node_max_abs_error?: number[];
  overall_rmse?: number;
  converged?: boolean;
  iterations_per_node?: number[];
  strikes?: number[];
  per_strike_fit_error?: number[];
}

/**
 * Diagnostics block describing one swaption vol surface
 * (quantra_SwaptionVolDiagnostics). Emitted by /vol-surfaces/sample and
 * /price-swaption when include_diagnostics=true and as the response payload
 * of /calibrate-swaption-vol.
 */
export interface SwaptionVolDiagnostics {
  vol_id: string;
  kind?: string;
  expiries?: Array<{ n: number; unit: string }>;
  tenors?: Array<{ n: number; unit: string }>;
  n_expiries?: number;
  n_tenors?: number;
  forward_per_node?: number[];
  atm_vol_per_node?: number[];
  time_to_expiry_per_node?: number[];
  alpha_per_node?: number[];
  beta_per_node?: number[];
  rho_per_node?: number[];
  nu_per_node?: number[];
  calibration?: SabrCalibrationDiagnostics;
  warnings?: string[];
}

/**
 * Response from POST /v1/calibrate-swaption-vol — a thin envelope around a
 * single SwaptionVolDiagnostics block.
 */
export interface CalibrateSwaptionVolResponse {
  vol_id: string;
  diagnostics: SwaptionVolDiagnostics;
}

// ============================================================================
// Calendar
// ============================================================================

export const CALENDAR_NAMES = [
  'Argentina',
  'Australia',
  'BespokeCalendar',
  'Brazil',
  'Canada',
  'China',
  'CzechRepublic',
  'Denmark',
  'Finland',
  'Germany',
  'HongKong',
  'Hungary',
  'Iceland',
  'India',
  'Indonesia',
  'Israel',
  'Italy',
  'Japan',
  'Mexico',
  'NewZealand',
  'Norway',
  'NullCalendar',
  'Poland',
  'Romania',
  'Russia',
  'SaudiArabia',
  'Singapore',
  'Slovakia',
  'SouthAfrica',
  'SouthKorea',
  'Sweden',
  'Switzerland',
  'TARGET',
  'Taiwan',
  'Turkey',
  'Ukraine',
  'UnitedKingdom',
  'UnitedStates',
  'UnitedStatesGovernmentBond',
  'UnitedStatesNERC',
  'UnitedStatesNYSE',
  'UnitedStatesSettlement',
  'WeekendsOnly',
] as const;

export type CalendarName = (typeof CALENDAR_NAMES)[number];
