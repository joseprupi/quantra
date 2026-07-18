// IR Swap list page — mirrors bond list patterns
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { StoredSwap, deriveIrSwapName, getIrSwaps, deleteIrSwap, exportIrSwaps, importIrSwaps, saveIrSwap } from '../../lib/storage/swaps';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function IrSwapList() {
  const navigate = useNavigate();
  const ui = entityUi.swap;
  const [swaps, setSwaps] = useState<StoredSwap[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<'All' | 'Vanilla' | 'OIS' | 'Basis'>('All');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const detectSwapKind = (request: StoredSwap['request']): 'Vanilla' | 'OIS' | 'Basis' => {
    const row = request?.swaps?.[0] as any;
    if (row?.ois_swap) return 'OIS';
    if (row?.basis_swap) return 'Basis';
    return 'Vanilla';
  };

  const loadSwaps = async () => {
    setLoading(true);
    try {
      setSwaps(await getIrSwaps());
    } catch {
      setError('Failed to load swaps');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadSwaps();
  }, []);

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await deleteIrSwap(id);
      setSwaps(swaps.filter(s => s.id !== id));
      setSuccess('Swap deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Failed to delete swap');
    }
  };

  const handleExportAll = () => {
    if (swaps.length === 0) { setError('No swaps to export'); return; }
    exportIrSwaps(swaps.map(s => s.request));
    setSuccess('Swaps exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importIrSwaps(file);
      loadSwaps();
      setSuccess(`Imported ${imported.length} swap(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleDuplicate = async (swap: StoredSwap) => {
    const baseName = swap.name || deriveIrSwapName(swap.request);
    await saveIrSwap(swap.request, { name: `${baseName} (copy)` });
    await loadSwaps();
    setSuccess('Swap duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Price Vanilla, OIS, and Basis swaps"
          actions={
            <div className="flex gap-2">
            <select
              value={typeFilter}
              onChange={e => setTypeFilter(e.target.value as any)}
              className="px-3 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors"
            >
              <option value="All">All types</option>
              <option value="Vanilla">Vanilla</option>
              <option value="OIS">OIS</option>
              <option value="Basis">Basis</option>
            </select>
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button
              onClick={() => fileInputRef.current?.click()}
              className={listStyles.secondaryButton}
            >
              <ImportIcon />
              Import
            </button>
            {swaps.length > 0 && (
              <button
                onClick={handleExportAll}
                className={listStyles.secondaryButton}
              >
                <ExportIcon />
                Export All
              </button>
            )}
            <button
              onClick={() => navigate('/products/ir-swap/new')}
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
        ) : swaps.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate('/products/ir-swap/new')}
          />
        ) : (
          <div className="space-y-3">
            {swaps
              .filter(s => typeFilter === 'All' || detectSwapKind(s.request) === typeFilter)
              .map(swap => {
                const request = swap.request;
                const row = request?.swaps?.[0] as any;
                const kind = detectSwapKind(request);
                const swapItem = row?.vanilla_swap || row?.ois_swap || row?.basis_swap || {};
                const indexRefId =
                  swapItem?.floating_leg?.index?.id ||
                  swapItem?.overnight_leg?.index?.id ||
                  swapItem?.leg1?.index?.id ||
                  'Index';
                const effective =
                  swapItem?.fixed_leg?.schedule?.effective_date ||
                  swapItem?.leg1?.schedule?.effective_date ||
                  '—';
                const termination =
                  swapItem?.fixed_leg?.schedule?.termination_date ||
                  swapItem?.leg1?.schedule?.termination_date ||
                  '—';
                const fixedRate = swapItem?.fixed_leg?.rate;
                const displayName = swap.name || deriveIrSwapName(request);
                return (
                  <div key={swap.id} className={listStyles.listCard} onClick={() => navigate(`/products/ir-swap/${swap.id}`)}>
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <h3
                            className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                            onClick={() => navigate(`/products/ir-swap/${swap.id}`)}
                          >
                            {displayName}
                          </h3>
                          <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                            {kind}
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                          <span>Fixed: <span className="font-medium text-[#525252]">{fixedRate !== undefined ? (fixedRate * 100).toFixed(3) + '%' : '—'}</span></span>
                          <span>Float: <span className="font-medium text-[#525252]">{indexRefId}</span></span>
                          <span>•</span>
                          <span>{effective} → {termination}</span>
                          <span>•</span>
                          <span>{swapItem?.fixed_leg?.schedule?.frequency || swapItem?.leg1?.schedule?.frequency || '—'} / {swapItem?.fixed_leg?.schedule?.calendar || swapItem?.leg1?.schedule?.calendar || '—'}</span>
                        </div>
                      </div>
                      <div className={listStyles.hoverActions}>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDuplicate(swap); }}
                          className={listStyles.duplicateButton}
                          title="Duplicate"
                        >
                          <DuplicateIcon />
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDelete(swap.id, displayName); }}
                          className={listStyles.deleteButton}
                          title="Delete"
                        >
                          <TrashButton />
                        </button>
                      </div>
                    </div>
                  </div>
                )
              })}
          </div>
        )}
      </main>
    </div>
  );
}
