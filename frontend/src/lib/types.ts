// Types for curves, market data, quotes, and curve sets
// Aligned with the full Quantra OpenAPI 3.0 schema

// ============================================================================
// Enums (matching quantra_enums_*)
// ============================================================================

export type DayCounter =
  | 'Actual360' | 'Actual365Fixed' | 'Actual365NoLeap'
  | 'ActualActual' | 'ActualActualISMA' | 'ActualActualBond'
  | 'ActualActualISDA' | 'ActualActualHistorical' | 'ActualActual365'
  | 'ActualActualAFB' | 'ActualActualEuro'
  | 'Business252' | 'One' | 'Simple' | 'Thirty360';

export type Interpolator = 'BackwardFlat' | 'ForwardFlat' | 'Linear' | 'LogCubic' | 'LogLinear';

export type BootstrapTrait =
  | 'Discount' | 'FwdRate' | 'InterpolatedDiscount'
  | 'InterpolatedFwd' | 'InterpolatedZero' | 'ZeroRate';

export type BusinessDayConvention =
  | 'Following' | 'HalfMonthModifiedFollowing' | 'ModifiedFollowing'
  | 'ModifiedPreceding' | 'Nearest' | 'Preceding' | 'Unadjusted';

export type Calendar =
  | 'Argentina' | 'Australia' | 'BespokeCalendar' | 'Brazil' | 'Canada'
  | 'China' | 'CzechRepublic' | 'Denmark' | 'Finland' | 'Germany'
  | 'HongKong' | 'Hungary' | 'Iceland' | 'India' | 'Indonesia'
  | 'Israel' | 'Italy' | 'Japan' | 'Mexico' | 'NewZealand'
  | 'Norway' | 'NullCalendar' | 'Poland' | 'Romania' | 'Russia'
  | 'SaudiArabia' | 'Singapore' | 'Slovakia' | 'SouthAfrica' | 'SouthKorea'
  | 'Sweden' | 'Switzerland' | 'TARGET' | 'Taiwan' | 'Turkey'
  | 'Ukraine' | 'UnitedKingdom' | 'UnitedStates' | 'UnitedStatesGovernmentBond'
  | 'UnitedStatesNERC' | 'UnitedStatesNYSE' | 'UnitedStatesSettlement'
  | 'WeekendsOnly';

export type TimeUnit = 'Days' | 'Hours' | 'Microseconds' | 'Milliseconds' | 'Minutes' | 'Months' | 'Seconds' | 'Weeks' | 'Years';

export type Compounding = 'Compounded' | 'Continuous' | 'Simple' | 'SimpleThenCompounded';

export type Frequency =
  | 'Annual' | 'Bimonthly' | 'Biweekly' | 'Daily'
  | 'EveryFourthMonth' | 'EveryFourthWeek' | 'Monthly'
  | 'NoFrequency' | 'Once' | 'OtherFrequency'
  | 'Quarterly' | 'Semiannual' | 'Weekly';

export type DateGenerationRule =
  | 'Backward' | 'CDS' | 'Forward' | 'OldCDS'
  | 'ThirdWednesday' | 'Twentieth' | 'TwentiethIMM' | 'Zero';

export type OvernightIndex = 'SOFR' | 'ESTR' | 'SONIA' | 'FedFunds' | 'EONIA';
export type IborFamily = 'Euribor' | 'USDLibor' | 'GBPLibor' | 'JPYLibor' | 'Tibor';
export type QuoteKind = 'Rate' | 'Spread' | 'Price' | 'FuturesPrice' | 'FxSpot' | 'FxPoints';
export type QuoteType = 'Curve' | 'Volatility' | 'Credit';
export type CurveRole = 'discount' | 'forward' | 'basis' | 'inflation';
export interface Period {
  n: number;
  unit: TimeUnit;
}

