import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import {
  type BusinessDayConvention,
  BUSINESS_DAY_CONVENTIONS,
  CALENDARS,
  CPI_INTERPOLATION_TYPES,
  type CPIInterpolationType,
  type Curve,
  DAY_COUNTERS,
  DATE_GENERATION_RULES,
  FREQUENCIES,
  type Frequency,
  type IndexDef,
  type InflationIndexSpec,
  type InflationCurveConfig,
  type InflationCurveKind,
  type Period,
  type TimeUnit,
  TIME_UNITS,
  collectIndexRefIds,
} from '../../lib/types';
import { type PricingErrorInfo } from '../../lib/quantra-types';
import { priceSwapsInflation } from '../../lib/api/swapsInflationPricingService';
import {
  persistSwapsInflationGraph,
  buildSwapsInflationPriceArm,
  asSwapsInflationAppGraph,
  type SwapsInflationAppGraph,
  type CurveGraphInput,
} from '../../lib/api/swapsInflationSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import PricingErrorCard from '../../components/products/PricingErrorCard';
import PricingTraceLink from '../../components/products/PricingTraceLink';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { buildInflationCurveSpec, buildInflationIndexSpec } from '../../lib/inflation-curves';
import { deriveInflationSwapName, getInflationSwapEntryById, saveInflationSwap, type InflationSwapRequest } from '../../lib/storage/inflationSwaps';
import { buildSourceRefs } from '../../lib/storage/sourceRefs';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { useAuth } from '../../hooks/useAuth';
import InflationIndexPicker from '../../components/curves/InflationIndexPicker';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

type InflationSwapKind = 'ZeroCoupon' | 'YearOnYear';
type SwapSide = 'Payer' | 'Receiver';

interface ScheduleParams {
  effectiveDate: string;
  terminationDate: string;
  calendar: string;
  frequency: string;
  convention: string;
  terminationDateConvention: string;
  dateGenerationRule: string;
}

