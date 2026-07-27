import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import PricingErrorCard from '../../components/products/PricingErrorCard';
import PricingTraceLink from '../../components/products/PricingTraceLink';
import IndexPicker from '../../components/curves/IndexPicker';
import { AnyCurvePoint, Curve, IndexDef, IndexRef, DAY_COUNTERS, CALENDARS, BUSINESS_DAY_CONVENTIONS, collectIndexRefIds } from '../../lib/types';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { priceSwaption } from '../../lib/api/swaptionPricingService';
import {
  persistSwaptionGraph,
  buildSwaptionPriceArm,
  asSwaptionAppGraph,
  type SwaptionAppGraph,
  type CurveGraphInput,
} from '../../lib/api/swaptionSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import { curveBodyForApi, normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { getQuoteBook, getResolutionMode, resolveQuoteValue } from '../../lib/storage/quoteBook';
import { buildPricingQuoteSnapshotWithBackend } from '../../lib/marketDataBackend';
import { SwaptionRequest, deriveSwaptionName, getSwaptionEntryById, saveSwaption } from '../../lib/storage/swaptions';
import { buildSourceRefs } from '../../lib/storage/sourceRefs';
import { VolSurfaceSpec, getVolSurfaces, getVolSurfaceResolutionMode, resolveVolSurfaceSnapshot } from '../../lib/storage/volSurfaces';
import { buildSwaptionVolWirePayload, VolSurfacePayloadError } from '../../lib/storage/volSurfacePayload';
import { getSwaptionModelById, getSwaptionModels, SwaptionModelRecord } from '../../lib/storage/swaptionModels';
import { useAuth } from '../../hooks/useAuth';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

type SwapType = 'Payer' | 'Receiver';
type ExerciseType = 'European' | 'Bermudan' | 'American';
type SettlementType = 'Physical' | 'Cash';
type VolatilityType = 'Normal' | 'Lognormal' | 'ShiftedLognormal';
type IrModelType = 'Black' | 'ShiftedBlack' | 'Bachelier' | 'HullWhiteLattice';
type ModelParamMode = 'Explicit' | 'Calibrate';
type UnderlyingType = 'IborSwap' | 'OisSwap';
type SettlementMethod = 'ParYieldCurve' | 'CollateralizedCashPrice';

interface ScheduleParams {
  calendar: string;
  effectiveDate: string;
  terminationDate: string;
  frequency: string;
  convention: string;
  terminationDateConvention: string;
  dateGenerationRule: string;
}

interface FixedLegParams {
  notional: number;
  rate: number;
  dayCounter: string;
  paymentConvention: string;
  schedule: ScheduleParams;
}

interface FloatingLegParams {
  notional: number;
  spread: number;
  dayCounter: string;
  paymentConvention: string;
  fixingDays: number;
  inArrears: boolean;
  schedule: ScheduleParams;
}

interface OvernightLegParams {
  notional: number;
  spread: number;
  dayCounter: string;
  paymentConvention: string;
  paymentCalendar: string;
  paymentLag: number;
  averagingMethod: string;
  lookbackDays: number;
  lockoutDays: number;
  applyObservationShift: boolean;
  telescopicValueDates: boolean;
  schedule: ScheduleParams;
}

interface SwaptionResultData {
  npv?: number;
  implied_volatility?: number;
  atm_forward?: number;
  annuity?: number;
  delta?: number;
  vega?: number;
  dv01?: number;
  gamma?: number;
  theta?: number;
}

const FREQUENCIES = ['Annual', 'Semiannual', 'Quarterly', 'Monthly'];
const EXERCISE_TYPES: ExerciseType[] = ['European', 'Bermudan', 'American'];
const SETTLEMENT_TYPES: SettlementType[] = ['Physical', 'Cash'];
const VOL_TYPES: VolatilityType[] = ['Normal', 'Lognormal', 'ShiftedLognormal'];
const MODEL_TYPES: IrModelType[] = ['Black', 'ShiftedBlack', 'Bachelier', 'HullWhiteLattice'];

async function resolveIndexDefs(ids: string[]): Promise<IndexDef[]> {
  if (ids.length === 0) return [];
  const savedSpecs = await indexStore.getAll();
  const result: IndexDef[] = [];
  const seen = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    const saved = savedSpecs.find(s => s.id === id);
    if (!saved) continue;
    const def = storedToRateIndexDef(saved);
    if (def) result.push(def);
  }
  return result;
}

function collectIndexIdsFromCurves(curves: Array<{ points?: AnyCurvePoint[] }>): string[] {
  const allPoints = curves.flatMap(c => c.points || []);
  return collectIndexRefIds(allPoints);
}

function collectQuoteIdsFromCurves(curves: Array<{ points?: AnyCurvePoint[] }>): string[] {
  const allPoints = curves.flatMap(c => c.points || []);
  return Array.from(new Set(allPoints.map(p => (p as any)?.point?.quote_id).filter(Boolean)));
}

function getSwaptionVolBase(vol: any): any {
  if (!vol || typeof vol !== 'object') return undefined;
  // New schema shape:
  // vol.payload.payload_type = SwaptionVolConstantSpec
  // vol.payload.payload.base = { ... }
  const nestedBase = vol?.payload?.payload?.base;
  if (nestedBase && typeof nestedBase === 'object') return nestedBase;
  // Backward compatibility with previously saved portal payloads.
  const legacyBase = vol?.payload?.base;
  return legacyBase && typeof legacyBase === 'object' ? legacyBase : undefined;
}

function deriveSwapIndexId(indexId: string, indices: IndexDef[], underlyingType: UnderlyingType, surface?: VolSurfaceSpec): string {
  const explicit = (surface as any)?.swap_index_id as string | undefined;
  if (explicit && explicit.trim().length > 0) return explicit.trim();

  if (underlyingType === 'OisSwap') {
    return `${indexId}_OIS`;
  }

  const idx = indices.find(i => i.id === indexId);
  const currency = (idx?.currency || indexId.split('_')[0] || 'USD').toUpperCase();
  const tenorN = idx?.tenor?.n;
  const tenorUnit = idx?.tenor?.unit;
  const unitCodeMap: Record<string, string> = {
    Days: 'D',
    Weeks: 'W',
    Months: 'M',
    Years: 'Y',
  };
  if (typeof tenorN === 'number' && tenorN > 0 && tenorUnit && unitCodeMap[tenorUnit]) {
    return `${currency}_SWAP_${tenorN}${unitCodeMap[tenorUnit]}`;
  }

  const tenorFromId = indexId.match(/(\d+[DWMY])$/i)?.[1]?.toUpperCase();
  if (tenorFromId) {
    return `${currency}_SWAP_${tenorFromId}`;
  }
  return `${currency}_SWAP_6M`;
}

function parseBermudanExerciseDates(raw: string): string[] {
  return Array.from(
    new Set(
      raw
        .split(/\r?\n|,/)
        .map((v) => v.trim())
        .filter((v) => /^\d{4}-\d{2}-\d{2}$/.test(v))
    )
  ).sort((a, b) => a.localeCompare(b));
}

// Weekend-skip business-day add. Used to compute leg effective dates from
// (exercise + index.fixing_days) without a backend round-trip. Holidays are
// not handled — if `src/lib/` ever gains a calendar-aware BD-roller, swap
// this out and pass the index's calendar through. Until then, holiday-edge
// trades require the user to hand-edit the leg effective_date after the
// auto-bump (the backend rejects with a clear "spot_days mismatch" error).
function addBusinessDaysWeekendSkip(isoDate: string, days: number): string | null {
  if (!isoDate) return null;
  const ms = Date.parse(`${isoDate}T00:00:00Z`);
  if (!Number.isFinite(ms)) return null;
  const out = new Date(ms);
  let remaining = Math.max(0, Math.floor(days));
  while (remaining > 0) {
    out.setUTCDate(out.getUTCDate() + 1);
    const dow = out.getUTCDay();
    if (dow !== 0 && dow !== 6) remaining--;
  }
  // Guard the days===0 path: if the anchor itself is a weekend, roll forward.
  while (out.getUTCDay() === 0 || out.getUTCDay() === 6) {
    out.setUTCDate(out.getUTCDate() + 1);
  }
  return out.toISOString().split('T')[0];
}