export type InflationCurveKind = 'ZeroInflation' | 'YoYInflation';
export type InflationCurveMeasure = 'ZeroRate' | 'YoYRate';
export type CPIInterpolationType = 'AsIndex' | 'Flat' | 'Linear';

// ============================================================================
// Quote Registry
// ============================================================================

export interface QuoteSpec {
  id: string;
  kind: QuoteKind;
  value: number;
  quote_type?: QuoteType;
  description?: string;
  label?: string;
  currency?: string;
  createdAt?: string;
  updatedAt?: string;
}

// ============================================================================
// IndexDef / IndexRef — data-driven index system (matches OpenAPI spec)
// ============================================================================

export type IndexType = 'Ibor' | 'Overnight';

export interface IndexFixing {
  date: string;
  value: number;
}

export interface IndexDef {
  id: string;
  /** Human-readable name / QuantLib familyName (e.g. "Euribor", "SOFR"). Required by API. */
  name: string;
  index_type: IndexType;
  currency: string;
  // New Period table shape
  tenor?: Period;
  // Legacy tenor fields kept for local data compatibility
  tenor_number?: number;
  tenor_time_unit?: TimeUnit;
  // Common conventions
  fixing_days: number;
  calendar: Calendar;
  day_counter: DayCounter;
  business_day_convention?: BusinessDayConvention;  // Ibor only
  end_of_month?: boolean;                           // Ibor only
  /** Past fixings needed for seasoned instruments */
  fixings?: IndexFixing[];
}

export interface IndexRef {
  id: string;  // references IndexDef.id
}

// ============================================================================
// Helper Dependencies
// ============================================================================

export interface HelperDependencies {
  discount_curve?: { id: string };
  projection_curve?: { id: string };
  projection_curve_2?: { id: string };
  fx_spot_quote_id?: string;
}

// ============================================================================
// Curve Point / Helper Types
// ============================================================================

export interface DepositHelper {
  point_type: 'DepositHelper';
  point: {
    rate?: number;
    quote_id?: string;
    tenor_number: number;
    tenor_time_unit: TimeUnit;
    fixing_days: number;
    calendar: Calendar;
    business_day_convention: BusinessDayConvention;
    day_counter: DayCounter;
  };
}

export interface SwapHelper {
  point_type: 'SwapHelper';
  point: {
    rate?: number;
    quote_id?: string;
    tenor_number: number;
    tenor_time_unit: TimeUnit;
    calendar: Calendar;
    sw_fixed_leg_frequency: Frequency;
    sw_fixed_leg_convention: BusinessDayConvention;
    sw_fixed_leg_day_counter: DayCounter;
    /** IndexRef — required by API. References an IndexDef.id */
    float_index?: IndexRef;
    spread?: number;
    fwd_start_days?: number;
    deps?: HelperDependencies;
  };
}

export interface FRAHelper {
  point_type: 'FRAHelper';
  point: {
    rate?: number;
    quote_id?: string;
    months_to_start: number;
    months_to_end: number;
    fixing_days: number;
    calendar: Calendar;
    business_day_convention: BusinessDayConvention;
    day_counter: DayCounter;
  };
}

export interface FutureHelper {
  point_type: 'FutureHelper';
  point: {
    rate?: number;
    quote_id?: string;
    future_start_date: string;
    future_months: number;
    calendar: Calendar;
    business_day_convention: BusinessDayConvention;
    day_counter: DayCounter;
    futures_price?: number;
    convexity_adjustment?: number;
  };
}

export interface BondHelper {
  point_type: 'BondHelper';
  point: {
    rate?: number;
    quote_id?: string;
    price?: number;
    settlement_days: number;
    face_amount: number;
    coupon_rate: number;
    day_counter: DayCounter;
    business_day_convention: BusinessDayConvention;
    redemption: number;
    issue_date: string;
    schedule: {
      calendar: Calendar;
      effective_date: string;
      termination_date: string;
      frequency: Frequency;
      convention: BusinessDayConvention;
      termination_date_convention: BusinessDayConvention;
      date_generation_rule: DateGenerationRule;
      end_of_month?: boolean;
    };
  };
}

