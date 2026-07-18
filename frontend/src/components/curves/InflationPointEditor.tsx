import { useEffect, useState } from 'react';
import {
  BUSINESS_DAY_CONVENTIONS,
  CALENDARS,
  CPI_INTERPOLATION_TYPES,
  DAY_COUNTERS,
  TIME_UNITS,
  type Curve,
  type InflationCurveConfig,
  type InflationPointWrapper,
  type Period,
  type QuoteSpec,
} from '../../lib/types';
import { getLegacyFlatQuotes } from '../../lib/storage/quoteBook';

interface Props {
  point?: InflationPointWrapper;
  kind: InflationCurveConfig['kind'];
  defaultObservationLag: Period;
  defaultCalendar: InflationPointWrapper['point']['calendar'];
  defaultPaymentConvention: InflationPointWrapper['point']['payment_convention'];
  defaultDayCounter: InflationPointWrapper['point']['day_counter'];
  defaultDiscountCurveId?: string;
  availableDiscountCurves?: Curve[];
  onSave: (point: InflationPointWrapper) => void;
  onCancel: () => void;
}

type MaturityMode = 'tenor' | 'dates';

function makeDefaultPoint(
  kind: InflationCurveConfig['kind'],
  observationLag: Period,
  calendar: InflationPointWrapper['point']['calendar'],
  paymentConvention: InflationPointWrapper['point']['payment_convention'],
  dayCounter: InflationPointWrapper['point']['day_counter'],
  discountCurveId?: string,
): InflationPointWrapper {
  return {
    point_type: kind === 'YoYInflation' ? 'YearOnYearInflationSwapHelper' : 'ZeroCouponInflationSwapHelper',
    point: {
      tenor: { n: 5, unit: 'Years' },
      quote_value: 0.0225,
      swap_observation_lag: observationLag,
      calendar,
      payment_convention: paymentConvention,
      day_counter: dayCounter,
      observation_interpolation: 'AsIndex',
      ...(kind === 'YoYInflation' ? { nominal_curve_id: discountCurveId || '' } : {}),
    },
  };
}

