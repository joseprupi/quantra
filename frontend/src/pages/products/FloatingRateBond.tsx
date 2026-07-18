// Floating Rate Bond Pricing Page
import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import PricingResults from '../../components/products/PricingResults';
import IndexPicker from '../../components/curves/IndexPicker';
import { Curve, IndexRef, IndexDef, DAY_COUNTERS, CALENDARS, collectIndexRefIds } from '../../lib/types';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { priceFloatingBond } from '../../lib/api/bondPricingService';
import {
  persistBondFloatingGraph,
  buildBondFloatingPriceArm,
  asBondFloatingAppGraph,
  type BondFloatingAppGraph,
} from '../../lib/api/bondSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import { normalizeCurveForApi } from '../../lib/api-normalizers';
import { floatingBondStore, SavedFloatingRateBond, generateId } from '../../lib/storage/bonds';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { getQuoteBook } from '../../lib/storage/quoteBook';
import { useAuth } from '../../hooks/useAuth';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

interface FloatingBondParams {
  settlementDays: number;
  faceAmount: number;
  spread: number;
  accrualDayCounter: string;
  paymentConvention: string;
  fixingDays: number;
  inArrears: boolean;
  redemption: number;
  issueDate: string;
  effectiveDate: string;
  terminationDate: string;
  frequency: string;
  calendar: string;
  convention: string;
  dateGenerationRule: string;
}

const FREQUENCIES = ['Annual', 'Semiannual', 'Quarterly', 'Monthly'];
const DATE_GEN_RULES = ['Forward', 'Backward'];

/** Resolve IndexDef objects for a list of index IDs (saved → skip) */
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