/** OIS overnight-coupon averaging (engine >= 0.6): 'Compound' | 'Simple'. */
export type OvernightAveragingMethod = 'Compound' | 'Simple';
export const OVERNIGHT_AVERAGING_METHODS: OvernightAveragingMethod[] = ['Compound', 'Simple'];

export interface OISHelper {
  point_type: 'OISHelper';
  point: {
    rate?: number;
    quote_id?: string;
    tenor_number: number;
    tenor_time_unit: TimeUnit;
    /** IndexRef — required by API. References an IndexDef.id for the overnight index */
    overnight_index?: IndexRef;
    settlement_days?: number;
    calendar?: Calendar;
    fixed_leg_frequency?: Frequency;
    fixed_leg_convention?: BusinessDayConvention;
    fixed_leg_day_counter?: DayCounter;
    /** Business days from accrual end to payment (engine >= 0.6; legacy default 0). */
    payment_lag?: number;
    /** Overnight averaging: Compound | Simple (engine >= 0.6; legacy default Compound). */
    averaging_method?: OvernightAveragingMethod;
    /** Rate observation lookback in days; 0 = none (engine >= 0.6). */
    lookback_days?: number;
    /** Rate lockout in days at period end; 0 = none (engine >= 0.6). */
    lockout_days?: number;
    /** Apply observation shift with lookback (engine >= 0.6; legacy default false). */
    apply_observation_shift?: boolean;
    deps?: HelperDependencies;
  };
}

export interface DatedOISHelper {
  point_type: 'DatedOISHelper';
  point: {
    rate?: number;
    quote_id?: string;
    start_date: string;
    end_date: string;
    /** IndexRef — required by API. References an IndexDef.id for the overnight index */
    overnight_index?: IndexRef;
    settlement_days?: number;
    calendar?: Calendar;
    /** Fixed leg payment frequency (engine >= 0.6; engine previously hardcoded Annual). */
    fixed_leg_frequency?: Frequency;
    fixed_leg_convention?: BusinessDayConvention;
    fixed_leg_day_counter?: DayCounter;
    /** Business days from accrual end to payment (engine >= 0.6; legacy default 0). */
    payment_lag?: number;
    /** Overnight averaging: Compound | Simple (engine >= 0.6; legacy default Compound). */
    averaging_method?: OvernightAveragingMethod;
    /** Rate observation lookback in days; 0 = none (engine >= 0.6). */
    lookback_days?: number;
    /** Rate lockout in days at period end; 0 = none (engine >= 0.6). */
    lockout_days?: number;
    /** Apply observation shift with lookback (engine >= 0.6; legacy default false). */
    apply_observation_shift?: boolean;
    deps?: HelperDependencies;
  };
}

export type CurvePoint = DepositHelper | SwapHelper | FRAHelper | FutureHelper | BondHelper | OISHelper | DatedOISHelper;
export type PointType = CurvePoint['point_type'];

// ============================================================================
// Value-based curve points (engine 0.5.0 Interpolated* families)
// ============================================================================
// A value curve carries explicit numbers (zero rates / discount factors /
// forward rates) at pillars instead of bootstrappable instruments. The pillar
// is EITHER an explicit ISO `date` or a tenor from the curve reference date
// (stored flat as `tenor_number`/`tenor_time_unit`, normalized to the nested
// wire `tenor` by `normalizeCurvePointForApi`, same as helper points).
// Exactly one of the inline value | `quote_id` per point (backend 422
// `ambiguous_value_source` / `missing_value_source` otherwise).

interface ValuePointBase {
  /** Explicit pillar date (ISO). Wins over the tenor when both are set. */
  date?: string;
  tenor_number?: number;
  tenor_time_unit?: TimeUnit;
  /** MD catalog reference — resolved server-side, never sent to the engine. */
  quote_id?: string;
}

