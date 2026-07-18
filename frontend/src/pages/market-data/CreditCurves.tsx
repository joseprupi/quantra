import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import {
  CreditCurveSource,
  CreditCurveSpec,
  deleteCreditCurve,
  refreshCreditCurves,
  exportCreditCurves,
  importCreditCurves,
  saveCreditCurve,
} from '../../lib/storage/creditCurves';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

const SOURCE_LABELS: Record<CreditCurveSource, string> = {
  flat: 'Flat hazard rate',
  manual: 'Manual spread points',
  quote_book: 'Quote Book spread points',
};

function sourceBadgeClass(source: CreditCurveSource) {
  if (source === 'flat') return 'bg-emerald-50 text-emerald-700';
  if (source === 'manual') return 'bg-blue-50 text-blue-700';
  return 'bg-amber-50 text-amber-700';
}

function sourceSummary(curve: CreditCurveSpec) {
  if (curve.source === 'flat') {
    return `Flat hazard: ${(curve.flat_hazard_rate ?? 0).toFixed(6)}`;
  }
  const count = curve.points?.length || 0;
  return `${count} spread point${count === 1 ? '' : 's'}`;
}

export default function CreditCurves() {
  const navigate = useNavigate();
  const ui = entityUi.creditCurve;
  const [items, setItems] = useState<CreditCurveSpec[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems(await refreshCreditCurves());
    } catch {
      setError('Failed to load credit curves');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const handleDelete = async (curve: CreditCurveSpec) => {
    if (!confirm(`Delete "${curve.name || curve.id}"? This cannot be undone.`)) return;
    await deleteCreditCurve(curve.id);
    await load();
    setSuccess('Credit curve deleted');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleDuplicate = async (curve: CreditCurveSpec) => {
    const now = new Date().toISOString();
    await saveCreditCurve({
      ...JSON.parse(JSON.stringify(curve)),
      id: `${curve.id}_copy_${Date.now()}`,
      name: curve.name ? `${curve.name} (copy)` : `${curve.id} (copy)`,
      createdAt: now,
      updatedAt: now,
    });
    await load();
    setSuccess(`Duplicated "${curve.name || curve.id}"`);
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleExportAll = () => {
    if (items.length === 0) {
      setError('No credit curves to export');
      return;
    }
    exportCreditCurves(items);
    setSuccess('Credit curves exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    try {
      const imported = await importCreditCurves(file);
      await load();
      setSuccess(`Imported ${imported.length} credit curve(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Import failed');
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Manage reusable hazard rate and spread curves for CDS pricing"
          actions={
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" className="hidden" onChange={handleImport} />
            <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
              <ImportIcon />
              Import
            </button>
            {items.length > 0 && (
              <button onClick={handleExportAll} className={listStyles.secondaryButton}>
                <ExportIcon />
                Export All
              </button>
            )}
            <button onClick={() => navigate('/credit-curves/new')} className={listStyles.primaryNewButton}>
              <NewIcon />
              {getNewLabel(ui)}
            </button>
            </div>
          }
        />

        {error && <FeedbackBanner tone="error" message={error} onDismiss={() => setError(null)} />}
        {success && <FeedbackBanner tone="success" message={success} />}

        {loading ? (
          <div className="text-center py-12">
            <div className="w-8 h-8 border-2 border-[#e5e5e5] border-t-[#8a6a2f] rounded-full animate-spin mx-auto" />
          </div>
        ) : items.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate('/credit-curves/new')}
          />
        ) : (
          <div className="space-y-3">
            {items.map((curve) => (
              <div key={curve.id} className={listStyles.listCard} onClick={() => navigate(`/credit-curves/${curve.id}`)}>
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <h3
                        className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                        onClick={() => navigate(`/credit-curves/${curve.id}`)}
                      >
                        {curve.name || curve.id}
                      </h3>
                      {curve.currency && (
                        <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                          {curve.currency}
                        </span>
                      )}
                      <span className={`px-2 py-0.5 text-xs font-medium rounded ${sourceBadgeClass(curve.source)}`}>
                        {SOURCE_LABELS[curve.source]}
                      </span>
                    </div>

                    {curve.reference_entity && (
                      <p className="text-sm text-[#737373] mb-3">{curve.reference_entity}</p>
                    )}

                    <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                      <span>ID: <span className="font-mono text-[#525252]">{curve.id}</span></span>
                      {curve.seniority && (
                        <span>Seniority: <span className="font-medium text-[#525252]">{curve.seniority}</span></span>
                      )}
                      <span>Recovery: <span className="font-medium text-[#525252]">{(curve.recovery_rate * 100).toFixed(2)}%</span></span>
                      <span>{sourceSummary(curve)}</span>
                    </div>
                  </div>

                  <div className={listStyles.hoverActions}>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDuplicate(curve); }}
                      className={listStyles.duplicateButton}
                      title="Duplicate"
                    >
                      <DuplicateIcon />
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDelete(curve); }}
                      className={listStyles.deleteButton}
                      title="Delete"
                    >
                      <TrashButton />
                    </button>
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
