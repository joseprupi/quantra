// Indices page — rate and inflation index registry
import { useState, useEffect, useRef } from 'react';
import Header from '../../components/Header';
import {
  DAY_COUNTERS, CALENDARS, BUSINESS_DAY_CONVENTIONS, TIME_UNITS,
  OVERNIGHT_INDICES, IBOR_FAMILIES, CURRENCIES, FREQUENCIES,
} from '../../lib/types';
import {
  StoredIndexSpec, indexStore, generateIndexId,
  exportIndices, importIndices,
} from '../../lib/storage/indices';
import { ExportIcon, ImportIcon, listStyles, NewIcon } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

function defaultForm(): Partial<StoredIndexSpec> {
  return {
    type: 'IBOR',
    family: 'Euribor',
    tenor_number: 6,
    tenor_time_unit: 'Months',
    fixing_days: 2,
    calendar: 'TARGET',
    business_day_convention: 'ModifiedFollowing',
    day_counter: 'Actual360',
    end_of_month: false,
    overnight_name: 'ESTR',
    family_name: 'EU HICP',
    currency: 'EUR',
    frequency: 'Monthly',
    availability_lag: { n: 2, unit: 'Months' },
    observation_lag: { n: 3, unit: 'Months' },
    interpolated: true,
    revised: false,
    kind: 'ZeroInflation',
    id: '',
    description: '',
    fixings: [],
  };
}