export interface ZeroRatePoint {
  point_type: 'ZeroRatePoint';
  point: ValuePointBase & {
    /** Decimal zero rate (0.025 = 2.5%). */
    zero_rate?: number;
    /** One quoting convention for the WHOLE curve (backend enforces uniformity). */
    compounding?: Compounding;
    frequency?: Frequency;
  };
}

export interface DiscountFactorPoint {
  point_type: 'DiscountFactorPoint';
  point: ValuePointBase & {
    /** Raw discount factor in (0, 1]; the first pillar must be 1.0 at the reference date. */
    discount_factor?: number;
  };
}

export interface ForwardRatePoint {
  point_type: 'ForwardRatePoint';
  point: ValuePointBase & {
    /** Decimal instantaneous forward rate. */
    forward_rate?: number;
  };
}

export type ValueCurvePoint = ZeroRatePoint | DiscountFactorPoint | ForwardRatePoint;
export type ValuePointType = ValueCurvePoint['point_type'];

/** Any point a curve row may carry: a rate helper OR a value point. */
export type AnyCurvePoint = CurvePoint | ValueCurvePoint;

export const VALUE_POINT_TYPES: ValuePointType[] = [
  'ZeroRatePoint',
  'DiscountFactorPoint',
  'ForwardRatePoint',
];

export function isValueCurvePoint(point: AnyCurvePoint): point is ValueCurvePoint {
  return (VALUE_POINT_TYPES as string[]).includes(point.point_type);
}

// ============================================================================
// Curve Definition
// ============================================================================

export interface Curve {
  id: string;
  name: string;
  description?: string;
  currency: string;
  role: CurveRole;
  day_counter: DayCounter;
  interpolator: Interpolator;
  bootstrap_trait: BootstrapTrait;
  reference_date: string;
  points: AnyCurvePoint[];
  discount_curve_id?: string;
  inflation_curve?: InflationCurveConfig;
  createdAt: string;
  updatedAt: string;
}

export interface InflationIndexSpec {
  id: string;
  family_name: string;
  currency: string;
  calendar: Calendar;
  day_counter: DayCounter;
  frequency: Frequency;
  availability_lag: Period;
  observation_lag: Period;
  interpolated: boolean;
  revised: boolean;
  kind: InflationCurveKind;
  underlying_zero_index_id?: string;
  fixings?: Array<{ date: string; value: number }>;
}

export interface InflationHelperBase {
  quote_value?: number;
  quote_id?: string;
  swap_observation_lag: Period;
  tenor?: Period;
  start_date?: string;
  end_date?: string;
  calendar: Calendar;
  payment_convention: BusinessDayConvention;
  day_counter: DayCounter;
  observation_interpolation: CPIInterpolationType;
  nominal_curve_id?: string;
}

export interface ZeroCouponInflationSwapHelperPoint {
  point_type: 'ZeroCouponInflationSwapHelper';
  point: InflationHelperBase;
}

export interface YearOnYearInflationSwapHelperPoint {
  point_type: 'YearOnYearInflationSwapHelper';
  point: InflationHelperBase & { nominal_curve_id?: string };
}

export type InflationPointWrapper = ZeroCouponInflationSwapHelperPoint | YearOnYearInflationSwapHelperPoint;

export interface LegacyInflationSwapPillar {
  maturity: Period;
  quote_value?: number;
  quote_id?: string;
}

export interface InflationCurveConfig {
  kind: InflationCurveKind;
  index: InflationIndexSpec;
  calendar: Calendar;
  business_day_convention: BusinessDayConvention;
  day_counter: DayCounter;
  interpolator: Interpolator;
  allow_extrapolation: boolean;
  bootstrap_accuracy?: number;
  points: InflationPointWrapper[];
  // Legacy import compat only
  payload_type?: string;
  pillars?: LegacyInflationSwapPillar[];
}

// ============================================================================
// Curve Set
// ============================================================================

