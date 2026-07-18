// Fixed Rate Bond Pricing Page
import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import PricingResults from '../../components/products/PricingResults';
import { Curve, DAY_COUNTERS, CALENDARS, BUSINESS_DAY_CONVENTIONS, IndexDef, collectIndexRefIds } from '../../lib/types';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { priceFixedBond } from '../../lib/api/bondPricingService';
import {
  persistBondFixedGraph,
  buildBondFixedPriceArm,
  asBondFixedAppGraph,
  type BondFixedAppGraph,
} from '../../lib/api/bondSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import { normalizeCurveForApi } from '../../lib/api-normalizers';
import { fixedBondStore, SavedFixedRateBond, generateId } from '../../lib/storage/bonds';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { useAuth } from '../../hooks/useAuth';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

interface BondParams {
  settlementDays: number;
  faceAmount: number;
  couponRate: number;
  accrualDayCounter: string;
  paymentConvention: string;
  redemption: number;
  issueDate: string;
  effectiveDate: string;
  terminationDate: string;
  frequency: string;
  calendar: string;
  convention: string;
  dateGenerationRule: string;
}

interface YieldParams {
  dayCounter: string;
  compounding: string;
  frequency: string;
}

const FREQUENCIES = ['Annual', 'Semiannual', 'Quarterly', 'Monthly'];
const COMPOUNDINGS = ['Compounded', 'Continuous', 'Simple'];
const DATE_GEN_RULES = ['Forward', 'Backward'];

function findInvalidNumbers(value: any, path: string[] = []): string[] {
  const issues: string[] = [];
  if (value === null) {
    issues.push(path.join('.') || '(root)');
    return issues;
  }
  if (typeof value === 'number' && Number.isNaN(value)) {
    issues.push(path.join('.') || '(root)');
    return issues;
  }
  if (Array.isArray(value)) {
    value.forEach((v, i) => {
      issues.push(...findInvalidNumbers(v, [...path, `[${i}]`]));
    });
    return issues;
  }
  if (typeof value === 'object') {
    Object.entries(value).forEach(([k, v]) => {
      issues.push(...findInvalidNumbers(v, [...path, k]));
    });
  }
  return issues;
}