interface InflationSwapResultData {
  npv?: number;
  fair_rate?: number;
  fair_spread?: number;
  fixed_leg_bps?: number;
  yoy_leg_bps?: number;
  fixed_leg_npv?: number;
  inflation_leg_npv?: number;
  yoy_leg_npv?: number;
  fixed_leg_flows?: Array<Record<string, unknown>>;
  inflation_leg_flows?: Array<Record<string, unknown>>;
  yoy_leg_flows?: Array<Record<string, unknown>>;
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function toLocalDate(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  return /^\d{4}\/\d{2}\/\d{2}$/.test(value) ? value.replace(/\//g, '-') : value;
}

function scheduleToApi(schedule: ScheduleParams) {
  return {
    effective_date: schedule.effectiveDate,
    termination_date: schedule.terminationDate,
    calendar: schedule.calendar,
    frequency: schedule.frequency,
    convention: schedule.convention,
    termination_date_convention: schedule.terminationDateConvention,
    date_generation_rule: schedule.dateGenerationRule,
  };
}

function mapScheduleFromApi(raw: any, fallback: ScheduleParams): ScheduleParams {
  return {
    effectiveDate: toLocalDate(raw?.effective_date, fallback.effectiveDate),
    terminationDate: toLocalDate(raw?.termination_date, fallback.terminationDate),
    calendar: raw?.calendar || fallback.calendar,
    frequency: raw?.frequency || fallback.frequency,
    convention: raw?.convention || fallback.convention,
    terminationDateConvention: raw?.termination_date_convention || fallback.terminationDateConvention,
    dateGenerationRule: raw?.date_generation_rule || fallback.dateGenerationRule,
  };
}

function getPricingRates(pricing: Record<string, any> | undefined) {
  if (!pricing) return {};
  return isRecord(pricing.rates) ? pricing.rates : pricing;
}

function getPricingInflation(pricing: Record<string, any> | undefined) {
  if (!pricing) return {};
  return isRecord(pricing.inflation)
    ? pricing.inflation
    : {
        inflation_indices: pricing.inflation_indices,
        inflation_curves: pricing.inflation_curves,
      };
}

function inferInflationKind(curve: Curve | null): InflationCurveKind | null {
  return curve?.inflation_curve?.kind || null;
}

function samePeriod(left: Period | null | undefined, right: Period | null | undefined): boolean {
  return left?.n === right?.n && left?.unit === right?.unit;
}

function addPeriod(date: Date, period: Period): Date | null {
  const next = new Date(date);
  if (!Number.isFinite(period.n)) return null;
  switch (period.unit) {
    case 'Days':
      next.setUTCDate(next.getUTCDate() + period.n);
      return next;
    case 'Weeks':
      next.setUTCDate(next.getUTCDate() + period.n * 7);
      return next;
    case 'Months':
      next.setUTCMonth(next.getUTCMonth() + period.n);
      return next;
    case 'Years':
      next.setUTCFullYear(next.getUTCFullYear() + period.n);
      return next;
    default:
      return null;
  }
}

function getRequiredInflationBaseFixingDate(index: InflationIndexSpec, referenceDate: string): string | null {
  const parsed = new Date(`${referenceDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  const lagged = addPeriod(parsed, { n: -(index.availability_lag?.n ?? 0), unit: index.availability_lag?.unit || 'Months' });
  if (!lagged) return null;

  const month = lagged.getUTCMonth();
  const year = lagged.getUTCFullYear();
  return `${year}-${String(month + 1).padStart(2, '0')}-01`;
}

async function resolveIndexDefs(ids: string[]): Promise<IndexDef[]> {
  if (ids.length === 0) return [];
  const savedSpecs = await indexStore.getAll();
  const result: IndexDef[] = [];
  const seen = new Set<string>();

  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    const saved = savedSpecs.find(spec => spec.id === id);
    if (!saved) continue;
    const def = storedToRateIndexDef(saved);
    if (def) result.push(def);
  }

  return result;
}

function mapInflationCurveFromPricing(
  inflationCurveSpec: any,
  inflationIndexSpec: any,
  fallbackReferenceDate: string,
): Curve | null {
  if (!isRecord(inflationCurveSpec) || !isRecord(inflationIndexSpec)) return null;

  return {
    id: inflationCurveSpec.id || 'inflation',
    name: inflationCurveSpec.id || 'Inflation Curve',
    description: '',
    currency: inflationIndexSpec.currency || 'EUR',
    role: 'inflation',
    day_counter: inflationCurveSpec.day_counter || 'Actual365Fixed',
    interpolator: inflationCurveSpec.interpolator || 'Linear',
    bootstrap_trait: inflationCurveSpec.kind === 'YoYInflation' ? 'ZeroRate' : 'ZeroRate',
    reference_date: toLocalDate(inflationCurveSpec.reference_date, fallbackReferenceDate),
    points: [],
    discount_curve_id: inflationCurveSpec.discount_curve_id,
    inflation_curve: {
      kind: inflationCurveSpec.kind || 'ZeroInflation',
      index: inflationIndexSpec,
      calendar: inflationCurveSpec.calendar || 'TARGET',
      business_day_convention: inflationCurveSpec.business_day_convention || 'ModifiedFollowing',
      day_counter: inflationCurveSpec.day_counter || 'Actual365Fixed',
      interpolator: inflationCurveSpec.interpolator || 'Linear',
      allow_extrapolation: inflationCurveSpec.allow_extrapolation ?? true,
      bootstrap_accuracy: inflationCurveSpec.bootstrap_accuracy,
      points: Array.isArray(inflationCurveSpec.points) ? inflationCurveSpec.points : [],
    } as InflationCurveConfig,
    createdAt: '',
    updatedAt: '',
  };
}

function buildFlowCsv(flows: Array<Record<string, unknown>>) {
  const columns = ['payment_date', 'accrual_start_date', 'accrual_end_date', 'fixing_date', 'index_fixing', 'spread', 'rate', 'amount', 'discount', 'present_value'];
  return [columns.join(','), ...flows.map(row => columns.map(col => JSON.stringify(row?.[col] ?? '')).join(','))].join('\n');
}

export default function InflationSwap() {
  const ui = entityUi.inflationSwap;
  const { id } = useParams();
  const navigate = useNavigate();
  const isNew = !id || id === 'new';
  const today = new Date().toISOString().split('T')[0];
  const fiveYearsLater = new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

  const [swapId, setSwapId] = useState(id && id !== 'new' ? id : '');
  const [swapName, setSwapName] = useState('');
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');
  // The persisted server-side UUID graph (curves + inflation index +
  // swaps_inflation). Present ⇒ price by-reference; absent ⇒ inline. Loaded
  // from the saved entry's ``appGraph``.
  const [appGraph, setAppGraph] = useState<SwapsInflationAppGraph | null>(null);
  const [tradeKind, setTradeKind] = useState<InflationSwapKind>('ZeroCoupon');
  const [swapType, setSwapType] = useState<SwapSide>('Payer');
  const [asOfDate, setAsOfDate] = useState(today);
  const { asOfDate: globalAsOf } = useAsOfDate();
  const { user, login } = useAuth();

  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [inflationCurveId, setInflationCurveId] = useState('');
  const [inflationCurve, setInflationCurve] = useState<Curve | null>(null);
  const [pricingInflationIndex, setPricingInflationIndex] = useState<InflationIndexSpec | null>(null);

  const [notional, setNotional] = useState(1000000);
  const [includeFlows, setIncludeFlows] = useState(true);
  const [observationLag, setObservationLag] = useState<Period>({ n: 3, unit: 'Months' });
  const [observationInterpolation, setObservationInterpolation] = useState<CPIInterpolationType>('Linear');

  const [zcStartDate, setZcStartDate] = useState(today);
  const [zcMaturityDate, setZcMaturityDate] = useState(fiveYearsLater);
  const [zcFixedRate, setZcFixedRate] = useState(0.0215);
  const [zcFixedCalendar, setZcFixedCalendar] = useState('TARGET');
  const [zcFixedConvention, setZcFixedConvention] = useState<BusinessDayConvention>('ModifiedFollowing');
  const [zcDayCounter, setZcDayCounter] = useState('Actual365Fixed');
  const [adjustObservationDates, setAdjustObservationDates] = useState(false);
  const [zcInflationCalendar, setZcInflationCalendar] = useState('NullCalendar');
  const [zcInflationConvention, setZcInflationConvention] = useState<BusinessDayConvention>('Following');

  const [yoyFixedRate, setYoyFixedRate] = useState(0.0204);
  const [yoySpread, setYoySpread] = useState(0.0002);
  const [yoyFixedDayCounter, setYoyFixedDayCounter] = useState('Actual365Fixed');
  const [yoyDayCounter, setYoyDayCounter] = useState('Actual365Fixed');
  const [yoyPaymentCalendar, setYoyPaymentCalendar] = useState('TARGET');
  const [yoyPaymentConvention, setYoyPaymentConvention] = useState<BusinessDayConvention>('ModifiedFollowing');
  const [fixedSchedule, setFixedSchedule] = useState<ScheduleParams>({
    effectiveDate: today,
    terminationDate: fiveYearsLater,
    calendar: 'TARGET',
    frequency: 'Annual',
    convention: 'ModifiedFollowing',
    terminationDateConvention: 'ModifiedFollowing',
    dateGenerationRule: 'Forward',
  });
  const [yoySchedule, setYoySchedule] = useState<ScheduleParams>({
    effectiveDate: today,
    terminationDate: fiveYearsLater,
    calendar: 'TARGET',
    frequency: 'Annual',
    convention: 'ModifiedFollowing',
    terminationDateConvention: 'ModifiedFollowing',
    dateGenerationRule: 'Forward',
  });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorInfo, setErrorInfo] = useState<PricingErrorInfo | undefined>(undefined);
  const [result, setResult] = useState<InflationSwapResultData | null>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const [activeFlowTab, setActiveFlowTab] = useState<'fixed' | 'inflation'>('fixed');

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  useEffect(() => {
    setAsOfDate(globalAsOf);
  }, [globalAsOf]);

  useEffect(() => {
    const selectedKind = inferInflationKind(inflationCurve);
    if (selectedKind === 'ZeroInflation' && tradeKind !== 'ZeroCoupon') {
      setTradeKind('ZeroCoupon');
    } else if (selectedKind === 'YoYInflation' && tradeKind !== 'YearOnYear') {
      setTradeKind('YearOnYear');
    }

    const curveObservationLag = inflationCurve?.inflation_curve?.index?.observation_lag;
    if (curveObservationLag?.n && curveObservationLag?.unit && !samePeriod(curveObservationLag, observationLag)) {
      setObservationLag(curveObservationLag);
    }
  }, [inflationCurve, observationLag, tradeKind]);

  useEffect(() => {
    if (inflationCurve?.inflation_curve?.index) {
      setPricingInflationIndex(inflationCurve.inflation_curve.index);
    } else {
      setPricingInflationIndex(null);
    }
  }, [inflationCurveId, inflationCurve]);

  useEffect(() => {
    if (!isNew || !id) return;
    setSwapId('');
  }, [id, isNew]);

  useEffect(() => {
    if (isNew || !id) return;
    let cancelled = false;
    getInflationSwapEntryById(id).then(entry => {
      if (cancelled || !entry) return;
      const saved = entry.request;
      const defaultObservationLag: Period = { n: 3, unit: 'Months' };
      setSwapId(entry.id);
      setSwapName(entry.name || '');
      setAppGraph(asSwapsInflationAppGraph(entry.appGraph));
      const pricing = isRecord(saved.pricing) ? saved.pricing : undefined;
      const rates = getPricingRates(pricing);
      const inflation = getPricingInflation(pricing);
      const curves = Array.isArray(rates.curves) ? rates.curves : [];
      const inflationCurves = Array.isArray(inflation.inflation_curves) ? inflation.inflation_curves : [];
      const inflationIndices = Array.isArray(inflation.inflation_indices) ? inflation.inflation_indices : [];
      const discount = curves.find((curve: any) => curve.id === 'discount') || curves[0];
      const inflationCurveSpec = inflationCurves[0];
      const inflationIndexSpec = inflationIndices[0];
      const trade = saved.swaps?.[0] as any;
      const zeroCouponTrade = trade?.zero_coupon_inflation_swap;
      const yoyTrade = trade?.year_on_year_inflation_swap;

      if (pricing?.as_of_date) setAsOfDate(toLocalDate(pricing.as_of_date, today));
      setIncludeFlows(Boolean(saved.include_flows));

      if (discount) {
        setDiscountCurveId(discount.id || 'discount');
        setDiscountCurve({
          id: discount.id || 'discount',
          name: discount.id || 'Discount Curve',
          description: '',
          currency: 'EUR',
          role: 'discount',
          day_counter: discount.day_counter,
          interpolator: discount.interpolator,
          bootstrap_trait: discount.bootstrap_trait,
          reference_date: toLocalDate(discount.reference_date, today),
          points: Array.isArray(discount.points) ? discount.points : [],
          createdAt: '',
          updatedAt: '',
        });
      }

      const restoredInflationCurve = mapInflationCurveFromPricing(inflationCurveSpec, inflationIndexSpec, pricing?.as_of_date || today);
      if (restoredInflationCurve) {
        setInflationCurveId(restoredInflationCurve.id);
        setInflationCurve(restoredInflationCurve);
      }
      if (inflationIndexSpec) {
        setPricingInflationIndex(inflationIndexSpec);
      }

      if (zeroCouponTrade) {
        setTradeKind('ZeroCoupon');
        setSwapType(zeroCouponTrade.swap_type || 'Payer');
        setNotional(zeroCouponTrade.notional ?? zeroCouponTrade.nominal ?? 1000000);
        setObservationLag(zeroCouponTrade.observation_lag || defaultObservationLag);
        setObservationInterpolation(zeroCouponTrade.observation_interpolation || 'Linear');
        setZcStartDate(toLocalDate(zeroCouponTrade.start_date, today));
        setZcMaturityDate(toLocalDate(zeroCouponTrade.maturity_date, fiveYearsLater));
        setZcFixedRate(zeroCouponTrade.fixed_rate ?? 0.0215);
        setZcFixedCalendar(zeroCouponTrade.fixed_calendar || 'TARGET');
        setZcFixedConvention(zeroCouponTrade.fixed_convention || 'ModifiedFollowing');
        setZcDayCounter(zeroCouponTrade.day_counter || 'Actual365Fixed');
        setAdjustObservationDates(Boolean(zeroCouponTrade.adjust_observation_dates));
        setZcInflationCalendar(zeroCouponTrade.inflation_calendar || 'NullCalendar');
        setZcInflationConvention(zeroCouponTrade.inflation_convention || 'Following');
      } else if (yoyTrade) {
        setTradeKind('YearOnYear');
        setSwapType(yoyTrade.swap_type || 'Receiver');
        setNotional(yoyTrade.notional ?? yoyTrade.nominal ?? 1000000);
        setObservationLag(yoyTrade.observation_lag || defaultObservationLag);
        setObservationInterpolation(yoyTrade.observation_interpolation || 'Linear');
        setYoyFixedRate(yoyTrade.fixed_rate ?? 0.0204);
        setYoySpread(yoyTrade.spread ?? 0.0002);
        setYoyFixedDayCounter(yoyTrade.fixed_day_counter || 'Actual365Fixed');
        setYoyDayCounter(yoyTrade.yoy_day_counter || 'Actual365Fixed');
        setYoyPaymentCalendar(yoyTrade.payment_calendar || 'TARGET');
        setYoyPaymentConvention(yoyTrade.payment_convention || 'ModifiedFollowing');
        setFixedSchedule(prev => mapScheduleFromApi(yoyTrade.fixed_schedule, prev));
        setYoySchedule(prev => mapScheduleFromApi(yoyTrade.yoy_schedule, prev));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [id, isNew, fiveYearsLater, today]);

  const validationError = useMemo(() => {
    if (!discountCurve) return 'Discounting curve is required.';
    if (!inflationCurve) return 'Inflation curve is required.';
    if (!pricingInflationIndex?.id) return 'Inflation index is required.';
    const kind = inferInflationKind(inflationCurve);
    if (tradeKind === 'ZeroCoupon' && kind !== 'ZeroInflation') {
      return 'Select a Zero Inflation curve for zero-coupon inflation swaps.';
    }
    if (tradeKind === 'YearOnYear' && kind !== 'YoYInflation') {
      return 'Select a YoY Inflation curve for year-on-year inflation swaps.';
    }
    if (tradeKind === 'YearOnYear' && pricingInflationIndex && inflationCurve?.reference_date) {
      const requiredFixingDate = getRequiredInflationBaseFixingDate(pricingInflationIndex, inflationCurve.reference_date);
      if (requiredFixingDate) {
        const hasBaseFixing = (pricingInflationIndex.fixings || []).some(
          (fixing) => fixing?.date === requiredFixingDate && Number.isFinite(fixing?.value),
        );
        if (!hasBaseFixing) {
          return `Selected YoY inflation index is missing the required base fixing for ${requiredFixingDate}. Add it in Market Data -> Indices or use an inline definition with that fixing.`;
        }
      }
    }
    if (tradeKind === 'ZeroCoupon' && zcStartDate >= zcMaturityDate) {
      return 'Maturity date must be after start date.';
    }
    if (tradeKind === 'YearOnYear' && fixedSchedule.effectiveDate >= fixedSchedule.terminationDate) {
      return 'Fixed leg termination date must be after effective date.';
    }
    if (tradeKind === 'YearOnYear' && yoySchedule.effectiveDate >= yoySchedule.terminationDate) {
      return 'YoY leg termination date must be after effective date.';
    }
    if (!Number.isFinite(notional) || notional <= 0) {
      return 'Notional must be greater than 0.';
    }
    if (!Number.isFinite(observationLag.n) || observationLag.n < 0) {
      return 'Observation lag must be zero or positive.';
    }
    return null;
  }, [discountCurve, fixedSchedule.effectiveDate, fixedSchedule.terminationDate, inflationCurve, notional, observationLag.n, pricingInflationIndex?.id, tradeKind, yoySchedule.effectiveDate, yoySchedule.terminationDate, zcMaturityDate, zcStartDate]);

  const activeFlows = useMemo(() => {
    if (!result) return [] as Array<Record<string, unknown>>;
    if (activeFlowTab === 'fixed') return (result.fixed_leg_flows || []) as Array<Record<string, unknown>>;
    return tradeKind === 'ZeroCoupon'
      ? ((result.inflation_leg_flows || []) as Array<Record<string, unknown>>)
      : ((result.yoy_leg_flows || []) as Array<Record<string, unknown>>);
  }, [activeFlowTab, result, tradeKind]);

  const secondaryFlowLabel = tradeKind === 'ZeroCoupon' ? 'Inflation' : 'YoY';

  // Map a portal-shaped curve (id, day_counter, points, interpolator,
  // bootstrap_trait, reference_date) onto the orchestrator's inline
  // CurveRef (which forbids extra fields). Role tagged via ``body.role``
  // so the backend picks nominal vs inflation. Points pass through
  // normalizeCurvePointForApi so the legacy
  // ``tenor_number``/``tenor_time_unit`` shape becomes
  // ``tenor: {n, unit}``; ``quote_id`` is forwarded unresolved
  // (the orchestrator's market-data resolution substitutes the value).
  const toThinACurve = (curve: Curve, role: 'nominal' | 'inflation'): any => {
    const normalized = normalizeCurveForApi({
      id: curve.id,
      day_counter: curve.day_counter,
      reference_date: curve.reference_date || asOfDate,
      points: curve.points,
    });
    const out: Record<string, any> = {
      name: curve.id || curve.name || role,
      points: normalized.points,
    };
    if (curve.currency) out.currency = curve.currency;
    if (curve.day_counter) out.day_counter = curve.day_counter;
    if (curve.reference_date || asOfDate) out.reference_date = curve.reference_date || asOfDate;
    out.body = { role };
    return out;
  };

  // Build the top-level inline ``inflation_index`` (InflationIndexRef).
  // InflationIndexRef forbids extra fields and requires ``name`` or
  // ``index_id`` (plus the ``body`` JSONB with family/frequency/lags/
  // fixings the engine consumes verbatim — the inflation-index body
  // does NOT flow through market data).
  const toThinAInflationIndex = (
    spec: InflationIndexSpec,
    currency: string | undefined,
  ): Record<string, any> => {
    return {
      name: spec.family_name || spec.id,
      index_id: spec.id,
      currency: currency || spec.currency,
      day_counter: spec.day_counter,
      body: {
        family_name: spec.family_name,
        frequency: spec.frequency,
        calendar: spec.calendar,
        day_counter: spec.day_counter,
        availability_lag: spec.availability_lag,
        observation_lag: spec.observation_lag,
        interpolated: spec.interpolated,
        revised: spec.revised,
        ...(spec.kind ? { kind: spec.kind } : {}),
        ...(spec.underlying_zero_index_id ? { underlying_zero_index_id: spec.underlying_zero_index_id } : {}),
        ...(Array.isArray(spec.fixings) ? { fixings: spec.fixings } : {}),
      },
    };
  };

  // Inline POST body for ``POST /v1/price/swaps/inflation``. The
  // flat trade body uses the engine-canonical nested
  // ``swaps[].zero_coupon_inflation_swap`` / ``year_on_year_inflation_swap``
  // shape (the backend accepts both; the nested form preserves the
  // per-trade conventions the portal authors today) plus role-tagged
  // top-level curves + top-level inflation_index. The
  // InflationSwapPriceRequest validator requires ≥ 2 entries in
  // ``curves`` (nominal first; inflation second) and an
  // ``inflation_index`` ref when ``swap`` is set inline.
  // Caveat: the nominal curve must extend past the trade's longest
  // cashflow — the CurveSetSelector lets the user pick any saved curve;
  // we forward its points verbatim. An under-spec'd curve surfaces as
  // ``engine_upstream_error`` (visible failure, not silent).
  const buildThinARequest = async (): Promise<Record<string, any>> => {
    if (!discountCurve) throw new Error('Please select a discounting curve.');
    if (!inflationCurve) throw new Error('Please select an inflation curve.');
    if (!pricingInflationIndex) throw new Error('Inflation index is missing.');

    // Build the inflation curve directly from the portal's
    // ``inflation_curve`` config, forwarding ``quote_id`` unresolved
    // (the orchestrator's market-data resolution substitutes). We do
    // NOT route through ``buildInflationCurveSpec`` because its
    // ``resolveInflationPoints`` step substitutes quote_ids client-side,
    // which is exactly the legacy pattern the inline contract retires.
    const pricingInflationCurve: Curve = {
      ...inflationCurve,
      discount_curve_id: 'discount',
      reference_date: inflationCurve.reference_date || asOfDate,
      inflation_curve: {
        ...inflationCurve.inflation_curve!,
        index: {
          ...(pricingInflationIndex || inflationCurve.inflation_curve!.index),
          currency: inflationCurve.currency,
          kind: inferInflationKind(inflationCurve) || inflationCurve.inflation_curve!.kind,
        },
      },
    };
    const inflationConfig = pricingInflationCurve.inflation_curve!;
    const inflationKind = inflationConfig.kind;
    const inflationIndexId = inflationConfig.index?.id;
    const inflationDiscountCurveId = pricingInflationCurve.discount_curve_id || 'discount';
    // Forward each helper wrapper as-is — quote_id stays unresolved,
    // and YoY helpers get the per-point ``nominal_curve_id`` link to the
    // role-tagged nominal curve (the orchestrator translator reads this
    // off the inflation helper rather than the curve body).
    const thinAInflationPoints = (inflationConfig.points || []).map((wrapper: any) => ({
      point_type: wrapper.point_type,
      point: {
        ...wrapper.point,
        ...(inflationKind === 'YoYInflation' && !wrapper.point?.nominal_curve_id
          ? { nominal_curve_id: inflationDiscountCurveId }
          : {}),
      },
    }));

    const thinAInflationCurve: Record<string, any> = {
      name: pricingInflationCurve.id || 'inflation',
      currency: inflationCurve.currency,
      day_counter: inflationConfig.day_counter || inflationCurve.day_counter,
      reference_date: pricingInflationCurve.reference_date,
      points: thinAInflationPoints,
      body: {
        role: 'inflation',
        kind: inflationKind,
        calendar: inflationConfig.calendar,
        business_day_convention: inflationConfig.business_day_convention,
        interpolator: inflationConfig.interpolator,
        allow_extrapolation: inflationConfig.allow_extrapolation,
        ...(inflationConfig.bootstrap_accuracy !== undefined
          ? { bootstrap_accuracy: inflationConfig.bootstrap_accuracy } : {}),
        ...(inflationDiscountCurveId ? { discount_curve_id: inflationDiscountCurveId } : {}),
        ...(inflationIndexId ? { index_id: inflationIndexId } : {}),
      },
    };

    const nominalIndexIds = collectIndexRefIds(discountCurve.points || []);
    const resolvedIndices = await resolveIndexDefs(nominalIndexIds);
    if (nominalIndexIds.length > 0 && resolvedIndices.length !== Array.from(new Set(nominalIndexIds)).length) {
      const missingIds = Array.from(new Set(nominalIndexIds)).filter(indexId => !resolvedIndices.some(def => def.id === indexId));
      throw new Error(`Selected discount curve references missing indices: ${missingIds.join(', ')}. Add them in Market Data -> Indices.`);
    }

    // The trade body's ``inflation_index_id`` is the engine-side string
    // identifier — same id the ``inflation_index`` ref carries below.
    const swap_inflation_index_id = inflationIndexId || pricingInflationIndex.id;

    const trade_inner = tradeKind === 'ZeroCoupon'
      ? {
          zero_coupon_inflation_swap: {
            swap_type: swapType,
            notional,
            start_date: zcStartDate,
            maturity_date: zcMaturityDate,
            fixed_calendar: zcFixedCalendar,
            fixed_convention: zcFixedConvention,
            day_counter: zcDayCounter,
            fixed_rate: zcFixedRate,
            inflation_index_id: swap_inflation_index_id,
            observation_lag: observationLag,
            observation_interpolation: observationInterpolation,
            adjust_observation_dates: adjustObservationDates,
            inflation_calendar: zcInflationCalendar,
            inflation_convention: zcInflationConvention,
          },
        }
      : {
          year_on_year_inflation_swap: {
            swap_type: swapType,
            notional,
            fixed_schedule: scheduleToApi(fixedSchedule),
            fixed_rate: yoyFixedRate,
            fixed_day_counter: yoyFixedDayCounter,
            yoy_schedule: scheduleToApi(yoySchedule),
            inflation_index_id: swap_inflation_index_id,
            observation_lag: observationLag,
            observation_interpolation: observationInterpolation,
            spread: yoySpread,
            yoy_day_counter: yoyDayCounter,
            payment_calendar: yoyPaymentCalendar,
            payment_convention: yoyPaymentConvention,
          },
        };

    return {
      swap: {
        swap_kind: tradeKind === 'ZeroCoupon' ? 'zero_coupon' : 'year_on_year',
        swaps: [trade_inner],
      },
      curves: [toThinACurve(discountCurve, 'nominal'), thinAInflationCurve],
      inflation_index: toThinAInflationIndex(pricingInflationIndex, inflationCurve.currency),
      as_of: asOfDate,
      // ``resolvedIndices`` (rate-index defs the nominal curve references)
      // is captured here so the backend can wire the per-trade
      // fixing-index reference, but the orchestrator's inline
      // validator forbids extra fields at the envelope root — the indices
      // ride through ``body.indices`` on the nominal curve, not the root.
      // For today the discount curve typically references no indices for
      // the inflation route (rates side), so this list stays empty.
    };
  };

  const buildRequest = async (): Promise<InflationSwapRequest> => {
    if (!discountCurve) throw new Error('Please select a discounting curve.');
    if (!inflationCurve) throw new Error('Please select an inflation curve.');

    // quote_ids reach the orchestrator UNRESOLVED (server-side market-data
    // resolution pulls the value); inline points pass through.
    const resolvedDiscountCurve = [{
      id: 'discount',
      day_counter: discountCurve.day_counter,
      interpolator: discountCurve.interpolator,
      bootstrap_trait: discountCurve.bootstrap_trait,
      reference_date: discountCurve.reference_date || asOfDate,
      points: discountCurve.points,
    }];

    const nominalIndexIds = collectIndexRefIds(discountCurve.points || []);
    const resolvedIndices = await resolveIndexDefs(nominalIndexIds);
    if (nominalIndexIds.length > 0 && resolvedIndices.length !== Array.from(new Set(nominalIndexIds)).length) {
      const missingIds = Array.from(new Set(nominalIndexIds)).filter(indexId => !resolvedIndices.some(def => def.id === indexId));
      throw new Error(`Selected discount curve references missing indices: ${missingIds.join(', ')}. Add them in Market Data -> Indices.`);
    }

    const pricingInflationCurve: Curve = {
      ...inflationCurve,
      discount_curve_id: 'discount',
      reference_date: inflationCurve.reference_date || asOfDate,
      inflation_curve: {
        ...inflationCurve.inflation_curve!,
        index: {
          ...(pricingInflationIndex || inflationCurve.inflation_curve!.index),
          currency: inflationCurve.currency,
          kind: inferInflationKind(inflationCurve) || inflationCurve.inflation_curve!.kind,
        },
      },
    };
    const inflationIndexSpec = buildInflationIndexSpec(pricingInflationCurve);
    const inflationCurveSpec = buildInflationCurveSpec(pricingInflationCurve, asOfDate);

    return {
      pricing: {
        as_of_date: asOfDate,
        curves: resolvedDiscountCurve.map(normalizeCurveForApi),
        ...(resolvedIndices.length > 0 ? { indices: resolvedIndices.map(normalizeIndexDefForApi) } : {}),
        inflation_indices: [inflationIndexSpec],
        inflation_curves: [inflationCurveSpec],
      },
      swaps: tradeKind === 'ZeroCoupon'
        ? [{
            zero_coupon_inflation_swap: {
              swap_type: swapType,
              notional,
              start_date: zcStartDate,
              maturity_date: zcMaturityDate,
              fixed_calendar: zcFixedCalendar,
              fixed_convention: zcFixedConvention,
              day_counter: zcDayCounter,
              fixed_rate: zcFixedRate,
              inflation_index_id: inflationIndexSpec.id,
              observation_lag: observationLag,
              observation_interpolation: observationInterpolation,
              adjust_observation_dates: adjustObservationDates,
              inflation_calendar: zcInflationCalendar,
              inflation_convention: zcInflationConvention,
            },
            discounting_curve: 'discount',
            inflation_curve: inflationCurveSpec.id,
          }]
        : [{
            year_on_year_inflation_swap: {
              swap_type: swapType,
              notional,
              fixed_schedule: scheduleToApi(fixedSchedule),
              fixed_rate: yoyFixedRate,
              fixed_day_counter: yoyFixedDayCounter,
              yoy_schedule: scheduleToApi(yoySchedule),
              inflation_index_id: inflationIndexSpec.id,
              observation_lag: observationLag,
              observation_interpolation: observationInterpolation,
              spread: yoySpread,
              yoy_day_counter: yoyDayCounter,
              payment_calendar: yoyPaymentCalendar,
              payment_convention: yoyPaymentConvention,
            },
            discounting_curve: 'discount',
            inflation_curve: inflationCurveSpec.id,
          }],
      include_flows: includeFlows,
    };
  };

  const handleSave = async () => {
    setSaveStatus(null);
    if (validationError) {
      setSaveStatus({ tone: 'error', message: `Cannot save: ${validationError}` });
      return;
    }
    setSaving(true);
    try {
      const request = await buildRequest();
      const trimmedName = swapName.trim();
      const priorGraph = appGraph;
      const displayName = trimmedName || deriveInflationSwapName(request);
      // Link the nominal + inflation curves by store id so
      // the lazy migration can reload the quote-id-bearing curves (the stripped
      // request lost them) and rebuild the persist graph. The inflation index is
      // consumed verbatim (no market data) so its ref is captured but optional.
      const sourceRefs = buildSourceRefs({
        curves: [
          { role: 'nominal', curveId: discountCurveId },
          { role: 'inflation', curveId: inflationCurveId },
        ],
        indexId: pricingInflationIndex?.id || undefined,
      });

      // Persist the entity graph server-side leaf→root (curves → index →
      // swaps_inflation) and stamp the UUIDs back. The persisted entity bodies
      // are derived from the SAME inline wire payload the inline arm posts
      // (``buildThinARequest``), so the by-reference price equals the inline
      // price for the same inputs. The localStorage save still runs so no user
      // data is lost (dual-read, additive).
      // Do NOT swallow a buildThinARequest() throw. With no market
      // payload there is nothing to reference server-side, so persist locally
      // (dual-read, no user data lost) and surface the throw as an error.
      // No success banner on failure (mirrors EquityOptions.handleSave).
      let thinARequest: Record<string, any>;
      try {
        thinARequest = await buildThinARequest();
      } catch (err) {
        await saveInflationSwap(request, {
          id: isNew ? undefined : (swapId || undefined),
          name: trimmedName || undefined,
          appId: priorGraph?.swapsInflationId,
          appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          sourceRefs,
        });
        setSaveStatus({
          tone: 'error',
          message: `Saved locally, but could not build the market payload to reference server-side: ${err instanceof Error ? err.message : 'unknown error'}`,
        });
        setSaving(false);
        return;
      }

      let nextGraph: SwapsInflationAppGraph | null = priorGraph;
      let serverNote = '';
      {
        const graphCurves: CurveGraphInput[] = (thinARequest.curves as any[]).map((c) => ({
          key: (c.body?.role as string) || 'nominal',
          body: c as components['schemas']['CurveCreate'],
        }));
        const idxRef = thinARequest.inflation_index as Record<string, any>;
        const indexBody: components['schemas']['IndexCreate'] = {
          name: (idxRef.name as string) || (idxRef.index_id as string),
          kind: 'Inflation',
          currency: (idxRef.currency as string | undefined) ?? null,
          day_counter: (idxRef.day_counter as string | undefined) ?? null,
          // Carry the engine string id inside the stored index body so the
          // by-ref ``pricing.inflation_index_id`` scalar resolves back to the
          // same ``index_id`` the trade references.
          body: { ...(idxRef.body as Record<string, unknown>), id: idxRef.index_id },
        };
        const swapEnvelope = thinARequest.swap as { swap_kind?: string; swaps?: unknown[] };
        const graphResult = await persistSwapsInflationGraph(
          {
            name: displayName,
            curves: graphCurves,
            index: indexBody,
            swapKind: swapEnvelope.swap_kind === 'year_on_year' ? 'year_on_year' : 'zero_coupon',
            swapsInflationRequest: { swaps: swapEnvelope.swaps ?? [], include_flows: request.include_flows },
            changeReason: changeReason.trim() || undefined,
          },
          priorGraph,
        );
        if (!graphResult.ok) {
          // Branch on the structured envelope code. The localStorage save
          // below still runs so no user data is lost (dual-read).
          const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
          await saveInflationSwap(request, {
            id: isNew ? undefined : (swapId || undefined),
            name: trimmedName || undefined,
            appId: priorGraph?.swapsInflationId,
            appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          });
          setSaveStatus({
            tone: 'error',
            message: `Saved locally, but server persist failed at ${stage} [${graphResult.envelope.code}]: ${graphResult.envelope.error}`,
          });
          setSaving(false);
          return;
        }
        nextGraph = graphResult.graph;
        serverNote = priorGraph ? ' Updated server copy.' : ' Referenced server-side for by-reference pricing.';
      }

      const savedId = await saveInflationSwap(request, {
        id: isNew ? undefined : (swapId || undefined),
        name: trimmedName || undefined,
        appId: nextGraph?.swapsInflationId,
        appGraph: (nextGraph ?? undefined) as unknown as Record<string, unknown> | undefined,
        sourceRefs,
      });
      setSwapId(savedId);
      setAppGraph(nextGraph);
      setSaveStatus({ tone: 'success', message: `Saved "${displayName}".${serverNote}` });
      setChangeReason('');
      if (isNew || id !== savedId) {
        navigate(`/products/inflation-swaps/${savedId}`, { replace: true });
      }
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save inflation swap' });
    } finally {
      setSaving(false);
    }
  };

  const handlePrice = async () => {
    if (!user) {
      const signedInUser = await login();
      if (!signedInUser) {
        setError('Sign in is required to run pricing calls.');
        return;
      }
    }

    if (validationError) {
      setError(validationError);
      return;
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    try {
      // Direct cutover: post the
      // flat-trade-body + role-tagged top-level curves + top-level
      // inflation_index envelope to the orchestrator. The legacy
      // ``quantraApi.priceZeroCouponInflationSwap`` /
      // ``priceYearOnYearInflationSwap`` calls against
      // ``api.quantra.io`` are retired in this dispatch — no fallback.
      // ``buildRequest`` (handleSave) still constructs the fat shape
      // for localStorage round-trips, so saved swaps keep loading
      // without churn.
      // Price-arm selection. A saved swap
      // (appGraph present) prices by-reference: a minimal {swap_id, as_of}
      // body, and the orchestrator loads the trade + curves + index
      // server-side — so we skip the local curve/index build entirely. An
      // unsaved swap prices inline, unchanged.
      const request = appGraph?.swapsInflationId
        ? buildSwapsInflationPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate })
        : await buildThinARequest();
      const response = await priceSwapsInflation(request, asOfDate);
      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);

      if (response.success && response.data?.swaps?.[0]) {
        setResult(response.data.swaps[0]);
        setActiveFlowTab('fixed');
      } else {
        setError(response.error || 'Pricing failed');
        setErrorInfo(response.errorInfo);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pricing failed');
      setErrorInfo(undefined);
    } finally {
      setLoading(false);
    }
  };

  const formatNumber = (value?: number, decimals = 4) => {
    if (value === undefined || value === null) return '—';
    return value.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  };

  const syncScheduleDate = (field: 'effectiveDate' | 'terminationDate', value: string) => {
    setFixedSchedule(prev => ({ ...prev, [field]: value }));
    setYoySchedule(prev => ({ ...prev, [field]: value }));
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : (swapName.trim() || ui.singular)}
          subtitle="Price zero-coupon and year-on-year inflation swaps from saved inflation curves"
          backLink={<BackLink onClick={() => navigate('/products/inflation-swaps')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={swapName}
              onNameChange={setSwapName}
              onSave={handleSave}
              saving={saving}
              placeholder="Inflation swap name…"
              reason={changeReason}
              onReasonChange={setChangeReason}
            />
          }
        />
        {saveStatus && (
          <FeedbackBanner
            tone={saveStatus.tone}
            message={saveStatus.message}
            onDismiss={() => setSaveStatus(null)}
          />
        )}

        <div className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Pricing Context</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>As Of Date</label>
                  <input type="date" value={asOfDate} onChange={e => setAsOfDate(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Trade Type</label>
                  <select value={tradeKind} onChange={e => setTradeKind(e.target.value as InflationSwapKind)} className={inputClass}>
                    <option value="ZeroCoupon">Zero Coupon Inflation Swap</option>
                    <option value="YearOnYear">Year-on-Year Inflation Swap</option>
                  </select>
                </div>
                <CurveSetSelector
                  label="Discounting Curve"
                  curveRole="discount"
                  curveSetId={curveSetId}
                  curveId={discountCurveId}
                  onChangeCurveSet={(nextCurveSetId) => setCurveSetId(nextCurveSetId)}
                  onChangeCurve={(nextCurveId, curve) => {
                    setDiscountCurveId(nextCurveId);
                    setDiscountCurve(curve);
                  }}
                />
                <CurveSetSelector
                  label="Inflation Curve"
                  curveRole="inflation"
                  curveSetId={curveSetId}
                  curveId={inflationCurveId}
                  onChangeCurveSet={(nextCurveSetId) => setCurveSetId(nextCurveSetId)}
                  onChangeCurve={(nextCurveId, curve) => {
                    setInflationCurveId(nextCurveId);
                    setInflationCurve(curve);
                  }}
                />
              </div>
              {inflationCurve?.inflation_curve?.index && (
                <div className="mt-4">
                  <InflationIndexPicker
                    value={pricingInflationIndex || inflationCurve.inflation_curve.index}
                    currency={inflationCurve.currency}
                    kind={inflationCurve.inflation_curve.kind}
                    onChange={setPricingInflationIndex}
                    label="Inflation Index"
                    helperText="By default this comes from the selected inflation curve. You can keep it inline or switch to a saved Market Data index definition."
                  />
                </div>
              )}
              {inflationCurve?.inflation_curve?.index && (
                <div className="mt-4 grid sm:grid-cols-3 gap-3 text-xs text-[#525252]">
                  <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                    Index: <span className="font-medium">{(pricingInflationIndex || inflationCurve.inflation_curve.index).id}</span>
                  </div>
                  <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                    Curve Kind: <span className="font-medium">{inflationCurve.inflation_curve.kind}</span>
                  </div>
                  <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                    Currency: <span className="font-medium">{inflationCurve.currency}</span>
                  </div>
                </div>
              )}
              <div className="mt-4 flex items-center gap-2">
                <input
                  id="includeInflationFlows"
                  type="checkbox"
                  checked={includeFlows}
                  onChange={e => setIncludeFlows(e.target.checked)}
                  className="rounded border-[#d4d4d4]"
                />
                <label htmlFor="includeInflationFlows" className="text-xs text-[#737373]">
                  Include detailed cash flows in pricing response
                </label>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Core Terms</h2>
              <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
                <div>
                  <label className={labelClass}>Payer / Receiver</label>
                  <select value={swapType} onChange={e => setSwapType(e.target.value as SwapSide)} className={inputClass}>
                    <option value="Payer">Payer</option>
                    <option value="Receiver">Receiver</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Notional</label>
                  <input type="number" value={notional} onChange={e => setNotional(parseFloat(e.target.value) || 0)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Observation Lag</label>
                  <div className="grid grid-cols-2 gap-2">
                    <input
                      type="number"
                      value={observationLag.n}
                      onChange={e => setObservationLag(prev => ({ ...prev, n: parseInt(e.target.value, 10) || 0 }))}
                      className={inputClass}
                    />
                    <select
                      value={observationLag.unit}
                      onChange={e => setObservationLag(prev => ({ ...prev, unit: e.target.value as TimeUnit }))}
                      className={inputClass}
                    >
                      {TIME_UNITS.map(unit => <option key={unit} value={unit}>{unit}</option>)}
                    </select>
                  </div>
                </div>
                <div>
                  <label className={labelClass}>Observation Interpolation</label>
                  <select
                    value={observationInterpolation}
                    onChange={e => setObservationInterpolation(e.target.value as CPIInterpolationType)}
                    className={inputClass}
                  >
                    {CPI_INTERPOLATION_TYPES.map(value => <option key={value} value={value}>{value}</option>)}
                  </select>
                </div>
              </div>
            </div>

            {tradeKind === 'ZeroCoupon' ? (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Zero Coupon Terms</h2>
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  <div>
                    <label className={labelClass}>Start Date</label>
                    <input type="date" value={zcStartDate} onChange={e => setZcStartDate(e.target.value)} className={inputClass} />
                  </div>
                  <div>
                    <label className={labelClass}>Maturity Date</label>
                    <input type="date" value={zcMaturityDate} onChange={e => setZcMaturityDate(e.target.value)} className={inputClass} />
                  </div>
                  <div>
                    <label className={labelClass}>Fixed Rate</label>
                    <input type="number" step="0.0001" value={zcFixedRate} onChange={e => setZcFixedRate(parseFloat(e.target.value) || 0)} className={inputClass} />
                  </div>
                  <div>
                    <label className={labelClass}>Fixed Day Counter</label>
                    <select value={zcDayCounter} onChange={e => setZcDayCounter(e.target.value)} className={inputClass}>
                      {DAY_COUNTERS.map(value => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Fixed Calendar</label>
                    <select value={zcFixedCalendar} onChange={e => setZcFixedCalendar(e.target.value)} className={inputClass}>
                      {CALENDARS.map(value => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Fixed Convention</label>
                    <select value={zcFixedConvention} onChange={e => setZcFixedConvention(e.target.value as BusinessDayConvention)} className={inputClass}>
                      {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Inflation Calendar</label>
                    <select value={zcInflationCalendar} onChange={e => setZcInflationCalendar(e.target.value)} className={inputClass}>
                      {CALENDARS.map(value => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Inflation Convention</label>
                    <select value={zcInflationConvention} onChange={e => setZcInflationConvention(e.target.value as BusinessDayConvention)} className={inputClass}>
                      {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                  <div className="flex items-end">
                    <label className="flex items-center gap-2 text-sm text-[#525252]">
                      <input
                        type="checkbox"
                        checked={adjustObservationDates}
                        onChange={e => setAdjustObservationDates(e.target.checked)}
                        className="rounded border-[#d4d4d4]"
                      />
                      Adjust observation dates
                    </label>
                  </div>
                </div>
              </div>
            ) : (
              <>
                <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Year-on-Year Legs</h2>
                  <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
                    <div>
                      <label className={labelClass}>Fixed Rate</label>
                      <input type="number" step="0.0001" value={yoyFixedRate} onChange={e => setYoyFixedRate(parseFloat(e.target.value) || 0)} className={inputClass} />
                    </div>
                    <div>
                      <label className={labelClass}>Spread</label>
                      <input type="number" step="0.0001" value={yoySpread} onChange={e => setYoySpread(parseFloat(e.target.value) || 0)} className={inputClass} />
                    </div>
                    <div>
                      <label className={labelClass}>Payment Calendar</label>
                      <select value={yoyPaymentCalendar} onChange={e => setYoyPaymentCalendar(e.target.value)} className={inputClass}>
                        {CALENDARS.map(value => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Fixed Day Counter</label>
                      <select value={yoyFixedDayCounter} onChange={e => setYoyFixedDayCounter(e.target.value)} className={inputClass}>
                        {DAY_COUNTERS.map(value => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>YoY Day Counter</label>
                      <select value={yoyDayCounter} onChange={e => setYoyDayCounter(e.target.value)} className={inputClass}>
                        {DAY_COUNTERS.map(value => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Payment Convention</label>
                      <select value={yoyPaymentConvention} onChange={e => setYoyPaymentConvention(e.target.value as BusinessDayConvention)} className={inputClass}>
                        {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </div>
                  </div>
                </div>

                <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                  <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Schedules</h2>
                  <div className="grid lg:grid-cols-2 gap-4">
                    <div className="border border-[#e5e5e5] rounded-lg p-4 space-y-3">
                      <h3 className="text-xs font-semibold text-[#525252]">Shared Dates</h3>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>Effective Date</label>
                          <input type="date" value={fixedSchedule.effectiveDate} onChange={e => syncScheduleDate('effectiveDate', e.target.value)} className={inputClass} />
                        </div>
                        <div>
                          <label className={labelClass}>Termination Date</label>
                          <input type="date" value={fixedSchedule.terminationDate} onChange={e => syncScheduleDate('terminationDate', e.target.value)} className={inputClass} />
                        </div>
                      </div>
                    </div>
                    <div className="border border-[#e5e5e5] rounded-lg p-4 space-y-3">
                      <h3 className="text-xs font-semibold text-[#525252]">Fixed Schedule</h3>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>Frequency</label>
                          <select value={fixedSchedule.frequency} onChange={e => setFixedSchedule(prev => ({ ...prev, frequency: e.target.value as Frequency }))} className={inputClass}>
                            {FREQUENCIES.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Calendar</label>
                          <select value={fixedSchedule.calendar} onChange={e => setFixedSchedule(prev => ({ ...prev, calendar: e.target.value }))} className={inputClass}>
                            {CALENDARS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Convention</label>
                          <select value={fixedSchedule.convention} onChange={e => setFixedSchedule(prev => ({ ...prev, convention: e.target.value }))} className={inputClass}>
                            {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Termination Convention</label>
                          <select value={fixedSchedule.terminationDateConvention} onChange={e => setFixedSchedule(prev => ({ ...prev, terminationDateConvention: e.target.value }))} className={inputClass}>
                            {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div className="col-span-2">
                          <label className={labelClass}>Date Generation Rule</label>
                          <select value={fixedSchedule.dateGenerationRule} onChange={e => setFixedSchedule(prev => ({ ...prev, dateGenerationRule: e.target.value }))} className={inputClass}>
                            {DATE_GENERATION_RULES.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                      </div>
                    </div>
                    <div className="lg:col-span-2 border border-[#e5e5e5] rounded-lg p-4 space-y-3">
                      <h3 className="text-xs font-semibold text-[#525252]">YoY Schedule</h3>
                      <div className="grid sm:grid-cols-3 gap-3">
                        <div>
                          <label className={labelClass}>Frequency</label>
                          <select value={yoySchedule.frequency} onChange={e => setYoySchedule(prev => ({ ...prev, frequency: e.target.value as Frequency }))} className={inputClass}>
                            {FREQUENCIES.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Calendar</label>
                          <select value={yoySchedule.calendar} onChange={e => setYoySchedule(prev => ({ ...prev, calendar: e.target.value }))} className={inputClass}>
                            {CALENDARS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Convention</label>
                          <select value={yoySchedule.convention} onChange={e => setYoySchedule(prev => ({ ...prev, convention: e.target.value }))} className={inputClass}>
                            {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Termination Convention</label>
                          <select value={yoySchedule.terminationDateConvention} onChange={e => setYoySchedule(prev => ({ ...prev, terminationDateConvention: e.target.value }))} className={inputClass}>
                            {BUSINESS_DAY_CONVENTIONS.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div className="sm:col-span-2">
                          <label className={labelClass}>Date Generation Rule</label>
                          <select value={yoySchedule.dateGenerationRule} onChange={e => setYoySchedule(prev => ({ ...prev, dateGenerationRule: e.target.value }))} className={inputClass}>
                            {DATE_GENERATION_RULES.map(value => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </>
            )}

            <div className="flex gap-2">
              <button
                onClick={handlePrice}
                disabled={loading || !!validationError}
                className="flex-1 py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50 transition-colors"
              >
                {loading ? 'Pricing...' : `Price ${tradeKind === 'ZeroCoupon' ? 'Zero Coupon' : 'Year-on-Year'} Swap`}
              </button>
            </div>
          </div>

          <div className="space-y-4">
            {loading && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
                <div className="flex items-center justify-center py-8">
                  <div className="w-6 h-6 border-2 border-[#e5e5e5] border-t-[#8a6a2f] rounded-full animate-spin" />
                  <span className="ml-3 text-sm text-[#737373]">Pricing...</span>
                </div>
              </div>
            )}
            {!loading && (error || validationError) && (
              <PricingErrorCard
                error={error || validationError || 'Pricing failed'}
                info={error ? errorInfo : undefined}
                durationMs={error ? durationMs : undefined}
                title={!error && validationError ? 'Invalid Request' : undefined}
              />
            )}
            {!loading && !error && !validationError && !result && (
              <PanelPlaceholder
                compact
                title="No pricing results yet"
                description={getInstructionText('trade parameters', 'Price')}
                icon={ui.icon}
              />
            )}
            {!loading && result && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold text-[#0a0a0a]">Pricing Results</h3>
                  <div className="flex items-center gap-3">
                    <PricingTraceLink requestId={requestId} />
                    {durationMs && <span className="text-xs text-[#a3a3a3]">{durationMs.toFixed(0)}ms</span>}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <div className="bg-[#f5f5f5] rounded-lg p-3 col-span-2">
                    <p className="text-xs text-[#737373] mb-1">NPV</p>
                    <p className="text-xl font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.npv, 2)}</p>
                  </div>
                  <div className="bg-[#f5f5f5] rounded-lg p-3">
                    <p className="text-xs text-[#737373] mb-1">Fair Rate</p>
                    <p className="text-base font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.fair_rate, 6)}</p>
                  </div>
                  <div className="bg-[#f5f5f5] rounded-lg p-3">
                    <p className="text-xs text-[#737373] mb-1">Fair Spread</p>
                    <p className="text-base font-semibold text-[#0a0a0a] font-mono">
                      {tradeKind === 'YearOnYear' ? formatNumber(result.fair_spread, 6) : '—'}
                    </p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-sm text-[#525252]">
                  <div>Fixed Leg BPS: <span className="font-mono">{formatNumber(result.fixed_leg_bps, 4)}</span></div>
                  <div>{secondaryFlowLabel} Leg BPS: <span className="font-mono">{formatNumber(result.yoy_leg_bps, 4)}</span></div>
                  <div>Fixed Leg NPV: <span className="font-mono">{formatNumber(result.fixed_leg_npv, 2)}</span></div>
                  <div>{secondaryFlowLabel} Leg NPV: <span className="font-mono">{formatNumber(tradeKind === 'ZeroCoupon' ? result.inflation_leg_npv : result.yoy_leg_npv, 2)}</span></div>
                </div>
              </div>
            )}
            {!loading && result && includeFlows && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-semibold text-[#0a0a0a]">Flows</h3>
                  <button
                    type="button"
                    onClick={() => {
                      const csv = buildFlowCsv(activeFlows);
                      const blob = new Blob([csv], { type: 'text/csv' });
                      const url = URL.createObjectURL(blob);
                      const link = document.createElement('a');
                      link.href = url;
                      link.download = `inflation-swap-${tradeKind.toLowerCase()}-${activeFlowTab}-flows.csv`;
                      document.body.appendChild(link);
                      link.click();
                      document.body.removeChild(link);
                      URL.revokeObjectURL(url);
                    }}
                    className="px-3 py-1.5 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
                  >
                    Download CSV
                  </button>
                </div>
                <div className="flex gap-2 mb-3">
                  <button
                    type="button"
                    onClick={() => setActiveFlowTab('fixed')}
                    className={`px-3 py-1.5 text-xs rounded-lg ${activeFlowTab === 'fixed' ? 'bg-[#0a0a0a] text-white' : 'border border-[#d4d4d4] text-[#525252] hover:bg-[#f5f5f5]'}`}
                  >
                    Fixed
                  </button>
                  <button
                    type="button"
                    onClick={() => setActiveFlowTab('inflation')}
                    className={`px-3 py-1.5 text-xs rounded-lg ${activeFlowTab === 'inflation' ? 'bg-[#0a0a0a] text-white' : 'border border-[#d4d4d4] text-[#525252] hover:bg-[#f5f5f5]'}`}
                  >
                    {secondaryFlowLabel}
                  </button>
                </div>
                <div className="overflow-auto max-h-72">
                  <table className="w-full text-xs">
                    <thead className="text-[#737373]">
                      <tr>
                        <th className="text-left py-1">payment_date</th>
                        <th className="text-left py-1">accrual_start_date</th>
                        <th className="text-left py-1">accrual_end_date</th>
                        <th className="text-left py-1">fixing_date</th>
                        <th className="text-right py-1">index_fixing</th>
                        <th className="text-right py-1">spread</th>
                        <th className="text-right py-1">rate</th>
                        <th className="text-right py-1">amount</th>
                        <th className="text-right py-1">discount</th>
                        <th className="text-right py-1">present_value</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono">
                      {activeFlows.map((flow: any, index: number) => (
                        <tr key={index} className="border-t border-[#f5f5f5]">
                          <td className="py-1 text-[#525252]">{flow.payment_date || '—'}</td>
                          <td>{flow.accrual_start_date || '—'}</td>
                          <td>{flow.accrual_end_date || '—'}</td>
                          <td>{flow.fixing_date || ''}</td>
                          <td className="text-right">{flow.index_fixing !== undefined ? formatNumber(flow.index_fixing, 6) : ''}</td>
                          <td className="text-right">{flow.spread !== undefined ? formatNumber(flow.spread, 6) : ''}</td>
                          <td className="text-right">{flow.rate !== undefined ? formatNumber(flow.rate, 6) : ''}</td>
                          <td className="text-right">{formatNumber(flow.amount, 2)}</td>
                          <td className="text-right">{flow.discount !== undefined ? formatNumber(flow.discount, 6) : ''}</td>
                          <td className="text-right">{formatNumber(flow.present_value, 2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        </div>

        {appGraph?.swapsInflationId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/swaps/inflation"
              entityId={appGraph.swapsInflationId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