export interface CurveSet {
  id: string;
  name: string;
  description?: string;
  currency: string;
  as_of_date: string;
  curve_refs: CurveSetCurveRef[];
  credit_curve_ids?: string[];
  quote_ids: string[];
  createdAt: string;
  updatedAt: string;
  // Legacy compat only
  curves?: Curve[];
}

export interface CurveSetCurveRef {
  id: string;
  curve_id: string;
  role: CurveRole;
  label?: string;
}

// ============================================================================
// Defaults
// ============================================================================

export const DEFAULT_DEPOSIT_HELPER: DepositHelper = {
  point_type: 'DepositHelper',
  point: {
    rate: 0.04, tenor_number: 3, tenor_time_unit: 'Months', fixing_days: 2,
    calendar: 'TARGET', business_day_convention: 'ModifiedFollowing', day_counter: 'Actual360',
  },
};

export const DEFAULT_SWAP_HELPER: SwapHelper = {
  point_type: 'SwapHelper',
  point: {
    rate: 0.035, tenor_number: 5, tenor_time_unit: 'Years', calendar: 'TARGET',
    sw_fixed_leg_frequency: 'Annual', sw_fixed_leg_convention: 'ModifiedFollowing',
    sw_fixed_leg_day_counter: 'Thirty360',
    float_index: { id: 'EURIBOR_6M' },
    spread: 0, fwd_start_days: 0,
  },
};

export const DEFAULT_FRA_HELPER: FRAHelper = {
  point_type: 'FRAHelper',
  point: {
    rate: 0.04, months_to_start: 3, months_to_end: 6, fixing_days: 2,
    calendar: 'TARGET', business_day_convention: 'ModifiedFollowing', day_counter: 'Actual360',
  },
};

export const DEFAULT_FUTURE_HELPER: FutureHelper = {
  point_type: 'FutureHelper',
  point: {
    rate: 96.0, future_start_date: new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
    future_months: 3, calendar: 'TARGET', business_day_convention: 'ModifiedFollowing', day_counter: 'Actual360',
  },
};

export const DEFAULT_BOND_HELPER: BondHelper = {
  point_type: 'BondHelper',
  point: {
    rate: 100.0, settlement_days: 2, face_amount: 100, coupon_rate: 0.05,
    day_counter: 'Thirty360', business_day_convention: 'ModifiedFollowing', redemption: 100,
    issue_date: new Date().toISOString().split('T')[0],
    schedule: {
      calendar: 'TARGET', effective_date: new Date().toISOString().split('T')[0],
      termination_date: new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
      frequency: 'Annual', convention: 'ModifiedFollowing', termination_date_convention: 'ModifiedFollowing',
      date_generation_rule: 'Forward',
    },
  },
};

export const DEFAULT_OIS_HELPER: OISHelper = {
  point_type: 'OISHelper',
  point: {
    rate: 0.035, tenor_number: 5, tenor_time_unit: 'Years',
    overnight_index: { id: 'ESTR' },
    settlement_days: 2, calendar: 'TARGET', fixed_leg_frequency: 'Annual',
    fixed_leg_convention: 'ModifiedFollowing', fixed_leg_day_counter: 'Actual360',
    payment_lag: 0, averaging_method: 'Compound',
    lookback_days: 0, lockout_days: 0, apply_observation_shift: false,
  },
};

export const DEFAULT_DATED_OIS_HELPER: DatedOISHelper = {
  point_type: 'DatedOISHelper',
  point: {
    rate: 0.035,
    start_date: new Date().toISOString().split('T')[0],
    end_date: new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
    overnight_index: { id: 'ESTR' },
    settlement_days: 2, calendar: 'TARGET', fixed_leg_frequency: 'Annual',
    fixed_leg_convention: 'ModifiedFollowing', fixed_leg_day_counter: 'Actual360',
    payment_lag: 0, averaging_method: 'Compound',
    lookback_days: 0, lockout_days: 0, apply_observation_shift: false,
  },
};