export default function FloatingRateBondPricer() {
  const ui = entityUi.floatingRateBond;
  const { id } = useParams();
  const navigate = useNavigate();
  const isNew = !id || id === 'new';
  
  // Dates
  const today = new Date().toISOString().split('T')[0];
  const fiveYearsLater = new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];
  
  const [asOfDate, setAsOfDate] = useState(today);
  const [attachQuotes, setAttachQuotes] = useState(true);
  const { asOfDate: globalAsOf } = useAsOfDate();
  useEffect(() => { setAsOfDate(globalAsOf); }, [globalAsOf]);
  const [settlementDate, setSettlementDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 2);
    return d.toISOString().split('T')[0];
  });
  
  // Bond name for save
  const [bondName, setBondName] = useState('');
  const [bondId, setBondId] = useState(id && id !== 'new' ? id : '');
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');
  // id↔uuid bridge — present ⇒ by-reference pricing.
  const [appGraph, setAppGraph] = useState<BondFloatingAppGraph | null>(null);

  // Curve selection
  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [forecastCurveId, setForecastCurveId] = useState('');
  const [forecastCurve, setForecastCurve] = useState<Curve | null>(null);
  const [useSameCurve, setUseSameCurve] = useState(true);
  
  // Bond parameters
  const [bond, setBond] = useState<FloatingBondParams>({
    settlementDays: 2,
    faceAmount: 100,
    spread: 0.001, // 10 bps
    accrualDayCounter: 'Actual360',
    paymentConvention: 'ModifiedFollowing',
    fixingDays: 2,
    inArrears: false,
    redemption: 100,
    issueDate: today,
    effectiveDate: today,
    terminationDate: fiveYearsLater,
    frequency: 'Semiannual',
    calendar: 'TARGET',
    convention: 'ModifiedFollowing',
    dateGenerationRule: 'Forward',
  });
  
  // Index reference (points to an IndexDef id)
  const [indexRef, setIndexRef] = useState<IndexRef>({ id: 'EURIBOR_6M' });
  
  // Pricing options
  const [includeFlows, setIncludeFlows] = useState(true);
  const [includeDetails, setIncludeDetails] = useState(true);
  
  // Results
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorInfo, setErrorInfo] = useState<PricingErrorInfo | undefined>(undefined);
  const [result, setResult] = useState<any>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const [lastRequest, setLastRequest] = useState<object | null>(null);
  const { user, login } = useAuth();
  
  // Load existing bond
  useEffect(() => {
    if (!isNew && id) {
      floatingBondStore.getById(id).then(saved => {
        if (saved) {
          setBondName(saved.name);
          setBondId(saved.id);
          // Re-hydrate the app.* bridge so a previously-saved bond prices
          // by-reference and re-saves PATCH the same rows (idempotent).
          setAppGraph(asBondFloatingAppGraph(saved.appGraph));
          setBond({
            settlementDays: saved.settlementDays,
            faceAmount: saved.faceAmount,
            spread: saved.spread,
            accrualDayCounter: saved.accrualDayCounter,
            paymentConvention: saved.paymentConvention,
            fixingDays: saved.fixingDays,
            inArrears: saved.inArrears,
            redemption: saved.redemption,
            issueDate: saved.issueDate,
            effectiveDate: saved.effectiveDate,
            terminationDate: saved.terminationDate,
            frequency: saved.frequency,
            calendar: saved.calendar,
            convention: saved.convention,
            dateGenerationRule: saved.dateGenerationRule,
          });
          // Load index ref — use saved indexRefId if available, else fallback to EURIBOR_6M
          if ((saved as any).indexRefId) {
            setIndexRef({ id: (saved as any).indexRefId });
          }
          setDiscountCurveId(saved.discountCurveId);
          setForecastCurveId(saved.forecastCurveId);
          setUseSameCurve(saved.useSameCurve);
        }
      });
    }
  }, [id, isNew]);
  
  const handleSaveBond = async () => {
    setSaveStatus(null);
    setSaving(true);
    try {
      const saveId = bondId || generateId();
      const finalName = bondName.trim() || `Floating Bond +${bond.spread * 10000}bp ${bond.terminationDate}`;
      const makeSaved = (graph: BondFloatingAppGraph | null): SavedFloatingRateBond => ({
        id: saveId,
        name: finalName,
        settlementDays: bond.settlementDays,
        faceAmount: bond.faceAmount,
        spread: bond.spread,
        accrualDayCounter: bond.accrualDayCounter,
        paymentConvention: bond.paymentConvention,
        fixingDays: bond.fixingDays,
        inArrears: bond.inArrears,
        redemption: bond.redemption,
        issueDate: bond.issueDate,
        effectiveDate: bond.effectiveDate,
        terminationDate: bond.terminationDate,
        frequency: bond.frequency,
        calendar: bond.calendar,
        convention: bond.convention,
        dateGenerationRule: bond.dateGenerationRule,
        indexPeriodNumber: 0,
        indexPeriodTimeUnit: '',
        indexSettlementDays: 0,
        indexCalendar: '',
        indexBusinessDayConvention: '',
        indexEndOfMonth: false,
        indexDayCounter: '',
        indexRefId: indexRef.id,
        discountCurveId: discountCurveId,
        forecastCurveId: forecastCurveId,
        useSameCurve,
        appId: graph?.bondId,
        appGraph: (graph ?? undefined) as Record<string, unknown> | undefined,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      });

      const priorGraph = appGraph;
      // Persist the entity graph leaf→root (curves → curve_set → index →
      // bonds_floating) and stamp the UUIDs. Runs when the discount curve + a
      // resolvable projection index are available; the localStorage save still
      // runs either way (dual-read, additive).
      let nextGraph: BondFloatingAppGraph | null = priorGraph;
      const resolvedForSave = await resolveIndexDefs([indexRef.id]);
      const indexDefForSave = resolvedForSave.find(d => d.id === indexRef.id);
      // Honest default: when the server persist below is skipped (no resolved
      // discount curve / projection index), say so instead of implying a full save.
      let serverNote = priorGraph
        ? ''
        : ' Saved locally only — select a discount curve and a resolvable index to persist server-side.';
      if (discountCurve && indexDefForSave) {
        const graphResult = await persistBondFloatingGraph(
          {
            name: finalName,
            curves: buildFloatingCurveGraphInputs(),
            curveSetCurrency: discountCurve.currency,
            index: buildIndexCreate(indexDefForSave),
            bondRequest: buildFloatingTradeBody(),
            changeReason: changeReason.trim() || undefined,
          },
          priorGraph,
        );
        if (!graphResult.ok) {
          // Branch on the structured envelope code. localStorage still saved.
          const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
          await floatingBondStore.save(makeSaved(priorGraph));
          setBondId(saveId);
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

      await floatingBondStore.save(makeSaved(nextGraph));
      setBondId(saveId);
      setAppGraph(nextGraph);
      setSaveStatus({ tone: 'success', message: `Saved "${finalName}".${serverNote}` });
      setChangeReason('');
      if (isNew) navigate(`/products/floating-rate-bond/${saveId}`, { replace: true });
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save bond' });
    } finally {
      setSaving(false);
    }
  };
  
  const updateBond = (field: keyof FloatingBondParams, value: any) => {
    setBond(prev => ({ ...prev, [field]: value }));
  };
  
  // Map a portal-shaped curve onto the orchestrator's inline CurveRef
  // (which forbids extra fields). Role-tagged via ``body.role``
  // so the backend maps the entry onto discount / projection.
  // Points pass through normalizeCurvePointForApi so legacy
  // ``tenor_number``/``tenor_time_unit`` becomes ``tenor: {n, unit}``;
  // ``quote_id``s are forwarded unresolved.
  const toThinACurve = (curve: Curve, role: 'discount' | 'projection'): any => {
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

  // Build the top-level inline IndexRef (extra="forbid", inline mode
  // requires both ``kind`` and ``body``). The body carries the index
  // conventions (tenor / fixing-days / calendar / day-counter / BDC),
  // matching the engine's per-product ``_index_*`` readers. Inline
  // ``name`` becomes the engine-facing index id (inline indices
  // have no UUID, so the resolved index name is the registry key).
  const toThinAIndex = (def: IndexDef): Record<string, any> => {
    const kind = def.index_type === 'Ibor' ? 'IborIndex' : 'OvernightIndex';
    const body: Record<string, any> = {
      fixingDays: def.fixing_days,
    };
    if (def.tenor) body.tenor = def.tenor;
    if (def.business_day_convention) body.businessDayConvention = def.business_day_convention;
    if (typeof def.end_of_month === 'boolean') body.endOfMonth = def.end_of_month;
    if (Array.isArray(def.fixings)) body.fixings = def.fixings;
    return {
      name: def.id,
      kind,
      currency: def.currency,
      calendar: def.calendar,
      day_counter: def.day_counter,
      body,
    };
  };

  // Inline POST body for ``POST /v1/price/bonds/floating``.
  // Flat trade body the backend reads + two
  // role-tagged top-level curves (discount + projection) + a top-level
  // ``index`` (the FloatingBondPriceRequest validator requires the
  // index override in inline mode). The orchestrator builds
  // ``rates.indices`` as the union of the per-trade index plus the
  // helpers' default forwarding index — no extra registry plumbing
  // needed from the portal.
  const buildThinARequest = (indexDef: IndexDef): Record<string, any> => {
    // Always send TWO role-tagged curves — the bonds assembler requires a
    // distinct ``role=projection`` ref for inline floating bonds. When the
    // user picked "single-curve mode", we send the same curve content
    // under both roles (the assembler short-circuits the projection
    // materialisation when both refs point at the same row, so this
    // costs nothing extra on the wire).
    const projectionSource = useSameCurve ? discountCurve! : (forecastCurve || discountCurve!);
    const curves: Record<string, any>[] = [
      toThinACurve(discountCurve!, 'discount'),
      toThinACurve(projectionSource, 'projection'),
    ];
    return {
      bond: buildFloatingTradeBody(),
      curves,
      index: toThinAIndex(indexDef),
      as_of: asOfDate,
    };
  };

  // Flat trade body persisted as the saved bond's request — the same
  // levers the inline ``bond`` carries. ``persistBondFloatingGraph``
  // injects ``pricing.{curve_set_id, discount_curve_id, forecast_curve_id,
  // index_id}`` (the verified read-path: discount + projection roles
  // + the projection index).
  const buildFloatingTradeBody = (): Record<string, unknown> => ({
    face_amount: bond.faceAmount,
    spread: bond.spread,
    fixing_days: bond.fixingDays,
    in_arrears: bond.inArrears,
    settlement_days: bond.settlementDays,
    redemption: bond.redemption,
    issue_date: bond.issueDate,
    effective_date: bond.effectiveDate,
    termination_date: bond.terminationDate,
  });

  // Build the persisted IndexCreate body. The stored index ``kind``
  // is constrained to {IBOR, Overnight, Inflation}, whereas the inline IndexRef
  // uses the engine discriminator ({IborIndex, OvernightIndex}). Map to the
  // column form for the persisted row; the backend normalises both back to
  // the same engine IndexType (``IborIndex``/``Ibor`` and
  // an unrecognised kind both resolve to ``Ibor``), so by-ref ↔ inline price
  // the same index. Everything else mirrors the inline ``toThinAIndex`` body.
  const buildIndexCreate = (def: IndexDef): components['schemas']['IndexCreate'] => {
    const ref = toThinAIndex(def);
    const columnKind: Record<string, string> = { IborIndex: 'IBOR', OvernightIndex: 'Overnight' };
    return { ...ref, kind: columnKind[ref.kind] ?? ref.kind } as components['schemas']['IndexCreate'];
  };

  // The two role-tagged curves to persist (discount + projection). When
  // single-curve mode is on, the projection persists the discount content
  // under role=projection (mirrors the inline arm; the assembler reuses the
  // row when both refs match). Caller guards ``discountCurve`` non-null.
  const buildFloatingCurveGraphInputs = () => {
    const projectionSource = useSameCurve ? discountCurve! : (forecastCurve || discountCurve!);
    return [
      { key: 'discount', body: toThinACurve(discountCurve!, 'discount') as components['schemas']['CurveCreate'] },
      { key: 'projection', body: toThinACurve(projectionSource, 'projection') as components['schemas']['CurveCreate'] },
    ];
  };

  const handleDownloadRequest = () => {
    if (!lastRequest) return;
    const blob = new Blob([JSON.stringify(lastRequest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `floating-rate-bond-request-${asOfDate}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };
  
  const handlePrice = async () => {
    if (!user) {
      const signedInUser = await login();
      if (!signedInUser) {
        setError('Sign in is required to run pricing calls.');
        return;
      }
    }
    // A saved bond (appGraph present) prices by-reference and needs no
    // local curves/index; an unsaved bond requires both curves inline.
    if (!appGraph?.bondId) {
      if (!discountCurve) {
        setError('Please select a discount curve');
        return;
      }
      const actualForecastCurve = useSameCurve ? discountCurve : forecastCurve;
      if (!actualForecastCurve) {
        setError('Please select a forecast curve');
        return;
      }
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    // By-reference arm: {bond_id, as_of}; the
    // orchestrator loads the trade + discount/projection curves + index
    // server-side. Skips the entire local curve/index/fixings build below.
    if (appGraph?.bondId) {
      try {
        const request = buildBondFloatingPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate });
        const response = await priceFloatingBond(request, asOfDate);
        setDurationMs(response.duration_ms);
        setRequestId(response.requestId);
        if (response.success && response.data?.bonds?.[0]) {
          setResult(response.data.bonds[0]);
          setLastRequest(request as object);
        } else {
          setError(response.error || 'Pricing failed');
          setErrorInfo(response.errorInfo);
          setLastRequest(null);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Pricing failed');
        setErrorInfo(undefined);
        setLastRequest(null);
      } finally {
        setLoading(false);
      }
      return;
    }

    // Inline arm: the guards above already returned if the discount
    // curve was missing — re-assert for the type-narrower past the early return.
    if (!discountCurve) {
      setError('Please select a discount curve');
      setLoading(false);
      return;
    }

    try {
      // Honour the seasoned-bond fixing requirement client-side so the user
      // sees a clear message before the request goes out.
      const quoteBookEntries = getQuoteBook();
      const quoteFixingsById = new Map(
        quoteBookEntries.map((entry) => [
          entry.id,
          (entry.series || []).filter((s) => s.date <= asOfDate).map((s) => ({ date: s.date, value: s.value })),
        ])
      );
      const indexFixingsById = new Map(
        (await indexStore.getAll())
          .filter(i => Array.isArray(i.fixings) && i.fixings.length > 0)
          .map(i => [
            i.id,
            i.fixings!.filter(f => f.date <= asOfDate).map(f => ({ date: f.date, value: f.value })),
          ])
      );
      const fixingsById = new Map<string, { date: string; value: number }[]>();
      for (const [id, series] of quoteFixingsById) fixingsById.set(String(id), series);
      for (const [id, series] of indexFixingsById) {
        if (series.length > 0) fixingsById.set(id, series);
      }

      const indexFixings = fixingsById.get(indexRef.id) || [];
      if (asOfDate > bond.effectiveDate && indexFixings.length === 0) {
        setError(`Missing ${indexRef.id} fixings for a seasoned bond. Add fixings in Market Data → Indices or Quote Book (id: ${indexRef.id}) or price with an as-of date on/before the effective date.`);
        setLoading(false);
        return;
      }

      // Resolve IndexDef for the referenced index so we (a) verify it
      // exists locally and (b) lift its conventions into the inline
      // IndexRef the orchestrator validator requires (rates.indices
      // is rebuilt server-side as the union of the per-trade index plus
      // the helpers' default; the portal doesn't need to send the helper
      // refs anymore).
      const actualForecastCurveForRoles = useSameCurve ? null : forecastCurve;
      const allIndexIds = Array.from(new Set([
        indexRef.id,
        ...collectIndexRefIds(discountCurve.points || []),
        ...collectIndexRefIds(actualForecastCurveForRoles?.points || []),
      ]));
      const resolvedIndices = await resolveIndexDefs(allIndexIds);
      const unresolvedIds = allIndexIds.filter(id => !resolvedIndices.find(d => d.id === id));
      if (unresolvedIds.length > 0) {
        setError(`Selected curve references unknown indices: ${unresolvedIds.join(', ')}. Add them in Market Data → Indices.`);
        setLoading(false);
        return;
      }
      const projectionIndexDef = resolvedIndices.find(d => d.id === indexRef.id);
      if (!projectionIndexDef) {
        setError(`Selected index ${indexRef.id} is not available. Add it in Market Data → Indices.`);
        setLoading(false);
        return;
      }
      const projectionFixings = fixingsById.get(projectionIndexDef.id) || [];
      const indexWithFixings = projectionFixings.length > 0
        ? { ...projectionIndexDef, fixings: projectionFixings }
        : projectionIndexDef;

      const request = buildThinARequest(indexWithFixings);

      const response = await priceFloatingBond(request, asOfDate);

      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);

      if (response.success && response.data?.bonds?.[0]) {
        setResult(response.data.bonds[0]);
        setLastRequest(request);
      } else {
        setError(response.error || 'Pricing failed');
        setErrorInfo(response.errorInfo);
        setLastRequest(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pricing failed');
      setErrorInfo(undefined);
      setLastRequest(null);
    } finally {
      setLoading(false);
    }
  };
  
  const inputClass = formStyles.input;
  const labelClass = formStyles.label;
  
  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        {/* Header */}
        <PageHeader
          title={isNew ? 'New Floating Rate Bond' : (bondName.trim() || 'Floating Rate Bond')}
          subtitle="Price floating rate notes linked to IBOR indices"
          backLink={<BackLink onClick={() => navigate('/products/floating-rate-bond')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={bondName}
              onNameChange={setBondName}
              onSave={handleSaveBond}
              saving={saving}
              placeholder="Bond name…"
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
          {/* Left: Bond Parameters */}
          <div className="lg:col-span-2 space-y-6">
            {/* Pricing Context */}
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
                  <label className={labelClass}>Settlement Date</label>
                  <input
                    type="date"
                    value={settlementDate}
                    onChange={e => setSettlementDate(e.target.value)}
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
              {/* Attach Quote Snapshot Toggle */}
              <div className="flex items-center gap-2 mt-3">
                <input
                  type="checkbox"
                  id="attachQuotesFloat"
                  checked={attachQuotes}
                  onChange={e => setAttachQuotes(e.target.checked)}
                  className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                />
                <label htmlFor="attachQuotesFloat" className="text-xs text-[#737373]">
                  Attach quote book snapshot to pricing request
                </label>
              </div>
            </div>
            
            {/* Bond Details */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Bond Details</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Face Amount</label>
                  <input
                    type="number"
                    value={bond.faceAmount}
                    onChange={e => updateBond('faceAmount', parseFloat(e.target.value) || 100)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Spread (bps)</label>
                  <input
                    type="number"
                    step="1"
                    value={(bond.spread * 10000).toFixed(0)}
                    onChange={e => updateBond('spread', (parseFloat(e.target.value) || 0) / 10000)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Redemption</label>
                  <input
                    type="number"
                    value={bond.redemption}
                    onChange={e => updateBond('redemption', parseFloat(e.target.value) || 100)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Settlement Days</label>
                  <input
                    type="number"
                    value={bond.settlementDays}
                    onChange={e => updateBond('settlementDays', parseInt(e.target.value) || 2)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Fixing Days</label>
                  <input
                    type="number"
                    value={bond.fixingDays}
                    onChange={e => updateBond('fixingDays', parseInt(e.target.value) || 2)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Accrual Day Counter</label>
                  <select
                    value={bond.accrualDayCounter}
                    onChange={e => updateBond('accrualDayCounter', e.target.value)}
                    className={inputClass}
                  >
                    {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                  </select>
                </div>
              </div>
              
              <div className="mt-4">
                <label className="flex items-center gap-2 text-sm text-[#525252]">
                  <input
                    type="checkbox"
                    checked={bond.inArrears}
                    onChange={e => updateBond('inArrears', e.target.checked)}
                    className="rounded border-[#d4d4d4]"
                  />
                  In Arrears
                </label>
              </div>
            </div>
            
            {/* Index */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Floating Index</h2>
              <IndexPicker
                label="IBOR Index"
                value={indexRef}
                onChange={(ref: IndexRef, _def: IndexDef) => setIndexRef(ref)}
                filter="Ibor"
              />
            </div>
            
            {/* Schedule */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Schedule</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Issue Date</label>
                  <input
                    type="date"
                    value={bond.issueDate}
                    onChange={e => updateBond('issueDate', e.target.value)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Effective Date</label>
                  <input
                    type="date"
                    value={bond.effectiveDate}
                    onChange={e => updateBond('effectiveDate', e.target.value)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Maturity Date</label>
                  <input
                    type="date"
                    value={bond.terminationDate}
                    onChange={e => updateBond('terminationDate', e.target.value)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Frequency</label>
                  <select
                    value={bond.frequency}
                    onChange={e => updateBond('frequency', e.target.value)}
                    className={inputClass}
                  >
                    {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Calendar</label>
                  <select
                    value={bond.calendar}
                    onChange={e => updateBond('calendar', e.target.value)}
                    className={inputClass}
                  >
                    {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Date Generation</label>
                  <select
                    value={bond.dateGenerationRule}
                    onChange={e => updateBond('dateGenerationRule', e.target.value)}
                    className={inputClass}
                  >
                    {DATE_GEN_RULES.map(r => <option key={r} value={r}>{r}</option>)}
                  </select>
                </div>
              </div>
            </div>
            
            {/* Options */}
            <div className="flex gap-4">
              <label className="flex items-center gap-2 text-sm text-[#525252]">
                <input
                  type="checkbox"
                  checked={includeDetails}
                  onChange={e => setIncludeDetails(e.target.checked)}
                  className="rounded border-[#d4d4d4]"
                />
                Include risk metrics
              </label>
              <label className="flex items-center gap-2 text-sm text-[#525252]">
                <input
                  type="checkbox"
                  checked={includeFlows}
                  onChange={e => setIncludeFlows(e.target.checked)}
                  className="rounded border-[#d4d4d4]"
                />
                Include cash flows
              </label>
            </div>
            
            {/* Price Button */}
            <div className="flex gap-2">
              <button
                onClick={handlePrice}
                disabled={loading || (!discountCurve && !appGraph?.bondId)}
                className="flex-1 py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50 transition-colors"
              >
                {loading ? 'Pricing...' : 'Price Bond'}
              </button>
              {lastRequest && (
                <button
                  onClick={handleDownloadRequest}
                  className="px-4 py-3 text-sm font-medium text-[#8a6a2f] bg-[#f5f0e6] border border-[#e5d9c3] rounded-xl hover:bg-[#efe5d3] transition-colors flex items-center gap-1.5"
                  title="Download API request JSON"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                  </svg>
                  Request JSON
                </button>
              )}
            </div>
          </div>
          
          {/* Right: Results */}
          <div>
            <PricingResults
              loading={loading}
              error={error}
              errorInfo={errorInfo}
              result={result}
              durationMs={durationMs}
              requestId={requestId}
            />
          </div>
        </div>

        {appGraph?.bondId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/bonds/floating"
              entityId={appGraph.bondId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
