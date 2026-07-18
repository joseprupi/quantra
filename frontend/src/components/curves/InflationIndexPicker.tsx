import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BUSINESS_DAY_CONVENTIONS,
  CALENDARS,
  DAY_COUNTERS,
  FREQUENCIES,
  TIME_UNITS,
  type InflationCurveKind,
  type InflationIndexSpec,
} from '../../lib/types';
import {
  indexStore,
  storedToInflationIndexSpec,
  type StoredIndexSpec,
} from '../../lib/storage/indices';

type PickerMode = 'saved' | 'inline';

interface InflationIndexPickerProps {
  value: InflationIndexSpec;
  currency: string;
  kind: InflationCurveKind;
  onChange: (spec: InflationIndexSpec) => void;
  label?: string;
  className?: string;
  helperText?: string;
}

function normalizeInflationSpec(spec: InflationIndexSpec, currency: string, kind: InflationCurveKind): InflationIndexSpec {
  return {
    ...spec,
    currency,
    kind,
    frequency: spec.frequency || 'Monthly',
    calendar: spec.calendar || 'TARGET',
    day_counter: spec.day_counter || 'Actual365Fixed',
    availability_lag: {
      n: spec.availability_lag?.n ?? 2,
      unit: spec.availability_lag?.unit || 'Months',
    },
    observation_lag: {
      n: spec.observation_lag?.n ?? 3,
      unit: spec.observation_lag?.unit || 'Months',
    },
    family_name: spec.family_name || spec.id,
    interpolated: spec.interpolated ?? true,
    revised: spec.revised ?? false,
  };
}

