// IndexPicker — unified component for selecting a floating-rate index
// Supports three modes: Preset (well-known indices), Saved (from index registry), Custom (full form)
import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  IndexDef, IndexRef, IndexType,
  DAY_COUNTERS, BUSINESS_DAY_CONVENTIONS, CALENDARS, TIME_UNITS, CURRENCIES,
  IBOR_FAMILIES, OVERNIGHT_INDICES,
} from '../../lib/types';
import { indexStore, StoredIndexSpec, storedToRateIndexDef } from '../../lib/storage/indices';

// ============================================================================
// Props
// ============================================================================

interface IndexPickerProps {
  /** Current index_ref (the selected index id) */
  value?: IndexRef;
  /** Called with the new IndexRef when the user picks/changes an index */
  onChange: (ref: IndexRef, def: IndexDef) => void;
  /** Filter to show only IBOR or only OVERNIGHT indices */
  filter?: IndexType;
  /** Label above the picker */
  label?: string;
  /** CSS classes for the wrapper */
  className?: string;
}

// ============================================================================
// Helpers
// ============================================================================

type PickerMode = 'saved' | 'custom';

// A fresh, valid Custom-form definition. The `name` (QL index family) defaults
// to a valid family for the picker's type so the constrained family dropdown
// always starts on a real family rather than an empty/invalid value.
function blankCustomDef(filter?: IndexType): IndexDef {
  const isOvernight = filter === 'Overnight';
  return {
    id: '', name: isOvernight ? 'ESTR' : 'Euribor',
    index_type: filter || 'Ibor', currency: 'EUR',
    tenor_number: 6, tenor_time_unit: 'Months',
    fixing_days: 2, calendar: 'TARGET', day_counter: 'Actual360',
    business_day_convention: 'ModifiedFollowing', end_of_month: false,
  };
}

function indexLabel(def: IndexDef): string {
  if (def.index_type === 'Ibor') {
    const n = def.tenor?.n ?? def.tenor_number;
    const unit = def.tenor?.unit ?? def.tenor_time_unit;
    return `${def.id} — ${def.currency} ${n}${unit?.[0] || ''} IBOR`;
  }
  return `${def.id} — ${def.currency} Overnight`;
}

// ============================================================================
// Component
// ============================================================================

