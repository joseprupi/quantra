// Component for editing a single curve point
// Now supports OIS helpers, quote references, curve dependencies, and IndexPicker
import { useState, useEffect } from 'react';
import {
  CurvePoint, PointType, Curve, IndexDef, IndexRef,
  DEFAULT_DEPOSIT_HELPER, DEFAULT_SWAP_HELPER, DEFAULT_FRA_HELPER,
  DEFAULT_FUTURE_HELPER, DEFAULT_BOND_HELPER, DEFAULT_OIS_HELPER, DEFAULT_DATED_OIS_HELPER,
  DAY_COUNTERS, BUSINESS_DAY_CONVENTIONS, CALENDARS, TIME_UNITS,
  FREQUENCIES, OVERNIGHT_AVERAGING_METHODS,
} from '../../lib/types';
import IndexPicker from './IndexPicker';
import {
  getMdBackendSettings,
  listCatalogSeries,
  resolveCatalogValuesAt,
} from '../../lib/marketDataBackend';
import { useAsOfDate } from '../../hooks/useAsOfDate';

// A quote option from the market-data catalog: the canonical id
// plus its value resolved at the app's global As-Of date.
interface MdQuoteOption {
  id: string;
  value: number | null;
  resolvedAsOf: string | null;
}

interface CurvePointEditorProps {
  point?: CurvePoint;
  onSave: (point: CurvePoint) => void;
  onCancel: () => void;
  // For dependency dropdowns (other curves in same set)
  availableCurves?: Curve[];
  currentCurveId?: string;
}

const POINT_TYPES: { type: PointType; label: string }[] = [
  { type: 'DepositHelper', label: 'Deposit' },
  { type: 'OISHelper', label: 'OIS' },
  { type: 'SwapHelper', label: 'Swap' },
  { type: 'FRAHelper', label: 'FRA' },
  { type: 'FutureHelper', label: 'Future' },
  { type: 'BondHelper', label: 'Bond' },
  { type: 'DatedOISHelper', label: 'Dated OIS' },
];

function getDefault(type: PointType): CurvePoint {
  const m: Record<PointType, CurvePoint> = {
    DepositHelper: JSON.parse(JSON.stringify(DEFAULT_DEPOSIT_HELPER)),
    SwapHelper: JSON.parse(JSON.stringify(DEFAULT_SWAP_HELPER)),
    FRAHelper: JSON.parse(JSON.stringify(DEFAULT_FRA_HELPER)),
    FutureHelper: JSON.parse(JSON.stringify(DEFAULT_FUTURE_HELPER)),
    BondHelper: JSON.parse(JSON.stringify(DEFAULT_BOND_HELPER)),
    OISHelper: JSON.parse(JSON.stringify(DEFAULT_OIS_HELPER)),
    DatedOISHelper: JSON.parse(JSON.stringify(DEFAULT_DATED_OIS_HELPER)),
  };
  return m[type];
}