function specsMatch(left: InflationIndexSpec, right: InflationIndexSpec): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export default function InflationIndexPicker({
  value,
  currency,
  kind,
  onChange,
  label,
  className,
  helperText,
}: InflationIndexPickerProps) {
  const navigate = useNavigate();
  const [savedIndices, setSavedIndices] = useState<StoredIndexSpec[]>([]);
  const [mode, setMode] = useState<PickerMode>('inline');

  useEffect(() => {
    indexStore.getAll().then(setSavedIndices).catch(() => setSavedIndices([]));
  }, []);

  const normalizedValue = useMemo(
    () => normalizeInflationSpec(value, currency, kind),
    [value, currency, kind],
  );

  const savedDefs = useMemo(
    () => savedIndices
      .map(storedToInflationIndexSpec)
      .filter((spec): spec is InflationIndexSpec => !!spec)
      .map((spec) => normalizeInflationSpec(spec, currency, kind)),
    [savedIndices, currency, kind],
  );

  useEffect(() => {
    const savedMatch = savedDefs.find((spec) => spec.id === normalizedValue.id && specsMatch(spec, normalizedValue));
    if (savedMatch) {
      setMode('saved');
    }
  }, [normalizedValue, savedDefs]);

  useEffect(() => {
    if (!specsMatch(normalizedValue, value)) {
      onChange(normalizedValue);
    }
  }, [normalizedValue, onChange, value]);

  const inputClass = 'w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors';
  const labelClass = 'block text-xs text-[#737373] mb-1.5 font-medium';
  const tabClass = (active: boolean) =>
    `px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${active ? 'bg-[#0a0a0a] text-white' : 'text-[#737373] hover:bg-[#f5f5f5]'}`;

  const update = (patch: Partial<InflationIndexSpec>) => {
    onChange(normalizeInflationSpec({ ...normalizedValue, ...patch }, currency, kind));
  };

  const handleSavedSelect = (id: string) => {
    const selected = savedDefs.find((spec) => spec.id === id);
    if (selected) {
      onChange(selected);
    }
  };

  const resolvedSaved = savedDefs.find((spec) => spec.id === normalizedValue.id && specsMatch(spec, normalizedValue));

  return (
    <div className={className}>
      {label && <label className={labelClass}>{label}</label>}

      <div className="flex gap-1 mb-3 items-center">
        <button type="button" onClick={() => setMode('saved')} className={tabClass(mode === 'saved')}>
          Saved{savedDefs.length > 0 ? ` (${savedDefs.length})` : ''}
        </button>
        <button type="button" onClick={() => setMode('inline')} className={tabClass(mode === 'inline')}>
          Inline
        </button>
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => navigate('/indices')}
          className="text-[10px] text-[#8a6a2f] hover:underline"
        >
          Manage in Indices →
        </button>
      </div>

      {mode === 'saved' ? (
        savedDefs.length === 0 ? (
          <p className="text-xs text-amber-600 py-2">
            No saved inflation indices yet. Add one in Market Data -&gt; Indices, or use an inline definition.
          </p>
        ) : (
          <select
            value={resolvedSaved?.id || ''}
            onChange={(e) => handleSavedSelect(e.target.value)}
            className={inputClass}
          >
            <option value="">— Select saved inflation index —</option>
            {savedDefs.map((spec) => (
              <option key={spec.id} value={spec.id}>
                {`${spec.id} — ${spec.family_name} (${spec.kind}, ${spec.currency})`}
              </option>
            ))}
          </select>
        )
      ) : (
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <label className={labelClass}>Index ID</label>
            <input value={normalizedValue.id} onChange={e => update({ id: e.target.value })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Family Name</label>
            <input value={normalizedValue.family_name} onChange={e => update({ family_name: e.target.value })} className={inputClass} />
          </div>
          <div>
            <label className={labelClass}>Currency</label>
            <input value={currency} disabled className={`${inputClass} bg-[#f5f5f5] text-[#737373]`} />
          </div>
          <div>
            <label className={labelClass}>Frequency</label>
            <select value={normalizedValue.frequency} onChange={e => update({ frequency: e.target.value as InflationIndexSpec['frequency'] })} className={inputClass}>
              {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Index Calendar</label>
            <select value={normalizedValue.calendar} onChange={e => update({ calendar: e.target.value as InflationIndexSpec['calendar'] })} className={inputClass}>
              {CALENDARS.map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Index Day Counter</label>
            <select value={normalizedValue.day_counter} onChange={e => update({ day_counter: e.target.value as InflationIndexSpec['day_counter'] })} className={inputClass}>
              {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
            </select>
          </div>
          <div>
            <label className={labelClass}>Availability Lag</label>
            <div className="flex gap-2">
              <input
                type="number"
                value={normalizedValue.availability_lag.n}
                onChange={e => update({ availability_lag: { ...normalizedValue.availability_lag, n: parseInt(e.target.value, 10) || 0 } })}
                className={`${inputClass} w-24`}
              />
              <select
                value={normalizedValue.availability_lag.unit}
                onChange={e => update({ availability_lag: { ...normalizedValue.availability_lag, unit: e.target.value as InflationIndexSpec['availability_lag']['unit'] } })}
                className={inputClass}
              >
                {TIME_UNITS.map(unit => <option key={unit}>{unit}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className={labelClass}>Observation Lag</label>
            <div className="flex gap-2">
              <input
                type="number"
                value={normalizedValue.observation_lag.n}
                onChange={e => update({ observation_lag: { ...normalizedValue.observation_lag, n: parseInt(e.target.value, 10) || 0 } })}
                className={`${inputClass} w-24`}
              />
              <select
                value={normalizedValue.observation_lag.unit}
                onChange={e => update({ observation_lag: { ...normalizedValue.observation_lag, unit: e.target.value as InflationIndexSpec['observation_lag']['unit'] } })}
                className={inputClass}
              >
                {TIME_UNITS.map(unit => <option key={unit}>{unit}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className={labelClass}>Business Day Convention</label>
            <select
              value="ModifiedFollowing"
              disabled
              className={`${inputClass} bg-[#f5f5f5] text-[#737373]`}
            >
              {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
            </select>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-[#525252]">
              <input
                type="checkbox"
                checked={normalizedValue.interpolated}
                onChange={e => update({ interpolated: e.target.checked })}
                className="rounded border-[#d4d4d4]"
              />
              Interpolated
            </label>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-[#525252]">
              <input
                type="checkbox"
                checked={normalizedValue.revised}
                onChange={e => update({ revised: e.target.checked })}
                className="rounded border-[#d4d4d4]"
              />
              Revised
            </label>
          </div>
          {kind === 'YoYInflation' && (
            <div className="sm:col-span-2">
              <label className={labelClass}>Underlying Zero Index ID</label>
              <input
                value={normalizedValue.underlying_zero_index_id || ''}
                onChange={e => update({ underlying_zero_index_id: e.target.value || undefined })}
                placeholder="Optional: linked zero inflation index"
                className={inputClass}
              />
            </div>
          )}
        </div>
      )}

      <div className="mt-2 flex items-center gap-1.5">
        <span className={`inline-block px-1.5 py-0.5 text-[10px] font-medium rounded ${mode === 'saved' ? 'bg-blue-50 text-blue-700' : 'bg-[#f5f5f5] text-[#737373]'}`}>
          {mode === 'saved' ? 'Saved' : 'Inline'}
        </span>
        <span className="text-xs text-[#525252]">{normalizedValue.id || 'No index selected'}</span>
        {normalizedValue.id && (
          <span className="text-xs text-[#a3a3a3]">
            {normalizedValue.kind} · {normalizedValue.currency}
          </span>
        )}
      </div>

      {helperText && <p className="text-xs text-[#a3a3a3] mt-2">{helperText}</p>}
    </div>
  );
}