/** Resolve IndexDef objects for any IndexRef ids used by curve points */
async function resolveIndexDefsFromCurve(curve: Curve): Promise<IndexDef[]> {
  const ids = collectIndexRefIds(curve.points || []);
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

export default function FixedRateBondPricer() {
  const ui = entityUi.fixedRateBond;
  const { id } = useParams();
  const navigate = useNavigate();
  const isNew = !id || id === 'new';
  
  // Dates
  const today = new Date().toISOString().split('T')[0];
  const fiveYearsLater = new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];
  
  const [asOfDate, setAsOfDate] = useState(today);
  const [attachQuotes, setAttachQuotes] = useState(true);
  // Sync with global as-of date
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
  const [appGraph, setAppGraph] = useState<BondFixedAppGraph | null>(null);

  // Curve selection
  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  
  // Bond parameters
  const [bond, setBond] = useState<BondParams>({
    settlementDays: 2,
    faceAmount: 100,
    couponRate: 0.05,
    accrualDayCounter: 'ActualActualBond',
    paymentConvention: 'ModifiedFollowing',
    redemption: 100,
    issueDate: today,
    effectiveDate: today,
    terminationDate: fiveYearsLater,
    frequency: 'Semiannual',
    calendar: 'TARGET',
    convention: 'ModifiedFollowing',
    dateGenerationRule: 'Forward',
  });
  
  // Yield calculation params
  const [yieldParams, setYieldParams] = useState<YieldParams>({
    dayCounter: 'Actual360',
    compounding: 'Compounded',
    frequency: 'Annual',
  });
  
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
      fixedBondStore.getById(id).then(saved => {
        if (saved) {
          setBondName(saved.name);
          setBondId(saved.id);
          // Re-hydrate the server-id bridge so a previously-saved bond prices
          // by-reference and re-saves PATCH the same rows (idempotent).
          setAppGraph(asBondFixedAppGraph(saved.appGraph));
          setBond({
            settlementDays: saved.settlementDays,
            faceAmount: saved.faceAmount,
            couponRate: saved.couponRate,
            accrualDayCounter: saved.accrualDayCounter,
            paymentConvention: saved.paymentConvention,
            redemption: saved.redemption,
            issueDate: saved.issueDate,
            effectiveDate: saved.effectiveDate,
            terminationDate: saved.terminationDate,
            frequency: saved.frequency,
            calendar: saved.calendar,
            convention: saved.convention,
            dateGenerationRule: saved.dateGenerationRule,
          });
          setDiscountCurveId(saved.discountCurveId);
          setYieldParams({
            dayCounter: saved.yieldDayCounter,
            compounding: saved.yieldCompounding,
            frequency: saved.yieldFrequency,
          });
        }
      });
    }
  }, [id, isNew]);
  
  const handleSaveBond = async () => {
    setSaveStatus(null);
    setSaving(true);
    try {
      const saveId = bondId || generateId();
      const finalName = bondName.trim() || `Fixed Bond ${bond.couponRate * 100}% ${bond.terminationDate}`;
      const makeSaved = (graph: BondFixedAppGraph | null): SavedFixedRateBond => ({
        id: saveId,
        name: finalName,
        settlementDays: bond.settlementDays,
        faceAmount: bond.faceAmount,
        couponRate: bond.couponRate,
        accrualDayCounter: bond.accrualDayCounter,
        paymentConvention: bond.paymentConvention,
        redemption: bond.redemption,
        issueDate: bond.issueDate,
        effectiveDate: bond.effectiveDate,
        terminationDate: bond.terminationDate,
        frequency: bond.frequency,
        calendar: bond.calendar,
        convention: bond.convention,
        dateGenerationRule: bond.dateGenerationRule,
        discountCurveId: discountCurveId,
        yieldDayCounter: yieldParams.dayCounter,
        yieldCompounding: yieldParams.compounding,
        yieldFrequency: yieldParams.frequency,
        appId: graph?.bondId,
        appGraph: (graph ?? undefined) as Record<string, unknown> | undefined,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      });

      const priorGraph = appGraph;
      // Persist the entity graph server-side leaf→root (curves → curve_set →
      // bonds_fixed) and stamp the UUIDs. Only runs when a discount curve is
      // selected; the localStorage save still runs either way (dual-read,
      // additive). On failure we keep the prior appGraph and surface the stage.
      let nextGraph: BondFixedAppGraph | null = priorGraph;
      // Honest default: when the server persist below is skipped (no resolved
      // discount curve), say so instead of implying a full save.
      let serverNote = priorGraph
        ? ''
        : ' Saved locally only — no discount curve selected, so it was not persisted server-side.';
      if (discountCurve) {
        const graphResult = await persistBondFixedGraph(
          {
            name: finalName,
            curves: [{ key: 'discount', body: toThinACurve(discountCurve, 'discount') as components['schemas']['CurveCreate'] }],
            curveSetCurrency: discountCurve.currency,
            bondRequest: buildFixedTradeBody(),
            changeReason: changeReason.trim() || undefined,
          },
          priorGraph,
        );
        if (!graphResult.ok) {
          // Branch on the structured envelope code. localStorage still saved.
          const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
          await fixedBondStore.save(makeSaved(priorGraph));
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

      await fixedBondStore.save(makeSaved(nextGraph));
      setBondId(saveId);
      setAppGraph(nextGraph);
      setSaveStatus({ tone: 'success', message: `Saved "${finalName}".${serverNote}` });
      setChangeReason('');
      if (isNew) navigate(`/products/fixed-rate-bond/${saveId}`, { replace: true });
    } catch (err) {
      setSaveStatus({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to save bond' });
    } finally {
      setSaving(false);
    }
  };
  
  const updateBond = (field: keyof BondParams, value: any) => {
    setBond(prev => ({ ...prev, [field]: value }));
  };
  
  // Map a portal-shaped curve onto the orchestrator's inline CurveRef
  // (which forbids extra fields). Only the fields CurveRef accepts
  // (name/currency/day_counter/reference_date/points/body) appear here;
  // interpolator + bootstrap_trait are dropped (not part of the wire
  // contract). Points pass through normalizeCurvePointForApi so legacy
  // ``tenor_number``/``tenor_time_unit`` becomes ``tenor: {n, unit}``;
  // ``quote_id``s are forwarded unresolved.
  const toThinACurve = (curve: Curve, role: 'discount' = 'discount'): any => {
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

  // Inline POST body for ``POST /v1/price/bonds/fixed``.
  // Flat trade body the backend reads + a single
  // role-tagged top-level discount curve. Uses the canonical
  // ``coupon_rate`` field name.
  // Flat trade body persisted as the saved bond's request — the same levers
  // the inline ``bond`` carries (canonical ``coupon_rate``).
  // ``persistBondFixedGraph`` injects ``pricing.{curve_set_id,
  // discount_curve_id}`` (the verified read-path).
  const buildFixedTradeBody = (): Record<string, unknown> => ({
    face_amount: bond.faceAmount,
    coupon_rate: bond.couponRate,
    settlement_days: bond.settlementDays,
    redemption: bond.redemption,
    issue_date: bond.issueDate,
    effective_date: bond.effectiveDate,
    termination_date: bond.terminationDate,
  });

  const buildThinARequest = (): Record<string, any> | null => {
    if (!discountCurve) return null;
    return {
      bond: buildFixedTradeBody(),
      curves: [toThinACurve(discountCurve, 'discount')],
      as_of: asOfDate,
    };
  };

  const handleDownloadRequest = () => {
    if (!lastRequest) return;
    const blob = new Blob([JSON.stringify(lastRequest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `fixed-rate-bond-request-${asOfDate}.json`;
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
    // local curve; an unsaved bond requires the discount curve inline.
    if (!appGraph?.bondId && !discountCurve) {
      setError('Please select a discount curve');
      return;
    }

    setLoading(true);
    setError(null);
    setErrorInfo(undefined);
    setResult(null);

    try {
      // Price-arm selection. Saved → by-reference
      // {bond_id, as_of}; the orchestrator loads the trade + discount curve
      // server-side. Unsaved → inline, unchanged.
      let request: any;
      if (appGraph?.bondId) {
        request = buildBondFixedPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate });
      } else {
        // Resolve indices referenced by curve points so the user can be told
        // before the request goes out. The inline wire body skips market-data index refs.
        const resolvedIndices = await resolveIndexDefsFromCurve(discountCurve!);
        const referencedIds = collectIndexRefIds(discountCurve!.points || []);
        if (referencedIds.length > 0 && resolvedIndices.length === 0) {
          setError('Selected curve references indices that are not available. Add them in Market Data → Indices.');
          setLoading(false);
          return;
        }

        // Inline POST body for ``POST /v1/price/bonds/fixed`` — flat trade
        // body + role-tagged top-level discount curve. ``coupon_rate`` is
        // canonical; curve points carry ``quote_id`` unresolved.
        const inline = buildThinARequest();
        if (!inline) {
          setError('Please select a discount curve');
          setLoading(false);
          return;
        }
        const invalidNumbers = findInvalidNumbers(inline);
        if (invalidNumbers.length > 0) {
          setLastRequest(inline);
          setError(`Request contains null/NaN values at: ${invalidNumbers.slice(0, 6).join(', ')}${invalidNumbers.length > 6 ? '…' : ''}`);
          setLoading(false);
          return;
        }
        request = inline;
      }

      const response = await priceFixedBond(request, asOfDate);

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
        <PageHeader
          title={isNew ? 'New Fixed Rate Bond' : (bondName.trim() || 'Fixed Rate Bond')}
          subtitle="Price fixed coupon bonds with yield, duration, and cash flows"
          backLink={<BackLink onClick={() => navigate('/products/fixed-rate-bond')} label={getBackLabel(ui.plural)} />}
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
              <div className="grid sm:grid-cols-3 gap-4">
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
                  label="Discount Curve"
                  curveRole="discount"
                  curveSetId={curveSetId}
                  curveId={discountCurveId}
                  onChangeCurveSet={(csId) => setCurveSetId(csId)}
                  onChangeCurve={(id, curve) => {
                    setDiscountCurveId(id);
                    setDiscountCurve(curve);
                  }}
                  className="sm:col-span-3"
                />
                {/* Attach Quote Snapshot Toggle */}
                <div className="sm:col-span-3 flex items-center gap-2 pt-1">
                  <input
                    type="checkbox"
                    id="attachQuotes"
                    checked={attachQuotes}
                    onChange={e => setAttachQuotes(e.target.checked)}
                    className="rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                  />
                  <label htmlFor="attachQuotes" className="text-xs text-[#737373]">
                    Attach quote book snapshot to pricing request
                  </label>
                </div>
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
                  <label className={labelClass}>Coupon Rate (%)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={(bond.couponRate * 100).toFixed(2)}
                    onChange={e => updateBond('couponRate', (parseFloat(e.target.value) || 0) / 100)}
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
                  <label className={labelClass}>Accrual Day Counter</label>
                  <select
                    value={bond.accrualDayCounter}
                    onChange={e => updateBond('accrualDayCounter', e.target.value)}
                    className={inputClass}
                  >
                    {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Payment Convention</label>
                  <select
                    value={bond.paymentConvention}
                    onChange={e => updateBond('paymentConvention', e.target.value)}
                    className={inputClass}
                  >
                    {BUSINESS_DAY_CONVENTIONS.map(bc => <option key={bc} value={bc}>{bc}</option>)}
                  </select>
                </div>
              </div>
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
            
            {/* Yield Calculation */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Yield Calculation</h2>
              <div className="grid sm:grid-cols-3 gap-4">
                <div>
                  <label className={labelClass}>Day Counter</label>
                  <select
                    value={yieldParams.dayCounter}
                    onChange={e => setYieldParams(p => ({ ...p, dayCounter: e.target.value }))}
                    className={inputClass}
                  >
                    {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Compounding</label>
                  <select
                    value={yieldParams.compounding}
                    onChange={e => setYieldParams(p => ({ ...p, compounding: e.target.value }))}
                    className={inputClass}
                  >
                    {COMPOUNDINGS.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Frequency</label>
                  <select
                    value={yieldParams.frequency}
                    onChange={e => setYieldParams(p => ({ ...p, frequency: e.target.value }))}
                    className={inputClass}
                  >
                    {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
                  </select>
                </div>
              </div>
              
              <div className="flex gap-4 mt-4">
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
              entityPath="/v1/bonds/fixed"
              entityId={appGraph.bondId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