export default function CurvePointEditor({ point, onSave, onCancel, availableCurves, currentCurveId }: CurvePointEditorProps) {
  const [pointType, setPointType] = useState<PointType>(point?.point_type || 'DepositHelper');
  const [currentPoint, setCurrentPoint] = useState<CurvePoint>(point ? JSON.parse(JSON.stringify(point)) : getDefault('DepositHelper'));
  const [quotes, setQuotes] = useState<MdQuoteOption[]>([]);
  const [quotesError, setQuotesError] = useState<string | null>(null);
  const [useQuoteRef, setUseQuoteRef] = useState(!!(point?.point as any)?.quote_id);
  const { asOfDate } = useAsOfDate();

  // Quote ids come from the market-data catalog (the only quote source);
  // values shown are resolved at the global As-Of ('previous' semantics —
  // the latest point at or before that date).
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const md = getMdBackendSettings();
        const series = await listCatalogSeries(md.baseUrl);
        const ids = series.map(s => s.canonical_id);
        const resolved = await resolveCatalogValuesAt(md.baseUrl, ids, asOfDate);
        if (!alive) return;
        const valueById = new Map(resolved.map(r => [r.canonical_id, r]));
        setQuotes(
          ids.map(id => ({
            id,
            value: valueById.get(id)?.value ?? null,
            resolvedAsOf: valueById.get(id)?.resolved_as_of?.slice(0, 10) ?? null,
          })),
        );
        setQuotesError(null);
      } catch {
        if (!alive) return;
        setQuotes([]);
        setQuotesError('Market-data server unreachable — quote list unavailable.');
      }
    })();
    return () => {
      alive = false;
    };
  }, [asOfDate]);

  const handleTypeChange = (newType: PointType) => {
    setPointType(newType);
    setCurrentPoint(getDefault(newType));
    setUseQuoteRef(false);
  };

  const updateField = (field: string, value: unknown) => {
    setCurrentPoint(prev => ({
      ...prev,
      point: { ...prev.point, [field]: value },
    } as CurvePoint));
  };

  const handleSave = () => onSave(currentPoint);

  const p = currentPoint.point as any;
  const inputClass = "w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors";
  const labelClass = "block text-xs text-[#737373] mb-1 font-medium";

  // Rate / Quote selector
  const RateOrQuote = () => (
    <div className="mb-4">
      <div className="flex items-center gap-3 mb-2">
        <label className="flex items-center gap-1.5 text-xs text-[#525252] cursor-pointer">
          <input type="radio" checked={!useQuoteRef} onChange={() => { setUseQuoteRef(false); updateField('quote_id', undefined); }}
            className="text-[#8a6a2f]" />
          Inline rate
        </label>
        <label className="flex items-center gap-1.5 text-xs text-[#525252] cursor-pointer">
          <input type="radio" checked={useQuoteRef} onChange={() => setUseQuoteRef(true)}
            className="text-[#8a6a2f]" />
          Quote reference
        </label>
      </div>
      {useQuoteRef ? (
        <div>
          <label className={labelClass}>Quote ID</label>
          <select value={p.quote_id || ''} onChange={e => { updateField('quote_id', e.target.value || undefined); updateField('rate', undefined); }} className={inputClass}>
            <option value="">— Select quote —</option>
            {quotes.map(q => (
              <option key={q.id} value={q.id}>
                {q.id}
                {q.value !== null ? ` — ${(q.value * 100).toFixed(3)}%` : ' — no value'}
                {q.resolvedAsOf ? ` @${q.resolvedAsOf}` : ''}
              </option>
            ))}
          </select>
          <p className="text-xs text-[#a3a3a3] mt-1">
            Values from the MD server, resolved at the global As-Of ({asOfDate}).
            The id is stored on the curve; pricing re-resolves it server-side.
          </p>
          {quotesError && (
            <p className="text-xs text-amber-600 mt-1">{quotesError}</p>
          )}
          {!quotesError && quotes.length === 0 && (
            <p className="text-xs text-amber-600 mt-1">MD catalog is empty — check the market-data server / Quote Book.</p>
          )}
        </div>
      ) : (
        <div>
          <label className={labelClass}>
            {pointType === 'FutureHelper' ? 'Price' : 'Rate (%)'}
          </label>
          <input
            type="number"
            step="0.001"
            value={pointType === 'FutureHelper' ? (p.rate ?? '') : ((p.rate ?? 0) * 100).toFixed(3)}
            onChange={e => {
              const v = parseFloat(e.target.value);
              updateField('rate', pointType === 'FutureHelper' ? v : v / 100);
              updateField('quote_id', undefined);
            }}
            className={inputClass}
          />
        </div>
      )}
    </div>
  );

  // Emit non-negative ints as numbers (never strings; blank/garbage -> 0).
  const nonNegativeInt = (raw: string): number => {
    const v = parseInt(raw, 10);
    return Number.isFinite(v) && v >= 0 ? v : 0;
  };

  // Engine >= 0.6 overnight-coupon params, shared by OISHelper + DatedOISHelper.
  const OvernightParams = () => (
    <>
      <div>
        <label htmlFor="ois-payment-lag" className={labelClass} title="Payment lag in business days (SOFR market standard: 2)">Payment lag (bd)</label>
        <input id="ois-payment-lag" type="number" min={0} step={1} value={p.payment_lag ?? 0}
          onChange={e => updateField('payment_lag', nonNegativeInt(e.target.value))} className={inputClass} />
      </div>
      <div>
        <label htmlFor="ois-averaging" className={labelClass} title="Overnight rate averaging over the coupon period">Averaging</label>
        <select id="ois-averaging" value={p.averaging_method || 'Compound'} onChange={e => updateField('averaging_method', e.target.value)} className={inputClass}>
          {OVERNIGHT_AVERAGING_METHODS.map(m => <option key={m}>{m}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor="ois-lookback" className={labelClass} title="Rate observation lookback in days (0 = none)">Lookback (d)</label>
        <input id="ois-lookback" type="number" min={0} step={1} value={p.lookback_days ?? 0}
          onChange={e => updateField('lookback_days', nonNegativeInt(e.target.value))} className={inputClass} />
      </div>
      <div>
        <label htmlFor="ois-lockout" className={labelClass} title="Rate lockout in days at period end (0 = none)">Lockout (d)</label>
        <input id="ois-lockout" type="number" min={0} step={1} value={p.lockout_days ?? 0}
          onChange={e => updateField('lockout_days', nonNegativeInt(e.target.value))} className={inputClass} />
      </div>
      <div className="col-span-2">
        <label className="flex items-center gap-1.5 text-xs text-[#525252] cursor-pointer"
          title="Apply observation shift together with the lookback">
          <input type="checkbox" checked={!!p.apply_observation_shift}
            onChange={e => updateField('apply_observation_shift', e.target.checked)} className="text-[#8a6a2f]" />
          Obs. shift
        </label>
      </div>
    </>
  );

  // Dependency selector for SwapHelper
  const DepsSelector = () => {
    if (!availableCurves || availableCurves.length === 0) return null;
    const otherCurves = availableCurves.filter(c => c.id !== currentCurveId);
    if (otherCurves.length === 0) return null;
    return (
      <div className="col-span-2 pt-3 border-t border-[#e5e5e5]">
        <p className="text-xs font-medium text-[#737373] mb-2">Dependencies</p>
        <div>
          <label className={labelClass}>Discount Curve</label>
          <select
            value={p.deps?.discount_curve?.id || ''}
            onChange={e => updateField('deps', e.target.value ? { discount_curve: { id: e.target.value } } : undefined)}
            className={inputClass}
          >
            <option value="">— Same as bootstrapped —</option>
            {otherCurves.map(c => <option key={c.id} value={c.id}>{c.name} ({c.role})</option>)}
          </select>
        </div>
      </div>
    );
  };

  return (
    <div className="bg-[#f5f5f5] rounded-xl p-5 border border-[#e5e5e5]">
      <h3 className="text-sm font-semibold text-[#0a0a0a] mb-4">
        {point ? 'Edit Instrument' : 'Add Instrument'}
      </h3>

      {/* Type selector */}
      <div className="mb-4">
        <label className={labelClass}>Instrument Type</label>
        <div className="flex flex-wrap gap-1.5">
          {POINT_TYPES.map(({ type, label }) => (
            <button key={type} onClick={() => handleTypeChange(type)}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                pointType === type ? 'bg-[#0a0a0a] text-white' : 'bg-white text-[#525252] border border-[#d4d4d4] hover:bg-[#e5e5e5]'
              }`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <RateOrQuote />

      {/* Type-specific fields */}
      {pointType === 'DepositHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Tenor</label>
            <div className="flex gap-2">
              <input type="number" value={p.tenor_number} onChange={e => updateField('tenor_number', parseInt(e.target.value) || 1)} className={`${inputClass} w-20`} />
              <select value={p.tenor_time_unit} onChange={e => updateField('tenor_time_unit', e.target.value)} className={inputClass}>
                {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className={labelClass}>Fixing Days</label>
            <input type="number" value={p.fixing_days} onChange={e => updateField('fixing_days', parseInt(e.target.value) || 0)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Day Counter</label>
            <select value={p.day_counter} onChange={e => updateField('day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div className="col-span-2">
            <label className={labelClass}>Convention</label>
            <select value={p.business_day_convention} onChange={e => updateField('business_day_convention', e.target.value)} className={inputClass}>
              {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
            </select>
          </div>
        </div>
      )}

      {pointType === 'OISHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Tenor</label>
            <div className="flex gap-2">
              <input type="number" value={p.tenor_number} onChange={e => updateField('tenor_number', parseInt(e.target.value) || 1)} className={`${inputClass} w-20`} />
              <select value={p.tenor_time_unit} onChange={e => updateField('tenor_time_unit', e.target.value)} className={inputClass}>
                {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
              </select>
            </div>
          </div>
          <div className="col-span-2">
            <IndexPicker
              label="Overnight Index"
              value={p.overnight_index}
              onChange={(ref: IndexRef, _def: IndexDef) => {
                updateField('overnight_index', ref);
              }}
              filter="Overnight"
            />
          </div>
          <div>
            <label className={labelClass}>Settlement Days</label>
            <input type="number" value={p.settlement_days ?? 2} onChange={e => updateField('settlement_days', parseInt(e.target.value))} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar || 'TARGET'} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Fixed Leg Frequency</label>
            <select value={p.fixed_leg_frequency || 'Annual'} onChange={e => updateField('fixed_leg_frequency', e.target.value)} className={inputClass}>
              {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Fixed Leg Day Counter</label>
            <select value={p.fixed_leg_day_counter || 'Actual360'} onChange={e => updateField('fixed_leg_day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <OvernightParams />
          <DepsSelector />
        </div>
      )}

      {pointType === 'DatedOISHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Start Date</label>
            <input type="date" value={p.start_date} onChange={e => updateField('start_date', e.target.value)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>End Date</label>
            <input type="date" value={p.end_date} onChange={e => updateField('end_date', e.target.value)} className={inputClass} />
          </div>
          <div className="col-span-2">
            <IndexPicker
              label="Overnight Index"
              value={p.overnight_index}
              onChange={(ref: IndexRef, _def: IndexDef) => {
                updateField('overnight_index', ref);
              }}
              filter="Overnight"
            />
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar || 'TARGET'} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="dated-ois-fixed-leg-freq" className={labelClass} title="Fixed leg payment frequency (engine hardcoded Annual before 0.6)">Fixed leg freq</label>
            <select id="dated-ois-fixed-leg-freq" value={p.fixed_leg_frequency || 'Annual'} onChange={e => updateField('fixed_leg_frequency', e.target.value)} className={inputClass}>
              {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Fixed Leg Day Counter</label>
            <select value={p.fixed_leg_day_counter || 'Actual360'} onChange={e => updateField('fixed_leg_day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Settlement Days</label>
            <input type="number" value={p.settlement_days ?? 2} onChange={e => updateField('settlement_days', parseInt(e.target.value))} className={inputClass} />
          </div>
          <OvernightParams />
        </div>
      )}

      {pointType === 'SwapHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Tenor</label>
            <div className="flex gap-2">
              <input type="number" value={p.tenor_number} onChange={e => updateField('tenor_number', parseInt(e.target.value) || 1)} className={`${inputClass} w-20`} />
              <select value={p.tenor_time_unit} onChange={e => updateField('tenor_time_unit', e.target.value)} className={inputClass}>
                {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
              </select>
            </div>
          </div>
          <div className="col-span-2">
            <IndexPicker
              label="Floating Index"
              value={p.float_index}
              onChange={(ref: IndexRef, _def: IndexDef) => {
                updateField('float_index', ref);
              }}
              filter="Ibor"
            />
          </div>
          <div>
            <label className={labelClass}>Fixed Leg Frequency</label>
            <select value={p.sw_fixed_leg_frequency} onChange={e => updateField('sw_fixed_leg_frequency', e.target.value)} className={inputClass}>
              {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Fixed Leg Day Counter</label>
            <select value={p.sw_fixed_leg_day_counter} onChange={e => updateField('sw_fixed_leg_day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Spread</label>
            <input type="number" step="0.0001" value={p.spread ?? 0} onChange={e => updateField('spread', parseFloat(e.target.value) || 0)} className={inputClass} />
          </div>
          <DepsSelector />
        </div>
      )}

      {pointType === 'FRAHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Months to Start</label>
            <input type="number" value={p.months_to_start} onChange={e => updateField('months_to_start', parseInt(e.target.value) || 1)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Months to End</label>
            <input type="number" value={p.months_to_end} onChange={e => updateField('months_to_end', parseInt(e.target.value) || 3)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Fixing Days</label>
            <input type="number" value={p.fixing_days} onChange={e => updateField('fixing_days', parseInt(e.target.value) || 0)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Day Counter</label>
            <select value={p.day_counter} onChange={e => updateField('day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Convention</label>
            <select value={p.business_day_convention} onChange={e => updateField('business_day_convention', e.target.value)} className={inputClass}>
              {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
            </select>
          </div>
        </div>
      )}

      {pointType === 'FutureHelper' && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Future Start Date</label>
            <input type="date" value={p.future_start_date} onChange={e => updateField('future_start_date', e.target.value)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Future Months</label>
            <input type="number" value={p.future_months} onChange={e => updateField('future_months', parseInt(e.target.value) || 3)} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Calendar</label>
            <select value={p.calendar} onChange={e => updateField('calendar', e.target.value)} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Day Counter</label>
            <select value={p.day_counter} onChange={e => updateField('day_counter', e.target.value)} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
        </div>
      )}

      {pointType === 'BondHelper' && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelClass}>Settlement Days</label>
              <input type="number" value={p.settlement_days} onChange={e => updateField('settlement_days', parseInt(e.target.value) || 2)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Face Amount</label>
              <input type="number" value={p.face_amount} onChange={e => updateField('face_amount', parseFloat(e.target.value) || 100)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Coupon Rate (%)</label>
              <input type="number" step="0.01" value={((p.coupon_rate ?? 0) * 100).toFixed(2)}
                onChange={e => updateField('coupon_rate', (parseFloat(e.target.value) || 0) / 100)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Redemption</label>
              <input type="number" value={p.redemption} onChange={e => updateField('redemption', parseFloat(e.target.value) || 100)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Issue Date</label>
              <input type="date" value={p.issue_date} onChange={e => updateField('issue_date', e.target.value)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Day Counter</label>
              <select value={p.day_counter} onChange={e => updateField('day_counter', e.target.value)} className={inputClass}>
                {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
              </select>
            </div>
          </div>
          <div className="pt-3 border-t border-[#e5e5e5]">
            <p className="text-xs font-medium text-[#737373] mb-2">Schedule</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelClass}>Effective Date</label>
                <input type="date" value={p.schedule?.effective_date || ''} onChange={e => updateField('schedule', { ...p.schedule, effective_date: e.target.value })} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>Termination Date</label>
                <input type="date" value={p.schedule?.termination_date || ''} onChange={e => updateField('schedule', { ...p.schedule, termination_date: e.target.value })} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>Frequency</label>
                <select value={p.schedule?.frequency || 'Annual'} onChange={e => updateField('schedule', { ...p.schedule, frequency: e.target.value })} className={inputClass}>
                  {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
                </select>
              </div>
              <div>
                <label className={labelClass}>Calendar</label>
                <select value={p.schedule?.calendar || 'TARGET'} onChange={e => updateField('schedule', { ...p.schedule, calendar: e.target.value })} className={inputClass}>
                  {CALENDARS.map(c => <option key={c}>{c}</option>)}
                </select>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2 mt-5">
        <button onClick={onCancel} className="flex-1 px-4 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">
          Cancel
        </button>
        <button onClick={handleSave} className="flex-1 px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626]">
          {point ? 'Update' : 'Add Instrument'}
        </button>
      </div>
    </div>
  );
}