export default function Swaption() {
  const { id } = useParams();
  const navigate = useNavigate();
  const ui = entityUi.swaption;
  const isNew = !id || id === 'new';
  const today = new Date().toISOString().split('T')[0];
  const oneYearLater = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];
  const sixYearsLater = new Date(Date.now() + 6 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

  const [asOfDate, setAsOfDate] = useState(today);
  const { asOfDate: globalAsOf } = useAsOfDate();
  useEffect(() => { setAsOfDate(globalAsOf); }, [globalAsOf]);

  const [swaptionId, setSwaptionId] = useState(id && id !== 'new' ? id : '');
  const [swaptionName, setSwaptionName] = useState('');
  // The persisted server-side UUID graph (curve_set + vol_surface +
  // swaption_model + swaption). Present ⇒ price by-reference;
  // absent ⇒ inline. Loaded from the saved entry's ``appGraph``.
  const [appGraph, setAppGraph] = useState<SwaptionAppGraph | null>(null);
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');
  const [exerciseType, setExerciseType] = useState<ExerciseType>('European');
  const [settlementType, setSettlementType] = useState<SettlementType>('Physical');
  const [settlementMethod, setSettlementMethod] = useState<SettlementMethod>('ParYieldCurve');
  const [exerciseDate, setExerciseDate] = useState(oneYearLater);
  const [bermudanExerciseDatesRaw, setBermudanExerciseDatesRaw] = useState('');
  const [attachQuotes, setAttachQuotes] = useState(true);
  const [swaptionPricingRebump, setSwaptionPricingRebump] = useState(false);
  const [swaptionPricingDetails, setSwaptionPricingDetails] = useState(false);

  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [forecastCurveId, setForecastCurveId] = useState('');
  const [forecastCurve, setForecastCurve] = useState<Curve | null>(null);
  const [useSameCurve, setUseSameCurve] = useState(true);

  const [swapType, setSwapType] = useState<SwapType>('Payer');
  const [underlyingType, setUnderlyingType] = useState<UnderlyingType>('IborSwap');
  const [indexRef, setIndexRef] = useState<IndexRef>({ id: 'EURIBOR_6M' });
  const [fixedLeg, setFixedLeg] = useState<FixedLegParams>({
    notional: 1000000,
    rate: 0.025,
    dayCounter: 'Thirty360',
    paymentConvention: 'ModifiedFollowing',
    schedule: {
      calendar: 'TARGET',
      effectiveDate: oneYearLater,
      terminationDate: sixYearsLater,
      frequency: 'Annual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });
  const [floatingLeg, setFloatingLeg] = useState<FloatingLegParams>({
    notional: 1000000,
    spread: 0,
    dayCounter: 'Actual360',
    paymentConvention: 'ModifiedFollowing',
    fixingDays: 2,
    inArrears: false,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: oneYearLater,
      terminationDate: sixYearsLater,
      frequency: 'Semiannual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });
  const [overnightLeg, setOvernightLeg] = useState<OvernightLegParams>({
    notional: 1000000,
    spread: 0,
    dayCounter: 'Actual360',
    paymentConvention: 'ModifiedFollowing',
    paymentCalendar: 'TARGET',
    paymentLag: 2,
    averagingMethod: 'Compound',
    lookbackDays: -1,
    lockoutDays: 0,
    applyObservationShift: false,
    telescopicValueDates: false,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: oneYearLater,
      terminationDate: sixYearsLater,
      frequency: 'Annual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });

  const [volId, setVolId] = useState('swaptvol');
  const [volType, setVolType] = useState<VolatilityType>('Normal');
  const [volatility, setVolatility] = useState(0.005);
  const [volDayCounter, setVolDayCounter] = useState('Actual365Fixed');
  const [volCalendar, setVolCalendar] = useState('TARGET');
  const [volConvention, setVolConvention] = useState('ModifiedFollowing');
  const [volDisplacement, setVolDisplacement] = useState(0.0);
  const [volSurfaces, setVolSurfaces] = useState<VolSurfaceSpec[]>([]);
  const [selectedVolSurfaceId, setSelectedVolSurfaceId] = useState<string>('');
  const [useSurfaceGrid, setUseSurfaceGrid] = useState(true);

  const [modelId, setModelId] = useState('swaptmodel');
  const [modelType, setModelType] = useState<IrModelType>('Bachelier');
  const [modelParamMode, setModelParamMode] = useState<ModelParamMode>('Explicit');
  const [hwA, setHwA] = useState(0.03);
  const [hwSigma, setHwSigma] = useState(0.01);
  const [latticeSteps, setLatticeSteps] = useState(60);
  const [swaptionModels, setSwaptionModels] = useState<SwaptionModelRecord[]>([]);
  const [bermudanModelSource, setBermudanModelSource] = useState<'inline' | 'saved'>('inline');
  const [selectedSavedModelId, setSelectedSavedModelId] = useState('');

  // Cached `fixing_days` for the currently-selected float index. Sourced
  // synchronously by the auto-bump useEffect that keeps leg.effectiveDate
  // aligned with `exercise + spot_days BD`. Loaded from the async index
  // store whenever `indexRef.id` changes; null until the first resolution
  // completes (the bump effect falls back to 2 in that window).
  const [floatIndexFixingDays, setFloatIndexFixingDays] = useState<number | null>(null);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorInfo, setErrorInfo] = useState<PricingErrorInfo | undefined>(undefined);
  const [result, setResult] = useState<SwaptionResultData | null>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const { user, login } = useAuth();

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;
  const usingStoredSurface = selectedVolSurfaceId.trim().length > 0;
  const selectedSurfaceShape = (
    usingStoredSurface
      ? (volSurfaces.find(s => s.id === selectedVolSurfaceId)?.base?.shape || 'Constant')
      : 'Constant'
  );
  const surfaceProvidesGrid = usingStoredSurface && selectedSurfaceShape !== 'Constant';
  const swaptionDisplayName = `${swapType} ${indexRef.id || 'Index'} Swaption`;

  useEffect(() => {
    if (!isNew && id) {
      // Requests store a resolved vol payload, so default to inline mode on open.
      setSelectedVolSurfaceId('');
      getSwaptionEntryById(id).then(entry => {
        if (!entry) return;
        const saved = entry.request;
        setSwaptionId(entry.id);
        setSwaptionName(entry.name || '');
        setAppGraph(asSwaptionAppGraph(entry.appGraph));
        const request = saved;
        const samplePricing = request?.pricing || {};
        const sampleSwaption = request?.swaptions?.[0]?.swaption || {};
        const isOisSwap = sampleSwaption?.underlying_type === 'OisSwap';
        const sampleSwap = sampleSwaption?.underlying || sampleSwaption?.underlying_swap || {};
        const sampleFixed = sampleSwap.fixed_leg || {};
        const sampleFloat = sampleSwap.floating_leg || {};
        const sampleOvernight = sampleSwap.overnight_leg || {};
        const requestCurves = Array.isArray(samplePricing.curves) ? samplePricing.curves : [];
        const discount = requestCurves.find((c: any) => c.id === 'discount') || requestCurves[0];
        const forward = requestCurves.find((c: any) => c.id === 'forward') || requestCurves[1];
        const vol = Array.isArray(samplePricing.vol_surfaces) ? samplePricing.vol_surfaces[0] : null;
        const model = Array.isArray(samplePricing.models) ? samplePricing.models[0] : null;

        if (samplePricing.as_of_date) setAsOfDate(samplePricing.as_of_date);
        if (samplePricing.swaption_pricing_rebump !== undefined) setSwaptionPricingRebump(!!samplePricing.swaption_pricing_rebump);
        if (samplePricing.swaption_pricing_details !== undefined) setSwaptionPricingDetails(!!samplePricing.swaption_pricing_details);
        if (sampleSwaption.exercise_type) setExerciseType(sampleSwaption.exercise_type);
        if (sampleSwaption.settlement_type) setSettlementType(sampleSwaption.settlement_type);
        if (sampleSwaption.settlement_method) setSettlementMethod(sampleSwaption.settlement_method);
        if (sampleSwaption.exercise_date) setExerciseDate(sampleSwaption.exercise_date);
        if (Array.isArray(sampleSwaption.exercise_dates) && sampleSwaption.exercise_dates.length > 0) {
          setBermudanExerciseDatesRaw(sampleSwaption.exercise_dates.join('\n'));
        }
        if (sampleSwap.swap_type) setSwapType(sampleSwap.swap_type);
        if (isOisSwap) setUnderlyingType('OisSwap');
        if (!isOisSwap && sampleFloat.index?.id) setIndexRef({ id: sampleFloat.index.id });
        if (isOisSwap && sampleOvernight.index?.id) setIndexRef({ id: sampleOvernight.index.id });

        if (discount) {
          setDiscountCurveId(discount.id || '');
          setDiscountCurve({
            id: discount.id || 'discount',
            name: discount.id || 'Discount',
            currency: 'EUR',
            role: 'discount',
            day_counter: discount.day_counter,
            interpolator: discount.interpolator,
            bootstrap_trait: discount.bootstrap_trait,
            reference_date: discount.reference_date || asOfDate,
            points: discount.points || [],
            createdAt: '',
            updatedAt: '',
          });
        }
        if (forward) {
          setForecastCurveId(forward.id || '');
          setForecastCurve({
            id: forward.id || 'forward',
            name: forward.id || 'Forward',
            currency: 'EUR',
            role: 'forward',
            day_counter: forward.day_counter,
            interpolator: forward.interpolator,
            bootstrap_trait: forward.bootstrap_trait,
            reference_date: forward.reference_date || asOfDate,
            points: forward.points || [],
            createdAt: '',
            updatedAt: '',
          });
          setUseSameCurve(false);
        }

        setFixedLeg({
          notional: sampleFixed.notional || fixedLeg.notional,
          rate: sampleFixed.rate || fixedLeg.rate,
          dayCounter: sampleFixed.day_counter || fixedLeg.dayCounter,
          paymentConvention: sampleFixed.payment_convention || fixedLeg.paymentConvention,
          schedule: {
            calendar: sampleFixed?.schedule?.calendar || fixedLeg.schedule.calendar,
            effectiveDate: sampleFixed?.schedule?.effective_date || fixedLeg.schedule.effectiveDate,
            terminationDate: sampleFixed?.schedule?.termination_date || fixedLeg.schedule.terminationDate,
            frequency: sampleFixed?.schedule?.frequency || fixedLeg.schedule.frequency,
            convention: sampleFixed?.schedule?.convention || fixedLeg.schedule.convention,
            terminationDateConvention: sampleFixed?.schedule?.termination_date_convention || fixedLeg.schedule.terminationDateConvention,
            dateGenerationRule: sampleFixed?.schedule?.date_generation_rule || fixedLeg.schedule.dateGenerationRule,
          },
        });
        if (!isOisSwap) {
          setFloatingLeg({
            notional: sampleFloat.notional || floatingLeg.notional,
            spread: sampleFloat.spread || floatingLeg.spread,
            dayCounter: sampleFloat.day_counter || floatingLeg.dayCounter,
            paymentConvention: sampleFloat.payment_convention || floatingLeg.paymentConvention,
            fixingDays: sampleFloat.fixing_days ?? floatingLeg.fixingDays,
            inArrears: sampleFloat.in_arrears ?? floatingLeg.inArrears,
            schedule: {
              calendar: sampleFloat?.schedule?.calendar || floatingLeg.schedule.calendar,
              effectiveDate: sampleFloat?.schedule?.effective_date || floatingLeg.schedule.effectiveDate,
              terminationDate: sampleFloat?.schedule?.termination_date || floatingLeg.schedule.terminationDate,
              frequency: sampleFloat?.schedule?.frequency || floatingLeg.schedule.frequency,
              convention: sampleFloat?.schedule?.convention || floatingLeg.schedule.convention,
              terminationDateConvention: sampleFloat?.schedule?.termination_date_convention || floatingLeg.schedule.terminationDateConvention,
              dateGenerationRule: sampleFloat?.schedule?.date_generation_rule || floatingLeg.schedule.dateGenerationRule,
            },
          });
        }
        if (isOisSwap) {
          setOvernightLeg({
            notional: sampleOvernight.notional || overnightLeg.notional,
            spread: sampleOvernight.spread || overnightLeg.spread,
            dayCounter: sampleOvernight.day_counter || overnightLeg.dayCounter,
            paymentConvention: sampleOvernight.payment_convention || overnightLeg.paymentConvention,
            paymentCalendar: sampleOvernight.payment_calendar || overnightLeg.paymentCalendar,
            paymentLag: sampleOvernight.payment_lag ?? overnightLeg.paymentLag,
            averagingMethod: sampleOvernight.averaging_method || overnightLeg.averagingMethod,
            lookbackDays: sampleOvernight.lookback_days ?? overnightLeg.lookbackDays,
            lockoutDays: sampleOvernight.lockout_days ?? overnightLeg.lockoutDays,
            applyObservationShift: sampleOvernight.apply_observation_shift ?? overnightLeg.applyObservationShift,
            telescopicValueDates: sampleOvernight.telescopic_value_dates ?? overnightLeg.telescopicValueDates,
            schedule: {
              calendar: sampleOvernight?.schedule?.calendar || overnightLeg.schedule.calendar,
              effectiveDate: sampleOvernight?.schedule?.effective_date || overnightLeg.schedule.effectiveDate,
              terminationDate: sampleOvernight?.schedule?.termination_date || overnightLeg.schedule.terminationDate,
              frequency: sampleOvernight?.schedule?.frequency || overnightLeg.schedule.frequency,
              convention: sampleOvernight?.schedule?.convention || overnightLeg.schedule.convention,
              terminationDateConvention: sampleOvernight?.schedule?.termination_date_convention || overnightLeg.schedule.terminationDateConvention,
              dateGenerationRule: sampleOvernight?.schedule?.date_generation_rule || overnightLeg.schedule.dateGenerationRule,
            },
          });
        }

        const volBase = getSwaptionVolBase(vol);
        if (vol?.id) setVolId(vol.id);
        if (volBase?.volatility_type) setVolType(volBase.volatility_type);
        if (volBase?.constant_vol !== undefined) setVolatility(volBase.constant_vol);
        if (volBase?.day_counter) setVolDayCounter(volBase.day_counter);
        if (volBase?.calendar) setVolCalendar(volBase.calendar);
        if (volBase?.business_day_convention) setVolConvention(volBase.business_day_convention);
        if (volBase?.displacement !== undefined) setVolDisplacement(volBase.displacement);

        if (model?.id) setModelId(model.id);
        if (model?.payload?.model_type) setModelType(model.payload.model_type);
        if (model?.payload?.param_mode) setModelParamMode(model.payload.param_mode);
        if (typeof model?.payload?.hw_a === 'number') setHwA(model.payload.hw_a);
        if (typeof model?.payload?.hw_sigma === 'number') setHwSigma(model.payload.hw_sigma);
        if (typeof model?.payload?.lattice_steps === 'number') setLatticeSteps(model.payload.lattice_steps);
      });
    }
  }, [id, isNew]);

  useEffect(() => {
    setVolSurfaces(getVolSurfaces());
    const models = getSwaptionModels();
    setSwaptionModels(models);
    if (models.length > 0 && !selectedSavedModelId) {
      setSelectedSavedModelId(models[0].id);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!indexRef.id) {
      setFloatIndexFixingDays(null);
      return () => { cancelled = true; };
    }
    resolveIndexDefs([indexRef.id]).then((defs) => {
      if (cancelled) return;
      const def = defs[0];
      setFloatIndexFixingDays(typeof def?.fixing_days === 'number' ? def.fixing_days : null);
    });
    return () => { cancelled = true; };
  }, [indexRef.id]);

  // Auto-bump the underlying-swap effective dates to `exercise + spot_days BD`
  // whenever the exercise input changes. Mirrors Bloomberg SWPM and keeps
  // the wire payload's `BD-roll(exercise, spot_days) == effective_date`
  // assertion satisfied without a per-trade spot_days override field.
  // Bermudan: uses the earliest date in the Bermudan exercise list (the list
  // is already pre-sorted by `parseBermudanExerciseDates`, but `[0]` reads
  // the earliest defensively). Manual overrides after the bump are respected
  // because each setter compares against `prev.schedule.effectiveDate`
  // before issuing a setState — a hand-edit only re-bumps if the user also
  // changes the exercise date afterwards. Holiday boundaries are not
  // handled (weekend-skip only); the user fixes those by hand on backend
  // rejection.
  useEffect(() => {
    const spotDays = floatIndexFixingDays ?? 2;
    let exerciseAnchor = exerciseDate;
    if (exerciseType === 'Bermudan') {
      const dates = parseBermudanExerciseDates(bermudanExerciseDatesRaw);
      exerciseAnchor = dates[0] || exerciseDate;
    }
    if (!exerciseAnchor) return;
    const rolled = addBusinessDaysWeekendSkip(exerciseAnchor, spotDays);
    if (!rolled) return;
    setFixedLeg((prev) =>
      prev.schedule.effectiveDate === rolled
        ? prev
        : { ...prev, schedule: { ...prev.schedule, effectiveDate: rolled } }
    );
    setFloatingLeg((prev) =>
      prev.schedule.effectiveDate === rolled
        ? prev
        : { ...prev, schedule: { ...prev.schedule, effectiveDate: rolled } }
    );
    setOvernightLeg((prev) =>
      prev.schedule.effectiveDate === rolled
        ? prev
        : { ...prev, schedule: { ...prev.schedule, effectiveDate: rolled } }
    );
  }, [exerciseDate, exerciseType, bermudanExerciseDatesRaw, floatIndexFixingDays]);

  useEffect(() => {
    if (!selectedVolSurfaceId) return;
    const surface = volSurfaces.find(s => s.id === selectedVolSurfaceId);
    if (!surface) return;
    setVolId(surface.id);
    setVolType(surface.base?.volatility_type || 'Normal');
    setVolatility(surface.base?.constant_vol ?? volatility);
    if (surface.base?.day_counter) setVolDayCounter(surface.base.day_counter);
    if (surface.base?.calendar) setVolCalendar(surface.base.calendar);
    if (surface.base?.business_day_convention) setVolConvention(surface.base.business_day_convention);
    if (surface.base?.displacement !== undefined) setVolDisplacement(surface.base.displacement);
  }, [selectedVolSurfaceId, volSurfaces]);

  useEffect(() => {
    if (modelType === 'Bachelier') {
      setVolType('Normal');
    } else if (modelType === 'Black') {
      setVolType('Lognormal');
    } else if (modelType === 'ShiftedBlack') {
      setVolType('ShiftedLognormal');
    }
  }, [modelType]);

  useEffect(() => {
    if (exerciseType === 'Bermudan') {
      setModelType('HullWhiteLattice');
      if (!bermudanExerciseDatesRaw.trim()) {
        const second = new Date(Date.parse(oneYearLater) + 365 * 24 * 60 * 60 * 1000)
          .toISOString()
          .slice(0, 10);
        setBermudanExerciseDatesRaw(`${oneYearLater}\n${second}`);
      }
    }
  }, [exerciseType, bermudanExerciseDatesRaw, oneYearLater]);

  useEffect(() => {
    if (bermudanModelSource !== 'saved' || !selectedSavedModelId) return;
    const saved = getSwaptionModelById(selectedSavedModelId);
    if (!saved) return;
    setModelId(saved.id);
    setModelType('HullWhiteLattice');
    setModelParamMode('Explicit');
    setHwA(saved.hw_a);
    setHwSigma(saved.hw_sigma);
  }, [bermudanModelSource, selectedSavedModelId]);

  const buildRequest = async (
    curves: any[],
    resolvedIndices: IndexDef[],
    preferBackendQuotes: boolean
  ): Promise<SwaptionRequest> => {
    const surfaceBase = selectedVolSurfaceId ? volSurfaces.find(s => s.id === selectedVolSurfaceId) : undefined;
    const surfaceSnapshot = surfaceBase
      ? resolveVolSurfaceSnapshot(surfaceBase, asOfDate, getVolSurfaceResolutionMode())
      : null;
    const surface = surfaceBase && surfaceSnapshot
      ? {
          ...surfaceBase,
          base: { ...surfaceBase.base, ...surfaceSnapshot.base },
          expiries: surfaceSnapshot.expiries || surfaceBase.expiries,
          tenors: surfaceSnapshot.tenors || surfaceBase.tenors,
          grid: surfaceSnapshot.grid || surfaceBase.grid,
        }
      : surfaceBase;

    const resolveSurfaceGridFromQuotes = (spec: VolSurfaceSpec | undefined) => {
      if (!spec || spec.quote_source !== 'quote_book' || !spec.quote_ids?.length) return spec?.grid;
      const entries = getQuoteBook().filter(q => (q.quote_type || 'Curve') === 'Volatility');
      const mode = getResolutionMode();
      const byId = new Map(entries.map(e => [e.id, e]));
      return spec.quote_ids.map(row => row.map(id => {
        const entry = byId.get(id);
        if (!entry) return NaN;
        const val = resolveQuoteValue(entry.series, asOfDate, mode);
        return val === null ? NaN : val;
      }));
    };

    const surfaceGrid = resolveSurfaceGridFromQuotes(surface);
    const surfaceWithGrid = surface ? { ...surface, grid: surfaceGrid || surface.grid } : surface;
    // The pricingAsOfDate must equal surface.base.reference_date in strict
    // mode (request/sample_vol_surfaces_request.cpp:352-356; same rule applies
    // to /price-swaption). Mirror VolWorkbench.runSample so loading a stored
    // surface dated at calibration time and pricing it today doesn't trip
    // "Strict mode: pricing.as_of_date must equal vol referenceDate".
    const pricingAsOfDate = surfaceWithGrid?.base?.reference_date || asOfDate;

    // The page's manual "Constant Vol" input is allowed to override the saved
    // surface's `constant_vol` when the user unticks "Use surface grid value".
    // This override path is meaningful only for Constant-shape surfaces (for
    // AtmMatrix/SmileCube/SABR the σ comes from a grid the helper resolves,
    // and the UI hides the manual-σ input — see Volatility & Model card
    // below). For other shapes we forward the surface unchanged.
    const surfaceShape = (surfaceWithGrid?.base?.shape || 'Constant');
    const surfaceForWire = surfaceWithGrid && surfaceShape === 'Constant' && !useSurfaceGrid
      ? { ...surfaceWithGrid, base: { ...surfaceWithGrid.base, constant_vol: volatility } }
      : surfaceWithGrid;

    const swapIndexId = deriveSwapIndexId(indexRef.id, resolvedIndices, underlyingType, surface);
    const curveQuoteIds = collectQuoteIdsFromCurves(curves);
    const pricingQuoteSnapshot = attachQuotes && curveQuoteIds.length > 0
      ? (
          await buildPricingQuoteSnapshotWithBackend(asOfDate, {
            quoteType: 'Curve',
            quoteIds: curveQuoteIds,
            preferBackend: preferBackendQuotes,
          })
        ).quotes
      : [];
    const bermudanDates = parseBermudanExerciseDates(bermudanExerciseDatesRaw);
    // quote_ids reach the orchestrator UNRESOLVED (server-side market-data
    // resolution pulls the value); inline points pass through.
    const resolvedCurvesForApi = curves.map(normalizeCurveForApi);
    // Drop inflation curves and pointless curves before they hit
    // pricing.curves — they're bootstrapped via term_structure_parser.cpp:36
    // which throws "Empty list of points for term structure" inside
    // PricingRegistryBuilder.build(), failing before per-trade dispatch.
    // CurveSetSelector role-filters today so this is defensive, mirroring
    // VolWorkbench.runSample.
    const filteredCurvesForApi = resolvedCurvesForApi.filter((c: any) =>
      c?.role !== 'inflation' && Array.isArray(c?.points) && c.points.length > 0
    );

    // Quote resolver shared by the vol-payload helper. Reuses the page's
    // existing quote-book wiring (anchored at pricingAsOfDate so a stored
    // surface dated in the past resolves quotes against its calibration date,
    // not today). VolWorkbench.runSample uses an identical pattern.
    const resolverMode = getResolutionMode();
    const bookByQuoteId = new Map(getQuoteBook().map(q => [q.id, q]));
    const resolveQuoteValueById = (qid?: string): number | null => {
      if (!qid) return null;
      const b = bookByQuoteId.get(qid);
      if (!b) return null;
      return resolveQuoteValue(b.series, pricingAsOfDate, resolverMode);
    };

    // Route the wire payload through the canonical helper so the envelope
    // matches surface.base.shape (Constant / AtmMatrix2D / SmileCube3D /
    // SabrParams). Inline-σ mode synthesizes a Constant shim so the call
    // site stays single-shape.
    let volSurfacePayload: any;
    try {
      if (surfaceForWire) {
        volSurfacePayload = buildSwaptionVolWirePayload(
          surfaceForWire,
          swapIndexId,
          pricingAsOfDate,
          resolveQuoteValueById,
        );
      } else {
        const inlineShim: VolSurfaceSpec = {
          id: volId,
          payload_type: 'SwaptionVolSpec',
          base: {
            reference_date: pricingAsOfDate,
            calendar: volCalendar,
            business_day_convention: volConvention,
            day_counter: volDayCounter,
            constant_vol: volatility,
            volatility_type: volType,
            shape: 'Constant',
            ...(volType === 'ShiftedLognormal' ? { displacement: volDisplacement } : {}),
          },
        } as VolSurfaceSpec;
        volSurfacePayload = buildSwaptionVolWirePayload(
          inlineShim,
          swapIndexId,
          pricingAsOfDate,
          resolveQuoteValueById,
        );
      }
    } catch (err) {
      if (err instanceof VolSurfacePayloadError) throw err;
      throw new Error(err instanceof Error ? err.message : 'Failed to build swaption vol payload');
    }

    // Non-Constant swaption vol surfaces (AtmMatrix / SmileCube / SabrParams)
    // wire a `swap_index_id` on the SwaptionVolSpec inner payload, which the
    // backend resolves against `pricing.swap_indices`. Pre-Step-2 the page
    // always emitted SwaptionVolConstantSpec — no swap_index_id, no registry
    // entry needed — so this block didn't exist. Now that the canonical
    // helper emits a real swap_index_id, register it here. Mirrors the
    // CMS-leg path in IrSwap.tsx.
    //
    // OisSwap underlying not handled here yet: deriveSwapIndexId returns an
    // `${index}_OIS` form for OIS, and the backend expects a different
    // swap-index `kind`. Surfacing OIS swaptions on non-Constant surfaces is
    // a follow-up; the existing OIS+Constant path is unaffected.
    const swaptionUsesSwapIndexRegistry = surfaceShape !== 'Constant';
    const selectedFloatIndexDef = resolvedIndices.find((d) => d.id === indexRef.id);
    // spot_days is an index convention, not a per-trade override. The backend
    // enforces `BD-roll(exercise, swap_index.spot_days) == swap.effective_date`,
    // which the auto-bump useEffect upstream keeps in sync as the user edits
    // the exercise date. The `?? 2` is defensive coverage for legacy index
    // records persisted before `fixing_days` was a saved field — not an
    // assumption that all indices are T+2.
    const swaptionSwapIndexDef = (swaptionUsesSwapIndexRegistry && underlyingType === 'IborSwap') ? {
      id: swapIndexId,
      kind: 'IborSwapIndex',
      spot_days: selectedFloatIndexDef?.fixing_days ?? 2,
      calendar: selectedFloatIndexDef?.calendar || fixedLeg.schedule.calendar || 'TARGET',
      business_day_convention: selectedFloatIndexDef?.business_day_convention || 'ModifiedFollowing',
      float_index_id: indexRef.id,
      fixed_leg: {
        fixed_frequency: fixedLeg.schedule.frequency,
        fixed_day_counter: fixedLeg.dayCounter,
        fixed_calendar: fixedLeg.schedule.calendar,
        fixed_bdc: fixedLeg.schedule.convention,
        fixed_term_bdc: fixedLeg.schedule.terminationDateConvention,
        fixed_date_rule: fixedLeg.schedule.dateGenerationRule,
        fixed_eom: false,
      },
      float_leg: {
        float_tenor: selectedFloatIndexDef?.tenor || { n: 6, unit: 'Months' },
        float_calendar: selectedFloatIndexDef?.calendar || 'TARGET',
        float_bdc: selectedFloatIndexDef?.business_day_convention || 'ModifiedFollowing',
        float_term_bdc: selectedFloatIndexDef?.business_day_convention || 'ModifiedFollowing',
        float_date_rule: 'Forward',
        float_eom: false,
      },
    } : null;

    const modelPayload: any = {
      model_type: modelType,
    };
    if (modelType === 'HullWhiteLattice') {
      modelPayload.param_mode = modelParamMode;
      modelPayload.hw_a = hwA;
      modelPayload.hw_sigma = hwSigma;
      modelPayload.lattice_steps = latticeSteps;
    }

    return ({
    pricing: {
      as_of_date: pricingAsOfDate,
      ...(swaptionPricingRebump ? { swaption_pricing_rebump: true } : {}),
      ...(swaptionPricingDetails ? { swaption_pricing_details: true } : {}),
      indices: resolvedIndices.map(normalizeIndexDefForApi),
      ...(swaptionSwapIndexDef ? { swap_indices: [swaptionSwapIndexDef] } : {}),
      curves: filteredCurvesForApi,
      ...(attachQuotes && pricingQuoteSnapshot.length > 0
        ? { quotes: pricingQuoteSnapshot }
        : {}),
      vol_surfaces: [volSurfacePayload],
      models: [{
        id: modelId,
        payload_type: 'SwaptionModelSpec',
        payload: modelPayload,
      }],
    },
    swaptions: [{
      swaption: {
        exercise_type: exerciseType,
        settlement_type: settlementType,
        ...(settlementType === 'Cash' ? { settlement_method: settlementMethod } : {}),
        ...(exerciseType === 'Bermudan'
          ? { exercise_dates: bermudanDates, exercise_date: bermudanDates[0] || exerciseDate }
          : { exercise_date: exerciseDate }),
        underlying_type: underlyingType === 'OisSwap' ? 'OisSwap' : 'VanillaSwap',
        underlying: underlyingType === 'OisSwap'
          ? {
              swap_type: swapType,
              fixed_leg: {
                notional: fixedLeg.notional,
                schedule: {
                  calendar: fixedLeg.schedule.calendar,
                  effective_date: fixedLeg.schedule.effectiveDate,
                  termination_date: fixedLeg.schedule.terminationDate,
                  frequency: fixedLeg.schedule.frequency,
                  convention: fixedLeg.schedule.convention,
                  termination_date_convention: fixedLeg.schedule.terminationDateConvention,
                  date_generation_rule: fixedLeg.schedule.dateGenerationRule,
                },
                rate: fixedLeg.rate,
                day_counter: fixedLeg.dayCounter,
                payment_convention: fixedLeg.paymentConvention,
              },
              overnight_leg: {
                notional: overnightLeg.notional,
                schedule: {
                  calendar: overnightLeg.schedule.calendar,
                  effective_date: overnightLeg.schedule.effectiveDate,
                  termination_date: overnightLeg.schedule.terminationDate,
                  frequency: overnightLeg.schedule.frequency,
                  convention: overnightLeg.schedule.convention,
                  termination_date_convention: overnightLeg.schedule.terminationDateConvention,
                  date_generation_rule: overnightLeg.schedule.dateGenerationRule,
                },
                index: { id: indexRef.id },
                spread: overnightLeg.spread,
                day_counter: overnightLeg.dayCounter,
                payment_convention: overnightLeg.paymentConvention,
                payment_calendar: overnightLeg.paymentCalendar,
                payment_lag: overnightLeg.paymentLag,
                averaging_method: overnightLeg.averagingMethod,
                lookback_days: overnightLeg.lookbackDays,
                lockout_days: overnightLeg.lockoutDays,
                apply_observation_shift: overnightLeg.applyObservationShift,
                telescopic_value_dates: overnightLeg.telescopicValueDates,
              },
            }
          : {
              swap_type: swapType,
              fixed_leg: {
                notional: fixedLeg.notional,
                schedule: {
                  calendar: fixedLeg.schedule.calendar,
                  effective_date: fixedLeg.schedule.effectiveDate,
                  termination_date: fixedLeg.schedule.terminationDate,
                  frequency: fixedLeg.schedule.frequency,
                  convention: fixedLeg.schedule.convention,
                  termination_date_convention: fixedLeg.schedule.terminationDateConvention,
                  date_generation_rule: fixedLeg.schedule.dateGenerationRule,
                },
                rate: fixedLeg.rate,
                day_counter: fixedLeg.dayCounter,
                payment_convention: fixedLeg.paymentConvention,
              },
              floating_leg: {
                notional: floatingLeg.notional,
                schedule: {
                  calendar: floatingLeg.schedule.calendar,
                  effective_date: floatingLeg.schedule.effectiveDate,
                  termination_date: floatingLeg.schedule.terminationDate,
                  frequency: floatingLeg.schedule.frequency,
                  convention: floatingLeg.schedule.convention,
                  termination_date_convention: floatingLeg.schedule.terminationDateConvention,
                  date_generation_rule: floatingLeg.schedule.dateGenerationRule,
                },
                index: { id: indexRef.id },
                spread: floatingLeg.spread,
                day_counter: floatingLeg.dayCounter,
                payment_convention: floatingLeg.paymentConvention,
                fixing_days: floatingLeg.fixingDays,
                in_arrears: floatingLeg.inArrears,
              },
            },
      },
      discounting_curve: 'discount',
      forwarding_curve: useSameCurve ? 'discount' : 'forward',
      volatility: surface?.id || volId,
      model: modelId,
    }],
  });
  };

  // Map a portal-shaped curve onto the orchestrator's inline CurveRef
  // (which forbids extra fields). Role tagged via ``body.role`` so the
  // backend picks the engine's discountingCurve vs forwardingCurve.
  // Points pass through normalizeCurvePointForApi so the legacy
  // ``tenor_number``/``tenor_time_unit`` shape becomes the canonical
  // ``tenor: {n, unit}``; ``quote_id`` is forwarded unresolved
  // (the orchestrator's market-data resolution substitutes the value).
  const toThinACurve = (curve: Curve, role: 'discount' | 'forwarding'): any => {
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
    out.body = curveBodyForApi(curve, role);
    return out;
  };

  // Build the top-level inline ``vol_surface`` (VolSurfaceRef).
  // VolSurfaceRef forbids extra fields and requires ``kind`` +
  // ``payload`` in inline mode. The orchestrator reads
  // ``payload.base`` directly, so the deep-nested wire shape
  // ``buildSwaptionVolWirePayload`` produces (``payload.payload.base``)
  // is **flattened** here: we lift ``base`` (and ``swap_index_id``) to
  // the top of ``payload``.
  //
  // Translation boundary: today the orchestrator only translates the
  // Constant-shape base-value path. Richer payloads (SmileCube /
  // SabrParams) are preserved-but-ignored downstream and already fall
  // back to the const vol via ``buildBase``.
  const toThinAVolSurface = (
    surfaceForWire: VolSurfaceSpec | undefined,
    swapIndexId: string,
    pricingAsOfDate: string,
    resolveQuoteValueById: (qid?: string) => number | null,
  ): { name: string; kind: string; payload: Record<string, any> } => {
    let wirePayload: any;
    if (surfaceForWire) {
      wirePayload = buildSwaptionVolWirePayload(
        surfaceForWire,
        swapIndexId,
        pricingAsOfDate,
        resolveQuoteValueById,
      );
    } else {
      const inlineShim: VolSurfaceSpec = {
        id: volId,
        payload_type: 'SwaptionVolSpec',
        base: {
          reference_date: pricingAsOfDate,
          calendar: volCalendar,
          business_day_convention: volConvention,
          day_counter: volDayCounter,
          constant_vol: volatility,
          volatility_type: volType,
          shape: 'Constant',
          ...(volType === 'ShiftedLognormal' ? { displacement: volDisplacement } : {}),
        },
      } as VolSurfaceSpec;
      wirePayload = buildSwaptionVolWirePayload(
        inlineShim,
        swapIndexId,
        pricingAsOfDate,
        resolveQuoteValueById,
      );
    }
    // wirePayload shape (engine-FB-style):
    //   { id, payload_type: 'SwaptionVolSpec',
    //     payload: { swap_index_id, payload_type, payload: { base, ... } } }
    // Flatten the inner ``payload.payload`` (base + shape-specific
    // arrays) up to the orchestrator-side ``payload`` level so the
    // translator finds ``base`` at the depth it reads.
    const outer = wirePayload?.payload || {};
    const inner = outer.payload || {};
    const flat: Record<string, any> = { ...inner };
    if (outer.swap_index_id) flat.swap_index_id = outer.swap_index_id;
    return {
      name: wirePayload.id || volId || 'inline_vol',
      kind: wirePayload.payload_type,  // 'SwaptionVolSpec'
      payload: flat,
    };
  };

  // Build the top-level inline ``swaption_model`` (SwaptionModelRef).
  // ``kind`` is the IrModelType member name (``HullWhiteLattice`` /
  // ``Black`` / ``ShiftedBlack`` / ``Bachelier``), which the backend
  // maps onto the engine's model enum (HullWhiteLattice default). The
  // HW knobs live in payload; other kinds carry an empty payload
  // (they're pure model selectors).
  //
  // Model note: today the production swaption path is HullWhiteLattice/
  // Explicit, which prices off hw_sigma and IGNORES the BlackVol
  // surface forwarded above. The portal still forwards the surface
  // faithfully (orchestrator accepts it; engine just doesn't consume
  // it for HW). Wiring the surface to actually drive HW pricing is
  // deferred.
  const toThinAModel = (): { name: string; kind: string; payload: Record<string, any> } => {
    const payload: Record<string, any> = {};
    if (modelType === 'HullWhiteLattice') {
      payload.param_mode = modelParamMode;
      payload.hw_a = hwA;
      payload.hw_sigma = hwSigma;
      payload.lattice_steps = latticeSteps;
    }
    return {
      name: modelId,
      kind: modelType,
      payload,
    };
  };

  // Inline POST body for ``POST /v1/price/swaption``. Flat trade
  // body the backend reads (notional / strike / swap_type /
  // exercise_date / effective_date / termination_date) + role-tagged
  // top-level curves + top-level vol_surface + top-level
  // swaption_model. The SwaptionPriceRequest validator requires all
  // three when ``swaption`` is set inline.
  const buildThinARequest = async (
    curves: any[],
    resolvedIndices: IndexDef[],
  ): Promise<Record<string, any>> => {
    const surfaceBase = selectedVolSurfaceId ? volSurfaces.find(s => s.id === selectedVolSurfaceId) : undefined;
    const surfaceSnapshot = surfaceBase
      ? resolveVolSurfaceSnapshot(surfaceBase, asOfDate, getVolSurfaceResolutionMode())
      : null;
    const surface = surfaceBase && surfaceSnapshot
      ? {
          ...surfaceBase,
          base: { ...surfaceBase.base, ...surfaceSnapshot.base },
          expiries: surfaceSnapshot.expiries || surfaceBase.expiries,
          tenors: surfaceSnapshot.tenors || surfaceBase.tenors,
          grid: surfaceSnapshot.grid || surfaceBase.grid,
        }
      : surfaceBase;
    const resolveSurfaceGridFromQuotes = (spec: VolSurfaceSpec | undefined) => {
      if (!spec || spec.quote_source !== 'quote_book' || !spec.quote_ids?.length) return spec?.grid;
      const entries = getQuoteBook().filter(q => (q.quote_type || 'Curve') === 'Volatility');
      const mode = getResolutionMode();
      const byId = new Map(entries.map(e => [e.id, e]));
      return spec.quote_ids.map(row => row.map(id => {
        const entry = byId.get(id);
        if (!entry) return NaN;
        const val = resolveQuoteValue(entry.series, asOfDate, mode);
        return val === null ? NaN : val;
      }));
    };
    const surfaceGrid = resolveSurfaceGridFromQuotes(surface);
    const surfaceWithGrid = surface ? { ...surface, grid: surfaceGrid || surface.grid } : surface;
    const pricingAsOfDate = surfaceWithGrid?.base?.reference_date || asOfDate;
    const surfaceShape = (surfaceWithGrid?.base?.shape || 'Constant');
    const surfaceForWire = surfaceWithGrid && surfaceShape === 'Constant' && !useSurfaceGrid
      ? { ...surfaceWithGrid, base: { ...surfaceWithGrid.base, constant_vol: volatility } }
      : surfaceWithGrid;
    const swapIndexId = deriveSwapIndexId(indexRef.id, resolvedIndices, underlyingType, surface);
    const resolverMode = getResolutionMode();
    const bookByQuoteId = new Map(getQuoteBook().map(q => [q.id, q]));
    const resolveQuoteValueById = (qid?: string): number | null => {
      if (!qid) return null;
      const b = bookByQuoteId.get(qid);
      if (!b) return null;
      return resolveQuoteValue(b.series, pricingAsOfDate, resolverMode);
    };

    let volSurfaceRef: { name: string; kind: string; payload: Record<string, any> };
    try {
      volSurfaceRef = toThinAVolSurface(surfaceForWire, swapIndexId, pricingAsOfDate, resolveQuoteValueById);
    } catch (err) {
      if (err instanceof VolSurfacePayloadError) throw err;
      throw new Error(err instanceof Error ? err.message : 'Failed to build swaption vol payload');
    }

    const bermudanDates = parseBermudanExerciseDates(bermudanExerciseDatesRaw);
    const trade: Record<string, any> = {
      notional: fixedLeg.notional,
      strike: fixedLeg.rate,
      swap_type: swapType,
      effective_date: fixedLeg.schedule.effectiveDate,
      termination_date: fixedLeg.schedule.terminationDate,
      exercise_type: exerciseType,
      settlement_type: settlementType,
      ...(settlementType === 'Cash' ? { settlement_method: settlementMethod } : {}),
      ...(exerciseType === 'Bermudan'
        ? { exercise_dates: bermudanDates, exercise_date: bermudanDates[0] || exerciseDate }
        : { exercise_date: exerciseDate }),
    };

    // Curves: role-tag the first as discount, the second as forwarding.
    // In single-curve mode we only send one entry (the backend's
    // forwarding-curve refs default to discount when the role isn't
    // tagged — it falls back to ``curves[0]``).
    const thinACurves = curves.length > 1
      ? curves.map((c, i) => toThinACurve(_curveToTyped(c), i === 0 ? 'discount' : 'forwarding'))
      : [toThinACurve(_curveToTyped(curves[0]), 'discount')];

    return {
      swaption: trade,
      curves: thinACurves,
      vol_surface: volSurfaceRef,
      swaption_model: toThinAModel(),
      as_of: asOfDate,
    };
  };

  // The legacy ``curves`` array in handlePrice/handleSave is a thin
  // anonymous shape; this casts it to a Curve so toThinACurve can read
  // currency / name / reference_date defensively. (The fields not
  // present on the legacy array fall through ``undefined`` and the
  // helper substitutes role / asOfDate.)
  const _curveToTyped = (c: any): Curve => c as Curve;

  const handleSave = async () => {
    setSaveStatus(null);
    if (exerciseType === 'Bermudan') {
      const bermudanDates = parseBermudanExerciseDates(bermudanExerciseDatesRaw);
      if (bermudanDates.length < 2) {
        setSaveStatus({ tone: 'error', message: 'Cannot save: Bermudan swaption requires at least two exercise dates.' });
        return;
      }
      if (bermudanModelSource === 'saved' && !selectedSavedModelId) {
        setSaveStatus({ tone: 'error', message: 'Cannot save: select a calibrated model from Models or choose inline parameters.' });
        return;
      }
    }
    if (!discountCurve) {
      setSaveStatus({ tone: 'error', message: 'Cannot save: please select a discount curve.' });
      return;
    }
    const actualForecastCurve = useSameCurve ? discountCurve : forecastCurve;
    if (!actualForecastCurve) {
      setSaveStatus({ tone: 'error', message: 'Cannot save: please select a forecast curve.' });
      return;
    }

    setSaving(true);
    try {
      const curves = [{
        id: 'discount',
        day_counter: discountCurve.day_counter,
        interpolator: discountCurve.interpolator,
        bootstrap_trait: discountCurve.bootstrap_trait,
        reference_date: discountCurve.reference_date || asOfDate,
        points: discountCurve.points,
      }];
      if (!useSameCurve && forecastCurve && forecastCurve.id !== discountCurve.id) {
        curves.push({
          id: 'forward',
          day_counter: forecastCurve.day_counter,
          interpolator: forecastCurve.interpolator,
          bootstrap_trait: forecastCurve.bootstrap_trait,
          reference_date: forecastCurve.reference_date || asOfDate,
          points: forecastCurve.points,
        });
      }

      const curveIndexIds = collectIndexIdsFromCurves(curves);
      const allIndexIds = Array.from(new Set([indexRef.id, ...curveIndexIds]));
      const resolvedIndices = await resolveIndexDefs(allIndexIds);
      const unresolvedIds = allIndexIds.filter(idx => !resolvedIndices.find(d => d.id === idx));
      if (unresolvedIds.length > 0) {
        setSaveStatus({
          tone: 'error',
          message: `Cannot save: selected curve references unknown indices: ${unresolvedIds.join(', ')}. Add them in Market Data → Indices.`,
        });
        return;
      }

      const request = await buildRequest(curves, resolvedIndices, false);
      const trimmedName = swaptionName.trim();
      const priorGraph = appGraph;
      // Link the selected curve / vol-surface / saved-model
      // store ids so the lazy migration can reload the quote-id-bearing entities
      // and rebuild the vol payload (the stripped request lost the curve quote-ids).
      const sourceRefs = buildSourceRefs({
        curves: [
          { role: 'discount', curveId: discountCurveId },
          ...(useSameCurve ? [] : [{ role: 'forward', curveId: forecastCurveId }]),
        ],
        volSurfaceId: selectedVolSurfaceId || undefined,
        swaptionModelId: bermudanModelSource === 'saved' ? (selectedSavedModelId || undefined) : undefined,
      });

      // Persist the entity graph server-side leaf→root (curves → curve_set →
      // vol_surface → swaption_model → swaption) and stamp the UUIDs back. The
      // persisted entity bodies are derived from the SAME inline wire payload
      // the inline arm posts (``buildThinARequest``), so the by-reference price
      // equals the inline price for the same model (HW prices off hw_sigma;
      // the surface is persisted faithfully but HW-ignored). The localStorage
      // save still runs so no user data is lost (dual-read, additive).
      let thinARequest: Record<string, any>;
      try {
        thinARequest = await buildThinARequest(curves, resolvedIndices);
      } catch (err) {
        await saveSwaption(request, {
          id: isNew ? undefined : (swaptionId || undefined),
          name: trimmedName || undefined,
          appId: priorGraph?.swaptionId,
          appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          sourceRefs,
        });
        setSaveStatus({
          tone: 'error',
          message: `Saved locally, but could not build the vol payload to reference server-side: ${err instanceof Error ? err.message : 'unknown error'}`,
        });
        setSaving(false);
        return;
      }

      const graphCurves: CurveGraphInput[] = (thinARequest.curves as any[]).map((c) => ({
        key: (c.body?.role as string) || 'discount',
        body: c as components['schemas']['CurveCreate'],
      }));

      let nextGraph: SwaptionAppGraph | null = priorGraph;
      let serverNote = '';
      const graphResult = await persistSwaptionGraph(
        {
          name: trimmedName || deriveSwaptionName(request),
          curves: graphCurves,
          curveSetCurrency: discountCurve?.currency,
          volSurface: thinARequest.vol_surface as components['schemas']['VolSurfaceCreate'],
          swaptionModel: thinARequest.swaption_model as components['schemas']['SwaptionModelCreate'],
          swaptionRequest: thinARequest.swaption as Record<string, unknown>,
          changeReason: changeReason.trim() || undefined,
        },
        priorGraph,
      );
      if (!graphResult.ok) {
        // Branch on the structured envelope code. The localStorage save
        // below still runs so no user data is lost (dual-read).
        const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
        await saveSwaption(request, {
          id: isNew ? undefined : (swaptionId || undefined),
          name: trimmedName || undefined,
          appId: priorGraph?.swaptionId,
          appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          sourceRefs,
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

      const savedId = await saveSwaption(request, {
        id: isNew ? undefined : (swaptionId || undefined),
        name: trimmedName || undefined,
        appId: nextGraph?.swaptionId,
        appGraph: (nextGraph ?? undefined) as unknown as Record<string, unknown> | undefined,
        sourceRefs,
      });
      setSwaptionId(savedId);
      setAppGraph(nextGraph);
      const displayName = trimmedName || deriveSwaptionName(request);
      setSaveStatus({ tone: 'success', message: `Saved "${displayName}".${serverNote}` });
      setChangeReason('');
      if (isNew || id !== savedId) {
        navigate(`/products/swaption/${savedId}`, { replace: true });
      }
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save swaption' });
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
    if (exerciseType === 'Bermudan') {
      const bermudanDates = parseBermudanExerciseDates(bermudanExerciseDatesRaw);
      if (bermudanDates.length < 2) {
        setError('Bermudan swaption requires at least two exercise dates.');
        return;
      }
      if (bermudanModelSource === 'saved' && !selectedSavedModelId) {
        setError('Select a calibrated model from Models or choose inline parameters.');
        return;
      }
    }
    // SABR-params surfaces require both discount and forwarding curves to
    // resolve a per-node ATM forward F(expiry, tenor) via
    // finalizeSwaptionVolEntryForPricing (request/swaption_vol_runtime.cpp).
    // Surface a clear, SABR-specific message before the request leaves the
    // browser instead of letting an opaque backend error bubble up.
    if (selectedSurfaceShape === 'SabrParams') {
      if (!discountCurve) {
        setError('SABR pricing requires a Discounting Curve.');
        return;
      }
      if (!useSameCurve && !forecastCurve) {
        setError('SABR pricing requires a Forwarding Curve (or enable single-curve mode).');
        return;
      }
    }
    if (!discountCurve) {
      setError('Please select a discount curve');
      return;
    }
    const actualForecastCurve = useSameCurve ? discountCurve : forecastCurve;
    if (!actualForecastCurve) {
      setError('Please select a forecast curve');
      return;
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    try {
      // Price-arm selection. A saved swaption (appGraph
      // present) prices by-reference: a minimal {swaption_id, as_of}
      // body, and the orchestrator loads the trade + curve_set + vol_surface +
      // swaption_model server-side — so we skip the local curve/vol build
      // entirely. An unsaved swaption prices inline, unchanged.
      let request: unknown;
      if (appGraph?.swaptionId) {
        request = buildSwaptionPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate });
      } else {
        const curves = [{
          id: 'discount',
          day_counter: discountCurve.day_counter,
          interpolator: discountCurve.interpolator,
          bootstrap_trait: discountCurve.bootstrap_trait,
          reference_date: discountCurve.reference_date || asOfDate,
          points: discountCurve.points,
        }];
        if (!useSameCurve && forecastCurve && forecastCurve.id !== discountCurve.id) {
          curves.push({
            id: 'forward',
            day_counter: forecastCurve.day_counter,
            interpolator: forecastCurve.interpolator,
            bootstrap_trait: forecastCurve.bootstrap_trait,
            reference_date: forecastCurve.reference_date || asOfDate,
            points: forecastCurve.points,
          });
        }

        const curveIndexIds = collectIndexIdsFromCurves(curves);
        const allIndexIds = Array.from(new Set([indexRef.id, ...curveIndexIds]));
        const resolvedIndices = await resolveIndexDefs(allIndexIds);
        const unresolvedIds = allIndexIds.filter(idx => !resolvedIndices.find(d => d.id === idx));
        if (unresolvedIds.length > 0) {
          setError(`Selected curve references unknown indices: ${unresolvedIds.join(', ')}. Add them in Market Data → Indices.`);
          setLoading(false);
          return;
        }

        // Inline arm: post the flat-trade-body + top-level curves +
        // top-level vol_surface + top-level swaption_model envelope.
        // ``preferBackendQuotes`` is no longer relevant — the portal
        // forwards quote_ids unresolved and the orchestrator
        // substitutes via its market-data client. The legacy ``buildRequest``
        // (handleSave) still constructs the fat shape for localStorage
        // round-trips, so saved swaptions keep loading without churn.
        const thinARequest = await buildThinARequest(curves, resolvedIndices);
        request = buildSwaptionPriceArm({ appGraph: null, inlineRequest: thinARequest, asOf: asOfDate });
      }

      const response = await priceSwaption(request, asOfDate);
      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);

      if (response.success && response.data?.swaptions?.[0]) {
        setResult(response.data.swaptions[0] as SwaptionResultData);
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

  const formatNumber = (n: number | undefined, decimals = 4) => {
    if (n === undefined || n === null) return '—';
    return n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : (swaptionName.trim() || swaptionDisplayName)}
          subtitle="Price European or Bermudan swaptions with model/volatility controls"
          backLink={<BackLink onClick={() => navigate('/products/swaption')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={swaptionName}
              onNameChange={setSwaptionName}
              onSave={handleSave}
              saving={saving}
              placeholder="Swaption name…"
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
                  <input
                    type="date"
                    value={asOfDate}
                    onChange={e => setAsOfDate(e.target.value)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Exercise Date</label>
                  <input
                    type="date"
                    value={exerciseDate}
                    onChange={e => setExerciseDate(e.target.value)}
                    className={inputClass}
                  />
                </div>
                <CurveSetSelector
                  label="Discounting Curve (PV)"
                  curveRole="discount"
                  curveSetId={curveSetId}
                  curveId={discountCurveId}
                  onChangeCurveSet={(csId) => setCurveSetId(csId)}
                  onChangeCurve={(id, curve) => {
                    setDiscountCurveId(id);
                    setDiscountCurve(curve);
                  }}
                />
                <div>
                  <label className="flex items-center gap-2 text-xs text-[#737373] mb-1.5 font-medium">
                    Forecasting Curve (Index projection)
                    <label className="flex items-center gap-1 text-[#525252] font-normal">
                      <input
                        type="checkbox"
                        checked={useSameCurve}
                        onChange={e => setUseSameCurve(e.target.checked)}
                        className="rounded border-[#d4d4d4] w-3 h-3"
                      />
                      <span className="text-xs">Single-curve mode</span>
                    </label>
                  </label>
                  {useSameCurve ? (
                    <input
                      type="text"
                      value={discountCurve?.name || 'Select discount curve first'}
                      disabled
                      className={`${inputClass} bg-[#f5f5f5] text-[#737373]`}
                    />
                  ) : (
                    <CurveSetSelector
                      label=""
                      curveRole="forward"
                      curveSetId={curveSetId}
                      curveId={forecastCurveId}
                      onChangeCurveSet={(csId) => setCurveSetId(csId)}
                      onChangeCurve={(id, curve) => {
                        setForecastCurveId(id);
                        setForecastCurve(curve);
                      }}
                    />
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2 mt-3">
                <input
                  type="checkbox"
                  id="attachQuotesSwaption"
                  checked={attachQuotes}
                  onChange={e => setAttachQuotes(e.target.checked)}
                  className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                />
                <label htmlFor="attachQuotesSwaption" className="text-xs text-[#737373]">
                  Attach quote book snapshot to pricing request
                </label>
              </div>
              <div className="flex flex-wrap items-center gap-4 mt-3">
                <label className="flex items-center gap-2 text-xs text-[#737373]">
                  <input
                    type="checkbox"
                    checked={swaptionPricingRebump}
                    onChange={e => setSwaptionPricingRebump(e.target.checked)}
                    className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                  />
                  Rebump greeks
                </label>
                <label className="flex items-center gap-2 text-xs text-[#737373]">
                  <input
                    type="checkbox"
                    checked={swaptionPricingDetails}
                    onChange={e => setSwaptionPricingDetails(e.target.checked)}
                    className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                  />
                  Pricing details
                </label>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Swaption Terms</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Exercise Type</label>
                  <select value={exerciseType} onChange={e => setExerciseType(e.target.value as ExerciseType)} className={inputClass}>
                    {EXERCISE_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Settlement Type</label>
                  <select value={settlementType} onChange={e => setSettlementType(e.target.value as SettlementType)} className={inputClass}>
                    {SETTLEMENT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
              </div>
              {settlementType === 'Cash' && (
                <div className="grid sm:grid-cols-2 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Settlement Method</label>
                    <select value={settlementMethod} onChange={e => setSettlementMethod(e.target.value as SettlementMethod)} className={inputClass}>
                      <option value="ParYieldCurve">ParYieldCurve</option>
                      <option value="CollateralizedCashPrice">CollateralizedCashPrice</option>
                    </select>
                  </div>
                </div>
              )}
              {exerciseType === 'Bermudan' && (
                <div className="mt-4">
                  <label className={labelClass}>Exercise Dates (one per line)</label>
                  <textarea
                    value={bermudanExerciseDatesRaw}
                    onChange={e => setBermudanExerciseDatesRaw(e.target.value)}
                    placeholder="2027-02-09&#10;2028-02-09&#10;2029-02-09"
                    className={`${inputClass} min-h-[92px] font-mono text-xs`}
                  />
                  <p className="text-xs text-[#737373] mt-1">
                    Bermudan requires at least two valid dates (YYYY-MM-DD).
                  </p>
                </div>
              )}
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Underlying Swap</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Swap Type</label>
                  <select value={swapType} onChange={e => setSwapType(e.target.value as SwapType)} className={inputClass}>
                    <option value="Payer">Payer</option>
                    <option value="Receiver">Receiver</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Underlying Type</label>
                  <select value={underlyingType} onChange={e => setUnderlyingType(e.target.value as UnderlyingType)} className={inputClass}>
                    <option value="IborSwap">IborSwap</option>
                    <option value="OisSwap">OisSwap</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Fixed Rate</label>
                  <input type="number" step="0.0001" value={fixedLeg.rate}
                    onChange={e => setFixedLeg(prev => ({ ...prev, rate: parseFloat(e.target.value) || 0 }))}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Notional</label>
                  <input type="number" value={fixedLeg.notional}
                    onChange={e => {
                      const notional = parseFloat(e.target.value) || 0;
                      setFixedLeg(prev => ({ ...prev, notional }));
                      setFloatingLeg(prev => ({ ...prev, notional }));
                      setOvernightLeg(prev => ({ ...prev, notional }));
                    }}
                    className={inputClass}
                  />
                </div>
              </div>

              <div className="grid sm:grid-cols-3 gap-4 mt-4">
                <div>
                  <label className={labelClass}>Fixed Day Counter</label>
                  <select value={fixedLeg.dayCounter} onChange={e => setFixedLeg(prev => ({ ...prev, dayCounter: e.target.value }))} className={inputClass}>
                    {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Fixed Payment Convention</label>
                  <select value={fixedLeg.paymentConvention} onChange={e => setFixedLeg(prev => ({ ...prev, paymentConvention: e.target.value }))} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bdc => <option key={bdc} value={bdc}>{bdc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>{underlyingType === 'OisSwap' ? 'Overnight Index' : 'Floating Index'}</label>
                  <IndexPicker
                    label=""
                    value={indexRef}
                    onChange={(ref: IndexRef) => setIndexRef(ref)}
                    filter={underlyingType === 'OisSwap' ? 'Overnight' : 'Ibor'}
                  />
                </div>
              </div>

              {underlyingType !== 'OisSwap' && (
                <div className="grid sm:grid-cols-2 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Floating Spread</label>
                    <input type="number" step="0.0001" value={floatingLeg.spread}
                      onChange={e => setFloatingLeg(prev => ({ ...prev, spread: parseFloat(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>Fixing Days</label>
                    <input type="number" value={floatingLeg.fixingDays}
                      onChange={e => setFloatingLeg(prev => ({ ...prev, fixingDays: parseInt(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                </div>
              )}
              {underlyingType === 'OisSwap' && (
                <div className="grid sm:grid-cols-3 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Overnight Spread</label>
                    <input type="number" step="0.0001" value={overnightLeg.spread}
                      onChange={e => setOvernightLeg(prev => ({ ...prev, spread: parseFloat(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>Payment Lag</label>
                    <input type="number" value={overnightLeg.paymentLag}
                      onChange={e => setOvernightLeg(prev => ({ ...prev, paymentLag: parseInt(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>Averaging Method</label>
                    <select value={overnightLeg.averagingMethod} onChange={e => setOvernightLeg(prev => ({ ...prev, averagingMethod: e.target.value }))} className={inputClass}>
                      <option value="Compound">Compound</option>
                      <option value="Simple">Simple</option>
                      <option value="SimpleThenCompounded">SimpleThenCompounded</option>
                    </select>
                  </div>
                </div>
              )}
              {underlyingType === 'OisSwap' && (
                <div className="grid sm:grid-cols-2 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Payment Calendar</label>
                    <select value={overnightLeg.paymentCalendar} onChange={e => setOvernightLeg(prev => ({ ...prev, paymentCalendar: e.target.value }))} className={inputClass}>
                      {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </div>
                </div>
              )}
              {underlyingType === 'OisSwap' && (
                <div className="grid sm:grid-cols-3 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Lookback Days</label>
                    <input type="number" value={overnightLeg.lookbackDays}
                      onChange={e => setOvernightLeg(prev => ({ ...prev, lookbackDays: parseInt(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>Lockout Days</label>
                    <input type="number" value={overnightLeg.lockoutDays}
                      onChange={e => setOvernightLeg(prev => ({ ...prev, lockoutDays: parseInt(e.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </div>
                  <div className="flex items-center gap-3 mt-6">
                    <label className="flex items-center gap-2 text-xs text-[#737373]">
                      <input
                        type="checkbox"
                        checked={overnightLeg.applyObservationShift}
                        onChange={e => setOvernightLeg(prev => ({ ...prev, applyObservationShift: e.target.checked }))}
                        className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                      />
                      Observation shift
                    </label>
                    <label className="flex items-center gap-2 text-xs text-[#737373]">
                      <input
                        type="checkbox"
                        checked={overnightLeg.telescopicValueDates}
                        onChange={e => setOvernightLeg(prev => ({ ...prev, telescopicValueDates: e.target.checked }))}
                        className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                      />
                      Telescopic dates
                    </label>
                  </div>
                </div>
              )}

              <div className="grid sm:grid-cols-2 gap-4 mt-4">
                <div>
                  <label className={labelClass}>Fixed Schedule</label>
                  <div className="grid grid-cols-2 gap-3">
                    <input type="date" value={fixedLeg.schedule.effectiveDate}
                      onChange={e => setFixedLeg(prev => ({ ...prev, schedule: { ...prev.schedule, effectiveDate: e.target.value } }))}
                      className={inputClass}
                    />
                    <input type="date" value={fixedLeg.schedule.terminationDate}
                      onChange={e => setFixedLeg(prev => ({ ...prev, schedule: { ...prev.schedule, terminationDate: e.target.value } }))}
                      className={inputClass}
                    />
                  </div>
                </div>
                <div>
                  <label className={labelClass}>{underlyingType === 'OisSwap' ? 'Overnight Schedule' : 'Floating Schedule'}</label>
                  <div className="grid grid-cols-2 gap-3">
                    <input type="date" value={underlyingType === 'OisSwap' ? overnightLeg.schedule.effectiveDate : floatingLeg.schedule.effectiveDate}
                      onChange={e => {
                        if (underlyingType === 'OisSwap') {
                          setOvernightLeg(prev => ({ ...prev, schedule: { ...prev.schedule, effectiveDate: e.target.value } }));
                        } else {
                          setFloatingLeg(prev => ({ ...prev, schedule: { ...prev.schedule, effectiveDate: e.target.value } }));
                        }
                      }}
                      className={inputClass}
                    />
                    <input type="date" value={underlyingType === 'OisSwap' ? overnightLeg.schedule.terminationDate : floatingLeg.schedule.terminationDate}
                      onChange={e => {
                        if (underlyingType === 'OisSwap') {
                          setOvernightLeg(prev => ({ ...prev, schedule: { ...prev.schedule, terminationDate: e.target.value } }));
                        } else {
                          setFloatingLeg(prev => ({ ...prev, schedule: { ...prev.schedule, terminationDate: e.target.value } }));
                        }
                      }}
                      className={inputClass}
                    />
                  </div>
                </div>
              </div>

              <div className="grid sm:grid-cols-3 gap-4 mt-4">
                <div>
                  <label className={labelClass}>Fixed Frequency</label>
                  <select value={fixedLeg.schedule.frequency} onChange={e => setFixedLeg(prev => ({ ...prev, schedule: { ...prev.schedule, frequency: e.target.value } }))} className={inputClass}>
                    {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>{underlyingType === 'OisSwap' ? 'Overnight Frequency' : 'Floating Frequency'}</label>
                  <select
                    value={underlyingType === 'OisSwap' ? overnightLeg.schedule.frequency : floatingLeg.schedule.frequency}
                    onChange={e => {
                      if (underlyingType === 'OisSwap') {
                        setOvernightLeg(prev => ({ ...prev, schedule: { ...prev.schedule, frequency: e.target.value } }));
                      } else {
                        setFloatingLeg(prev => ({ ...prev, schedule: { ...prev.schedule, frequency: e.target.value } }));
                      }
                    }}
                    className={inputClass}
                  >
                    {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Calendar</label>
                  <select
                    value={fixedLeg.schedule.calendar}
                    onChange={e => {
                      setFixedLeg(prev => ({ ...prev, schedule: { ...prev.schedule, calendar: e.target.value } }));
                      if (underlyingType === 'OisSwap') {
                        setOvernightLeg(prev => ({ ...prev, schedule: { ...prev.schedule, calendar: e.target.value } }));
                      } else {
                        setFloatingLeg(prev => ({ ...prev, schedule: { ...prev.schedule, calendar: e.target.value } }));
                      }
                    }}
                    className={inputClass}
                  >
                    {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Volatility & Model</h2>
              <p className="text-xs text-[#737373] mb-4">Routes Constant, AtmMatrix, SmileCube, and SABR-params surfaces through the canonical wire helper.</p>
              <div className="grid sm:grid-cols-2 gap-4 mb-4">
                <div>
                  <label className={labelClass}>Volatility Source</label>
                  <select
                    value={selectedVolSurfaceId}
                    onChange={e => setSelectedVolSurfaceId(e.target.value)}
                    className={inputClass}
                  >
                    <option value="">Use inline settings</option>
                    {volSurfaces.map(surface => (
                      <option key={surface.id} value={surface.id}>{surface.id}</option>
                    ))}
                  </select>
                </div>
                {usingStoredSurface && selectedSurfaceShape === 'Constant' && (
                  <div className="flex items-end gap-2">
                    <input
                      type="checkbox"
                      id="useSurfaceGrid"
                      checked={useSurfaceGrid}
                      onChange={e => setUseSurfaceGrid(e.target.checked)}
                      className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                    />
                    <label htmlFor="useSurfaceGrid" className="text-xs text-[#737373]">
                      Use surface constant_vol (uncheck to override with inline σ)
                    </label>
                  </div>
                )}
              </div>
              {surfaceProvidesGrid && (
                <div className="mb-4 rounded-lg border border-[#e5e5e5] bg-[#fafafa] px-3 py-2">
                  <p className="text-xs text-[#525252]">
                    σ comes from the saved <span className="font-medium">{selectedSurfaceShape}</span> surface
                    (resolved per (expiry, tenor) node by the backend). Edit grid values in
                    Market Data → Vol Surfaces. Manual σ override is unavailable for non-Constant shapes.
                  </p>
                </div>
              )}
              {!surfaceProvidesGrid && (
                <>
                  <div className="grid sm:grid-cols-2 gap-4">
                    <div>
                      <label className={labelClass}>Volatility Type</label>
                      <select value={volType} onChange={e => setVolType(e.target.value as VolatilityType)} className={inputClass}>
                        {VOL_TYPES.map(v => <option key={v} value={v}>{v}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Constant Vol</label>
                      <input type="number" step="0.000001" value={volatility}
                        onChange={e => setVolatility(parseFloat(e.target.value) || 0)}
                        className={inputClass}
                      />
                    </div>
                  </div>
                  <div className="grid sm:grid-cols-3 gap-4 mt-4">
                    <div>
                      <label className={labelClass}>Vol Day Counter</label>
                      <select value={volDayCounter} onChange={e => setVolDayCounter(e.target.value)} className={inputClass}>
                        {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Vol Calendar</label>
                      <select value={volCalendar} onChange={e => setVolCalendar(e.target.value)} className={inputClass}>
                        {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Vol Convention</label>
                      <select value={volConvention} onChange={e => setVolConvention(e.target.value)} className={inputClass}>
                        {BUSINESS_DAY_CONVENTIONS.map(bdc => <option key={bdc} value={bdc}>{bdc}</option>)}
                      </select>
                    </div>
                  </div>
                  {volType === 'ShiftedLognormal' && (
                    <div className="mt-4">
                      <label className={labelClass}>Displacement</label>
                      <input type="number" step="0.0001" value={volDisplacement}
                        onChange={e => setVolDisplacement(parseFloat(e.target.value) || 0)}
                        className={inputClass}
                      />
                    </div>
                  )}
                </>
              )}
              <div className="grid sm:grid-cols-2 gap-4 mt-4">
                <div>
                  <label className={labelClass}>Model ID</label>
                  <input type="text" value={modelId} onChange={e => setModelId(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Model Type</label>
                  <select
                    value={modelType}
                    onChange={e => setModelType(e.target.value as IrModelType)}
                    className={inputClass}
                    disabled={exerciseType === 'Bermudan'}
                  >
                    {MODEL_TYPES.map(m => <option key={m} value={m}>{m}</option>)}
                  </select>
                </div>
              </div>
              {exerciseType === 'Bermudan' && (
                <div className="mt-4 border border-[#e5e5e5] rounded-lg p-3 bg-[#fafafa]">
                  <p className="text-xs text-[#525252] mb-2 font-medium">Bermudan Model Source</p>
                  <div className="flex flex-wrap items-center gap-4">
                    <label className="flex items-center gap-2 text-xs text-[#525252]">
                      <input
                        type="radio"
                        name="bermudanModelSource"
                        checked={bermudanModelSource === 'inline'}
                        onChange={() => setBermudanModelSource('inline')}
                      />
                      Inline Hull-White parameters
                    </label>
                    <label className="flex items-center gap-2 text-xs text-[#525252]">
                      <input
                        type="radio"
                        name="bermudanModelSource"
                        checked={bermudanModelSource === 'saved'}
                        onChange={() => setBermudanModelSource('saved')}
                      />
                      Use calibrated model from Models
                    </label>
                  </div>
                  {bermudanModelSource === 'saved' && (
                    <div className="mt-3">
                      <label className={labelClass}>Saved Calibrated Model</label>
                      <select
                        value={selectedSavedModelId}
                        onChange={e => setSelectedSavedModelId(e.target.value)}
                        className={inputClass}
                      >
                        <option value="">Select model...</option>
                        {swaptionModels.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.id} (a={m.hw_a.toFixed(4)}, sigma={m.hw_sigma.toFixed(4)})
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-[#737373] mt-1">
                        Manage calibrations in the Models menu.
                      </p>
                    </div>
                  )}
                </div>
              )}
              {modelType === 'HullWhiteLattice' && (
                <div className="grid sm:grid-cols-3 gap-4 mt-4">
                  <div>
                    <label className={labelClass}>Parameter Mode</label>
                    <select value={modelParamMode} onChange={e => setModelParamMode(e.target.value as ModelParamMode)} className={inputClass}>
                      <option value="Explicit">Explicit</option>
                      <option value="Calibrate">Calibrate</option>
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>HW a</label>
                    <input
                      type="number"
                      step="0.0001"
                      value={hwA}
                      onChange={e => setHwA(parseFloat(e.target.value) || 0)}
                      className={inputClass}
                      disabled={exerciseType === 'Bermudan' && bermudanModelSource === 'saved'}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>HW sigma</label>
                    <input
                      type="number"
                      step="0.0001"
                      value={hwSigma}
                      onChange={e => setHwSigma(parseFloat(e.target.value) || 0)}
                      className={inputClass}
                      disabled={exerciseType === 'Bermudan' && bermudanModelSource === 'saved'}
                    />
                  </div>
                  <div>
                    <label className={labelClass}>Lattice Steps</label>
                    <input
                      type="number"
                      value={latticeSteps}
                      onChange={e => setLatticeSteps(parseInt(e.target.value) || 0)}
                      className={inputClass}
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="flex gap-2">
              <button
                onClick={handlePrice}
                disabled={loading || !discountCurve}
                className="flex-1 py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50 transition-colors"
              >
                {loading ? 'Pricing...' : 'Price Swaption'}
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
            {!loading && error && (
              <PricingErrorCard error={error} info={errorInfo} durationMs={durationMs} />
            )}
            {!loading && !error && !result && (
              <PanelPlaceholder
                compact
                title="No pricing results yet"
                description={getInstructionText('swaption parameters', 'Price')}
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
                  <div className="bg-[#f5f5f5] rounded-lg p-3">
                    <p className="text-xs text-[#737373] mb-1">NPV</p>
                    <p className="text-lg font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.npv, 2)}</p>
                  </div>
                  <div className="bg-[#f5f5f5] rounded-lg p-3">
                    <p className="text-xs text-[#737373] mb-1">Implied Vol</p>
                    <p className="text-lg font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.implied_volatility, 6)}</p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 text-sm text-[#525252]">
                  <div>ATM Forward: <span className="font-mono">{formatNumber(result.atm_forward, 6)}</span></div>
                  <div>Annuity: <span className="font-mono">{formatNumber(result.annuity, 6)}</span></div>
                  <div>Delta: <span className="font-mono">{formatNumber(result.delta, 6)}</span></div>
                  <div>Vega: <span className="font-mono">{formatNumber(result.vega, 6)}</span></div>
                  <div>DV01: <span className="font-mono">{formatNumber(result.dv01, 6)}</span></div>
                  <div>Gamma: <span className="font-mono">{formatNumber(result.gamma, 6)}</span></div>
                  <div>Theta: <span className="font-mono">{formatNumber(result.theta, 6)}</span></div>
                </div>
              </div>
            )}
          </div>
        </div>

        {appGraph?.swaptionId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/swaptions"
              entityId={appGraph.swaptionId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
