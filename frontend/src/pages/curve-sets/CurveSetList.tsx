// Curve Sets list page
import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { Curve, CurveSet } from '../../lib/types';
import { refreshCurveSets, saveCurveSet, deleteCurveSet, generateCurveSetId, resolveCurveSetRefs } from '../../lib/storage/curveSets';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function CurveSetList() {
  const navigate = useNavigate();
  const ui = entityUi.curveSet;
  const [sets, setSets] = useState<CurveSet[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      setSets(await refreshCurveSets());
    } catch {
      setError('Failed to load curve sets');
    }
  };
  useEffect(() => { void load(); }, []);

  const handleNew = async () => {
    const now = new Date().toISOString();
    const cs: CurveSet = {
      id: generateCurveSetId(),
      // Draft name with a short unique suffix: the backend enforces a
      // per-owner unique name, so a CONSTANT draft name 409s whenever an
      // un-renamed draft already exists (or two tabs create at once). The
      // editor renames it on first edit anyway.
      name: `New Curve Set (${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)})`,
      description: '',
      currency: 'EUR',
      as_of_date: new Date().toISOString().split('T')[0],
      curve_refs: [],
      quote_ids: [],
      createdAt: now,
      updatedAt: now,
    };
    try {
      // The backend mints the durable id — navigate to the created row.
      const created = await saveCurveSet(cs);
      navigate(`/curve-sets/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create curve set');
    }
  };

  const handleDuplicate = async (cs: CurveSet) => {
    const now = new Date().toISOString();
    const dup: CurveSet = {
      ...JSON.parse(JSON.stringify(cs)),
      id: generateCurveSetId(),
      name: `${cs.name} (copy)`,
      createdAt: now,
      updatedAt: now,
    };
    try {
      await saveCurveSet(dup);
      await load();
      setSuccess(`Duplicated "${cs.name}"`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to duplicate curve set');
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await deleteCurveSet(id);
      await load();
      setSuccess('Curve set deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete curve set');
    }
  };

  const handleExportAll = () => {
    if (sets.length === 0) { setError('No curve sets to export'); return; }
    const blob = new Blob([JSON.stringify(sets, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `curve-sets-${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setSuccess('Exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const arr: CurveSet[] = Array.isArray(data) ? data : [data];
      for (const cs of arr) {
        // Imported ids are foreign — create fresh backend rows.
        cs.id = generateCurveSetId();
        await saveCurveSet(cs);
      }
      await load();
      setSuccess(`Imported ${arr.length} curve set(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Invalid JSON file');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Reference pricing environments built from standalone yield and inflation curves"
          actions={
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
              <ImportIcon />
              Import
            </button>
            {sets.length > 0 && (
              <button onClick={handleExportAll} className={listStyles.secondaryButton}>
                <ExportIcon />
                Export All
              </button>
            )}
            <button onClick={handleNew} className={listStyles.primaryNewButton}>
              <NewIcon />
              {getNewLabel(ui)}
            </button>
            </div>
          }
        />

        {error && <FeedbackBanner tone="error" message={error} onDismiss={() => setError(null)} />}
        {success && <FeedbackBanner tone="success" message={success} />}

        {sets.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={handleNew}
          />
        ) : (
          <div className="space-y-3">
            {sets.map(cs => {
              const refs = resolveCurveSetRefs(cs);
              const resolvedCurves = refs.map(ref => ref.curve).filter((curve): curve is Curve => curve !== null);
              return (
              <div key={cs.id} className={listStyles.listCard} onClick={() => navigate(`/curve-sets/${cs.id}`)}>
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <h3
                        className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                        onClick={() => navigate(`/curve-sets/${cs.id}`)}
                      >
                        {cs.name}
                      </h3>
                      <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                        {cs.currency}
                      </span>
                    </div>
                    {cs.description && (
                      <p className="text-sm text-[#737373] mb-2">{cs.description}</p>
                    )}
                    <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                      <span>{refs.length} reference{refs.length !== 1 ? 's' : ''}</span>
                      {refs.length > 0 && (
                        <>
                          <span>•</span>
                          <span>
                            {refs.filter(ref => ref.role === 'discount').length} discount,{' '}
                            {refs.filter(ref => ref.role === 'forward').length} forward,{' '}
                            {refs.filter(ref => ref.role === 'inflation').length} inflation
                          </span>
                        </>
                      )}
                      {refs.length !== resolvedCurves.length && (
                        <>
                          <span>•</span>
                          <span>{refs.length - resolvedCurves.length} missing</span>
                        </>
                      )}
                      {(cs.credit_curve_ids || []).length > 0 && (
                        <>
                          <span>•</span>
                          <span>{(cs.credit_curve_ids || []).length} credit</span>
                        </>
                      )}
                      <span>•</span>
                      <span>As of: {cs.as_of_date}</span>
                    </div>
                    {refs.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {refs.map(ref => (
                          <span key={ref.id} className={`px-2 py-0.5 text-[10px] font-medium rounded ${ref.role === 'discount' ? 'bg-emerald-50 text-emerald-700' :
                              ref.role === 'forward' ? 'bg-blue-50 text-blue-700' :
                                ref.role === 'inflation' ? 'bg-rose-50 text-rose-700' :
                                'bg-purple-50 text-purple-700'
                            }`}>
                            {ref.curve?.name || ref.label || ref.curve_id}
                          </span>
                        ))}
                      </div>
                    )}
                    {(cs.credit_curve_ids || []).length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {(cs.credit_curve_ids || []).map((creditCurveId) => (
                          <span key={creditCurveId} className="px-2 py-0.5 text-[10px] font-medium rounded bg-amber-50 text-amber-700">
                            {creditCurveId}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className={listStyles.hoverActions}>
                    <button onClick={(e) => { e.stopPropagation(); handleDuplicate(cs); }} className={listStyles.duplicateButton} title="Duplicate">
                      <DuplicateIcon />
                    </button>
                    <button onClick={(e) => { e.stopPropagation(); handleDelete(cs.id, cs.name); }} className={listStyles.deleteButton} title="Delete">
                      <TrashButton />
                    </button>
                  </div>
                </div>
              </div>
            );})}
          </div>
        )}
      </main>
    </div>
  );
}
