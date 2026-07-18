import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { StoredSwaption, deriveSwaptionName, deleteSwaption, exportSwaptions, getSwaptions, importSwaptions, saveSwaption } from '../../lib/storage/swaptions';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function SwaptionList() {
  const navigate = useNavigate();
  const ui = entityUi.swaption;
  const [swaptions, setSwaptions] = useState<StoredSwaption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadSwaptions = async () => {
    setLoading(true);
    try {
      setSwaptions(await getSwaptions());
    } catch {
      setError('Failed to load swaptions');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadSwaptions(); }, []);

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await deleteSwaption(id);
      setSwaptions(swaptions.filter(s => s.id !== id));
      setSuccess('Swaption deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Failed to delete swaption');
    }
  };

  const handleExportAll = () => {
    if (swaptions.length === 0) { setError('No swaptions to export'); return; }
    exportSwaptions(swaptions.map(s => s.request));
    setSuccess('Swaptions exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importSwaptions(file);
      loadSwaptions();
      setSuccess(`Imported ${imported.length} swaption(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleDuplicate = async (swaption: StoredSwaption) => {
    const baseName = swaption.name || deriveSwaptionName(swaption.request);
    await saveSwaption(swaption.request, { name: `${baseName} (copy)` });
    await loadSwaptions();
    setSuccess('Swaption duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Price European/Bermudan swaptions with model and volatility inputs"
          actions={
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button
              onClick={() => fileInputRef.current?.click()}
              className={listStyles.secondaryButton}
            >
              <ImportIcon />
              Import
            </button>
            {swaptions.length > 0 && (
              <button
                onClick={handleExportAll}
                className={listStyles.secondaryButton}
              >
                <ExportIcon />
                Export All
              </button>
            )}
            <button
              onClick={() => navigate('/products/swaption/new')}
              className={listStyles.primaryNewButton}
            >
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
        ) : swaptions.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate('/products/swaption/new')}
          />
        ) : (
          <div className="space-y-3">
            {swaptions.map(swaption => {
              const request = swaption.request;
              const swaptionItem = request?.swaptions?.[0]?.swaption || {};
              const isOisSwap = swaptionItem?.underlying_type === 'OisSwap';
              const underlying = swaptionItem?.underlying || swaptionItem?.underlying_swap || {};
              const indexRefId = isOisSwap
                ? (underlying?.overnight_leg?.index?.id || 'Index')
                : (underlying?.floating_leg?.index?.id || 'Index');
              const exercise = Array.isArray(swaptionItem?.exercise_dates) && swaptionItem.exercise_dates.length > 0
                ? `${swaptionItem.exercise_dates.length} dates`
                : (swaptionItem?.exercise_date || '—');
              const maturity = underlying?.fixed_leg?.schedule?.termination_date || '—';
              const displayName = swaption.name || deriveSwaptionName(request);
              return (
                <div key={swaption.id} className={listStyles.listCard} onClick={() => navigate(`/products/swaption/${swaption.id}`)}>
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <h3
                          className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                          onClick={() => navigate(`/products/swaption/${swaption.id}`)}
                        >
                          {displayName}
                        </h3>
                        <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                          {swaptionItem?.exercise_type || 'European'}
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                        <span>Exercise: <span className="font-medium text-[#525252]">{exercise}</span></span>
                        <span>Underlying: <span className="font-medium text-[#525252]">{indexRefId}</span></span>
                        <span>•</span>
                        <span>{exercise} → {maturity}</span>
                      </div>
                    </div>
                    <div className={listStyles.hoverActions}>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDuplicate(swaption); }}
                        className={listStyles.duplicateButton}
                        title="Duplicate"
                      >
                        <DuplicateIcon />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(swaption.id, displayName); }}
                        className={listStyles.deleteButton}
                        title="Delete"
                      >
                        <TrashButton />
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