export default function InflationPointEditor({
  point,
  kind,
  defaultObservationLag,
  defaultCalendar,
  defaultPaymentConvention,
  defaultDayCounter,
  defaultDiscountCurveId,
  availableDiscountCurves = [],
  onSave,
  onCancel,
}: Props) {
  const [quotes, setQuotes] = useState<QuoteSpec[]>([]);
  const [currentPoint, setCurrentPoint] = useState<InflationPointWrapper>(() => (
    point
      ? JSON.parse(JSON.stringify(point))
      : makeDefaultPoint(kind, defaultObservationLag, defaultCalendar, defaultPaymentConvention, defaultDayCounter, defaultDiscountCurveId)
  ));
  const [useQuoteRef, setUseQuoteRef] = useState<boolean>(!!point?.point.quote_id);
  const [maturityMode, setMaturityMode] = useState<MaturityMode>(() => (
    point?.point.start_date || point?.point.end_date ? 'dates' : 'tenor'
  ));

  useEffect(() => {
    setQuotes(getLegacyFlatQuotes());
  }, []);

  const inputClass = 'w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors';
  const labelClass = 'block text-xs text-[#737373] mb-1 font-medium';
  const helperLabel = kind === 'YoYInflation' ? 'Year-on-Year Swap' : 'Zero Coupon Swap';
  const p = currentPoint.point;

  const updateField = (field: keyof InflationPointWrapper['point'], value: unknown) => {
    setCurrentPoint((prev) => ({
      ...prev,
      point_type: kind === 'YoYInflation' ? 'YearOnYearInflationSwapHelper' : 'ZeroCouponInflationSwapHelper',
      point: {
        ...prev.point,
        [field]: value,
      },
    }));
  };

  const handleSave = () => {
    const normalized: InflationPointWrapper = {
      point_type: kind === 'YoYInflation' ? 'YearOnYearInflationSwapHelper' : 'ZeroCouponInflationSwapHelper',
      point: {
        ...currentPoint.point,
        ...(kind === 'YoYInflation'
          ? { nominal_curve_id: currentPoint.point.nominal_curve_id || defaultDiscountCurveId || '' }
          : { nominal_curve_id: undefined }),
      },
    };
    onSave(normalized);
  };

  return (
    <div className="bg-[#f5f5f5] rounded-xl p-5 border border-[#e5e5e5]">
      <h3 className="text-sm font-semibold text-[#0a0a0a] mb-4">
        {point ? 'Edit Helper' : 'Add Helper'}
      </h3>

      <div className="mb-4">
        <label className={labelClass}>Helper Type</label>
        <div className="flex flex-wrap gap-1.5">
          <button
            type="button"
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-[#0a0a0a] text-white"
          >
            {helperLabel}
          </button>
        </div>
      </div>

      <div className="mb-4">
        <div className="flex items-center gap-3 mb-2">
          <label className="flex items-center gap-1.5 text-xs text-[#525252] cursor-pointer">
            <input
              type="radio"
              checked={!useQuoteRef}
              onChange={() => {
                setUseQuoteRef(false);
                updateField('quote_id', undefined);
              }}
              className="text-[#8a6a2f]"
            />
            Inline rate
          </label>
          <label className="flex items-center gap-1.5 text-xs text-[#525252] cursor-pointer">
            <input
              type="radio"
              checked={useQuoteRef}
              onChange={() => setUseQuoteRef(true)}
              className="text-[#8a6a2f]"
            />
            Quote reference
          </label>
        </div>
        {useQuoteRef ? (
          <div>
            <label className={labelClass}>Quote ID</label>
            <select
              value={p.quote_id || ''}
              onChange={e => {
                updateField('quote_id', e.target.value || undefined);
                updateField('quote_value', undefined);
              }}
              className={inputClass}
            >
              <option value="">— Select quote —</option>
              {quotes.map((quote) => (
                <option key={quote.id} value={quote.id}>
                  {quote.id} — {(quote.value * 100).toFixed(3)}%
                  {quote.label ? ` (${quote.label})` : ''}
                </option>
              ))}
            </select>
            {quotes.length === 0 && (
              <p className="text-xs text-amber-600 mt-1">No quotes defined. Add quotes in Market Data first.</p>
            )}
          </div>
        ) : (
          <div>
            <label className={labelClass}>Rate (%)</label>
            <input
              type="number"
              step="0.001"
              value={((p.quote_value ?? 0) * 100).toFixed(3)}
              onChange={e => {
                const value = parseFloat(e.target.value);
                updateField('quote_value', Number.isFinite(value) ? value / 100 : undefined);
                updateField('quote_id', undefined);
              }}
              className={inputClass}
            />
          </div>
        )}
      </div>

      <div className="mb-4">
        <label className={labelClass}>Maturity Definition</label>
        <div className="flex gap-1.5">
          <button
            type="button"
            onClick={() => {
              setMaturityMode('tenor');
              updateField('start_date', undefined);
              updateField('end_date', undefined);
              updateField('tenor', p.tenor || { n: 5, unit: 'Years' });
            }}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
              maturityMode === 'tenor' ? 'bg-[#0a0a0a] text-white' : 'bg-white text-[#525252] border border-[#d4d4d4] hover:bg-[#e5e5e5]'
            }`}
          >
            Tenor
          </button>
          <button
            type="button"
            onClick={() => {
              setMaturityMode('dates');
              updateField('tenor', undefined);
              updateField('start_date', p.start_date || new Date().toISOString().split('T')[0]);
              updateField('end_date', p.end_date || new Date(Date.now() + 5 * 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0]);
            }}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
              maturityMode === 'dates' ? 'bg-[#0a0a0a] text-white' : 'bg-white text-[#525252] border border-[#d4d4d4] hover:bg-[#e5e5e5]'
            }`}
          >
            Explicit Dates
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {maturityMode === 'tenor' ? (
          <div>
            <label className={labelClass}>Tenor</label>
            <div className="flex gap-2">
              <input
                type="number"
                value={p.tenor?.n ?? 5}
                onChange={e => updateField('tenor', { ...(p.tenor || { n: 5, unit: 'Years' }), n: parseInt(e.target.value, 10) || 0 })}
                className={`${inputClass} w-20`}
              />
              <select
                value={p.tenor?.unit || 'Years'}
                onChange={e => updateField('tenor', { ...(p.tenor || { n: 5, unit: 'Years' }), unit: e.target.value as any })}
                className={inputClass}
              >
                {TIME_UNITS.map(unit => <option key={unit}>{unit}</option>)}
              </select>
            </div>
          </div>
        ) : (
          <>
            <div>
              <label className={labelClass}>Start Date</label>
              <input type="date" value={p.start_date || ''} onChange={e => updateField('start_date', e.target.value || undefined)} className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>End Date</label>
              <input type="date" value={p.end_date || ''} onChange={e => updateField('end_date', e.target.value || undefined)} className={inputClass} />
            </div>
          </>
        )}

        <div>
          <label className={labelClass}>Swap Observation Lag</label>
          <div className="flex gap-2">
            <input
              type="number"
              value={p.swap_observation_lag.n}
              onChange={e => updateField('swap_observation_lag', { ...p.swap_observation_lag, n: parseInt(e.target.value, 10) || 0 })}
              className={`${inputClass} w-20`}
            />
            <select
              value={p.swap_observation_lag.unit}
              onChange={e => updateField('swap_observation_lag', { ...p.swap_observation_lag, unit: e.target.value as any })}
              className={inputClass}
            >
              {TIME_UNITS.map(unit => <option key={unit}>{unit}</option>)}
            </select>
          </div>
        </div>

        <div>
          <label className={labelClass}>Calendar</label>
          <select value={p.calendar} onChange={e => updateField('calendar', e.target.value as any)} className={inputClass}>
            {CALENDARS.map(calendar => <option key={calendar}>{calendar}</option>)}
          </select>
        </div>

        <div>
          <label className={labelClass}>Payment Convention</label>
          <select value={p.payment_convention} onChange={e => updateField('payment_convention', e.target.value as any)} className={inputClass}>
            {BUSINESS_DAY_CONVENTIONS.map(convention => <option key={convention}>{convention}</option>)}
          </select>
        </div>

        <div>
          <label className={labelClass}>Helper Day Counter</label>
          <select value={p.day_counter} onChange={e => updateField('day_counter', e.target.value as any)} className={inputClass}>
            {DAY_COUNTERS.map(counter => <option key={counter}>{counter}</option>)}
          </select>
        </div>

        <div>
          <label className={labelClass}>Observation Interpolation</label>
          <select value={p.observation_interpolation} onChange={e => updateField('observation_interpolation', e.target.value as any)} className={inputClass}>
            {CPI_INTERPOLATION_TYPES.map(interpolation => <option key={interpolation}>{interpolation}</option>)}
          </select>
        </div>

        {kind === 'YoYInflation' && (
          <div className="col-span-2">
            <label className={labelClass}>Nominal Curve ID</label>
            <select
              value={p.nominal_curve_id || defaultDiscountCurveId || ''}
              onChange={e => updateField('nominal_curve_id', e.target.value || undefined)}
              className={inputClass}
            >
              <option value="">— Select nominal curve —</option>
              {availableDiscountCurves.map(curve => (
                <option key={curve.id} value={curve.id}>{curve.name}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div className="flex gap-2 mt-5">
        <button onClick={onCancel} className="flex-1 px-4 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">
          Cancel
        </button>
        <button onClick={handleSave} className="flex-1 px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626]">
          {point ? 'Update' : 'Add Helper'}
        </button>
      </div>
    </div>
  );
}
