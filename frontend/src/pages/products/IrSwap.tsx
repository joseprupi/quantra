import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import IndexPicker from '../../components/curves/IndexPicker';
import { Curve, CurvePoint, Frequency, IndexDef, IndexRef, DAY_COUNTERS, CALENDARS, BUSINESS_DAY_CONVENTIONS, FREQUENCIES, collectIndexRefIds } from '../../lib/types';
import { frequencyFromIndexDef } from '../../lib/frequencyFromTenor';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { priceIrSwap } from '../../lib/api/irSwapPricingService';
import type { SwapFlow } from '../../lib/api/orchestrator';
import {
  persistIrSwapGraph,
  buildIrSwapPriceArm,
  asIrSwapAppGraph,
  type IrSwapAppGraph,
  type CurveGraphInput,
} from '../../lib/api/irSwapSaveGraph';
import PricingErrorCard from '../../components/products/PricingErrorCard';
import PricingTraceLink from '../../components/products/PricingTraceLink';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { buildPricingQuoteSnapshotWithBackend } from '../../lib/marketDataBackend';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { IrSwapRequest, deriveIrSwapName, getIrSwapEntryById, saveIrSwap } from '../../lib/storage/swaps';
import { buildSourceRefs } from '../../lib/storage/sourceRefs';
import { VolSurfaceSpec, getVolSurfaces } from '../../lib/storage/volSurfaces';
import { buildSwaptionVolWirePayload, VolSurfacePayloadError } from '../../lib/storage/volSurfacePayload';
import { getQuoteBook, getResolutionMode, resolveQuoteValue } from '../../lib/storage/quoteBook';
import { useAuth } from '../../hooks/useAuth';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

type SwapSide = 'Payer' | 'Receiver';
type SwapKind = 'Vanilla' | 'OIS' | 'Basis' | 'CMS';
type FlowsTab = 'fixed' | 'floating' | 'overnight' | 'leg1' | 'leg2' | 'cms';
type LegType = 'Fixed' | 'Floating' | 'Overnight' | 'CMS';

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
  averagingMethod: 'Simple' | 'Compound';
  lookbackDays: number;
  lockoutDays: number;
  applyObservationShift: boolean;
  telescopicValueDates: boolean;
  schedule: ScheduleParams;
}

interface CmsPricerParams {
  pricerType: 'LinearTsr' | 'HaganAnalytic' | 'HaganNumeric';
  yieldCurveModel: 'Standard' | 'ExactYield' | 'ParallelShifts' | 'NonParallelShifts';
  meanReversion: number;
  haganLowerLimit: number;
  haganUpperLimit: number;
  haganPrecision: number;
  haganHardUpperLimit: number;
}

interface CmsLegParams {
  notional: number;
  dayCounter: string;
  paymentConvention: string;
  gear: number;
  spread: number;
  schedule: ScheduleParams;
  swapIndexId: string;
  swapTenorN: number;
  swapTenorUnit: 'Months' | 'Years';
  swaptionVolId: string;
  fixingDaysOverride?: number;
  useFixingDaysOverride: boolean;
  usePricerSpec: boolean;
  pricer: CmsPricerParams;
}

interface SwapResultData {
  npv?: number;
  fair_rate?: number;
  fair_spread?: number;
  fixed_leg_bps?: number;
  floating_leg_bps?: number;
  fixed_leg_npv?: number;
  floating_leg_npv?: number;
  fixed_leg_flows?: SwapFlow[];
  floating_leg_flows?: SwapFlow[];
  overnight_leg_bps?: number;
  overnight_leg_npv?: number;
  overnight_leg_flows?: Array<Record<string, unknown>>;
  fair_spread_leg1?: number;
  fair_spread_leg2?: number;
  leg1_bps?: number;
  leg2_bps?: number;
  leg1_npv?: number;
  leg2_npv?: number;
  leg1_flows?: Array<Record<string, unknown>>;
  leg2_flows?: Array<Record<string, unknown>>;
  cms_leg_bps?: number;
  cms_leg_npv?: number;
  cms_leg_flows?: Array<Record<string, unknown>>;
  used_cms_pricer_type?: string;
  used_cms_yield_curve_model?: string;
  used_cms_mean_reversion?: number;
  used_cms_hagan_lower_limit?: number;
  used_cms_hagan_upper_limit?: number;
  used_cms_hagan_precision?: number;
  used_cms_hagan_hard_upper_limit?: number;
}

const DATE_GEN_RULES = ['Forward', 'Backward'];
const LAST_SWAP_KIND_KEY = 'quantra_swap_last_kind';
const CMS_PRICER_TYPES: Array<CmsPricerParams['pricerType']> = ['LinearTsr', 'HaganAnalytic', 'HaganNumeric'];
const CMS_YIELD_CURVE_MODELS: Array<CmsPricerParams['yieldCurveModel']> = ['Standard', 'ExactYield', 'ParallelShifts', 'NonParallelShifts'];

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

function tenorToYears(tenorNumber?: number, tenorUnit?: string): number {
  if (!tenorNumber || !tenorUnit) return 0;
  const unit = tenorUnit.toLowerCase();
  if (unit.startsWith('day')) return tenorNumber / 365;
  if (unit.startsWith('week')) return tenorNumber * 7 / 365;
  if (unit.startsWith('month')) return tenorNumber / 12;
  if (unit.startsWith('year')) return tenorNumber;
  return 0;
}

function estimateCurveMaxYears(curve: any): number | null {
  const points = Array.isArray(curve?.points) ? curve.points : [];
  const maxYears = points.reduce((max: number, wrapper: any) => {
    const p = wrapper?.point || {};
    const years = tenorToYears(p.tenor_number, p.tenor_time_unit);
    return Math.max(max, years);
  }, 0);
  return maxYears > 0 ? maxYears : null;
}

function isLikelyIborIndex(spec: any): boolean {
  const rawType = String(spec?.type || '').toLowerCase();
  if (rawType === 'ibor') return true;
  // Backward compatibility for older/malformed local records.
  return typeof spec?.tenor_number === 'number' || Boolean(spec?.family);
}

function isLikelyOvernightIndex(spec: any): boolean {
  const rawType = String(spec?.type || '').toLowerCase();
  if (rawType === 'overnight') return true;
  return Boolean(spec?.overnight_name);
}

function collectIndexIdsFromCurves(curves: Array<{ points?: CurvePoint[] }>): string[] {
  const allPoints = curves.flatMap(c => c.points || []);
  return collectIndexRefIds(allPoints);
}

// Options for a floating leg's payment-frequency select. The tenor-derived
// defaults can land outside the standard list (e.g. Bimonthly for a 2M
// index), so the current value is prepended when missing — the select never
// shows blank and the user can always pick a standard frequency back.
function frequencyOptions(current: string): string[] {
  return FREQUENCIES.includes(current as Frequency) ? FREQUENCIES : [current, ...FREQUENCIES];
}

function scheduleToApi(schedule: ScheduleParams) {
  return {
    calendar: schedule.calendar,
    effective_date: schedule.effectiveDate,
    termination_date: schedule.terminationDate,
    frequency: schedule.frequency,
    convention: schedule.convention,
    termination_date_convention: schedule.terminationDateConvention,
    date_generation_rule: schedule.dateGenerationRule,
  };
}