export const DEFAULT_INFLATION_CURVE: InflationCurveConfig = {
  kind: 'ZeroInflation',
  index: {
    id: 'EUHICP',
    family_name: 'EU HICP',
    currency: 'EUR',
    calendar: 'TARGET',
    day_counter: 'Actual365Fixed',
    frequency: 'Monthly',
    availability_lag: { n: 2, unit: 'Months' },
    observation_lag: { n: 3, unit: 'Months' },
    interpolated: true,
    revised: false,
    kind: 'ZeroInflation',
  },
  calendar: 'TARGET',
  business_day_convention: 'ModifiedFollowing',
  day_counter: 'Actual365Fixed',
  interpolator: 'Linear',
  allow_extrapolation: true,
  bootstrap_accuracy: 1.0e-12,
  points: [
    {
      point_type: 'ZeroCouponInflationSwapHelper',
      point: {
        tenor: { n: 1, unit: 'Years' },
        quote_value: 0.02,
        swap_observation_lag: { n: 3, unit: 'Months' },
        calendar: 'TARGET',
        payment_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
        observation_interpolation: 'AsIndex',
      },
    },
    {
      point_type: 'ZeroCouponInflationSwapHelper',
      point: {
        tenor: { n: 2, unit: 'Years' },
        quote_value: 0.021,
        swap_observation_lag: { n: 3, unit: 'Months' },
        calendar: 'TARGET',
        payment_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
        observation_interpolation: 'AsIndex',
      },
    },
  ],
};

// ============================================================================
// Dropdown options
// ============================================================================

export const DAY_COUNTERS: DayCounter[] = [
  'Actual360', 'Actual365Fixed', 'Thirty360', 'ActualActual', 'ActualActualISDA',
  'ActualActualISMA', 'ActualActualBond', 'ActualActualHistorical',
  'ActualActual365', 'ActualActualAFB', 'ActualActualEuro',
  'Actual365NoLeap', 'Business252', 'One', 'Simple',
];

export const INTERPOLATORS: Interpolator[] = ['Linear', 'LogLinear', 'LogCubic', 'BackwardFlat', 'ForwardFlat'];

export const BOOTSTRAP_TRAITS: BootstrapTrait[] = [
  'Discount', 'ZeroRate', 'FwdRate', 'InterpolatedDiscount', 'InterpolatedZero', 'InterpolatedFwd',
];

/** Traits available in "Bootstrap from instruments" mode (the Interpolated*
 * entries were dead there — they belong to the values construction mode). */
export const HELPER_BOOTSTRAP_TRAITS: BootstrapTrait[] = ['Discount', 'ZeroRate', 'FwdRate'];

/** Traits that mean "Interpolate given values" (value-based construction). */
export const VALUE_BOOTSTRAP_TRAITS: BootstrapTrait[] = [
  'InterpolatedZero', 'InterpolatedDiscount', 'InterpolatedFwd',
];

export function isValueBootstrapTrait(trait: string | undefined): boolean {
  return !!trait && (VALUE_BOOTSTRAP_TRAITS as string[]).includes(trait);
}

export const COMPOUNDINGS: Compounding[] = [
  'Continuous', 'Simple', 'Compounded', 'SimpleThenCompounded',
];

export const BUSINESS_DAY_CONVENTIONS: BusinessDayConvention[] = [
  'Following', 'ModifiedFollowing', 'Preceding', 'Unadjusted',
  'HalfMonthModifiedFollowing', 'ModifiedPreceding', 'Nearest',
];

export const CALENDARS: Calendar[] = [
  'TARGET', 'UnitedStates', 'UnitedStatesGovernmentBond', 'UnitedKingdom', 'Japan', 'Germany',
  'Switzerland', 'Canada', 'Australia', 'Italy', 'Brazil', 'China',
  'NullCalendar', 'WeekendsOnly',
];