export default function IndexPicker({ value, onChange, filter, label, className }: IndexPickerProps) {
  const navigate = useNavigate();
  const [mode, setMode] = useState<PickerMode>('saved');
  const [savedIndices, setSavedIndices] = useState<StoredIndexSpec[]>([]);

  // Custom index form state. The Custom tab always *defines a new* index — it
  // is not an editor for an already-saved one (edit those via the Saved tab /
  // Manage page). The form resets after a successful apply so a subsequent
  // edit can never silently mutate the entry that was just saved.
  const [customDef, setCustomDef] = useState<IndexDef>(() => blankCustomDef(filter));

  // Load saved indices
  useEffect(() => {
    indexStore.getAll().then(setSavedIndices);
  }, []);

  const savedDefs = useMemo(
    () => savedIndices
      .map(storedToRateIndexDef)
      .filter((def): def is IndexDef => !!def)
      .filter(d => !filter || d.index_type === filter),
    [savedIndices, filter],
  );

  const currentId = value?.id || '';

  // Determine which mode the current value belongs to
  useEffect(() => {
    if (!currentId) return;
    if (savedIndices.find(s => s.id === currentId)) {
      setMode('saved');
    }
  }, [currentId, savedIndices]);

  const inputClass = "w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors";
  const labelClass = "block text-xs text-[#737373] mb-1 font-medium";
  const tabClass = (active: boolean) =>
    `px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
      active ? 'bg-[#0a0a0a] text-white' : 'text-[#737373] hover:bg-[#f5f5f5]'
    }`;

  const handleSavedSelect = (id: string) => {
    const stored = savedIndices.find(s => s.id === id);
    if (stored) {
      const def = storedToRateIndexDef(stored);
      if (def) {
        onChange({ id }, def);
      }
    }
  };

  const handleCustomApply = async () => {
    if (!customDef.id.trim()) return;
    const def = customDef;
    // Persist the definition, then refresh our local saved list FROM the store
    // so the just-applied index resolves immediately — no false "not found in
    // saved indices" badge while it is in fact saved+selected.
    await indexStore.save(def as unknown as Parameters<typeof indexStore.save>[0]);
    setSavedIndices(await indexStore.getAll());
    onChange({ id: def.id }, def);
    // Reset the form so the Custom tab is ready to define ANOTHER new index —
    // a later name/field edit then starts from a blank definition instead of
    // aliasing (and silently mutating) the entry we just saved.
    setCustomDef(blankCustomDef(filter));
  };

  const updateCustom = (field: keyof IndexDef, val: unknown) => {
    setCustomDef(prev => ({ ...prev, [field]: val }));
  };

  // Resolve current selection for display
  const resolvedDef = currentId ? savedDefs.find(d => d.id === currentId) : undefined;
  const hasCurrentInSaved = currentId ? savedDefs.some(d => d.id === currentId) : false;

  return (
    <div className={className}>
      {label && <label className={labelClass}>{label}</label>}

      {/* Mode tabs */}
      <div className="flex gap-1 mb-2 items-center">
        <button onClick={() => setMode('saved')} className={tabClass(mode === 'saved')}>
          Saved{savedDefs.length > 0 && ` (${savedDefs.length})`}
        </button>
        <button onClick={() => setMode('custom')} className={tabClass(mode === 'custom')}>Custom</button>
        <span className="flex-1" />
        <button
          onClick={() => navigate('/indices')}
          className="text-[10px] text-[#8a6a2f] hover:underline"
        >
          Manage →
        </button>
      </div>

      {/* Saved mode */}
      {mode === 'saved' && (
        savedDefs.length === 0 ? (
          <p className="text-xs text-amber-600 py-2">
            No saved indices. Add indices in Market Data → Indices.
          </p>
        ) : (
          <select
            value={currentId}
            onChange={e => handleSavedSelect(e.target.value)}
            className={inputClass}
          >
            <option value="">— Select saved index —</option>
            {currentId && !hasCurrentInSaved && (
              <option value={currentId}>{`${currentId} — currently selected (not in saved indices)`}</option>
            )}
            {savedDefs.map(d => (
              <option key={d.id} value={d.id}>{indexLabel(d)}</option>
            ))}
          </select>
        )
      )}

      {/* Custom mode */}
      {mode === 'custom' && (
        <div className="space-y-2 p-3 bg-white border border-[#e5e5e5] rounded-lg">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className={labelClass}>Index ID</label>
              <input
                type="text" placeholder="e.g. MY_IBOR_3M"
                value={customDef.id}
                onChange={e => {
                  const val = e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '');
                  updateCustom('id', val);
                }}
                className={inputClass}
              />
            </div>
            <div>
              {/* QL index family — constrained to the same valid-family list the
                  Market Data → Indices page uses, so both screens offer
                  the identical set and a user can't type an unrecognised family. */}
              <label className={labelClass}>Family (QL)</label>
              <select value={customDef.name} onChange={e => updateCustom('name', e.target.value)} className={inputClass}>
                {(customDef.index_type === 'Overnight' ? OVERNIGHT_INDICES : IBOR_FAMILIES).map(f => (
                  <option key={f} value={f}>{f}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelClass}>Type</label>
              <select
                value={customDef.index_type}
                onChange={e => {
                  const nextType = e.target.value as IndexType;
                  // Reset the family to a valid one for the new type so the
                  // constrained dropdown never carries a family from the other
                  // index kind (e.g. an IBOR family left on an Overnight index).
                  setCustomDef(prev => ({
                    ...prev,
                    index_type: nextType,
                    name: nextType === 'Overnight' ? 'ESTR' : 'Euribor',
                  }));
                }}
                className={inputClass}
              >
                {(!filter || filter === 'Ibor') && <option value="Ibor">IBOR</option>}
                {(!filter || filter === 'Overnight') && <option value="Overnight">Overnight</option>}
              </select>
            </div>
            <div>
              <label className={labelClass}>Currency</label>
              <select value={customDef.currency} onChange={e => updateCustom('currency', e.target.value)} className={inputClass}>
                {CURRENCIES.map(c => <option key={c}>{c}</option>)}
              </select>
            </div>
            {customDef.index_type === 'Ibor' && (
              <>
                <div>
                  <label className={labelClass}>Tenor</label>
                  <div className="flex gap-1">
                    <input type="number" min={1} value={customDef.tenor_number ?? 6}
                      onChange={e => updateCustom('tenor_number', parseInt(e.target.value) || 1)}
                      className={`${inputClass} w-16`} />
                    <select value={customDef.tenor_time_unit || 'Months'} onChange={e => updateCustom('tenor_time_unit', e.target.value)} className={inputClass}>
                      {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
                    </select>
                  </div>
                </div>
                <div>
                  <label className={labelClass}>Convention</label>
                  <select value={customDef.business_day_convention || 'ModifiedFollowing'} onChange={e => updateCustom('business_day_convention', e.target.value)} className={inputClass}>
                    {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
                  </select>
                </div>
              </>
            )}
            <div>
              <label className={labelClass}>Fixing Days</label>
              <input type="number" min={0} value={customDef.fixing_days}
                onChange={e => updateCustom('fixing_days', parseInt(e.target.value) || 0)}
                className={inputClass} />
            </div>
            <div>
              <label className={labelClass}>Calendar</label>
              <select value={customDef.calendar} onChange={e => updateCustom('calendar', e.target.value)} className={inputClass}>
                {CALENDARS.map(c => <option key={c}>{c}</option>)}
              </select>
            </div>
            <div>
              <label className={labelClass}>Day Counter</label>
              <select value={customDef.day_counter} onChange={e => updateCustom('day_counter', e.target.value)} className={inputClass}>
                {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
              </select>
            </div>
            {customDef.index_type === 'Ibor' && (
              <div className="flex items-center gap-2 pt-4">
                <input type="checkbox" checked={customDef.end_of_month ?? false}
                  onChange={e => updateCustom('end_of_month', e.target.checked)}
                  className="rounded" />
                <span className="text-xs text-[#525252]">End of month</span>
              </div>
            )}
          </div>
          <button
            onClick={handleCustomApply}
            disabled={!customDef.id.trim()}
            className="w-full mt-2 px-3 py-1.5 text-xs font-medium bg-[#0a0a0a] text-white rounded-lg hover:bg-[#262626] disabled:opacity-40"
          >
            Apply Custom Index
          </button>
        </div>
      )}

      {/* Current selection badge */}
      {resolvedDef && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className={`inline-block px-1.5 py-0.5 text-[10px] font-medium rounded ${
            resolvedDef.index_type === 'Ibor' ? 'bg-blue-50 text-blue-700' : 'bg-emerald-50 text-emerald-700'
          }`}>
            {resolvedDef.index_type}
          </span>
          <span className="text-xs text-[#525252]">{resolvedDef.id}</span>
          {resolvedDef.index_type === 'Ibor' && (
            <span className="text-xs text-[#a3a3a3]">
              {(resolvedDef.tenor?.n ?? resolvedDef.tenor_number)}{(resolvedDef.tenor?.unit ?? resolvedDef.tenor_time_unit)?.[0]} · {resolvedDef.calendar} · {resolvedDef.day_counter}
            </span>
          )}
          {resolvedDef.index_type === 'Overnight' && (
            <span className="text-xs text-[#a3a3a3]">
              {resolvedDef.calendar} · {resolvedDef.day_counter}
            </span>
          )}
        </div>
      )}
      {currentId && !resolvedDef && (
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className="inline-block px-1.5 py-0.5 text-[10px] font-medium rounded bg-amber-50 text-amber-700">
            Unresolved
          </span>
          <span className="text-xs text-[#525252]">{currentId}</span>
          <span className="text-xs text-[#a3a3a3]">Not found in saved indices. Add it in Market Data → Indices.</span>
        </div>
      )}
    </div>
  );
}