export default function IndicesPage() {
  const ui = entityUi.index;
  const [indices, setIndices] = useState<StoredIndexSpec[]>([]);
  const [showEditor, setShowEditor] = useState(false);
  const [editing, setEditing] = useState<StoredIndexSpec | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => setIndices(await indexStore.getAll());
  useEffect(() => { load(); }, []);

  // Editor state
  const [form, setForm] = useState<Partial<StoredIndexSpec>>(defaultForm());
  const [fixingDate, setFixingDate] = useState('');
  const [fixingValue, setFixingValue] = useState('');

  const openEditor = (idx?: StoredIndexSpec) => {
    if (idx) {
      setEditing(idx);
      setForm({ ...defaultForm(), ...idx });
      setFixingDate('');
      setFixingValue('');
    } else {
      setEditing(null);
      setForm(defaultForm());
      setFixingDate('');
      setFixingValue('');
    }
    setShowEditor(true);
  };

  const addFixing = () => {
    if (!fixingDate) return;
    const value = parseFloat(fixingValue);
    if (Number.isNaN(value)) return;
    const next = [...(form.fixings || []).filter(f => f.date !== fixingDate), { date: fixingDate, value }]
      .sort((a, b) => a.date.localeCompare(b.date));
    setForm({ ...form, fixings: next });
    setFixingValue('');
  };

  const removeFixing = (date: string) => {
    const next = (form.fixings || []).filter(f => f.date !== date);
    setForm({ ...form, fixings: next });
  };

  const handleSave = async () => {
    const id = form.id?.trim() || generateIndexId();
    // Check uniqueness
    if (!editing && indices.some(i => i.id === id)) {
      setError(`Index ID "${id}" already exists`);
      return;
    }
    const now = new Date().toISOString();
    const spec: StoredIndexSpec = {
      id,
      type: form.type || 'IBOR',
      family: form.type === 'IBOR' ? form.family : undefined,
      tenor_number: form.type === 'IBOR' ? form.tenor_number : undefined,
      tenor_time_unit: form.type === 'IBOR' ? form.tenor_time_unit : undefined,
      overnight_name: form.type === 'Overnight' ? form.overnight_name : undefined,
      family_name: form.type === 'Inflation' ? form.family_name : undefined,
      currency: form.type === 'Inflation' ? (form.currency || 'EUR') : undefined,
      frequency: form.type === 'Inflation' ? (form.frequency || 'Monthly') : undefined,
      availability_lag: form.type === 'Inflation'
        ? {
            n: form.availability_lag?.n ?? 2,
            unit: form.availability_lag?.unit || 'Months',
          }
        : undefined,
      observation_lag: form.type === 'Inflation'
        ? {
            n: form.observation_lag?.n ?? 3,
            unit: form.observation_lag?.unit || 'Months',
          }
        : undefined,
      interpolated: form.type === 'Inflation' ? (form.interpolated ?? true) : undefined,
      revised: form.type === 'Inflation' ? (form.revised ?? false) : undefined,
      kind: form.type === 'Inflation' ? (form.kind || 'ZeroInflation') : undefined,
      underlying_zero_index_id: form.type === 'Inflation' ? form.underlying_zero_index_id : undefined,
      fixing_days: form.type === 'Inflation' ? 0 : (form.fixing_days ?? 2),
      calendar: form.calendar || 'TARGET',
      business_day_convention: form.type === 'Inflation' ? undefined : form.business_day_convention,
      day_counter: form.day_counter || (form.type === 'Inflation' ? 'Actual365Fixed' : 'Actual360'),
      end_of_month: form.type === 'IBOR' ? form.end_of_month : undefined,
      description: form.description,
      fixings: (form.fixings || []).filter(f => f.date && Number.isFinite(f.value)),
      createdAt: editing?.createdAt || now,
      updatedAt: now,
    };
    await indexStore.save(spec);
    await load();
    setShowEditor(false);
    setEditing(null);
    setSuccess(editing ? 'Index updated' : 'Index created');
    setTimeout(() => setSuccess(null), 2000);
  };

  const handleDelete = async (id: string) => {
    if (!confirm(`Delete index "${id}"?`)) return;
    await indexStore.delete(id);
    await load();
    setSuccess('Index deleted');
    setTimeout(() => setSuccess(null), 2000);
  };

  const handleExport = () => {
    if (indices.length === 0) { setError('No indices to export'); return; }
    exportIndices(indices);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importIndices(file);
      await load();
      setSuccess(`Imported ${imported.length} index(es)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch { setError('Invalid JSON'); }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const inputClass = formStyles.input;
  const labelClass = formStyles.compactLabel;

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="IBOR, Overnight, and Inflation index definitions for pricing and curve helpers"
          actions={
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
              <ImportIcon />
              Import
            </button>
            {indices.length > 0 && (
              <button onClick={handleExport} className={listStyles.secondaryButton}>
                <ExportIcon />
                Export All
              </button>
            )}
            <button onClick={() => openEditor()} className={listStyles.primaryNewButton}>
              <NewIcon />
              {getNewLabel(ui)}
            </button>
            </div>
          }
        />

        <div className="mb-4 bg-[#fafafa] border border-[#e5e5e5] rounded-lg p-3">
          <p className="text-xs text-[#737373]">
            Indices you create here are saved on the Quantra server (owner-scoped) and are visible from any browser you
            sign in to. Use Import/Export to back them up or move them as files.
          </p>
        </div>

        {error && <FeedbackBanner tone="error" message={error} onDismiss={() => setError(null)} className="mb-4 p-3" />}
        {success && <FeedbackBanner tone="success" message={success} className="mb-4 p-3" />}

        {/* Editor modal */}
        {showEditor && (
          <div className="mb-6 bg-white border border-[#e5e5e5] rounded-xl p-5">
            <h3 className="text-sm font-semibold text-[#0a0a0a] mb-4">{editing ? 'Edit Index' : 'New Index'}</h3>
            <div className="grid sm:grid-cols-3 gap-3">
              <div>
                <label className={labelClass}>Index ID *</label>
                <input type="text" value={form.id || ''} onChange={e => setForm({ ...form, id: e.target.value })} placeholder="e.g. EURIBOR_6M" className={inputClass} disabled={!!editing} />
              </div>
              <div>
                <label className={labelClass}>Type</label>
                <select
                  value={form.type}
                  onChange={e => setForm({
                    ...defaultForm(),
                    id: form.id,
                    description: form.description,
                    fixings: form.fixings,
                    type: e.target.value as StoredIndexSpec['type'],
                  })}
                  className={inputClass}
                >
                  <option value="IBOR">IBOR</option>
                  <option value="Overnight">Overnight</option>
                  <option value="Inflation">Inflation</option>
                </select>
              </div>
              <div>
                <label className={labelClass}>Description</label>
                <input type="text" value={form.description || ''} onChange={e => setForm({ ...form, description: e.target.value })} className={inputClass} />
              </div>

              {form.type === 'IBOR' && (
                <>
                  <div>
                    <label className={labelClass}>Family</label>
                    <select value={form.family || 'Euribor'} onChange={e => setForm({ ...form, family: e.target.value })} className={inputClass}>
                      {IBOR_FAMILIES.map(f => <option key={f}>{f}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Tenor Number</label>
                    <input type="number" value={form.tenor_number ?? 6} onChange={e => setForm({ ...form, tenor_number: +e.target.value })} className={inputClass} />
                  </div>
                  <div>
                    <label className={labelClass}>Tenor Unit</label>
                    <select value={form.tenor_time_unit || 'Months'} onChange={e => setForm({ ...form, tenor_time_unit: e.target.value })} className={inputClass}>
                      {TIME_UNITS.filter(u => ['Days', 'Weeks', 'Months', 'Years'].includes(u)).map(u => <option key={u}>{u}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Business Day Convention</label>
                    <select value={form.business_day_convention || 'ModifiedFollowing'} onChange={e => setForm({ ...form, business_day_convention: e.target.value })} className={inputClass}>
                      {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
                    </select>
                  </div>
                  <div className="flex items-center gap-2 self-end pb-2">
                    <input type="checkbox" checked={form.end_of_month ?? false} onChange={e => setForm({ ...form, end_of_month: e.target.checked })} id="eom" />
                    <label htmlFor="eom" className="text-xs text-[#737373]">End of Month</label>
                  </div>
                </>
              )}
              {form.type === 'Overnight' && (
                <>
                  <div>
                    <label className={labelClass}>Overnight Index</label>
                    <select value={form.overnight_name || 'ESTR'} onChange={e => setForm({ ...form, overnight_name: e.target.value })} className={inputClass}>
                      {OVERNIGHT_INDICES.map(o => <option key={o}>{o}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Business Day Convention</label>
                    <select value={form.business_day_convention || 'ModifiedFollowing'} onChange={e => setForm({ ...form, business_day_convention: e.target.value })} className={inputClass}>
                      {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b}>{b}</option>)}
                    </select>
                  </div>
                </>
              )}
              {form.type === 'Inflation' && (
                <>
                  <div>
                    <label className={labelClass}>Family Name</label>
                    <input value={form.family_name || ''} onChange={e => setForm({ ...form, family_name: e.target.value })} className={inputClass} />
                  </div>
                  <div>
                    <label className={labelClass}>Currency</label>
                    <select value={form.currency || 'EUR'} onChange={e => setForm({ ...form, currency: e.target.value })} className={inputClass}>
                      {CURRENCIES.map(currency => <option key={currency}>{currency}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Curve Kind</label>
                    <select value={form.kind || 'ZeroInflation'} onChange={e => setForm({ ...form, kind: e.target.value as StoredIndexSpec['kind'] })} className={inputClass}>
                      <option value="ZeroInflation">Zero Inflation</option>
                      <option value="YoYInflation">YoY Inflation</option>
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Frequency</label>
                    <select value={form.frequency || 'Monthly'} onChange={e => setForm({ ...form, frequency: e.target.value })} className={inputClass}>
                      {FREQUENCIES.map(f => <option key={f}>{f}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Availability Lag</label>
                    <div className="grid grid-cols-2 gap-2">
                      <input type="number" value={form.availability_lag?.n ?? 2} onChange={e => setForm({ ...form, availability_lag: { ...(form.availability_lag || { n: 2, unit: 'Months' }), n: parseInt(e.target.value, 10) || 0 } })} className={inputClass} />
                      <select value={form.availability_lag?.unit || 'Months'} onChange={e => setForm({ ...form, availability_lag: { ...(form.availability_lag || { n: 2, unit: 'Months' }), unit: e.target.value } })} className={inputClass}>
                        {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
                      </select>
                    </div>
                  </div>
                  <div>
                    <label className={labelClass}>Observation Lag</label>
                    <div className="grid grid-cols-2 gap-2">
                      <input type="number" value={form.observation_lag?.n ?? 3} onChange={e => setForm({ ...form, observation_lag: { ...(form.observation_lag || { n: 3, unit: 'Months' }), n: parseInt(e.target.value, 10) || 0 } })} className={inputClass} />
                      <select value={form.observation_lag?.unit || 'Months'} onChange={e => setForm({ ...form, observation_lag: { ...(form.observation_lag || { n: 3, unit: 'Months' }), unit: e.target.value } })} className={inputClass}>
                        {TIME_UNITS.map(u => <option key={u}>{u}</option>)}
                      </select>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 self-end pb-2">
                    <input type="checkbox" checked={form.interpolated ?? true} onChange={e => setForm({ ...form, interpolated: e.target.checked })} id="inflation-interpolated" />
                    <label htmlFor="inflation-interpolated" className="text-xs text-[#737373]">Interpolated</label>
                  </div>
                  <div className="flex items-center gap-2 self-end pb-2">
                    <input type="checkbox" checked={form.revised ?? false} onChange={e => setForm({ ...form, revised: e.target.checked })} id="inflation-revised" />
                    <label htmlFor="inflation-revised" className="text-xs text-[#737373]">Revised</label>
                  </div>
                  {(form.kind || 'ZeroInflation') === 'YoYInflation' && (
                    <div className="sm:col-span-3">
                      <label className={labelClass}>Underlying Zero Index ID</label>
                      <input value={form.underlying_zero_index_id || ''} onChange={e => setForm({ ...form, underlying_zero_index_id: e.target.value })} className={inputClass} />
                    </div>
                  )}
                </>
              )}
              <div>
                <label className={labelClass}>Fixing Days</label>
                <input type="number" value={form.type === 'Inflation' ? 0 : (form.fixing_days ?? 2)} onChange={e => setForm({ ...form, fixing_days: +e.target.value })} className={inputClass} disabled={form.type === 'Inflation'} />
              </div>
              <div>
                <label className={labelClass}>Calendar</label>
                <select value={form.calendar || 'TARGET'} onChange={e => setForm({ ...form, calendar: e.target.value })} className={inputClass}>
                  {CALENDARS.map(c => <option key={c}>{c}</option>)}
                </select>
              </div>
              <div>
                <label className={labelClass}>Day Counter</label>
                <select value={form.day_counter || (form.type === 'Inflation' ? 'Actual365Fixed' : 'Actual360')} onChange={e => setForm({ ...form, day_counter: e.target.value })} className={inputClass}>
                  {DAY_COUNTERS.map(dc => <option key={dc}>{dc}</option>)}
                </select>
              </div>
            </div>
            <div className="mt-4">
              <h4 className="text-sm font-semibold text-[#0a0a0a] mb-2">Index Fixings (Past Rates)</h4>
              <div className="grid sm:grid-cols-3 gap-3 items-end">
                <div>
                  <label className={labelClass}>Date</label>
                  <input type="date" value={fixingDate} onChange={e => setFixingDate(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Value</label>
                  <input type="number" step="0.0001" value={fixingValue} onChange={e => setFixingValue(e.target.value)} className={inputClass} />
                </div>
                <div>
                  <button
                    onClick={addFixing}
                    className="px-3 py-2 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
                  >
                    Add Fixing
                  </button>
                </div>
              </div>
              {form.fixings && form.fixings.length > 0 ? (
                <div className="mt-3 overflow-auto border border-[#e5e5e5] rounded-lg">
                  <table className="w-full text-xs">
                    <thead className="bg-[#fafafa] text-[#737373]">
                      <tr>
                        <th className="text-left px-3 py-2">Date</th>
                        <th className="text-right px-3 py-2">Value</th>
                        <th className="w-16"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {(form.fixings || []).map(f => (
                        <tr key={f.date} className="border-t border-[#f5f5f5]">
                          <td className="px-3 py-2 font-mono">{f.date}</td>
                          <td className="px-3 py-2 text-right font-mono">{f.value}</td>
                          <td className="px-3 py-2 text-right">
                            <button onClick={() => removeFixing(f.date)} className="text-xs text-red-600 hover:underline">Remove</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-[#a3a3a3] mt-2">No fixings yet</p>
              )}
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => { setShowEditor(false); setEditing(null); }} className="px-4 py-2 text-sm text-[#525252] border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Cancel</button>
              <button onClick={handleSave} className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626]">Save</button>
            </div>
          </div>
        )}

        {/* Index list */}
        {indices.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => openEditor()}
          />
        ) : (
          <div className="space-y-2">
            {indices.map(idx => (
              <div
                key={idx.id}
                onClick={() => openEditor(idx)}
                className="bg-white border border-[#e5e5e5] rounded-xl p-4 hover:border-[#d4d4d4] transition-colors group cursor-pointer"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className={`px-2 py-0.5 text-[10px] font-semibold rounded ${
                      idx.type === 'IBOR'
                        ? 'bg-blue-50 text-blue-700'
                        : idx.type === 'Overnight'
                          ? 'bg-purple-50 text-purple-700'
                          : 'bg-rose-50 text-rose-700'
                    }`}>
                      {idx.type}
                    </span>
                    <span className="text-sm font-medium text-[#0a0a0a]">{idx.id}</span>
                    {idx.description && <span className="text-xs text-[#a3a3a3]">— {idx.description}</span>}
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="text-xs text-[#737373]">
                      {idx.type === 'IBOR'
                        ? `${idx.family} ${idx.tenor_number}${(idx.tenor_time_unit || '')[0]} • ${idx.calendar} • ${idx.day_counter}`
                        : idx.type === 'Overnight'
                          ? `${idx.overnight_name} • ${idx.calendar} • ${idx.day_counter}`
                          : `${idx.family_name || idx.id} • ${idx.kind || 'ZeroInflation'} • ${idx.currency || 'EUR'}`
                      }
                    </div>
                    <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button onClick={() => openEditor(idx)} className="p-1.5 text-[#737373] hover:text-[#0a0a0a] hover:bg-[#f5f5f5] rounded transition-colors" title="Edit">
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" /></svg>
                      </button>
                      <button onClick={() => handleDelete(idx.id)} className="p-1.5 text-[#737373] hover:text-red-500 hover:bg-red-50 rounded transition-colors" title="Delete">
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