export const TIME_UNITS: TimeUnit[] = ['Days', 'Weeks', 'Months', 'Years'];

export const FREQUENCIES: Frequency[] = [
  'Annual', 'Semiannual', 'Quarterly', 'Monthly', 'Weekly', 'Daily', 'Once', 'NoFrequency',
];

export const DATE_GENERATION_RULES: DateGenerationRule[] = [
  'Forward', 'Backward', 'CDS', 'OldCDS', 'ThirdWednesday', 'Twentieth', 'TwentiethIMM', 'Zero',
];

export const OVERNIGHT_INDICES: OvernightIndex[] = ['SOFR', 'ESTR', 'SONIA', 'FedFunds', 'EONIA'];

export const IBOR_FAMILIES: IborFamily[] = ['Euribor', 'USDLibor', 'GBPLibor', 'JPYLibor', 'Tibor'];

export const CPI_INTERPOLATION_TYPES: CPIInterpolationType[] = ['AsIndex', 'Flat', 'Linear'];

export const CURRENCIES = ['EUR', 'USD', 'GBP', 'JPY', 'CHF', 'AUD', 'CAD', 'SEK', 'NOK', 'DKK'];

export const CURVE_ROLES: { value: CurveRole; label: string; description: string }[] = [
  { value: 'discount', label: 'Discount (OIS)', description: 'Used for discounting cash flows' },
  { value: 'forward', label: 'Forward (IBOR)', description: 'Used for projecting floating rates' },
  { value: 'basis', label: 'Basis', description: 'Basis spread curve' },
  { value: 'inflation', label: 'Inflation', description: 'Inflation curve bootstrapped from inflation swap pillars' },
];

// ============================================================================
// Helpers
// ============================================================================

export function getPointRate(point: AnyCurvePoint, quotes: QuoteSpec[]): number | null {
  const p = point.point as any;
  if (p.quote_id) {
    const q = quotes.find(q => q.id === p.quote_id);
    return q ? q.value : null;
  }
  return p.rate ?? p.price ?? p.zero_rate ?? p.discount_factor ?? p.forward_rate ?? null;
}

export function getPointTenorLabel(point: AnyCurvePoint): string {
  const p = point.point as any;
  switch (point.point_type) {
    case 'DepositHelper': return `${p.tenor_number}${(p.tenor_time_unit || '')[0] || ''}`;
    case 'SwapHelper': return `${p.tenor_number}${(p.tenor_time_unit || '')[0] || ''}`;
    case 'FRAHelper': return `${p.months_to_start}x${p.months_to_end}`;
    case 'FutureHelper': return `${p.future_months}M Fut`;
    case 'BondHelper': return 'Bond';
    case 'OISHelper': return `${p.tenor_number}${(p.tenor_time_unit || '')[0] || ''} OIS`;
    case 'DatedOISHelper': return `Dated OIS`;
    case 'ZeroRatePoint':
    case 'DiscountFactorPoint':
    case 'ForwardRatePoint':
      return p.date || `${p.tenor_number ?? '?'}${(p.tenor_time_unit || '')[0] || ''}`;
    default: return '?';
  }
}

/** Get the index ref id from a curve point (SwapHelper→float_index, OIS→overnight_index) */
export function getPointIndexRefId(point: AnyCurvePoint): string | undefined {
  const p = point.point as any;
  // SwapHelper uses float_index
  if (p.float_index?.id) return p.float_index.id;
  // OISHelper/DatedOISHelper use overnight_index (now IndexRef, not enum)
  if (p.overnight_index?.id) return p.overnight_index.id;
  return undefined;
}

/** Collect all unique index_ref IDs from a list of curve points */
export function collectIndexRefIds(points: AnyCurvePoint[]): string[] {
  const ids = new Set<string>();
  for (const pt of points) {
    const id = getPointIndexRefId(pt);
    if (id) ids.add(id);
  }
  return Array.from(ids);
}
