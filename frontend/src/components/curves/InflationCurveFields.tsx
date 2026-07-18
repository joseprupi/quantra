import { useEffect } from 'react';
import {
  BUSINESS_DAY_CONVENTIONS,
  CALENDARS,
  DAY_COUNTERS,
  INTERPOLATORS,
  type Curve,
  type InflationCurveConfig,
  type InflationCurveKind,
} from '../../lib/types';
import { syncInflationConfig } from '../../lib/inflation-curves';
import InflationIndexPicker from './InflationIndexPicker';

interface Props {
  currency: string;
  config: InflationCurveConfig;
  discountCurveId?: string;
  availableDiscountCurves?: Curve[];
  onChange: (config: InflationCurveConfig) => void;
  onChangeDiscountCurveId?: (curveId?: string) => void;
}

export default function InflationCurveFields({
  currency,
  config,
  discountCurveId,
  availableDiscountCurves = [],
  onChange,
  onChangeDiscountCurveId,
}: Props) {
  useEffect(() => {
    onChange(syncInflationConfig(config, currency));
  }, [currency]);

  const inputClass = 'w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors';
  const labelClass = 'block text-xs text-[#737373] mb-1.5 font-medium';

  const updateConfig = (patch: Partial<InflationCurveConfig>) => {
    onChange(syncInflationConfig({ ...config, ...patch }, currency));
  };

  const updateIndex = (patch: Partial<InflationCurveConfig['index']>) => {
    updateConfig({
      index: {
        ...config.index,
        ...patch,
      },
    });
  };

  const setKind = (kind: InflationCurveKind) => {
    updateConfig({
      kind,
      index: {
        ...config.index,
        kind,
      },
      points: config.points.map((wrapper) => ({
        point_type: kind === 'YoYInflation' ? 'YearOnYearInflationSwapHelper' : 'ZeroCouponInflationSwapHelper',
        point: {
          ...wrapper.point,
          ...(kind === 'YoYInflation'
            ? { nominal_curve_id: wrapper.point.nominal_curve_id || discountCurveId || '' }
            : { nominal_curve_id: undefined }),
        },
      })),
    });
  };

  return (
    <div className="space-y-6">
      <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
        <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Inflation Curve Settings</h2>
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <label className={labelClass}>Inflation Curve Kind</label>
            <select value={config.kind} onChange={e => setKind(e.target.value as InflationCurveKind)} className={inputClass}>
              <option value="ZeroInflation">Zero Inflation</option>
              <option value="YoYInflation">YoY Inflation</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>Interpolator</label>
            <select value={config.interpolator} onChange={e => updateConfig({ interpolator: e.target.value as InflationCurveConfig['interpolator'] })} className={inputClass}>
              {INTERPOLATORS.map(i => <option key={i}>{i}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Bootstrap Accuracy</label>
            <input
              type="number"
              step="0.000000000001"
              value={config.bootstrap_accuracy ?? 1.0e-12}
              onChange={e => updateConfig({ bootstrap_accuracy: parseFloat(e.target.value) || 1.0e-12 })}
              className={inputClass}
            />
          </div>
          <div>
            <label className={labelClass}>Curve Calendar</label>
            <select value={config.calendar} onChange={e => updateConfig({ calendar: e.target.value as InflationCurveConfig['calendar'] })} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Curve Day Counter</label>
            <select value={config.day_counter} onChange={e => updateConfig({ day_counter: e.target.value as InflationCurveConfig['day_counter'] })} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Business Day Convention</label>
            <select value={config.business_day_convention} onChange={e => updateConfig({ business_day_convention: e.target.value as InflationCurveConfig['business_day_convention'] })} className={inputClass}>
              {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
            </select>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-[#525252]">
              <input
                type="checkbox"
                checked={config.allow_extrapolation}
                onChange={e => updateConfig({ allow_extrapolation: e.target.checked })}
                className="rounded border-[#d4d4d4]"
              />
              Allow extrapolation
            </label>
          </div>
          {onChangeDiscountCurveId && (
            <div className="sm:col-span-2">
              <label className={labelClass}>Nominal Discount Curve</label>
              <select
                value={discountCurveId || ''}
                onChange={e => onChangeDiscountCurveId(e.target.value || undefined)}
                className={inputClass}
              >
                <option value="">None</option>
                {availableDiscountCurves.map(curve => (
                  <option key={curve.id} value={curve.id}>{curve.name}</option>
                ))}
              </select>
              <p className="text-[10px] text-[#a3a3a3] mt-1">
                Reused as the default nominal curve for YoY helpers.
              </p>
            </div>
          )}
        </div>
      </div>

      <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
        <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Inflation Index</h2>
        <InflationIndexPicker
          value={config.index}
          currency={currency}
          kind={config.kind}
          onChange={(index) => updateIndex(index)}
          helperText="Use a saved market-data inflation index for consistency across products, or keep the index inline on this curve."
        />
        <p className="text-xs text-[#a3a3a3] mt-4">
          Helper instruments are managed in the section below, using the same list-and-editor workflow as yield curves.
        </p>
      </div>
    </div>
  );
}
