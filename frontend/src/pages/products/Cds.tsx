import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import { AnyCurvePoint, BUSINESS_DAY_CONVENTIONS, CALENDARS, collectIndexRefIds, Curve, DATE_GENERATION_RULES, DAY_COUNTERS, FREQUENCIES, IndexDef, TIME_UNITS, TimeUnit } from '../../lib/types';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { priceCds } from '../../lib/api/cdsPricingService';
import {
  persistCdsGraph,
  buildCdsPriceArm,
  asCdsAppGraph,
  type CdsAppGraph,
  type CurveGraphInput,
} from '../../lib/api/cdsSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import PricingErrorCard from '../../components/products/PricingErrorCard';
import PricingTraceLink from '../../components/products/PricingTraceLink';
import { curveBodyForApi, normalizeCdsQuoteForApi, normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { getQuoteBook } from '../../lib/storage/quoteBook';
import { buildPricingQuoteSnapshotWithBackend } from '../../lib/marketDataBackend';
import { CdsRequest, deriveCdsName, getCdsEntryById, saveCds } from '../../lib/storage/cds';
import { buildSourceRefs } from '../../lib/storage/sourceRefs';
import { getCreditCurveById, getCreditCurves, resolveCreditCurve } from '../../lib/storage/creditCurves';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { useAuth } from '../../hooks/useAuth';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

type ProtectionSide = 'Buyer' | 'Seller';
type CreditCurveMode = 'flat' | 'quotes';
type CreditCurveSourceMode = 'saved' | 'inline';
type CdsQuoteType = 'ParSpread' | 'Upfront';

interface CreditSpreadPoint {
  tenor_number: number;
  tenor_time_unit: TimeUnit;
  spread: number;
}

interface ApiCdsQuote {
  tenor_number: number;
  tenor_time_unit: TimeUnit;
  quote_type: CdsQuoteType;
  quote_id?: string;
  quoted_par_spread?: number;
  quoted_upfront?: number;
  running_coupon?: number;
}

interface CdsResultData {
  npv?: number;
  fair_spread?: number;
  fair_upfront?: number;
  default_leg_npv?: number;
  premium_leg_npv?: number;
  error?: { error_message?: string };
}

const defaultSpreadPoints: CreditSpreadPoint[] = [
  { tenor_number: 1, tenor_time_unit: 'Years', spread: 0.0080 },
  { tenor_number: 3, tenor_time_unit: 'Years', spread: 0.0100 },
  { tenor_number: 5, tenor_time_unit: 'Years', spread: 0.0120 },
];

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

export default function Cds() {
  const { id } = useParams();
  const navigate = useNavigate();
  const ui = entityUi.cds;
  const isNew = !id || id === 'new';
  const today = new Date().toISOString().split('T')[0];
  const fiveYearsLater = new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];
  const twoDaysLater = new Date(Date.now() + 2 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

  const [asOfDate, setAsOfDate] = useState(today);
  const { asOfDate: globalAsOf } = useAsOfDate();
  useEffect(() => { setAsOfDate(globalAsOf); }, [globalAsOf]);

  const [cdsId, setCdsId] = useState(id && id !== 'new' ? id : '');
  const [cdsName, setCdsName] = useState('');
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');
  // id↔uuid bridge. When present this cds is persisted server-side
  // and prices via the by-reference arm; null ⇒ inline.
  const [appGraph, setAppGraph] = useState<CdsAppGraph | null>(null);
  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [attachQuotes, setAttachQuotes] = useState(true);

  const [side, setSide] = useState<ProtectionSide>('Buyer');
  const [notional, setNotional] = useState(10_000_000);
  const [spread, setSpread] = useState(0.0100);
  const [upfront, setUpfront] = useState(0);
  const [dayCounter, setDayCounter] = useState('Actual360');
  const [businessDayConvention, setBusinessDayConvention] = useState('Following');

  const [effectiveDate, setEffectiveDate] = useState(twoDaysLater);
  const [terminationDate, setTerminationDate] = useState(fiveYearsLater);
  const [calendar, setCalendar] = useState('TARGET');
  const [frequency, setFrequency] = useState('Quarterly');
  const [convention, setConvention] = useState('Following');
  const [terminationConvention, setTerminationConvention] = useState('Unadjusted');
  const [dateGenerationRule, setDateGenerationRule] = useState('TwentiethIMM');
  const [endOfMonth, setEndOfMonth] = useState(false);

  const [creditCurveMode, setCreditCurveMode] = useState<CreditCurveMode>('quotes');
  const [creditCurveSourceMode, setCreditCurveSourceMode] = useState<CreditCurveSourceMode>('saved');
  const [selectedCreditCurveId, setSelectedCreditCurveId] = useState('');
  const [recoveryRate, setRecoveryRate] = useState(0.4);
  const [flatHazardRate, setFlatHazardRate] = useState(0.02);
  const [spreadPoints, setSpreadPoints] = useState<CreditSpreadPoint[]>(defaultSpreadPoints);
  const [savedCreditCurveIds, setSavedCreditCurveIds] = useState<string[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorInfo, setErrorInfo] = useState<PricingErrorInfo | undefined>(undefined);
  const [result, setResult] = useState<CdsResultData | null>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const { user, login } = useAuth();

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  useEffect(() => {
    const ids = getCreditCurves().map(c => c.id);
    setSavedCreditCurveIds(ids);
    if (!selectedCreditCurveId && ids.length > 0) setSelectedCreditCurveId(ids[0]);
  }, []);

  const selectedCreditCurve = useMemo(
    () => (selectedCreditCurveId ? getCreditCurveById(selectedCreditCurveId) : null),
    [selectedCreditCurveId]
  );

  useEffect(() => {
    if (!isNew && id) {
      getCdsEntryById(id).then(entry => {
        if (!entry) return;
        const saved = entry.request;
        setCdsId(entry.id);
        setCdsName(entry.name || '');
        // Re-hydrate the app.* bridge so a previously-saved cds prices
        // by-reference and re-saves PATCH the same rows (idempotent).
        setAppGraph(asCdsAppGraph(entry.appGraph));
        const pricing = saved?.pricing || {};
        const cdsItem = saved?.cds_list?.[0] || {};
        const cds = cdsItem?.cds || {};
        const pricingCreditCurves = Array.isArray(pricing.credit_curves) ? pricing.credit_curves : [];
        const pricingCreditCurve = pricingCreditCurves.find((c: any) => c.id === cdsItem?.credit_curve_id) || pricingCreditCurves[0] || {};
        const creditCurve = cdsItem?.credit_curve || pricingCreditCurve || {};
        const schedule = cds?.schedule || {};
        const existingSavedCurveId = cdsItem?.credit_curve_id && getCreditCurveById(cdsItem.credit_curve_id) ? cdsItem.credit_curve_id : '';
        if (existingSavedCurveId) {
          setCreditCurveSourceMode('saved');
          setSelectedCreditCurveId(existingSavedCurveId);
        } else {
          setCreditCurveSourceMode('inline');
        }
        const reqCurve = Array.isArray(pricing.curves) ? pricing.curves[0] : null;

        if (pricing.as_of_date) setAsOfDate(pricing.as_of_date);
        if (reqCurve) {
          setDiscountCurveId(reqCurve.id || 'discount');
          setDiscountCurve({
            id: reqCurve.id || 'discount',
            name: reqCurve.id || 'Discount',
            currency: 'EUR',
            role: 'discount',
            day_counter: reqCurve.day_counter,
            interpolator: reqCurve.interpolator,
            bootstrap_trait: reqCurve.bootstrap_trait,
            reference_date: reqCurve.reference_date || asOfDate,
            points: reqCurve.points || [],
            createdAt: '',
            updatedAt: '',
          });
        }

        if (cds.side) setSide(cds.side);
        if (cds.notional !== undefined) setNotional(cds.notional);
        if (cds.running_coupon !== undefined) setSpread(cds.running_coupon);
        else if (cds.spread !== undefined) setSpread(cds.spread);
        if (cds.upfront !== undefined) setUpfront(cds.upfront);
        if (cds.day_counter) setDayCounter(cds.day_counter);
        if (cds.business_day_convention) setBusinessDayConvention(cds.business_day_convention);

        if (schedule.effective_date) setEffectiveDate(schedule.effective_date);
        if (schedule.termination_date) setTerminationDate(schedule.termination_date);
        if (schedule.calendar) setCalendar(schedule.calendar);
        if (schedule.frequency) setFrequency(schedule.frequency);
        if (schedule.convention) setConvention(schedule.convention);
        if (schedule.termination_date_convention) setTerminationConvention(schedule.termination_date_convention);
        if (schedule.date_generation_rule) setDateGenerationRule(schedule.date_generation_rule);
        if (schedule.end_of_month !== undefined) setEndOfMonth(!!schedule.end_of_month);

        if (creditCurve.recovery_rate !== undefined) setRecoveryRate(creditCurve.recovery_rate);
        if (creditCurve.flat_hazard_rate !== undefined) {
          setCreditCurveMode('flat');
          setFlatHazardRate(creditCurve.flat_hazard_rate);
        } else if (Array.isArray(creditCurve.quotes) && creditCurve.quotes.length > 0) {
          setCreditCurveMode('quotes');
          setSpreadPoints(creditCurve.quotes.map((q: any) => ({
            tenor_number: q.tenor_number,
            tenor_time_unit: q.tenor_time_unit,
            spread: q.quoted_par_spread ?? q.spread ?? 0,
          })));
        }
      });
    }
  }, [id, isNew, asOfDate]);

  const updateSpreadPoint = (index: number, patch: Partial<CreditSpreadPoint>) => {
    setSpreadPoints(prev => prev.map((row, i) => i === index ? { ...row, ...patch } : row));
  };

  const addSpreadPoint = () => {
    setSpreadPoints(prev => [...prev, { tenor_number: 7, tenor_time_unit: 'Years', spread: 0.013 }]);
  };

  const removeSpreadPoint = (index: number) => {
    setSpreadPoints(prev => prev.filter((_, i) => i !== index));
  };

  // Inline POST body for ``POST /v1/price/cds``.
  // Flat trade body + role-tagged top-level discount curve (CurveRef,
  // which forbids extra fields) + top-level ``credit_curve``
  // (CreditCurveRef) the backend validator requires. Curve points carry
  // ``quote_id`` unresolved (the backend substitutes). Credit-curve
  // points carry inline ``quoted_par_spread`` / ``quoted_upfront`` only
  // (credit curves bypass market data). ``upfront_date`` is OMITTED unless an
  // explicit ``upfront`` value is provided (the backend emits upfront_date
  // only when a real upfront exists; emitting it unconditionally pulls the
  // engine's upfront-CDS path, a known trap).
  const toThinACurve = (curve: any, role: 'discount' = 'discount'): any => {
    const normalized = normalizeCurveForApi(curve);
    const out: Record<string, any> = {
      name: curve.id || curve.name || role,
      points: normalized.points,
    };
    if (curve.currency) out.currency = curve.currency;
    if (curve.day_counter) out.day_counter = curve.day_counter;
    if (curve.reference_date) out.reference_date = curve.reference_date;
    out.body = curveBodyForApi(curve, role);
    return out;
  };

  // Build the top-level ``credit_curve`` (CreditCurveRef, inline).
  // Credit curves bypass market data, so points carry literal values and
  // never a ``quote_id``. ``resolveCreditCurve`` (creditCurves.ts) returns numeric
  // ``quoted_par_spread`` for the saved/quote_book path; we strip any
  // ``quote_id`` here belt-and-braces.
  const toThinACreditCurve = (): { ref?: { credit_curve_id: string }; inline?: Record<string, any> } | null => {
    const cleanedSpreadPoints = spreadPoints
      .filter(p => p.tenor_number > 0)
      .map(p => ({ ...p, spread: Number(p.spread) }));
    const inlineResolved = creditCurveMode === 'flat'
      ? { recovery_rate: recoveryRate, flat_hazard_rate: flatHazardRate }
      : {
          recovery_rate: recoveryRate,
          quotes: cleanedSpreadPoints.map(p => ({
            tenor_number: p.tenor_number,
            tenor_time_unit: p.tenor_time_unit,
            quote_type: 'ParSpread' as const,
            quoted_par_spread: p.spread,
          })),
        };
    const savedResolved = selectedCreditCurve ? resolveCreditCurve(selectedCreditCurve, asOfDate) : null;
    const resolved: any = creditCurveSourceMode === 'saved' ? savedResolved : inlineResolved;
    if (!resolved) return null;
    const hasFlat = typeof resolved.flat_hazard_rate === 'number';
    const hasQuotes = Array.isArray(resolved.quotes) && resolved.quotes.length > 0;
    if (!hasFlat && !hasQuotes) return null;

    const name = (creditCurveSourceMode === 'saved' && selectedCreditCurve
      ? (selectedCreditCurve.name || selectedCreditCurve.id)
      : 'inline_credit_curve') || 'credit_curve';

    const body: Record<string, any> = {};
    if (hasFlat) {
      body.flat_hazard_rate = resolved.flat_hazard_rate;
    } else {
      // Drop ``quote_id`` and normalize the tenor to ``{n, unit}``.
      body.points = (resolved.quotes as any[]).map((q: any) => {
        const stripped = { ...q };
        delete stripped.quote_id;
        return normalizeCdsQuoteForApi(stripped);
      });
    }
    return {
      inline: {
        name,
        source: creditCurveSourceMode === 'saved' && selectedCreditCurve?.source
          ? selectedCreditCurve.source
          : (hasFlat ? 'flat' : 'manual'),
        recovery_rate: resolved.recovery_rate,
        body,
      },
    };
  };

  const buildThinARequest = (curves: any[]): Record<string, any> | null => {
    if (!curves.length) return null;
    const cc = toThinACreditCurve();
    if (!cc) return null;

    const trade: Record<string, any> = {
      side,
      notional,
      running_coupon: spread,
      day_counter: dayCounter,
      business_day_convention: businessDayConvention,
      schedule: {
        calendar,
        effective_date: effectiveDate,
        termination_date: terminationDate,
        frequency,
        convention,
        termination_date_convention: terminationConvention,
        date_generation_rule: dateGenerationRule,
        end_of_month: endOfMonth,
      },
    };
    // Emit ``upfront`` (and only then is upfront_date
    // engineered by the backend) only when the user actually entered one.
    // Omitting upfront_date keeps the running-spread CDS off the engine's
    // fairUpfront() path.
    if (upfront !== 0) trade.upfront = upfront;

    return {
      cds: trade,
      curves: [toThinACurve(curves[0], 'discount')],
      credit_curve: cc.inline,
      as_of: asOfDate,
    };
  };

  // Legacy "fat" request shape — preserved for the save flow so persisted
  // CDS entries continue to round-trip through localStorage with the same
  // schema. Not posted on the wire anymore.
  const buildRequest = async (
    curves: any[],
    resolvedIndices: IndexDef[],
    preferBackendQuotes: boolean
  ): Promise<CdsRequest | null> => {
    const cleanedSpreadPoints = spreadPoints
      .filter(p => p.tenor_number > 0)
      .map(p => ({ ...p, spread: Number(p.spread) }));

    const inlineCreditCurve = creditCurveMode === 'flat'
      ? {
          recovery_rate: recoveryRate,
          flat_hazard_rate: flatHazardRate,
        }
      : {
          recovery_rate: recoveryRate,
          quotes: cleanedSpreadPoints,
        };

    const savedCreditCurveResolved = selectedCreditCurve ? resolveCreditCurve(selectedCreditCurve, asOfDate) : null;
    const creditCurve = creditCurveSourceMode === 'saved'
      ? savedCreditCurveResolved
      : inlineCreditCurve;

    if (!creditCurve) return null;
    if (creditCurve.flat_hazard_rate === undefined && (!creditCurve.quotes || creditCurve.quotes.length === 0)) {
      return null;
    }

    const quoteIdsForPricing = getQuoteBook()
      .filter((q) => (q.quote_type || 'Curve') !== 'Volatility')
      .map((q) => q.id);
    const quoteSnapshot = attachQuotes
      ? (
          await buildPricingQuoteSnapshotWithBackend(asOfDate, {
            quoteType: 'Any',
            quoteIds: quoteIdsForPricing,
            preferBackend: preferBackendQuotes,
          })
        ).quotes
      : [];

    const creditQuoteIds = new Set<string>();
    for (const q of (creditCurve.quotes || []) as any[]) {
      if (q.quote_id) creditQuoteIds.add(q.quote_id);
    }

    // Ensure explicit quote types for backend quote registry:
    // - credit ids must be Credit
    // - all other attached ids default to Curve
    const typedQuoteSnapshot = quoteSnapshot.map((q: any) => ({
      id: q.id,
      kind: q.kind || (creditQuoteIds.has(q.id) ? 'Spread' : 'Rate'),
      value: q.value,
      quote_type: creditQuoteIds.has(q.id) ? 'Credit' : (q.quote_type || 'Curve'),
    }));

    const availableQuoteIds = new Set(typedQuoteSnapshot.map((q: any) => q.id));
    const missingReferencedCreditQuotes = (creditCurve.quotes || [])
      .map((q: any) => q.quote_id)
      .filter((id: any): id is string => typeof id === 'string' && id.length > 0 && !availableQuoteIds.has(id));
    if (missingReferencedCreditQuotes.length > 0) {
      setError(
        `Missing credit quote(s) in Quote Book for selected As-Of (${asOfDate}): ${missingReferencedCreditQuotes.join(', ')}. ` +
        `Add them as Quote Type=Credit (or load market data) before pricing CDS.`
      );
      return null;
    }

    const apiQuotes: ApiCdsQuote[] = (creditCurve.quotes || []).map((q: any) => ({
      tenor_number: q.tenor_number,
      tenor_time_unit: q.tenor_time_unit,
      quote_type: q.quote_type || 'ParSpread',
      ...(q.quote_id ? { quote_id: q.quote_id } : {}),
      ...(q.quoted_par_spread !== undefined
        ? { quoted_par_spread: q.quoted_par_spread }
        : {}),
      ...(q.quoted_upfront !== undefined
        ? { quoted_upfront: q.quoted_upfront }
        : {}),
      ...(q.running_coupon !== undefined
        ? { running_coupon: q.running_coupon }
        : {}),
    }));

    const apiCreditCurveId = creditCurveSourceMode === 'saved' && selectedCreditCurveId
      ? selectedCreditCurveId
      : 'inline_credit_curve';

    const apiCreditCurve = {
      id: apiCreditCurveId,
      reference_date: asOfDate,
      calendar: 'TARGET',
      day_counter: 'Actual365Fixed',
      recovery_rate: creditCurve.recovery_rate,
      curve_interpolator: 'LogLinear',
      helper_conventions: {
        settlement_days: 0,
        frequency: 'Quarterly',
        business_day_convention: 'Following',
        date_generation_rule: 'TwentiethIMM',
        last_period_day_counter: 'Actual365Fixed',
        settles_accrual: true,
        pays_at_default_time: true,
        rebates_accrual: true,
        helper_model: 'MidPoint',
      },
      ...(creditCurve.flat_hazard_rate !== undefined
        ? { flat_hazard_rate: creditCurve.flat_hazard_rate }
        : { quotes: apiQuotes.map(normalizeCdsQuoteForApi) }),
    };

    // quote_ids reach the orchestrator UNRESOLVED (server-side market-data
    // resolution pulls the value); inline points pass through.
    const resolvedCurvesForApi = curves.map(normalizeCurveForApi);

    const request: CdsRequest = {
      pricing: {
        as_of_date: asOfDate,
        indices: resolvedIndices.map(normalizeIndexDefForApi),
        curves: resolvedCurvesForApi,
        credit_curves: [apiCreditCurve],
        models: [
          {
            id: 'cds_midpoint',
            payload_type: 'CdsModelSpec',
            payload: {
              engine_type: 'MidPoint',
            },
          },
        ],
        ...(attachQuotes ? {
          // CDS needs both standard curve quotes and credit quotes from the same quote book.
          quotes: typedQuoteSnapshot,
        } : {}),
      },
      cds_list: [{
        cds: {
          side,
          notional,
          running_coupon: spread,
          ...(upfront !== 0 ? { upfront } : {}),
          day_counter: dayCounter,
          business_day_convention: businessDayConvention,
          schedule: {
            calendar,
            effective_date: effectiveDate,
            termination_date: terminationDate,
            frequency,
            convention,
            termination_date_convention: terminationConvention,
            date_generation_rule: dateGenerationRule,
            end_of_month: endOfMonth,
          },
        },
        discounting_curve: 'discount',
        credit_curve_id: apiCreditCurveId,
        model: 'cds_midpoint',
      }],
    };

    return request;
  };

  // Flat CDS trade body persisted as the saved cds's request — the same levers
  // the inline ``cds`` carries (side / notional / running_coupon / schedule).
  // ``persistCdsGraph`` injects ``pricing.{curve_set_id, discount_curve_id,
  // credit_curve_id}`` so the backend's by-reference assembly chains trade →
  // discount curve + credit curve. ``upfront`` is omitted unless explicitly
  // entered (keeps a running-spread CDS off the engine's fairUpfront() path).
  const buildCdsTradeBody = (): Record<string, unknown> => {
    const trade: Record<string, unknown> = {
      side,
      notional,
      running_coupon: spread,
      day_counter: dayCounter,
      business_day_convention: businessDayConvention,
      schedule: {
        calendar,
        effective_date: effectiveDate,
        termination_date: terminationDate,
        frequency,
        convention,
        termination_date_convention: terminationConvention,
        date_generation_rule: dateGenerationRule,
        end_of_month: endOfMonth,
      },
    };
    if (upfront !== 0) trade.upfront = upfront;
    return trade;
  };

  // The single discount curve → CurveCreate body (via the same toThinACurve
  // used on the inline wire), keyed 'discount' so the curve_set ref + the
  // idempotent re-save id↔uuid map line up. CDS consumes exactly one curve.
  const buildCurveGraphInputs = (curves: any[]): CurveGraphInput[] => [
    { key: 'discount', body: toThinACurve(curves[0], 'discount') as components['schemas']['CurveCreate'] },
  ];

  // The credit-curve CreditCurveCreate body — identical to the inline
  // ``credit_curve`` the inline arm builds, so by-ref ↔ inline price the same
  // curve. Carries literal flat_hazard_rate / par-spread points + recovery and
  // never a quote_id (credit curves bypass market data).
  const buildCreditCurveCreate = (): components['schemas']['CreditCurveCreate'] | null => {
    const cc = toThinACreditCurve();
    if (!cc?.inline) return null;
    return cc.inline as components['schemas']['CreditCurveCreate'];
  };

  const handleSave = async () => {
    setSaveStatus(null);
    if (!discountCurve) {
      setSaveStatus({ tone: 'error', message: 'Cannot save: please select a discount curve and a valid credit curve.' });
      return;
    }
    if (creditCurveSourceMode === 'saved' && !selectedCreditCurve) {
      setSaveStatus({ tone: 'error', message: 'Cannot save: please select a saved credit curve.' });
      return;
    }
    if (creditCurveSourceMode === 'inline' && creditCurveMode === 'quotes' && spreadPoints.length === 0) {
      setSaveStatus({ tone: 'error', message: 'Cannot save: add at least one credit spread quote.' });
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
      const curveIndexIds = collectIndexIdsFromCurves(curves);
      const resolvedIndices = await resolveIndexDefs(curveIndexIds);
      const unresolvedIds = curveIndexIds.filter(idx => !resolvedIndices.find(d => d.id === idx));
      if (unresolvedIds.length > 0) {
        setSaveStatus({
          tone: 'error',
          message: `Cannot save: selected discount curve references unknown indices: ${unresolvedIds.join(', ')}. Add them in Market Data → Indices.`,
        });
        return;
      }
      const request = await buildRequest(curves, resolvedIndices, false);
      if (!request) {
        setSaveStatus({ tone: 'error', message: 'Cannot save: please complete required pricing inputs.' });
        return;
      }
      const trimmedName = cdsName.trim();
      const priorGraph = appGraph;
      // Link the discount curve by store id (it carries
      // the quote-id leaves the stripped request lost) + the saved credit curve
      // when one is selected. The credit curve itself bypasses market data and
      // is already faithful in the request, so its ref is optional.
      const sourceRefs = buildSourceRefs({
        curves: [{ role: 'discount', curveId: discountCurveId }],
        creditCurveId: creditCurveSourceMode === 'saved' ? selectedCreditCurveId : undefined,
      });

      // Persist the entity graph server-side leaf→root (curves → curve_set →
      // credit_curve → cds) and stamp the UUIDs back. The localStorage save
      // below still runs so no user data is lost (dual-read, additive).
      const creditCurveBody = buildCreditCurveCreate();
      let nextGraph: CdsAppGraph | null = priorGraph;
      // Honest default: when the server persist below is skipped (no inline
      // credit-curve payload), say so instead of implying a full save.
      let serverNote = priorGraph
        ? ''
        : ' Saved locally only — complete the credit-curve inputs to persist server-side.';
      if (creditCurveBody) {
        const graphResult = await persistCdsGraph(
          {
            name: trimmedName || deriveCdsName(request),
            curves: buildCurveGraphInputs(curves),
            curveSetCurrency: discountCurve?.currency,
            creditCurve: creditCurveBody,
            cdsRequest: buildCdsTradeBody(),
            changeReason: changeReason.trim() || undefined,
          },
          priorGraph,
        );
        if (!graphResult.ok) {
          // Branch on the structured envelope code. The localStorage
          // save below still runs so no user data is lost (dual-read).
          const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
          await saveCds(request, {
            id: isNew ? undefined : (cdsId || undefined),
            name: trimmedName || undefined,
            appId: priorGraph?.cdsId,
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

      const savedId = await saveCds(request, {
        id: isNew ? undefined : (cdsId || undefined),
        name: trimmedName || undefined,
        appId: nextGraph?.cdsId,
        appGraph: (nextGraph ?? undefined) as Record<string, unknown> | undefined,
        sourceRefs,
      });
      setCdsId(savedId);
      setAppGraph(nextGraph);
      const displayName = trimmedName || deriveCdsName(request);
      setSaveStatus({ tone: 'success', message: `Saved "${displayName}".${serverNote}` });
      setChangeReason('');
      if (isNew || id !== savedId) {
        navigate(`/products/cds/${savedId}`, { replace: true });
      }
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save CDS request' });
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
    if (!discountCurve) {
      setError('Please select a discount curve and valid credit curve');
      return;
    }

    // Price-arm selection. A saved cds (appGraph present)
    // prices by-reference: a minimal {cds_id, as_of} body, and the
    // orchestrator loads the trade + discount/credit curves server-side — so
    // we skip the local curve/credit build entirely. An unsaved cds prices
    // inline, unchanged.
    let request: unknown;
    if (appGraph?.cdsId) {
      request = buildCdsPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate });
    } else {
      const curves = [{
        id: 'discount',
        day_counter: discountCurve.day_counter,
        interpolator: discountCurve.interpolator,
        bootstrap_trait: discountCurve.bootstrap_trait,
        reference_date: discountCurve.reference_date || asOfDate,
        points: discountCurve.points,
      }];
      const curveIndexIds = collectIndexIdsFromCurves(curves);
      const resolvedIndices = await resolveIndexDefs(curveIndexIds);
      const unresolvedIds = curveIndexIds.filter(idx => !resolvedIndices.find(d => d.id === idx));
      if (unresolvedIds.length > 0) {
        setError(`Selected discount curve references unknown indices: ${unresolvedIds.join(', ')}. Add them in Market Data -> Indices.`);
        return;
      }
      if (creditCurveSourceMode === 'saved' && !selectedCreditCurve) {
        setError('Please select a saved credit curve');
        return;
      }
      // Inline arm: POST the flat-trade-body + top-level curves +
      // top-level credit_curve envelope priced directly by the orchestrator.
      // ``resolvedIndices`` is still needed for the save-flow legacy shape
      // (handleSave -> buildRequest) but the wire body skips market-data index
      // refs entirely; the orchestrator resolves curve helpers itself.
      void resolvedIndices;
      const thinARequest = buildThinARequest(curves);
      if (!thinARequest) {
        setError('Please select a discount curve and valid credit curve');
        return;
      }
      request = buildCdsPriceArm({ appGraph: null, inlineRequest: thinARequest, asOf: asOfDate });
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    try {
      const response = await priceCds(request, asOfDate);
      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);
      const first = response.data?.cds_list?.[0];
      if (response.success && first) {
        setResult(first as CdsResultData);
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

  const formatNumber = (n?: number, decimals = 4) => {
    if (n === undefined || n === null) return '—';
    return n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : (cdsName.trim() || ui.singular)}
          subtitle="Price a credit default swap using a discount curve plus credit curve assumptions"
          backLink={<BackLink onClick={() => navigate('/products/cds')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={cdsName}
              onNameChange={setCdsName}
              onSave={handleSave}
              saving={saving}
              placeholder="CDS name…"
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
                <CurveSetSelector
                  label="Discount Curve"
                  curveRole="discount"
                  curveSetId={curveSetId}
                  curveId={discountCurveId}
                  onChangeCurveSet={(csId) => setCurveSetId(csId)}
                  onChangeCurve={(id, curve) => { setDiscountCurveId(id); setDiscountCurve(curve); }}
                />
              </div>
              <div className="flex items-center gap-2 mt-3">
                <input
                  type="checkbox"
                  id="attachQuotesCds"
                  checked={attachQuotes}
                  onChange={e => setAttachQuotes(e.target.checked)}
                  className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                />
                <label htmlFor="attachQuotesCds" className="text-xs text-[#737373]">
                  Attach quote book snapshot for curve quote_ids
                </label>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">CDS Terms</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Protection Side</label>
                  <select value={side} onChange={e => setSide(e.target.value as ProtectionSide)} className={inputClass}>
                    <option value="Buyer">Buyer</option>
                    <option value="Seller">Seller</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Notional</label>
                  <input type="number" value={notional} onChange={e => setNotional(parseFloat(e.target.value) || 0)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Running Spread (bps)</label>
                  <input type="number" step="0.1" value={spread * 10000} onChange={e => setSpread((parseFloat(e.target.value) || 0) / 10000)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Upfront</label>
                  <input type="number" step="0.0001" value={upfront} onChange={e => setUpfront(parseFloat(e.target.value) || 0)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Day Counter</label>
                  <select value={dayCounter} onChange={e => setDayCounter(e.target.value)} className={inputClass}>
                    {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Business Day Convention</label>
                  <select value={businessDayConvention} onChange={e => setBusinessDayConvention(e.target.value)} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bdc => <option key={bdc} value={bdc}>{bdc}</option>)}
                  </select>
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Schedule</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Effective Date</label>
                  <input type="date" value={effectiveDate} onChange={e => setEffectiveDate(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Termination Date</label>
                  <input type="date" value={terminationDate} onChange={e => setTerminationDate(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Calendar</label>
                  <select value={calendar} onChange={e => setCalendar(e.target.value)} className={inputClass}>
                    {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Frequency</label>
                  <select value={frequency} onChange={e => setFrequency(e.target.value)} className={inputClass}>
                    {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Convention</label>
                  <select value={convention} onChange={e => setConvention(e.target.value)} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bdc => <option key={bdc} value={bdc}>{bdc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Termination Convention</label>
                  <select value={terminationConvention} onChange={e => setTerminationConvention(e.target.value)} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(bdc => <option key={bdc} value={bdc}>{bdc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Date Generation Rule</label>
                  <select value={dateGenerationRule} onChange={e => setDateGenerationRule(e.target.value)} className={inputClass}>
                    {DATE_GENERATION_RULES.map(rule => <option key={rule} value={rule}>{rule}</option>)}
                  </select>
                </div>
                <div className="flex items-end">
                  <label className="flex items-center gap-2 text-xs text-[#737373]">
                    <input type="checkbox" checked={endOfMonth} onChange={e => setEndOfMonth(e.target.checked)} className="rounded border-[#d4d4d4]" />
                    End of month
                  </label>
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Credit Curve</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Curve Source</label>
                  <select value={creditCurveSourceMode} onChange={e => setCreditCurveSourceMode(e.target.value as CreditCurveSourceMode)} className={inputClass}>
                    <option value="saved">Use saved credit curve</option>
                    <option value="inline">Inline override in this trade</option>
                  </select>
                </div>
                {creditCurveSourceMode === 'saved' && (
                  <div>
                    <label className={labelClass}>Saved Credit Curve</label>
                    <div className="flex gap-2">
                      <select value={selectedCreditCurveId} onChange={e => setSelectedCreditCurveId(e.target.value)} className={inputClass}>
                        <option value="">Select curve...</option>
                        {savedCreditCurveIds.map(idItem => (
                          <option key={idItem} value={idItem}>{idItem}</option>
                        ))}
                      </select>
                      <Link to="/credit-curves" className="px-3 py-2 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] whitespace-nowrap">
                        Manage
                      </Link>
                    </div>
                  </div>
                )}
              </div>

              {creditCurveSourceMode === 'saved' && selectedCreditCurve && (
                <div className="mt-3 p-3 rounded-lg border border-[#e5e5e5] bg-[#fafafa] text-xs text-[#737373]">
                  <div>
                    Source: <span className="font-mono text-[#525252]">{selectedCreditCurve.source}</span>
                  </div>
                  <div>
                    Recovery rate: <span className="font-mono text-[#525252]">{(selectedCreditCurve.recovery_rate * 100).toFixed(2)}%</span>
                  </div>
                  <div>
                    Points: <span className="font-mono text-[#525252]">{selectedCreditCurve.points?.length || 0}</span>
                  </div>
                </div>
              )}

              {creditCurveSourceMode === 'inline' && (
                <>
                  <div className="grid sm:grid-cols-3 gap-4 mt-4">
                    <div>
                      <label className={labelClass}>Recovery Rate</label>
                      <input type="number" step="0.01" value={recoveryRate} onChange={e => setRecoveryRate(parseFloat(e.target.value) || 0)} className={inputClass} />
                    </div>
                    <div>
                      <label className={labelClass}>Curve Mode</label>
                      <select value={creditCurveMode} onChange={e => setCreditCurveMode(e.target.value as CreditCurveMode)} className={inputClass}>
                        <option value="quotes">Bootstrapped from spread quotes</option>
                        <option value="flat">Flat hazard rate</option>
                      </select>
                    </div>
                    {creditCurveMode === 'flat' && (
                      <div>
                        <label className={labelClass}>Flat Hazard Rate</label>
                        <input type="number" step="0.0001" value={flatHazardRate} onChange={e => setFlatHazardRate(parseFloat(e.target.value) || 0)} className={inputClass} />
                      </div>
                    )}
                  </div>

                  {creditCurveMode === 'quotes' && (
                    <div className="mt-4">
                      <div className="flex items-center justify-between mb-2">
                        <p className="text-xs text-[#737373]">Credit spread quotes (tenor + spread)</p>
                        <button onClick={addSpreadPoint} className="px-2 py-1 text-xs bg-[#f5f5f5] rounded border border-[#d4d4d4] hover:bg-[#e5e5e5]">Add point</button>
                      </div>
                      <div className="space-y-2">
                        {spreadPoints.map((row, i) => (
                          <div key={i} className="grid grid-cols-12 gap-2 items-end">
                            <div className="col-span-3">
                              <label className={labelClass}>Tenor #</label>
                              <input type="number" value={row.tenor_number} onChange={e => updateSpreadPoint(i, { tenor_number: parseInt(e.target.value) || 0 })} className={inputClass} />
                            </div>
                            <div className="col-span-3">
                              <label className={labelClass}>Unit</label>
                              <select value={row.tenor_time_unit} onChange={e => updateSpreadPoint(i, { tenor_time_unit: e.target.value as TimeUnit })} className={inputClass}>
                                {TIME_UNITS.map(u => <option key={u} value={u}>{u}</option>)}
                              </select>
                            </div>
                            <div className="col-span-4">
                              <label className={labelClass}>Spread (bps)</label>
                              <input type="number" step="0.1" value={row.spread * 10000} onChange={e => updateSpreadPoint(i, { spread: (parseFloat(e.target.value) || 0) / 10000 })} className={inputClass} />
                            </div>
                            <div className="col-span-2">
                              <button onClick={() => removeSpreadPoint(i)} className="w-full px-2 py-2 text-xs border border-[#d4d4d4] rounded hover:bg-red-50 hover:text-red-600">
                                Remove
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>

            <button
              onClick={handlePrice}
              disabled={loading || !discountCurve}
              className="w-full py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50 transition-colors"
            >
              {loading ? 'Pricing...' : 'Price CDS'}
            </button>
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
                description={getInstructionText('CDS parameters', 'Price')}
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
                    <p className="text-xs text-[#737373] mb-1">Fair Spread</p>
                    <p className="text-lg font-semibold text-[#0a0a0a] font-mono">{formatNumber((result.fair_spread ?? 0) * 10000, 2)} bps</p>
                  </div>
                </div>
                <div className="grid grid-cols-1 gap-2 text-sm text-[#525252]">
                  <div>Fair Upfront: <span className="font-mono">{formatNumber(result.fair_upfront, 6)}</span></div>
                  <div>Default Leg NPV: <span className="font-mono">{formatNumber(result.default_leg_npv, 2)}</span></div>
                  <div>Premium Leg NPV: <span className="font-mono">{formatNumber(result.premium_leg_npv, 2)}</span></div>
                </div>
              </div>
            )}
          </div>
        </div>

        {appGraph?.cdsId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/cds"
              entityId={appGraph.cdsId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