export default function IrSwap() {
  const { id } = useParams();
  const navigate = useNavigate();
  const ui = entityUi.swap;
  const isNew = !id || id === 'new';
  const today = new Date().toISOString().split('T')[0];
  const fiveYearsLater = new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

  const [asOfDate, setAsOfDate] = useState(today);
  const [settlementDate, setSettlementDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 2);
    return d.toISOString().split('T')[0];
  });
  const [swapId, setSwapId] = useState(id && id !== 'new' ? id : '');
  const [swapName, setSwapName] = useState('');
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');
  // id↔uuid bridge. When present this swap is persisted server-side
  // and prices via the by-reference arm; null ⇒ inline.
  const [appGraph, setAppGraph] = useState<IrSwapAppGraph | null>(null);

  const [attachQuotes, setAttachQuotes] = useState(true);
  const { asOfDate: globalAsOf } = useAsOfDate();
  useEffect(() => { setAsOfDate(globalAsOf); }, [globalAsOf]);

  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [forecastCurveId, setForecastCurveId] = useState('');
  const [forecastCurve, setForecastCurve] = useState<Curve | null>(null);
  const [useSameCurve, setUseSameCurve] = useState(true);
  const [basisForwardCurve1Id, setBasisForwardCurve1Id] = useState('');
  const [basisForwardCurve1, setBasisForwardCurve1] = useState<Curve | null>(null);
  const [basisForwardCurve2Id, setBasisForwardCurve2Id] = useState('');
  const [basisForwardCurve2, setBasisForwardCurve2] = useState<Curve | null>(null);

  const [swapKind, setSwapKind] = useState<SwapKind>(() => {
    const last = localStorage.getItem(LAST_SWAP_KIND_KEY);
    return (last === 'Vanilla' || last === 'OIS' || last === 'Basis' || last === 'CMS') ? last : 'Vanilla';
  });
  const [leg1Type, setLeg1Type] = useState<LegType>('Fixed');
  const [leg2Type, setLeg2Type] = useState<LegType>('Floating');
  const [swapType, setSwapType] = useState<SwapSide>('Payer');
  const [indexRef, setIndexRef] = useState<IndexRef>({ id: '' });
  const [overnightIndexRef, setOvernightIndexRef] = useState<IndexRef>({ id: '' });
  const [basisIndexRef1, setBasisIndexRef1] = useState<IndexRef>({ id: '' });
  const [basisIndexRef2, setBasisIndexRef2] = useState<IndexRef>({ id: '' });
  const [fixedLeg, setFixedLeg] = useState<FixedLegParams>({
    notional: 1000000,
    rate: 0.025,
    dayCounter: 'Thirty360',
    paymentConvention: 'ModifiedFollowing',
    schedule: {
      calendar: 'TARGET',
      effectiveDate: today,
      terminationDate: fiveYearsLater,
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
      effectiveDate: today,
      terminationDate: fiveYearsLater,
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
    paymentLag: 0,
    averagingMethod: 'Compound',
    lookbackDays: -1,
    lockoutDays: 0,
    applyObservationShift: false,
    telescopicValueDates: false,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: today,
      terminationDate: fiveYearsLater,
      frequency: 'Annual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });
  const [basisLeg1, setBasisLeg1] = useState<FloatingLegParams>({
    notional: 1000000,
    spread: 0,
    dayCounter: 'Actual360',
    paymentConvention: 'ModifiedFollowing',
    fixingDays: 2,
    inArrears: false,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: today,
      terminationDate: fiveYearsLater,
      frequency: 'Quarterly',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });
  const [basisLeg2, setBasisLeg2] = useState<FloatingLegParams>({
    notional: 1000000,
    spread: 0,
    dayCounter: 'Actual360',
    paymentConvention: 'ModifiedFollowing',
    fixingDays: 2,
    inArrears: false,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: today,
      terminationDate: fiveYearsLater,
      frequency: 'Semiannual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
  });
  const [cmsLeg, setCmsLeg] = useState<CmsLegParams>({
    notional: 1000000,
    dayCounter: 'Actual365Fixed',
    paymentConvention: 'ModifiedFollowing',
    gear: 1,
    spread: 0,
    schedule: {
      calendar: 'TARGET',
      effectiveDate: today,
      terminationDate: fiveYearsLater,
      frequency: 'Annual',
      convention: 'ModifiedFollowing',
      terminationDateConvention: 'ModifiedFollowing',
      dateGenerationRule: 'Forward',
    },
    swapIndexId: 'EUR_SWAP_6M',
    swapTenorN: 10,
    swapTenorUnit: 'Years',
    swaptionVolId: '',
    fixingDaysOverride: 2,
    useFixingDaysOverride: false,
    usePricerSpec: false,
    pricer: {
      pricerType: 'HaganAnalytic',
      yieldCurveModel: 'Standard',
      meanReversion: 0.03,
      haganLowerLimit: 0,
      haganUpperLimit: 2,
      haganPrecision: 1e-5,
      haganHardUpperLimit: 0,
    },
  });
  const [volSurfaces, setVolSurfaces] = useState<VolSurfaceSpec[]>([]);

  const [includeFlows, setIncludeFlows] = useState(true);
  const [flowTab, setFlowTab] = useState<FlowsTab>('fixed');
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  const [hasOvernightIndex, setHasOvernightIndex] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorInfo, setErrorInfo] = useState<PricingErrorInfo | undefined>(undefined);
  const [result, setResult] = useState<SwapResultData | null>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const { user, login } = useAuth();

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  useEffect(() => {
    localStorage.setItem(LAST_SWAP_KIND_KEY, swapKind);
  }, [swapKind]);

  useEffect(() => {
    indexStore.getAll().then(specs => {
      const iborIds = specs
        .filter(s => isLikelyIborIndex(s))
        .map(s => s.id);
      const overnightIds = specs
        .filter(s => isLikelyOvernightIndex(s))
        .map(s => s.id);
      const anyIds = specs.map(s => s.id).filter(Boolean);
      setHasOvernightIndex(overnightIds.length > 0);

      // Keep the default UX usable: when no index is selected yet, preselect
      // the first saved index for the active swap kind.
      if ((swapKind === 'Vanilla' || swapKind === 'CMS') && (!indexRef.id || !iborIds.includes(indexRef.id))) {
        const vanillaDefault = iborIds[0] || anyIds[0];
        if (vanillaDefault) setIndexRef({ id: vanillaDefault });
      }
      if (swapKind === 'OIS' && overnightIds.length > 0 && (!overnightIndexRef.id || !overnightIds.includes(overnightIndexRef.id))) {
        setOvernightIndexRef({ id: overnightIds[0] });
      }
      if (swapKind === 'Basis' && iborIds.length > 0) {
        if (!basisIndexRef1.id || !iborIds.includes(basisIndexRef1.id)) {
          setBasisIndexRef1({ id: iborIds[0] });
        }
        if (!basisIndexRef2.id || !iborIds.includes(basisIndexRef2.id)) {
          setBasisIndexRef2({ id: iborIds[1] || iborIds[0] });
        }
      }
    });
  }, [swapKind, indexRef.id, overnightIndexRef.id, basisIndexRef1.id, basisIndexRef2.id]);

  useEffect(() => {
    const surfaces = getVolSurfaces();
    setVolSurfaces(surfaces);
    if (!cmsLeg.swaptionVolId && surfaces.length > 0) {
      setCmsLeg(prev => ({ ...prev, swaptionVolId: surfaces[0].id }));
    }
  }, [cmsLeg.swaptionVolId]);

  const flowTabs: FlowsTab[] = useMemo(() => {
    if (swapKind === 'Basis') return ['leg1', 'leg2'];
    if (swapKind === 'OIS') return ['fixed', 'overnight'];
    if (swapKind === 'CMS') return ['fixed', 'cms'];
    return ['fixed', 'floating'];
  }, [swapKind]);

  const inferSwapKind = (left: LegType, right: LegType): SwapKind | null => {
    const pair = `${left}-${right}`;
    if (pair === 'Fixed-Floating' || pair === 'Floating-Fixed') return 'Vanilla';
    if (pair === 'Floating-Floating') return 'Basis';
    if (pair === 'Fixed-Overnight' || pair === 'Overnight-Fixed') return 'OIS';
    if (pair === 'Fixed-CMS' || pair === 'CMS-Fixed') return 'CMS';
    return null;
  };

  const applySwapKind = (nextKind: SwapKind) => {
    setSwapKind(nextKind);
    setValidationErrors({});
    if (nextKind === 'Vanilla') {
      setFlowTab('fixed');
      setLeg1Type('Fixed');
      setLeg2Type('Floating');
      setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, frequency: 'Semiannual' } }));
    } else if (nextKind === 'OIS') {
      setFlowTab('fixed');
      setLeg1Type('Fixed');
      setLeg2Type('Overnight');
      setOvernightLeg(p => ({ ...p, averagingMethod: 'Compound', paymentLag: 0 }));
      setFloatingLeg(p => ({ ...p, spread: 0, fixingDays: 2, inArrears: false }));
    } else if (nextKind === 'Basis') {
      setFlowTab('leg1');
      setLeg1Type('Floating');
      setLeg2Type('Floating');
      setUseSameCurve(false);
      setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, frequency: 'Quarterly' } }));
      setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, frequency: 'Semiannual' } }));
      indexStore.getAll().then(specs => {
        const iborIds = specs
          .filter(s => isLikelyIborIndex(s))
          .map(s => s.id);
        if (iborIds.length > 0) {
          setBasisIndexRef1({ id: iborIds[0] });
          setBasisIndexRef2({ id: iborIds[1] || iborIds[0] });
        }
      });
    } else {
      setFlowTab('fixed');
      setLeg1Type('Fixed');
      setLeg2Type('CMS');
      if (!cmsLeg.swaptionVolId) {
        const firstSurface = getVolSurfaces()[0];
        if (firstSurface?.id) {
          setCmsLeg(prev => ({ ...prev, swaptionVolId: firstSurface.id }));
        }
      }
    }
  };

  useEffect(() => {
    if (!isNew && id) {
      getIrSwapEntryById(id).then(async entry => {
        if (!entry) return;
        const saved = entry.request;
        setSwapId(entry.id);
        setSwapName(entry.name || '');
        // Re-hydrate the server-id bridge so a previously-saved swap prices
        // by-reference and re-saves PATCH the same rows (idempotent).
        setAppGraph(asIrSwapAppGraph(entry.appGraph));
        const specs = await indexStore.getAll();
        const iborIds = specs
          .filter(s => isLikelyIborIndex(s))
          .map(s => s.id);
        const overnightIds = specs
          .filter(s => isLikelyOvernightIndex(s))
          .map(s => s.id);
        const resolvePreferred = (preferredId: string | undefined, allowedIds: string[], fallbackIds: string[] = []) => {
          if (preferredId && allowedIds.includes(preferredId)) return preferredId;
          const fallbackMatch = fallbackIds.find(id => allowedIds.includes(id));
          if (fallbackMatch) return fallbackMatch;
          return allowedIds[0] || '';
        };
        const request: any = saved;
        const samplePricing = request?.pricing || {};
        const row = request?.swaps?.[0] || {};
        const requestCurves = Array.isArray(samplePricing.curves) ? samplePricing.curves : [];
        const curveReferencedIndexIds = collectIndexIdsFromCurves(requestCurves);
        const mapCurve = (curve: any, role: 'discount' | 'forward'): Curve => ({
          id: curve.id || role,
          name: curve.id || role,
          currency: 'EUR',
          role,
          day_counter: curve.day_counter,
          interpolator: curve.interpolator,
          bootstrap_trait: curve.bootstrap_trait,
          reference_date: curve.reference_date || asOfDate,
          points: curve.points || [],
          createdAt: '',
          updatedAt: '',
        });
        const discount = requestCurves.find((c: any) => c.id === 'discount') || requestCurves[0];
        const forward = requestCurves.find((c: any) => c.id === 'forward') || requestCurves[1];
        const forward1 = requestCurves.find((c: any) => c.id === 'forward_leg1') || requestCurves[1];
        const forward2 = requestCurves.find((c: any) => c.id === 'forward_leg2') || requestCurves[2];

        if (samplePricing.as_of_date) setAsOfDate(samplePricing.as_of_date);
        if (samplePricing.settlement_date) setSettlementDate(samplePricing.settlement_date);
        setIncludeFlows(Boolean(request.include_flows));

        if (discount) {
          setDiscountCurveId(discount.id || '');
          setDiscountCurve(mapCurve(discount, 'discount'));
        }

        if (row.vanilla_swap) {
          const sampleSwap = row.vanilla_swap;
          const sampleFixed = sampleSwap.fixed_leg || {};
          const sampleFloat = sampleSwap.floating_leg || {};
          const sampleCms = sampleSwap.cms_leg || {};
          const isCms = Boolean(sampleCms && typeof sampleCms === 'object' && Object.keys(sampleCms).length > 0);
          applySwapKind(isCms ? 'CMS' : 'Vanilla');
          if (sampleSwap.swap_type) setSwapType(sampleSwap.swap_type);
          const resolvedVanillaIndexId = resolvePreferred(sampleFloat.index?.id || sampleCms.swap_index_id, iborIds, curveReferencedIndexIds);
          if (resolvedVanillaIndexId) setIndexRef({ id: resolvedVanillaIndexId });
          if (forward) {
            setForecastCurveId(forward.id || '');
            setForecastCurve(mapCurve(forward, 'forward'));
            setUseSameCurve((row.forwarding_curve || 'discount') === 'discount');
          }
          setFixedLeg(prev => ({
            ...prev,
            notional: sampleFixed.notional ?? prev.notional,
            rate: sampleFixed.rate ?? prev.rate,
            dayCounter: sampleFixed.day_counter ?? prev.dayCounter,
            paymentConvention: sampleFixed.payment_convention ?? prev.paymentConvention,
            schedule: {
              ...prev.schedule,
              ...(sampleFixed.schedule ? {
                calendar: sampleFixed.schedule.calendar ?? prev.schedule.calendar,
                effectiveDate: sampleFixed.schedule.effective_date ?? prev.schedule.effectiveDate,
                terminationDate: sampleFixed.schedule.termination_date ?? prev.schedule.terminationDate,
                frequency: sampleFixed.schedule.frequency ?? prev.schedule.frequency,
                convention: sampleFixed.schedule.convention ?? prev.schedule.convention,
                terminationDateConvention: sampleFixed.schedule.termination_date_convention ?? prev.schedule.terminationDateConvention,
                dateGenerationRule: sampleFixed.schedule.date_generation_rule ?? prev.schedule.dateGenerationRule,
              } : {}),
            },
          }));
          setFloatingLeg(prev => ({
            ...prev,
            notional: sampleFloat.notional ?? prev.notional,
            spread: sampleFloat.spread ?? prev.spread,
            dayCounter: sampleFloat.day_counter ?? prev.dayCounter,
            paymentConvention: sampleFloat.payment_convention ?? prev.paymentConvention,
            fixingDays: sampleFloat.fixing_days ?? prev.fixingDays,
            inArrears: sampleFloat.in_arrears ?? prev.inArrears,
            schedule: {
              ...prev.schedule,
              ...(sampleFloat.schedule ? {
                calendar: sampleFloat.schedule.calendar ?? prev.schedule.calendar,
                effectiveDate: sampleFloat.schedule.effective_date ?? prev.schedule.effectiveDate,
                terminationDate: sampleFloat.schedule.termination_date ?? prev.schedule.terminationDate,
                frequency: sampleFloat.schedule.frequency ?? prev.schedule.frequency,
                convention: sampleFloat.schedule.convention ?? prev.schedule.convention,
                terminationDateConvention: sampleFloat.schedule.termination_date_convention ?? prev.schedule.terminationDateConvention,
                dateGenerationRule: sampleFloat.schedule.date_generation_rule ?? prev.schedule.dateGenerationRule,
              } : {}),
            },
          }));
          if (isCms) {
            setCmsLeg(prev => ({
              ...prev,
              notional: sampleCms.notional ?? prev.notional,
              dayCounter: sampleCms.day_counter ?? prev.dayCounter,
              paymentConvention: sampleCms.payment_convention ?? prev.paymentConvention,
              gear: sampleCms.gear ?? prev.gear,
              spread: sampleCms.spread ?? prev.spread,
              swapIndexId: sampleCms.swap_index_id ?? prev.swapIndexId,
              swapTenorN: sampleCms.swap_tenor?.n ?? prev.swapTenorN,
              swapTenorUnit: sampleCms.swap_tenor?.unit ?? prev.swapTenorUnit,
              swaptionVolId: sampleCms.swaption_vol_id ?? prev.swaptionVolId,
              fixingDaysOverride: sampleCms.fixing_days ?? prev.fixingDaysOverride,
              useFixingDaysOverride: sampleCms.fixing_days !== undefined,
              usePricerSpec: Boolean(sampleCms.pricer),
              pricer: {
                ...prev.pricer,
                ...(sampleCms.pricer ? {
                  pricerType: sampleCms.pricer.pricer_type ?? prev.pricer.pricerType,
                  yieldCurveModel: sampleCms.pricer.yield_curve_model ?? prev.pricer.yieldCurveModel,
                  meanReversion: sampleCms.pricer.mean_reversion ?? prev.pricer.meanReversion,
                  haganLowerLimit: sampleCms.pricer.hagan_lower_limit ?? prev.pricer.haganLowerLimit,
                  haganUpperLimit: sampleCms.pricer.hagan_upper_limit ?? prev.pricer.haganUpperLimit,
                  haganPrecision: sampleCms.pricer.hagan_precision ?? prev.pricer.haganPrecision,
                  haganHardUpperLimit: sampleCms.pricer.hagan_hard_upper_limit ?? prev.pricer.haganHardUpperLimit,
                } : {}),
              },
              schedule: {
                ...prev.schedule,
                ...(sampleCms.schedule ? {
                  calendar: sampleCms.schedule.calendar ?? prev.schedule.calendar,
                  effectiveDate: sampleCms.schedule.effective_date ?? prev.schedule.effectiveDate,
                  terminationDate: sampleCms.schedule.termination_date ?? prev.schedule.terminationDate,
                  frequency: sampleCms.schedule.frequency ?? prev.schedule.frequency,
                  convention: sampleCms.schedule.convention ?? prev.schedule.convention,
                  terminationDateConvention: sampleCms.schedule.termination_date_convention ?? prev.schedule.terminationDateConvention,
                  dateGenerationRule: sampleCms.schedule.date_generation_rule ?? prev.schedule.dateGenerationRule,
                } : {}),
              },
            }));
          }
        } else if (row.ois_swap) {
          const sampleSwap = row.ois_swap;
          const sampleFixed = sampleSwap.fixed_leg || {};
          const sampleOn = sampleSwap.overnight_leg || {};
          applySwapKind('OIS');
          if (sampleSwap.swap_type) setSwapType(sampleSwap.swap_type);
          const resolvedOvernightIndexId = resolvePreferred(sampleOn.index?.id, overnightIds, curveReferencedIndexIds);
          if (resolvedOvernightIndexId) setOvernightIndexRef({ id: resolvedOvernightIndexId });
          if (forward) {
            setForecastCurveId(forward.id || '');
            setForecastCurve(mapCurve(forward, 'forward'));
            setUseSameCurve((row.forwarding_curve || 'discount') === 'discount');
          }
          setFixedLeg(prev => ({
            ...prev,
            notional: sampleFixed.notional ?? prev.notional,
            rate: sampleFixed.rate ?? prev.rate,
            dayCounter: sampleFixed.day_counter ?? prev.dayCounter,
            paymentConvention: sampleFixed.payment_convention ?? prev.paymentConvention,
          }));
          setOvernightLeg(prev => ({
            ...prev,
            notional: sampleOn.notional ?? prev.notional,
            spread: sampleOn.spread ?? prev.spread,
            dayCounter: sampleOn.day_counter ?? prev.dayCounter,
            paymentConvention: sampleOn.payment_convention ?? prev.paymentConvention,
            paymentCalendar: sampleOn.payment_calendar ?? prev.paymentCalendar,
            paymentLag: sampleOn.payment_lag ?? prev.paymentLag,
            averagingMethod: sampleOn.averaging_method ?? prev.averagingMethod,
            lookbackDays: sampleOn.lookback_days ?? prev.lookbackDays,
            lockoutDays: sampleOn.lockout_days ?? prev.lockoutDays,
            applyObservationShift: sampleOn.apply_observation_shift ?? prev.applyObservationShift,
            telescopicValueDates: sampleOn.telescopic_value_dates ?? prev.telescopicValueDates,
          }));
        } else if (row.basis_swap) {
          const sampleSwap = row.basis_swap;
          const leg1 = sampleSwap.leg1 || {};
          const leg2 = sampleSwap.leg2 || {};
          applySwapKind('Basis');
          if (sampleSwap.swap_type) setSwapType(sampleSwap.swap_type);
          const resolvedLeg1IndexId = resolvePreferred(leg1.index?.id, iborIds, curveReferencedIndexIds);
          const iborIdsForLeg2 = iborIds.filter(indexId => indexId !== resolvedLeg1IndexId);
          const resolvedLeg2IndexId = resolvePreferred(leg2.index?.id, iborIdsForLeg2.length > 0 ? iborIdsForLeg2 : iborIds, curveReferencedIndexIds);
          if (resolvedLeg1IndexId) setBasisIndexRef1({ id: resolvedLeg1IndexId });
          if (resolvedLeg2IndexId) setBasisIndexRef2({ id: resolvedLeg2IndexId });
          if (forward1) {
            setBasisForwardCurve1Id(forward1.id || '');
            setBasisForwardCurve1(mapCurve(forward1, 'forward'));
          }
          if (forward2) {
            setBasisForwardCurve2Id(forward2.id || '');
            setBasisForwardCurve2(mapCurve(forward2, 'forward'));
          }
          setBasisLeg1(prev => ({ ...prev, notional: leg1.notional ?? prev.notional, spread: leg1.spread ?? prev.spread, dayCounter: leg1.day_counter ?? prev.dayCounter, paymentConvention: leg1.payment_convention ?? prev.paymentConvention, fixingDays: leg1.fixing_days ?? prev.fixingDays, inArrears: leg1.in_arrears ?? prev.inArrears }));
          setBasisLeg2(prev => ({ ...prev, notional: leg2.notional ?? prev.notional, spread: leg2.spread ?? prev.spread, dayCounter: leg2.day_counter ?? prev.dayCounter, paymentConvention: leg2.payment_convention ?? prev.paymentConvention, fixingDays: leg2.fixing_days ?? prev.fixingDays, inArrears: leg2.in_arrears ?? prev.inArrears }));
        }
      });
    }
  }, [id, isNew, asOfDate]);

  // Map a portal-shaped curve (id, day_counter, points, interpolator,
  // bootstrap_trait, reference_date) onto the orchestrator's inline
  // CurveRef. Strict ``extra="forbid"``: only the fields
  // CurveRef accepts (id/name/currency/day_counter/helper_kind/
  // reference_date/points/body) may appear. Interpolator + bootstrap
  // trait are not part of the wire contract and are dropped here.
  // Points pass through normalizeCurvePointForApi so legacy
  // ``tenor_number``/``tenor_time_unit`` becomes the canonical
  // ``tenor: {n, unit}`` shape the backend reads; quote IDs are forwarded
  // unresolved (the backend substitutes their values).
  const toThinACurve = (curve: any, role?: 'discount' | 'forwarding'): any => {
    const normalized = normalizeCurveForApi(curve);
    const out: Record<string, any> = {
      name: curve.id || curve.name || (role ?? 'curve'),
      points: normalized.points,
    };
    if (curve.currency) out.currency = curve.currency;
    if (curve.day_counter) out.day_counter = curve.day_counter;
    if (curve.reference_date) out.reference_date = curve.reference_date;
    if (role) out.body = { role };
    return out;
  };

  // The user's selected float-leg forwarding index, normalised to the wire
  // shape the orchestrator reads (``pricing.indices[0]``, keys
  // ``indices``/``index``). Without an inline
  // index in the vanilla trade body the backend resolves ``None`` and
  // falls back to its default forwarding index (Euribor-6M), so a
  // user-selected index (e.g. Euribor 3M) never reaches the engine. Returns the
  // ``{ pricing: { indices: [...] } }`` fragment to splat into the trade body,
  // or ``{}`` when no leg index resolved (keeps the orchestrator default path).
  const buildIndexPricingFragment = (
    resolvedIndices: any[],
  ): Record<string, unknown> => {
    const legIndex = resolvedIndices.find((d: any) => d?.id === indexRef.id);
    if (!legIndex) return {};
    return { pricing: { indices: [normalizeIndexDefForApi(legIndex)] } };
  };

  // Commit an IndexPicker selection into a leg's form state. A Custom index is
  // authored inline in the picker and handed back via the second (`def`) arg;
  // it is NOT in the local index registry, so (a) resolveIndexDefs — which only
  // reads indexStore — can't resolve it (buildIndexPricingFragment then drops it
  // and the orchestrator falls back to Euribor-6M), and (b) the auto-preselect
  // effect immediately reverts indexRef because the id isn't in the saved
  // iborIds. Persisting an unknown def first makes it resolvable AND keeps the
  // selection from being reset. Saved selections already exist in the store, so
  // this is a no-op for them.
  const applyIndexPick = async (
    ref: IndexRef,
    def: IndexDef | undefined,
    setter: (r: IndexRef) => void,
    applyTenorFrequency?: (frequency: Frequency) => void,
  ) => {
    if (def && ref.id) {
      const existing = await indexStore.getById(ref.id);
      if (!existing) {
        await indexStore.save(def as unknown as Parameters<typeof indexStore.save>[0]);
      }
    }
    setter(ref);
    // The floating leg's payment schedule should follow the picked
    // index's tenor by default (3M → Quarterly, 6M → Semiannual, 1M →
    // Monthly, 1Y → Annual), matching the backend's derivation. Only
    // applied here, on an EXPLICIT index pick — a manual frequency override
    // afterwards is respected (nothing else re-derives it on re-renders), and
    // an unclean tenor (e.g. 5M) derives null and keeps the current default.
    if (applyTenorFrequency && ref.id) {
      const picked = def ?? (await indexStore.getById(ref.id));
      const derived = frequencyFromIndexDef(picked as Parameters<typeof frequencyFromIndexDef>[0]);
      if (derived) applyTenorFrequency(derived);
    }
  };

  // POST body for ``POST /v1/price/swap/ir`` (the inline shape).
  // Vanilla case ships the flat trade body + role-tagged top-level
  // curves the backend's request validation requires. Non-vanilla branches
  // (OIS / Basis / CMS) keep their legacy fat shape wrapped under
  // ``swap:``; they 422-reject on the wire until the non-vanilla
  // translation fidelity work lands — by design, not silently.
  const buildRequest = async (
    curves: any[],
    indices: any[],
    preferBackendQuotes: boolean
  ): Promise<any> => {
    if (swapKind === 'Vanilla') {
      const thinACurves = curves.length > 1
        ? curves.map((c, i) => toThinACurve(c, i === 0 ? 'discount' : 'forwarding'))
        : [toThinACurve(curves[0])];
      return {
        swap: {
          notional: fixedLeg.notional,
          fixed_rate: fixedLeg.rate,
          swap_type: swapType,
          effective_date: fixedLeg.schedule.effectiveDate,
          termination_date: fixedLeg.schedule.terminationDate,
          // Ship the float-leg payment frequency (tenor-derived default,
          // user-overridable) so the outgoing request states the coupon
          // schedule explicitly. The orchestrator currently derives the same
          // value from the resolved index tenor and tolerates unknown
          // trade-body keys, so this is forward-compatible.
          floating_leg: { schedule: { frequency: floatingLeg.schedule.frequency } },
          ...buildIndexPricingFragment(indices),
        },
        curves: thinACurves,
        as_of: asOfDate,
      };
    }
    // TODO: non-vanilla swap variants (OIS / Basis / CMS) still ship
    // the legacy fat body and 422 on the wire — visible failure is the
    // intent until their backend translation fidelity work lands.
    const fat = await buildLegacyFatRequest(curves, indices, preferBackendQuotes);
    return { swap: fat, as_of: asOfDate };
  };

  const buildLegacyFatRequest = async (
    curves: any[],
    indices: any[],
    preferBackendQuotes: boolean
  ): Promise<IrSwapRequest> => {
    const selectedCmsSurface = swapKind === 'CMS' ? volSurfaces.find(v => v.id === cmsLeg.swaptionVolId) : null;
    // pricing.as_of_date must equal surface.base.reference_date in strict
    // mode (request/sample_vol_surfaces_request.cpp:352-356; same rule on
    // /price-vanilla-swap CMS path). Mirror VolWorkbench.runSample so a
    // stored CMS vol surface dated at calibration time prices today without
    // tripping the strict-mode guard.
    const pricingAsOfDate = selectedCmsSurface?.base?.reference_date || asOfDate;
    const pricingQuotes = attachQuotes
      ? (
          await buildPricingQuoteSnapshotWithBackend(pricingAsOfDate, {
            quoteType: 'Curve',
            preferBackend: preferBackendQuotes,
          })
        ).quotes
      : [];
    const selectedCmsIndex = indices.find((d: any) => d.id === indexRef.id);
    // spot_days is an index convention; default reads from the saved index
    // definition. The CMS leg keeps its explicit "Override fixing days"
    // toggle as a power-user escape hatch — when on, the override wins; when
    // off, fall back to the index's `fixing_days`. The `?? 2` covers legacy
    // index records persisted before `fixing_days` was a saved field.
    const cmsSwapIndexDef = swapKind === 'CMS' ? {
      id: cmsLeg.swapIndexId,
      kind: 'IborSwapIndex',
      spot_days: cmsLeg.useFixingDaysOverride && Number.isFinite(cmsLeg.fixingDaysOverride)
        ? cmsLeg.fixingDaysOverride
        : (selectedCmsIndex?.fixing_days ?? 2),
      calendar: selectedCmsIndex?.calendar || 'TARGET',
      business_day_convention: selectedCmsIndex?.business_day_convention || 'ModifiedFollowing',
      float_index_id: indexRef.id,
      fixed_leg: {
        fixed_frequency: 'Annual',
        fixed_day_counter: fixedLeg.dayCounter,
        fixed_calendar: fixedLeg.schedule.calendar,
        fixed_bdc: fixedLeg.schedule.convention,
        fixed_term_bdc: fixedLeg.schedule.terminationDateConvention,
        fixed_date_rule: fixedLeg.schedule.dateGenerationRule,
        fixed_eom: false,
      },
      float_leg: {
        float_tenor: selectedCmsIndex?.tenor || { n: 6, unit: 'Months' },
        float_calendar: selectedCmsIndex?.calendar || 'TARGET',
        float_bdc: selectedCmsIndex?.business_day_convention || 'ModifiedFollowing',
        float_term_bdc: selectedCmsIndex?.business_day_convention || 'ModifiedFollowing',
        float_date_rule: 'Forward',
        float_eom: false,
      },
    } : null;

    // Quote resolver shared by the vol-payload helper. Anchored at
    // pricingAsOfDate so a surface dated in the past resolves quotes against
    // its calibration date, not today. Mirrors VolWorkbench.runSample.
    const resolverMode = getResolutionMode();
    const bookByQuoteId = new Map(getQuoteBook().map(q => [q.id, q]));
    const resolveQuoteValueById = (qid?: string): number | null => {
      if (!qid) return null;
      const b = bookByQuoteId.get(qid);
      if (!b) return null;
      return resolveQuoteValue(b.series, pricingAsOfDate, resolverMode);
    };

    // Route the CMS vol payload through the canonical helper so the wire
    // shape is driven by surface.base.shape (Constant / AtmMatrix2D /
    // SmileCube3D / SabrParams) instead of a local heuristic. Single helper,
    // single set of pre-flight checks (period-int, dimensional, lognormal-
    // SABR-only).
    let cmsVolSurfacePayload: any = null;
    if (swapKind === 'CMS' && selectedCmsSurface) {
      try {
        cmsVolSurfacePayload = buildSwaptionVolWirePayload(
          selectedCmsSurface,
          cmsLeg.swapIndexId,
          pricingAsOfDate,
          resolveQuoteValueById,
        );
      } catch (err) {
        if (err instanceof VolSurfacePayloadError) throw err;
        throw new Error(err instanceof Error ? err.message : 'Failed to build CMS vol payload');
      }
    }
    // Do NOT resolve quote_ids client-side.
    // Curve points carrying a quote_id reach the orchestrator UNRESOLVED; the
    // orchestrator's md_resolution walker pulls the value from the MD server.
    // Inline (literal-rate) points pass through untouched.
    const resolvedCurvesForApi = curves.map(normalizeCurveForApi);
    // Drop inflation curves and pointless curves before pricing.curves —
    // term_structure_parser.cpp:36 throws "Empty list of points for term
    // structure" inside PricingRegistryBuilder.build(), failing the request
    // before per-trade dispatch. Defensive parity with VolWorkbench.runSample.
    const filteredCurvesForApi = resolvedCurvesForApi.filter((c: any) =>
      c?.role !== 'inflation' && Array.isArray(c?.points) && c.points.length > 0
    );
    const base = {
      pricing: {
        as_of_date: pricingAsOfDate,
        settlement_date: settlementDate,
        indices: indices.map(normalizeIndexDefForApi),
        curves: filteredCurvesForApi,
        ...(attachQuotes ? { quotes: pricingQuotes } : {}),
        ...(swapKind === 'CMS' && cmsSwapIndexDef ? { swap_indices: [cmsSwapIndexDef] } : {}),
        ...(swapKind === 'CMS' && cmsVolSurfacePayload ? { vol_surfaces: [cmsVolSurfacePayload] } : {}),
      },
      include_flows: includeFlows,
    };
    if (swapKind === 'OIS') {
      return {
        ...base,
        swaps: [{
          ois_swap: {
            swap_type: swapType,
            fixed_leg: {
              notional: fixedLeg.notional,
              rate: fixedLeg.rate,
              day_counter: fixedLeg.dayCounter,
              payment_convention: fixedLeg.paymentConvention,
              schedule: scheduleToApi(fixedLeg.schedule),
            },
            overnight_leg: {
              notional: overnightLeg.notional,
              index: { id: overnightIndexRef.id },
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
              schedule: scheduleToApi(overnightLeg.schedule),
            },
          },
          discounting_curve: 'discount',
          forwarding_curve: useSameCurve ? 'discount' : 'forward',
        }],
      };
    }
    if (swapKind === 'Basis') {
      return {
        ...base,
        swaps: [{
          basis_swap: {
            swap_type: swapType,
            leg1: {
              notional: basisLeg1.notional,
              index: { id: basisIndexRef1.id },
              spread: basisLeg1.spread,
              day_counter: basisLeg1.dayCounter,
              payment_convention: basisLeg1.paymentConvention,
              fixing_days: basisLeg1.fixingDays,
              in_arrears: basisLeg1.inArrears,
              schedule: scheduleToApi(basisLeg1.schedule),
            },
            leg2: {
              notional: basisLeg2.notional,
              index: { id: basisIndexRef2.id },
              spread: basisLeg2.spread,
              day_counter: basisLeg2.dayCounter,
              payment_convention: basisLeg2.paymentConvention,
              fixing_days: basisLeg2.fixingDays,
              in_arrears: basisLeg2.inArrears,
              schedule: scheduleToApi(basisLeg2.schedule),
            },
          },
          discounting_curve: 'discount',
          forwarding_curve_leg1: 'forward_leg1',
          forwarding_curve_leg2: 'forward_leg2',
        }],
      };
    }
    if (swapKind === 'CMS') {
      const cmsLegPayload: any = {
        notional: cmsLeg.notional,
        schedule: scheduleToApi(cmsLeg.schedule),
        swap_index_id: cmsLeg.swapIndexId,
        swap_tenor: { n: cmsLeg.swapTenorN, unit: cmsLeg.swapTenorUnit },
        swaption_vol_id: cmsLeg.swaptionVolId,
        day_counter: cmsLeg.dayCounter,
        payment_convention: cmsLeg.paymentConvention,
        gear: cmsLeg.gear,
        spread: cmsLeg.spread,
      };
      if (cmsLeg.useFixingDaysOverride && Number.isFinite(cmsLeg.fixingDaysOverride)) {
        cmsLegPayload.fixing_days = cmsLeg.fixingDaysOverride;
      }
      if (cmsLeg.usePricerSpec) {
        cmsLegPayload.pricer = {
          pricer_type: cmsLeg.pricer.pricerType,
          yield_curve_model: cmsLeg.pricer.yieldCurveModel,
          mean_reversion: cmsLeg.pricer.meanReversion,
          ...(cmsLeg.pricer.pricerType === 'HaganNumeric' ? {
            hagan_lower_limit: cmsLeg.pricer.haganLowerLimit,
            hagan_upper_limit: cmsLeg.pricer.haganUpperLimit,
            hagan_precision: cmsLeg.pricer.haganPrecision,
            hagan_hard_upper_limit: cmsLeg.pricer.haganHardUpperLimit,
          } : {}),
        };
      }
      return {
        ...base,
        swaps: [{
          vanilla_swap: {
            swap_type: swapType,
            fixed_leg: {
              notional: fixedLeg.notional,
              rate: fixedLeg.rate,
              day_counter: fixedLeg.dayCounter,
              payment_convention: fixedLeg.paymentConvention,
              schedule: scheduleToApi(fixedLeg.schedule),
            },
            cms_leg: cmsLegPayload,
          },
          discounting_curve: 'discount',
          forwarding_curve: useSameCurve ? 'discount' : 'forward',
        }],
      };
    }
    return {
      ...base,
      swaps: [{
        vanilla_swap: {
          swap_type: swapType,
          fixed_leg: {
            notional: fixedLeg.notional,
            rate: fixedLeg.rate,
            day_counter: fixedLeg.dayCounter,
            payment_convention: fixedLeg.paymentConvention,
            schedule: scheduleToApi(fixedLeg.schedule),
          },
          floating_leg: {
            notional: floatingLeg.notional,
            index: { id: indexRef.id },
            spread: floatingLeg.spread,
            day_counter: floatingLeg.dayCounter,
            payment_convention: floatingLeg.paymentConvention,
            fixing_days: floatingLeg.fixingDays,
            in_arrears: floatingLeg.inArrears,
            schedule: scheduleToApi(floatingLeg.schedule),
          },
        },
        discounting_curve: 'discount',
        forwarding_curve: useSameCurve ? 'discount' : 'forward',
      }],
    };
  };

  // Flat vanilla trade body persisted as the saved swap's request — identical
  // to the inline ``swap`` levers the backend reads (notional / fixed_rate
  // / dates), plus the selected float-leg index under ``pricing.indices`` so
  // the by-reference path resolves the user's index instead of the Euribor-6M
  // default (parity with the inline arm). ``persistIrSwapGraph`` merges
  // ``pricing.curve_set_id`` into this same ``pricing`` block so the backend
  // chains trade → curve_set → curves.
  const buildVanillaTradeBody = (resolvedIndices: any[]): Record<string, unknown> => ({
    notional: fixedLeg.notional,
    fixed_rate: fixedLeg.rate,
    swap_type: swapType,
    effective_date: fixedLeg.schedule.effectiveDate,
    termination_date: fixedLeg.schedule.terminationDate,
    // Parity with the inline body — persist the float-leg payment
    // frequency (tenor-derived default, user-overridable) alongside the index.
    floating_leg: { schedule: { frequency: floatingLeg.schedule.frequency } },
    ...buildIndexPricingFragment(resolvedIndices),
  });

  // Map the priced curves to CurveCreate bodies (via the same toThinACurve
  // used on the inline wire), keyed by role so the curve_set ref order and the
  // idempotent re-save id↔uuid map line up (discount first; assembler reads
  // roles positionally).
  const buildCurveGraphInputs = (curves: any[]): CurveGraphInput[] =>
    curves.length > 1
      ? curves.map((c, i) => ({
          key: i === 0 ? 'discount' : 'forward',
          body: toThinACurve(c, i === 0 ? 'discount' : 'forwarding'),
        }))
      : [{ key: 'discount', body: toThinACurve(curves[0]) }];

  const handleSaveSwap = async () => {
    setSaveStatus(null);
    const validation = validateRequestInputs();
    setValidationErrors(validation);
    if (Object.keys(validation).length > 0) {
      const issues = Object.values(validation).filter(Boolean);
      setSaveStatus({
        tone: 'error',
        message: issues.length > 0
          ? `Cannot save: ${issues.join(' ')}`
          : 'Cannot save while the form has validation errors.',
      });
      return;
    }
    setSaving(true);
    try {
      const curves = buildCurvesPayload();
      const resolvedIndices = await resolveIndicesForCurrentSwap(curves);
      // localStorage save keeps the legacy fat IrSwapRequest shape so the load
      // path (and deriveIrSwapName) keep working unchanged and stay the
      // dual-read fallback (additive, not a rip-out).
      const request = await buildLegacyFatRequest(curves, resolvedIndices, false);
      const trimmedName = swapName.trim();
      const priorGraph = appGraph;
      // Stamp the selected curve-store ids so the lazy
      // migration can reload the quote-id-bearing curves and rebuild the
      // persist graph faithfully. Validated against storage/curves — a record
      // re-loaded then re-saved without re-selecting (selector id clobbered to
      // 'discount') simply omits the ref and stays migration-skipped.
      const sourceRefs = buildSourceRefs({
        curves: [
          { role: 'discount', curveId: discountCurveId },
          ...(useSameCurve ? [] : [{ role: 'forward', curveId: forecastCurveId }]),
        ],
      });

      // Persist the entity graph server-side leaf→root and stamp the UUIDs
      // back. Only the vanilla path is wired (inline + by-ref are live for
      // it); OIS/Basis/CMS stay localStorage-only until their backend
      // translation fidelity lands, so they keep pricing inline (and 422 as
      // today).
      let nextGraph: IrSwapAppGraph | null = priorGraph;
      // Honest default: when the server persist below is skipped (non-vanilla
      // kinds), say so instead of implying a full save.
      let serverNote = priorGraph
        ? ''
        : ' Saved locally only — OIS/Basis/CMS kinds are not yet persisted server-side.';
      if (swapKind === 'Vanilla') {
        const graphResult = await persistIrSwapGraph(
          {
            name: trimmedName || deriveIrSwapName(request),
            curves: buildCurveGraphInputs(curves),
            curveSetCurrency: discountCurve?.currency,
            swapRequest: buildVanillaTradeBody(resolvedIndices),
            changeReason: changeReason.trim() || undefined,
          },
          priorGraph,
        );
        if (!graphResult.ok) {
          // Branch on the structured envelope code. The localStorage
          // save below still runs so no user data is lost (dual-read).
          const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
          await saveIrSwap(request, {
            id: isNew ? undefined : (swapId || undefined),
            name: trimmedName || undefined,
            appId: priorGraph?.swapId,
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
      }

      const savedId = await saveIrSwap(request, {
        id: isNew ? undefined : (swapId || undefined),
        name: trimmedName || undefined,
        appId: nextGraph?.swapId,
        appGraph: (nextGraph ?? undefined) as Record<string, unknown> | undefined,
        sourceRefs,
      });
      setSwapId(savedId);
      setAppGraph(nextGraph);
      const displayName = trimmedName || deriveIrSwapName(request);
      setSaveStatus({ tone: 'success', message: `Saved "${displayName}".${serverNote}` });
      setChangeReason('');
      if (isNew || id !== savedId) {
        navigate(`/products/ir-swap/${savedId}`, { replace: true });
      }
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save swap' });
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
    const validation = validateRequestInputs();
    setValidationErrors(validation);
    if (Object.keys(validation).length > 0) {
      setError('Please fix highlighted validation errors.');
      return;
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    try {
      // Price-arm selection. A saved swap (appGraph
      // present) prices by-reference: a minimal {swap_id, as_of} body,
      // and the orchestrator loads the trade + curve chain server-side — so we
      // skip the local curve build/coverage check entirely. An unsaved swap
      // prices inline, unchanged.
      let request: any;
      let wireAsOf: string;
      if (appGraph?.swapId) {
        wireAsOf = asOfDate;
        request = buildIrSwapPriceArm({ appGraph, inlineRequest: undefined, asOf: wireAsOf });
      } else {
        const curves = buildCurvesPayload();
        validateCurveCoverage(curves);
        const resolvedIndices = await resolveIndicesForCurrentSwap(curves);
        const inlineRequest = await buildRequest(curves, resolvedIndices, true);
        // The inline vanilla body carries top-level ``as_of``; legacy fat
        // (OIS/Basis/CMS branches) reads from the wrapped ``swap.pricing.as_of_date``.
        wireAsOf = inlineRequest?.as_of
          ?? inlineRequest?.swap?.pricing?.as_of_date
          ?? asOfDate;
        request = buildIrSwapPriceArm({ appGraph: null, inlineRequest, asOf: wireAsOf });
      }
      const response = await priceIrSwap(request, wireAsOf);
      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);

      if (response.success && response.data?.swaps?.[0]) {
        setResult(response.data.swaps[0] as SwapResultData);
      } else {
        setError(response.error || 'Pricing failed');
        setErrorInfo(response.errorInfo);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Pricing failed';
      if (message.includes('past max curve time')) {
        setError('Selected curve does not extend far enough for the swap maturity. Add longer-tenor points to your curve (or choose a shorter maturity).');
      } else {
        setError(message);
      }
      setErrorInfo(undefined);
    } finally {
      setLoading(false);
    }
  };

  const buildCurvesPayload = () => {
    if (!discountCurve) throw new Error('Please select a discounting curve.');
    const curves: any[] = [{
      id: 'discount',
      day_counter: discountCurve.day_counter,
      interpolator: discountCurve.interpolator,
      bootstrap_trait: discountCurve.bootstrap_trait,
      reference_date: discountCurve.reference_date || asOfDate,
      points: discountCurve.points,
    }];
    if ((swapKind === 'Vanilla' || swapKind === 'OIS' || swapKind === 'CMS') && !useSameCurve) {
      if (!forecastCurve) throw new Error('Please select a forwarding curve.');
      curves.push({
        id: 'forward',
        day_counter: forecastCurve.day_counter,
        interpolator: forecastCurve.interpolator,
        bootstrap_trait: forecastCurve.bootstrap_trait,
        reference_date: forecastCurve.reference_date || asOfDate,
        points: forecastCurve.points,
      });
    }
    if (swapKind === 'Basis') {
      // In basis mode, default both forwarding curves to discounting curve
      // so pricing can run without extra curve picks.
      const forward1 = basisForwardCurve1 || discountCurve;
      const forward2 = basisForwardCurve2 || discountCurve;
      curves.push({
        id: 'forward_leg1',
        day_counter: forward1.day_counter,
        interpolator: forward1.interpolator,
        bootstrap_trait: forward1.bootstrap_trait,
        reference_date: forward1.reference_date || asOfDate,
        points: forward1.points,
      });
      curves.push({
        id: 'forward_leg2',
        day_counter: forward2.day_counter,
        interpolator: forward2.interpolator,
        bootstrap_trait: forward2.bootstrap_trait,
        reference_date: forward2.reference_date || asOfDate,
        points: forward2.points,
      });
    }
    return curves;
  };

  const resolveIndicesForCurrentSwap = async (curves: any[]) => {
    const curveIndexIds = collectIndexIdsFromCurves(curves);
    const legIndexIds = swapKind === 'Vanilla'
      ? [indexRef.id]
      : swapKind === 'OIS'
        ? [overnightIndexRef.id]
        : swapKind === 'CMS'
          ? [indexRef.id]
          : [basisIndexRef1.id, basisIndexRef2.id];
    const allIndexIds = Array.from(new Set([...legIndexIds, ...curveIndexIds]));
    const resolvedIndices = await resolveIndexDefs(allIndexIds);
    const missingAll = allIndexIds.filter(id => id && !resolvedIndices.some(def => def.id === id));
    if (missingAll.length > 0) {
      const missingLeg = legIndexIds.filter(id => id && missingAll.includes(id));
      const missingCurve = curveIndexIds.filter(id => id && missingAll.includes(id));
      const parts: string[] = [];
      if (missingLeg.length > 0) parts.push(`swap legs: ${Array.from(new Set(missingLeg)).join(', ')}`);
      if (missingCurve.length > 0) parts.push(`curve helpers: ${Array.from(new Set(missingCurve)).join(', ')}`);
      throw new Error(`Missing index definitions in Market Data -> Indices (${parts.join(' | ')})`);
    }
    if (resolvedIndices.length === 0) {
      throw new Error('No index definitions found. Add at least one index in Market Data -> Indices.');
    }
    return resolvedIndices;
  };

  const getSwapRequiredYears = (): number | null => {
    const termDates = (
      swapKind === 'Vanilla'
        ? [fixedLeg.schedule.terminationDate, floatingLeg.schedule.terminationDate]
        : swapKind === 'OIS'
          ? [fixedLeg.schedule.terminationDate, overnightLeg.schedule.terminationDate]
          : swapKind === 'CMS'
            ? [fixedLeg.schedule.terminationDate, cmsLeg.schedule.terminationDate]
            : [basisLeg1.schedule.terminationDate, basisLeg2.schedule.terminationDate]
    )
      .map(d => new Date(d))
      .filter(d => !Number.isNaN(d.getTime()));
    const effectiveDates = (
      swapKind === 'Vanilla'
        ? [fixedLeg.schedule.effectiveDate, floatingLeg.schedule.effectiveDate]
        : swapKind === 'OIS'
          ? [fixedLeg.schedule.effectiveDate, overnightLeg.schedule.effectiveDate]
          : swapKind === 'CMS'
            ? [fixedLeg.schedule.effectiveDate, cmsLeg.schedule.effectiveDate]
            : [basisLeg1.schedule.effectiveDate, basisLeg2.schedule.effectiveDate]
    )
      .map(d => new Date(d))
      .filter(d => !Number.isNaN(d.getTime()));
    if (termDates.length === 0 || effectiveDates.length === 0) return null;
    const maxTerm = Math.max(...termDates.map(d => d.getTime()));
    const minEff = Math.min(...effectiveDates.map(d => d.getTime()));
    return Math.max(0, (maxTerm - minEff) / (365 * 24 * 60 * 60 * 1000));
  };

  const validateCurveCoverage = (curves: any[]) => {
    const requiredYears = getSwapRequiredYears();
    if (!requiredYears || requiredYears <= 0) return;
    const tooShort = curves
      .map(c => ({ id: c.id || 'curve', maxYears: estimateCurveMaxYears(c) }))
      .filter(c => c.maxYears !== null && (c.maxYears as number) + 1e-6 < requiredYears);
    if (tooShort.length > 0) {
      const details = tooShort.map(c => `${c.id} (~${(c.maxYears as number).toFixed(2)}y)`).join(', ');
      throw new Error(`Curve horizon too short for swap maturity (~${requiredYears.toFixed(2)}y). Extend curve tenors for: ${details}.`);
    }
  };

  const validateRequestInputs = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    const inferred = inferSwapKind(leg1Type, leg2Type);
    if (!inferred) {
      errors.legPair = 'Unsupported leg combination. Use Fixed/Floating (Vanilla), Floating/Floating (Basis), Fixed/Overnight (OIS), or Fixed/CMS (CMS).';
      return errors;
    }
    if (!discountCurve) errors.discountCurve = 'Discounting curve is required.';
    if ((swapKind === 'Vanilla' || swapKind === 'OIS' || swapKind === 'CMS') && !useSameCurve && !forecastCurve) {
      errors.forwardCurve = 'Forwarding curve is required.';
    }
    if (swapKind === 'Vanilla' && !indexRef.id) errors.index = 'Floating index is required.';
    if (swapKind === 'CMS') {
      if (!indexRef.id) errors.index = 'Floating index is required for CMS conventions.';
      if (!cmsLeg.swapIndexId.trim()) errors.cmsSwapIndex = 'CMS swap index id is required.';
      if (!cmsLeg.swaptionVolId.trim()) errors.cmsSwaptionVol = 'CMS swaption vol id is required.';
      if (!Number.isFinite(cmsLeg.swapTenorN) || cmsLeg.swapTenorN <= 0) errors.cmsSwapTenor = 'CMS swap tenor must be positive.';
      if (!volSurfaces.some(v => v.id === cmsLeg.swaptionVolId)) errors.cmsSwaptionVol = 'Selected CMS swaption vol id was not found.';
      // SABR-params surfaces require both discount + forwarding curves to
      // resolve a per-node ATM forward via finalizeSwaptionVolEntryForPricing
      // (request/swaption_vol_runtime.cpp). Surface a clear message before
      // the request leaves the browser instead of an opaque backend error.
      const cmsSurface = volSurfaces.find(v => v.id === cmsLeg.swaptionVolId);
      if (cmsSurface?.base?.shape === 'SabrParams') {
        if (!discountCurve) errors.discountCurve = 'SABR CMS pricing requires a Discounting Curve.';
        if (!useSameCurve && !forecastCurve) errors.forwardCurve = 'SABR CMS pricing requires a Forwarding Curve (or enable single-curve mode).';
      }
      if (cmsLeg.usePricerSpec && cmsLeg.pricer.pricerType === 'HaganNumeric') {
        if (!(cmsLeg.pricer.haganUpperLimit > cmsLeg.pricer.haganLowerLimit)) {
          errors.cmsHaganBounds = 'Hagan upper limit must be greater than lower limit.';
        }
        if (!(cmsLeg.pricer.haganPrecision > 0)) {
          errors.cmsHaganPrecision = 'Hagan precision must be greater than 0.';
        }
      }
    }
    if (swapKind === 'OIS' && !overnightIndexRef.id) errors.overnightIndex = 'Overnight index is required.';
    if (swapKind === 'Basis') {
      if (!basisIndexRef1.id) errors.leg1Index = 'Leg 1 index is required.';
      if (!basisIndexRef2.id) errors.leg2Index = 'Leg 2 index is required.';
    }
    return errors;
  };

  const formatNumber = (n?: number, decimals = 4) => {
    if (n === undefined || n === null) return '—';
    return n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  };

  const activeFlows = useMemo(() => {
    if (!result) return [] as Array<Record<string, unknown>>;
    if (flowTab === 'fixed') return (result.fixed_leg_flows || []) as unknown as Array<Record<string, unknown>>;
    if (flowTab === 'floating') return (result.floating_leg_flows || []) as unknown as Array<Record<string, unknown>>;
    if (flowTab === 'cms') return ((result.cms_leg_flows || result.floating_leg_flows || []) as unknown as Array<Record<string, unknown>>);
    if (flowTab === 'overnight') return (result.overnight_leg_flows || []) as Array<Record<string, unknown>>;
    if (flowTab === 'leg1') return (result.leg1_flows || []) as Array<Record<string, unknown>>;
    return (result.leg2_flows || []) as Array<Record<string, unknown>>;
  }, [result, flowTab]);

  const summaryIndexLabel = useMemo(() => {
    if (swapKind === 'OIS') return overnightIndexRef.id || '—';
    if (swapKind === 'Basis') {
      const labels = [basisIndexRef1.id, basisIndexRef2.id].filter(Boolean);
      return labels.length > 0 ? labels.join(' / ') : '—';
    }
    return indexRef.id || '—';
  }, [basisIndexRef1.id, basisIndexRef2.id, indexRef.id, overnightIndexRef.id, swapKind]);

  const summaryCurrency = useMemo(() => {
    return discountCurve?.currency || forecastCurve?.currency || basisForwardCurve1?.currency || basisForwardCurve2?.currency || 'EUR';
  }, [basisForwardCurve1?.currency, basisForwardCurve2?.currency, discountCurve?.currency, forecastCurve?.currency]);

  const updateLegTypes = (nextLeg1: LegType, nextLeg2: LegType) => {
    setLeg1Type(nextLeg1);
    setLeg2Type(nextLeg2);
    const inferred = inferSwapKind(nextLeg1, nextLeg2);
    if (inferred) {
      setSwapKind(inferred);
      setFlowTab(inferred === 'Basis' ? 'leg1' : 'fixed');
      setValidationErrors(prev => {
        const next = { ...prev };
        delete next.legPair;
        return next;
      });
    } else {
      setValidationErrors(prev => ({
        ...prev,
        legPair: 'Unsupported leg combination. Use Fixed/Floating, Floating/Floating, Fixed/Overnight, or Fixed/CMS.',
      }));
    }
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : (swapName.trim() || ui.singular)}
          subtitle="Price Vanilla, OIS, Basis, and CMS swaps in one editor"
          backLink={<BackLink onClick={() => navigate('/products/ir-swap')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={swapName}
              onNameChange={setSwapName}
              onSave={handleSaveSwap}
              saving={saving}
              placeholder="Swap name…"
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
                  <label className={labelClass}>Settlement Date</label>
                  <input type="date" value={settlementDate} onChange={e => setSettlementDate(e.target.value)} className={inputClass} />
                </div>
                <CurveSetSelector
                  label="Discounting Curve (PV)"
                  curveRole="discount"
                  curveSetId={curveSetId}
                  curveId={discountCurveId}
                  onChangeCurveSet={(csId) => setCurveSetId(csId)}
                  onChangeCurve={(id, curve) => { setDiscountCurveId(id); setDiscountCurve(curve); }}
                />
                {(swapKind === 'Vanilla' || swapKind === 'OIS' || swapKind === 'CMS') && (
                <div>
                  <label className="flex items-center gap-2 text-xs text-[#737373] mb-1.5 font-medium">
                    Forwarding Curve (Index projection)
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
                      onChangeCurve={(id, curve) => { setForecastCurveId(id); setForecastCurve(curve); }}
                    />
                  )}
                  {validationErrors.forwardCurve && <p className="text-xs text-red-600 mt-1">{validationErrors.forwardCurve}</p>}
                </div>
                )}
              </div>
              {swapKind === 'Basis' && (
                <div className="grid sm:grid-cols-2 gap-4 mt-4">
                  <div>
                    <CurveSetSelector
                      label="Forwarding Curve Leg 1"
                      curveRole="forward"
                      curveSetId={curveSetId}
                      curveId={basisForwardCurve1Id}
                      onChangeCurveSet={(csId) => setCurveSetId(csId)}
                      onChangeCurve={(id, curve) => { setBasisForwardCurve1Id(id); setBasisForwardCurve1(curve); }}
                    />
                    {validationErrors.forwardCurveLeg1 && <p className="text-xs text-red-600 mt-1">{validationErrors.forwardCurveLeg1}</p>}
                  </div>
                  <div>
                    <CurveSetSelector
                      label="Forwarding Curve Leg 2"
                      curveRole="forward"
                      curveSetId={curveSetId}
                      curveId={basisForwardCurve2Id}
                      onChangeCurveSet={(csId) => setCurveSetId(csId)}
                      onChangeCurve={(id, curve) => { setBasisForwardCurve2Id(id); setBasisForwardCurve2(curve); }}
                    />
                    {validationErrors.forwardCurveLeg2 && <p className="text-xs text-red-600 mt-1">{validationErrors.forwardCurveLeg2}</p>}
                  </div>
                </div>
              )}
              {validationErrors.discountCurve && <p className="text-xs text-red-600 mt-2">{validationErrors.discountCurve}</p>}
              <div className="flex items-center gap-2 mt-3">
                <input
                  type="checkbox"
                  id="attachQuotesSwap"
                  checked={attachQuotes}
                  onChange={e => setAttachQuotes(e.target.checked)}
                  className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                />
                <label htmlFor="attachQuotesSwap" className="text-xs text-[#737373]">
                  Attach quote book snapshot to pricing request
                </label>
              </div>
              <div className="mt-4 grid sm:grid-cols-3 gap-3 text-xs text-[#525252]">
                <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                  Index: <span className="font-medium">{summaryIndexLabel}</span>
                </div>
                <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                  Swap Kind: <span className="font-medium">{swapKind}</span>
                </div>
                <div className="bg-[#fafafa] border border-[#f0f0f0] rounded-lg px-3 py-2">
                  Currency: <span className="font-medium">{summaryCurrency}</span>
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Swap Basics</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Swap Kind</label>
                  <select value={swapKind} onChange={e => applySwapKind(e.target.value as SwapKind)} className={inputClass}>
                    <option value="Vanilla">Vanilla</option>
                    <option value="OIS">OIS</option>
                    <option value="Basis">Basis</option>
                    <option value="CMS">CMS</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Payer / Receiver</label>
                  <select value={swapType} onChange={e => setSwapType(e.target.value as SwapSide)} className={inputClass}>
                    <option value="Payer">Payer</option>
                    <option value="Receiver">Receiver</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Include Flows</label>
                  <label className="flex items-center gap-2 text-sm text-[#525252] h-[42px]">
                    <input
                      type="checkbox"
                      checked={includeFlows}
                      onChange={e => setIncludeFlows(e.target.checked)}
                      className="rounded border-[#d4d4d4]"
                    />
                    Include cash flows
                  </label>
                </div>
              </div>
              {validationErrors.legPair && <p className="text-xs text-red-600 mt-2">{validationErrors.legPair}</p>}
              {validationErrors.index && <p className="text-xs text-red-600 mt-2">{validationErrors.index}</p>}
              {validationErrors.cmsSwapIndex && <p className="text-xs text-red-600 mt-1">{validationErrors.cmsSwapIndex}</p>}
              {validationErrors.cmsSwapTenor && <p className="text-xs text-red-600 mt-1">{validationErrors.cmsSwapTenor}</p>}
              {validationErrors.cmsSwaptionVol && <p className="text-xs text-red-600 mt-1">{validationErrors.cmsSwaptionVol}</p>}
              {validationErrors.cmsHaganBounds && <p className="text-xs text-red-600 mt-1">{validationErrors.cmsHaganBounds}</p>}
              {validationErrors.cmsHaganPrecision && <p className="text-xs text-red-600 mt-1">{validationErrors.cmsHaganPrecision}</p>}
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Legs</h2>
              <div className="grid lg:grid-cols-2 gap-4">
                <div className="border border-[#e5e5e5] rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-xs font-semibold text-[#525252]">Leg 1</h3>
                    <select value={leg1Type} onChange={e => updateLegTypes(e.target.value as LegType, leg2Type)} className="px-2 py-1 text-xs border border-[#d4d4d4] rounded">
                      <option value="Fixed">Fixed</option>
                      <option value="Floating">Floating</option>
                      <option value="Overnight">Overnight</option>
                      <option value="CMS">CMS</option>
                    </select>
                  </div>
                  {leg1Type === 'Fixed' && (
                    <div className="space-y-3">
                      <div><label className={labelClass}>Notional</label><input type="number" value={fixedLeg.notional} onChange={e => setFixedLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Rate (%)</label><input type="number" value={(fixedLeg.rate * 100).toFixed(3)} onChange={e => setFixedLeg(p => ({ ...p, rate: (parseFloat(e.target.value) || 0) / 100 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Day Counter</label><select value={fixedLeg.dayCounter} onChange={e => setFixedLeg(p => ({ ...p, dayCounter: e.target.value }))} className={inputClass}>{DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}</select></div>
                    </div>
                  )}
                  {leg1Type === 'Floating' && (
                    <div className="space-y-3">
                      <div><label className={labelClass}>Notional</label><input type="number" value={(swapKind === 'Basis' ? basisLeg1.notional : floatingLeg.notional)} onChange={e => swapKind === 'Basis' ? setBasisLeg1(p => ({ ...p, notional: parseFloat(e.target.value) || 0 })) : setFloatingLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Spread (bps)</label><input type="number" value={((swapKind === 'Basis' ? basisLeg1.spread : floatingLeg.spread) * 10000).toFixed(0)} onChange={e => swapKind === 'Basis' ? setBasisLeg1(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 })) : setFloatingLeg(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 }))} className={inputClass} /></div>
                      <IndexPicker label="Index" value={swapKind === 'Basis' ? basisIndexRef1 : indexRef} onChange={(ref, def) => applyIndexPick(ref, def, swapKind === 'Basis' ? setBasisIndexRef1 : setIndexRef, freq => swapKind === 'Basis' ? setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, frequency: freq } })) : setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, frequency: freq } })))} />
                      <div><label className={labelClass}>Payment Frequency</label><select value={swapKind === 'Basis' ? basisLeg1.schedule.frequency : floatingLeg.schedule.frequency} onChange={e => swapKind === 'Basis' ? setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, frequency: e.target.value } })) : setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, frequency: e.target.value } }))} className={inputClass}>{frequencyOptions(swapKind === 'Basis' ? basisLeg1.schedule.frequency : floatingLeg.schedule.frequency).map(f => <option key={f} value={f}>{f}</option>)}</select></div>
                    </div>
                  )}
                  {leg1Type === 'Overnight' && (
                    <div className="space-y-3">
                      {!hasOvernightIndex && <p className="text-xs text-amber-700">No Overnight indices in Pricing.indices[] (add one like USD_SOFR).</p>}
                      <div><label className={labelClass}>Notional</label><input type="number" value={overnightLeg.notional} onChange={e => setOvernightLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Spread (bps)</label><input type="number" value={(overnightLeg.spread * 10000).toFixed(0)} onChange={e => setOvernightLeg(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 }))} className={inputClass} /></div>
                      <IndexPicker label="Overnight Index" value={overnightIndexRef} onChange={(ref, def) => applyIndexPick(ref, def, setOvernightIndexRef)} filter="Overnight" />
                    </div>
                  )}
                  {leg1Type === 'CMS' && (
                    <div className="space-y-3">
                      <div><label className={labelClass}>Notional</label><input type="number" value={cmsLeg.notional} onChange={e => setCmsLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Gear</label><input type="number" value={cmsLeg.gear} onChange={e => setCmsLeg(p => ({ ...p, gear: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Spread (bps)</label><input type="number" value={(cmsLeg.spread * 10000).toFixed(0)} onChange={e => setCmsLeg(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Swap Index Id</label><input type="text" value={cmsLeg.swapIndexId} onChange={e => setCmsLeg(p => ({ ...p, swapIndexId: e.target.value }))} className={inputClass} /></div>
                      <div className="grid grid-cols-2 gap-2">
                        <div><label className={labelClass}>Swap Tenor N</label><input type="number" value={cmsLeg.swapTenorN} onChange={e => setCmsLeg(p => ({ ...p, swapTenorN: parseInt(e.target.value, 10) || 0 }))} className={inputClass} /></div>
                        <div><label className={labelClass}>Swap Tenor Unit</label><select value={cmsLeg.swapTenorUnit} onChange={e => setCmsLeg(p => ({ ...p, swapTenorUnit: e.target.value as 'Months' | 'Years' }))} className={inputClass}><option value="Months">Months</option><option value="Years">Years</option></select></div>
                      </div>
                      <div><label className={labelClass}>Swaption Vol Surface</label><select value={cmsLeg.swaptionVolId} onChange={e => setCmsLeg(p => ({ ...p, swaptionVolId: e.target.value }))} className={inputClass}>{volSurfaces.map(v => <option key={v.id} value={v.id}>{v.id}</option>)}</select></div>
                      <div><label className={labelClass}>Day Counter</label><select value={cmsLeg.dayCounter} onChange={e => setCmsLeg(p => ({ ...p, dayCounter: e.target.value }))} className={inputClass}>{DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}</select></div>
                      <div><label className={labelClass}>Payment Convention</label><select value={cmsLeg.paymentConvention} onChange={e => setCmsLeg(p => ({ ...p, paymentConvention: e.target.value }))} className={inputClass}>{BUSINESS_DAY_CONVENTIONS.map(bc => <option key={bc}>{bc}</option>)}</select></div>
                      <label className="flex items-center gap-2 text-xs text-[#525252]"><input type="checkbox" checked={cmsLeg.useFixingDaysOverride} onChange={e => setCmsLeg(p => ({ ...p, useFixingDaysOverride: e.target.checked }))} />Override fixing days</label>
                      {cmsLeg.useFixingDaysOverride && <div><label className={labelClass}>Fixing Days</label><input type="number" value={cmsLeg.fixingDaysOverride ?? 2} onChange={e => setCmsLeg(p => ({ ...p, fixingDaysOverride: parseInt(e.target.value, 10) || 0 }))} className={inputClass} /></div>}
                      <label className="flex items-center gap-2 text-xs text-[#525252]"><input type="checkbox" checked={cmsLeg.usePricerSpec} onChange={e => setCmsLeg(p => ({ ...p, usePricerSpec: e.target.checked }))} />Use CMS pricer spec override</label>
                      {cmsLeg.usePricerSpec && (
                        <div className="space-y-2 border border-[#e5e5e5] rounded p-2">
                          <div><label className={labelClass}>Pricer Type</label><select value={cmsLeg.pricer.pricerType} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, pricerType: e.target.value as CmsPricerParams['pricerType'] } }))} className={inputClass}>{CMS_PRICER_TYPES.map(t => <option key={t} value={t}>{t}</option>)}</select></div>
                          <div><label className={labelClass}>Yield Curve Model</label><select value={cmsLeg.pricer.yieldCurveModel} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, yieldCurveModel: e.target.value as CmsPricerParams['yieldCurveModel'] } }))} className={inputClass}>{CMS_YIELD_CURVE_MODELS.map(m => <option key={m} value={m}>{m}</option>)}</select></div>
                          <div><label className={labelClass}>Mean Reversion</label><input type="number" value={cmsLeg.pricer.meanReversion} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, meanReversion: parseFloat(e.target.value) || 0 } }))} className={inputClass} /></div>
                          {cmsLeg.pricer.pricerType === 'HaganNumeric' && (
                            <>
                              <div><label className={labelClass}>Hagan Lower Limit</label><input type="number" value={cmsLeg.pricer.haganLowerLimit} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, haganLowerLimit: parseFloat(e.target.value) || 0 } }))} className={inputClass} /></div>
                              <div><label className={labelClass}>Hagan Upper Limit</label><input type="number" value={cmsLeg.pricer.haganUpperLimit} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, haganUpperLimit: parseFloat(e.target.value) || 0 } }))} className={inputClass} /></div>
                              <div><label className={labelClass}>Hagan Precision</label><input type="number" value={cmsLeg.pricer.haganPrecision} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, haganPrecision: parseFloat(e.target.value) || 0 } }))} className={inputClass} /></div>
                              <div><label className={labelClass}>Hagan Hard Upper Limit</label><input type="number" value={cmsLeg.pricer.haganHardUpperLimit} onChange={e => setCmsLeg(p => ({ ...p, pricer: { ...p.pricer, haganHardUpperLimit: parseFloat(e.target.value) || 0 } }))} className={inputClass} /></div>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                <div className="border border-[#e5e5e5] rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-xs font-semibold text-[#525252]">Leg 2</h3>
                    <select value={leg2Type} onChange={e => updateLegTypes(leg1Type, e.target.value as LegType)} className="px-2 py-1 text-xs border border-[#d4d4d4] rounded">
                      <option value="Fixed">Fixed</option>
                      <option value="Floating">Floating</option>
                      <option value="Overnight">Overnight</option>
                      <option value="CMS">CMS</option>
                    </select>
                  </div>
                  {leg2Type === 'Fixed' && (
                    <div className="space-y-3">
                      <div><label className={labelClass}>Notional</label><input type="number" value={fixedLeg.notional} onChange={e => setFixedLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Rate (%)</label><input type="number" value={(fixedLeg.rate * 100).toFixed(3)} onChange={e => setFixedLeg(p => ({ ...p, rate: (parseFloat(e.target.value) || 0) / 100 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Day Counter</label><select value={fixedLeg.dayCounter} onChange={e => setFixedLeg(p => ({ ...p, dayCounter: e.target.value }))} className={inputClass}>{DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}</select></div>
                    </div>
                  )}
                  {leg2Type === 'Floating' && (
                    <div className="space-y-3">
                      <div><label className={labelClass}>Notional</label><input type="number" value={(swapKind === 'Basis' ? basisLeg2.notional : floatingLeg.notional)} onChange={e => swapKind === 'Basis' ? setBasisLeg2(p => ({ ...p, notional: parseFloat(e.target.value) || 0 })) : setFloatingLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Spread (bps)</label><input type="number" value={((swapKind === 'Basis' ? basisLeg2.spread : floatingLeg.spread) * 10000).toFixed(0)} onChange={e => swapKind === 'Basis' ? setBasisLeg2(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 })) : setFloatingLeg(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 }))} className={inputClass} /></div>
                      <IndexPicker label="Index" value={swapKind === 'Basis' ? basisIndexRef2 : indexRef} onChange={(ref, def) => applyIndexPick(ref, def, swapKind === 'Basis' ? setBasisIndexRef2 : setIndexRef, freq => swapKind === 'Basis' ? setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, frequency: freq } })) : setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, frequency: freq } })))} />
                      <div><label className={labelClass}>Payment Frequency</label><select value={swapKind === 'Basis' ? basisLeg2.schedule.frequency : floatingLeg.schedule.frequency} onChange={e => swapKind === 'Basis' ? setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, frequency: e.target.value } })) : setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, frequency: e.target.value } }))} className={inputClass}>{frequencyOptions(swapKind === 'Basis' ? basisLeg2.schedule.frequency : floatingLeg.schedule.frequency).map(f => <option key={f} value={f}>{f}</option>)}</select></div>
                    </div>
                  )}
                  {leg2Type === 'Overnight' && (
                    <div className="space-y-3">
                      {!hasOvernightIndex && <p className="text-xs text-amber-700">No Overnight indices in Pricing.indices[] (add one like USD_SOFR).</p>}
                      <div><label className={labelClass}>Notional</label><input type="number" value={overnightLeg.notional} onChange={e => setOvernightLeg(p => ({ ...p, notional: parseFloat(e.target.value) || 0 }))} className={inputClass} /></div>
                      <div><label className={labelClass}>Spread (bps)</label><input type="number" value={(overnightLeg.spread * 10000).toFixed(0)} onChange={e => setOvernightLeg(p => ({ ...p, spread: (parseFloat(e.target.value) || 0) / 10000 }))} className={inputClass} /></div>
                      <IndexPicker label="Overnight Index" value={overnightIndexRef} onChange={(ref, def) => applyIndexPick(ref, def, setOvernightIndexRef)} filter="Overnight" />
                    </div>
                  )}
                  {leg2Type === 'CMS' && (
                    <div className="space-y-3">
                      <p className="text-xs text-[#737373]">Configure CMS leg in Leg 1 CMS editor.</p>
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Schedule</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Effective Date</label>
                  <input type="date" value={fixedLeg.schedule.effectiveDate} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, effectiveDate: value } }));
                  }} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Termination Date</label>
                  <input type="date" value={fixedLeg.schedule.terminationDate} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDate: value } }));
                  }} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Calendar</label>
                  <select value={fixedLeg.schedule.calendar} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, calendar: value } }));
                  }} className={inputClass}>
                    {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Convention</label>
                  <select value={fixedLeg.schedule.convention} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, convention: value } }));
                  }} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bc => <option key={bc} value={bc}>{bc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Termination Convention</label>
                  <select value={fixedLeg.schedule.terminationDateConvention} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, terminationDateConvention: value } }));
                  }} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bc => <option key={bc} value={bc}>{bc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Date Generation</label>
                  <select value={fixedLeg.schedule.dateGenerationRule} onChange={e => {
                    const value = e.target.value;
                    setFixedLeg(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                    setFloatingLeg(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                    setOvernightLeg(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                    setBasisLeg1(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                    setBasisLeg2(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                    setCmsLeg(p => ({ ...p, schedule: { ...p.schedule, dateGenerationRule: value } }));
                  }} className={inputClass}>
                    {DATE_GEN_RULES.map(r => <option key={r} value={r}>{r}</option>)}
                  </select>
                </div>
              </div>
            </div>

            <div className="flex gap-2">
              <button
                onClick={handlePrice}
                disabled={loading || Object.keys(validateRequestInputs()).length > 0}
                className="flex-1 py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50 transition-colors"
              >
                {loading ? 'Pricing...' : 'Price'}
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
                description={getInstructionText('swap parameters', 'Price')}
                icon={ui.icon}
              />
            )}
            {!loading && result && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold text-[#0a0a0a]">Swap Results</h3>
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
                  {swapKind !== 'Basis' ? (
                    <>
                      <div className="bg-[#f5f5f5] rounded-lg p-3">
                        <p className="text-xs text-[#737373] mb-1">Fair Rate</p>
                        <p className="text-base font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.fair_rate, 6)}</p>
                      </div>
                      <div className="bg-[#f5f5f5] rounded-lg p-3">
                        <p className="text-xs text-[#737373] mb-1">Fair Spread</p>
                        <p className="text-base font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.fair_spread, 6)}</p>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="bg-[#f5f5f5] rounded-lg p-3">
                        <p className="text-xs text-[#737373] mb-1">Fair Spread Leg 1</p>
                        <p className="text-base font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.fair_spread_leg1, 6)}</p>
                      </div>
                      <div className="bg-[#f5f5f5] rounded-lg p-3">
                        <p className="text-xs text-[#737373] mb-1">Fair Spread Leg 2</p>
                        <p className="text-base font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.fair_spread_leg2, 6)}</p>
                      </div>
                    </>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-3 text-sm text-[#525252]">
                  {swapKind === 'Vanilla' && (
                    <>
                      <div>Fixed Leg BPS: <span className="font-mono">{formatNumber(result.fixed_leg_bps, 4)}</span></div>
                      <div>Floating Leg BPS: <span className="font-mono">{formatNumber(result.floating_leg_bps, 4)}</span></div>
                      <div>Fixed Leg NPV: <span className="font-mono">{formatNumber(result.fixed_leg_npv, 2)}</span></div>
                      <div>Floating Leg NPV: <span className="font-mono">{formatNumber(result.floating_leg_npv, 2)}</span></div>
                    </>
                  )}
                  {swapKind === 'OIS' && (
                    <>
                      <div>Fixed Leg BPS: <span className="font-mono">{formatNumber(result.fixed_leg_bps, 4)}</span></div>
                      <div>Overnight Leg BPS: <span className="font-mono">{formatNumber(result.overnight_leg_bps, 4)}</span></div>
                      <div>Fixed Leg NPV: <span className="font-mono">{formatNumber(result.fixed_leg_npv, 2)}</span></div>
                      <div>Overnight Leg NPV: <span className="font-mono">{formatNumber(result.overnight_leg_npv, 2)}</span></div>
                    </>
                  )}
                  {swapKind === 'Basis' && (
                    <>
                      <div>Leg 1 BPS: <span className="font-mono">{formatNumber(result.leg1_bps, 4)}</span></div>
                      <div>Leg 2 BPS: <span className="font-mono">{formatNumber(result.leg2_bps, 4)}</span></div>
                      <div>Leg 1 NPV: <span className="font-mono">{formatNumber(result.leg1_npv, 2)}</span></div>
                      <div>Leg 2 NPV: <span className="font-mono">{formatNumber(result.leg2_npv, 2)}</span></div>
                    </>
                  )}
                  {swapKind === 'CMS' && (
                    <>
                      <div>Fixed Leg BPS: <span className="font-mono">{formatNumber(result.fixed_leg_bps, 4)}</span></div>
                      <div>CMS Leg BPS: <span className="font-mono">{formatNumber(result.cms_leg_bps ?? result.floating_leg_bps, 4)}</span></div>
                      <div>Fixed Leg NPV: <span className="font-mono">{formatNumber(result.fixed_leg_npv, 2)}</span></div>
                      <div>CMS Leg NPV: <span className="font-mono">{formatNumber(result.cms_leg_npv ?? result.floating_leg_npv, 2)}</span></div>
                      <div>Used CMS Pricer: <span className="font-mono">{result.used_cms_pricer_type || '—'}</span></div>
                      <div>Used YCM: <span className="font-mono">{result.used_cms_yield_curve_model || '—'}</span></div>
                      <div>Used Mean Reversion: <span className="font-mono">{formatNumber(result.used_cms_mean_reversion, 6)}</span></div>
                      <div>Hagan Lower: <span className="font-mono">{formatNumber(result.used_cms_hagan_lower_limit, 6)}</span></div>
                      <div>Hagan Upper: <span className="font-mono">{formatNumber(result.used_cms_hagan_upper_limit, 6)}</span></div>
                      <div>Hagan Precision: <span className="font-mono">{formatNumber(result.used_cms_hagan_precision, 8)}</span></div>
                      <div>Hagan Hard Upper: <span className="font-mono">{formatNumber(result.used_cms_hagan_hard_upper_limit, 6)}</span></div>
                    </>
                  )}
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
                      const data = activeFlows;
                      const cols = flowTab === 'cms'
                        ? ['payment_date', 'accrual_start_date', 'accrual_end_date', 'fixing_date', 'index_fixing', 'cms_swap_rate', 'spread', 'rate', 'amount', 'discount', 'present_value']
                        : ['payment_date', 'accrual_start_date', 'accrual_end_date', 'fixing_date', 'index_fixing', 'spread', 'rate', 'amount', 'discount', 'present_value'];
                      const csv = [cols.join(','), ...data.map((r: any) => cols.map(c => JSON.stringify(r?.[c] ?? '')).join(','))].join('\n');
                      const blob = new Blob([csv], { type: 'text/csv' });
                      const url = URL.createObjectURL(blob);
                      const link = document.createElement('a');
                      link.href = url;
                      link.download = `swap-${swapKind.toLowerCase()}-${flowTab}-flows.csv`;
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
                  {flowTabs.map(tab => (
                    <button key={tab} type="button" onClick={() => setFlowTab(tab)} className={`px-3 py-1.5 text-xs rounded-lg ${flowTab === tab ? 'bg-[#0a0a0a] text-white' : 'border border-[#d4d4d4] text-[#525252] hover:bg-[#f5f5f5]'}`}>
                      {tab === 'fixed' ? 'Fixed' : tab === 'floating' ? 'Floating' : tab === 'overnight' ? 'Overnight' : tab === 'cms' ? 'CMS' : tab === 'leg1' ? 'Leg 1' : 'Leg 2'}
                    </button>
                  ))}
                </div>
                {activeFlows.length === 0 ? (
                  <p className="text-xs text-[#737373] py-2">No cashflows returned</p>
                ) : (
                <div className="overflow-auto max-h-72">
                  <table className="w-full text-xs">
                    <thead className="text-[#737373]">
                      <tr>
                        <th className="text-left py-1">payment_date</th>
                        <th className="text-left py-1">accrual_start_date</th>
                        <th className="text-left py-1">accrual_end_date</th>
                        <th className="text-left py-1">fixing_date</th>
                        <th className="text-right py-1">{flowTab === 'cms' ? 'cms_fixing' : 'index_fixing'}</th>
                        {flowTab === 'cms' && <th className="text-right py-1">cms_swap_rate</th>}
                        <th className="text-right py-1">spread</th>
                        <th className="text-right py-1">rate</th>
                        <th className="text-right py-1">amount</th>
                        <th className="text-right py-1">discount</th>
                        <th className="text-right py-1">present_value</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono">
                      {activeFlows.map((flow: any, i: number) => (
                        <tr key={i} className="border-t border-[#f5f5f5]">
                          <td className="py-1 text-[#525252]">{flow.payment_date || '—'}</td>
                          <td>{flow.accrual_start_date || '—'}</td>
                          <td>{flow.accrual_end_date || '—'}</td>
                          <td>{flow.fixing_date || ''}</td>
                          <td className="text-right">
                            {flowTab === 'cms'
                              ? (
                                (flow.cms_swap_rate !== undefined
                                  ? formatNumber(flow.cms_swap_rate as number, 6)
                                  : (flow.index_fixing !== undefined ? formatNumber(flow.index_fixing as number, 6) : '')
                                )
                              )
                              : (flow.index_fixing !== undefined ? formatNumber(flow.index_fixing as number, 6) : '')
                            }
                          </td>
                          {flowTab === 'cms' && <td className="text-right">{flow.has_cms_swap_rate ? formatNumber(flow.cms_swap_rate as number, 6) : ''}</td>}
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
                )}
              </div>
            )}
          </div>
        </div>

        {appGraph?.swapId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/swaps/ir"
              entityId={appGraph.swapId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
