import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import {
  deleteEquityOption,
  deriveEquityOptionName,
  exportEquityOptions,
  importEquityOptions,
  listEquityOptions,
  saveEquityOption,
  StoredEquityOption,
} from '../../lib/storage/equityOptions';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function EquityOptionsList() {
  const navigate = useNavigate();
  const ui = entityUi.equityOption;
  const [items, setItems] = useState<StoredEquityOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems(await listEquityOptions());
    } catch {
      setError('Failed to load equity options');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this equity option? This cannot be undone.')) return;
    await deleteEquityOption(id);
    await load();
    setSuccess('Equity option deleted');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleDuplicate = async (item: StoredEquityOption) => {
    await saveEquityOption(item.request as any);
    await load();
    setSuccess('Equity option duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleExportAll = () => {
    if (items.length === 0) return;
    exportEquityOptions(items.map(i => i.request));
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importEquityOptions(file);
      await load();
      setSuccess(`Imported ${imported.length} equity option(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Price and manage saved equity option trades"
          actions={
            <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
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
            <button onClick={() => navigate('/products/equity-options/new')} className={listStyles.primaryNewButton}>
              <NewIcon />
              {getNewLabel(ui)}
            </button>
            </div>
          }
        />

        {error && <FeedbackBanner tone="error" message={error} onDismiss={() => setError(null)} className="mb-4 p-3" />}
        {success && <FeedbackBanner tone="success" message={success} className="mb-4 p-3" />}

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
            onAction={() => navigate('/products/equity-options/new')}
          />
        ) : (
          <div className="space-y-3">
            {items.map(item => {
              const option = item.request?.options?.[0] || {};
              const strike = option?.strike;
              const expiry = option?.expiry_date || '—';
              const displayName = item.name || deriveEquityOptionName(item.request);
              return (
                <div key={item.id} className={listStyles.listCard} onClick={() => navigate(`/products/equity-options/${item.id}`)}>
                  <div className="flex items-start justify-between">
                    <div>
                      <h3 className="text-lg font-medium text-[#0a0a0a]">{displayName}</h3>
                      <div className="flex flex-wrap gap-4 text-xs text-[#737373] mt-1">
                        <span>Expiry: <span className="font-medium text-[#525252]">{expiry}</span></span>
                        <span>Strike: <span className="font-medium text-[#525252]">{strike ?? '—'}</span></span>
                      </div>
                    </div>
                    <div className={listStyles.hoverActions}>
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleDuplicate(item); }}
                        className={listStyles.duplicateButton}
                        title="Duplicate"
                      >
                        <DuplicateIcon />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleDelete(item.id); }}
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
