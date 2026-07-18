// Fixed Rate Bond list page — mirrors CurvesList
import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { SavedFixedRateBond, fixedBondStore, exportFixedBonds, importFixedBonds } from '../../lib/storage/bonds';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function FixedRateBondList() {
  const navigate = useNavigate();
  const ui = entityUi.fixedRateBond;
  const [bonds, setBonds] = useState<SavedFixedRateBond[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadBonds = async () => {
    setLoading(true);
    try {
      setBonds(await fixedBondStore.getAll());
    } catch {
      setError('Failed to load bonds');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadBonds(); }, []);

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await fixedBondStore.delete(id);
      setBonds(bonds.filter(b => b.id !== id));
      setSuccess('Bond deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Failed to delete bond');
    }
  };

  const handleExportAll = () => {
    if (bonds.length === 0) { setError('No bonds to export'); return; }
    exportFixedBonds(bonds);
    setSuccess('Bonds exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importFixedBonds(file);
      loadBonds();
      setSuccess(`Imported ${imported.length} bond(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleDuplicate = async (bond: SavedFixedRateBond) => {
    const now = new Date().toISOString();
    await fixedBondStore.save({
      ...JSON.parse(JSON.stringify(bond)),
      id: `${bond.id}_copy_${Date.now()}`,
      name: `${bond.name} (copy)`,
      createdAt: now,
      updatedAt: now,
    });
    await loadBonds();
    setSuccess('Bond duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Price fixed coupon bonds with yield, duration, and cash flows"
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
            {bonds.length > 0 && (
              <button
                onClick={handleExportAll}
                className={listStyles.secondaryButton}
              >
                <ExportIcon />
                Export All
              </button>
            )}
            <button
              onClick={() => navigate('/products/fixed-rate-bond/new')}
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
        ) : bonds.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate('/products/fixed-rate-bond/new')}
          />
        ) : (
          <div className="space-y-3">
            {bonds.map(bond => (
              <div key={bond.id} className={listStyles.listCard} onClick={() => navigate(`/products/fixed-rate-bond/${bond.id}`)}>
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <h3
                        className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                        onClick={() => navigate(`/products/fixed-rate-bond/${bond.id}`)}
                      >
                        {bond.name}
                      </h3>
                      <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                        Fixed
                      </span>
                    </div>
                    {bond.description && (
                      <p className="text-sm text-[#737373] mb-3">{bond.description}</p>
                    )}
                    <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                      <span>Coupon: <span className="font-medium text-[#525252]">{(bond.couponRate * 100).toFixed(2)}%</span></span>
                      <span>Face: <span className="font-medium text-[#525252]">{bond.faceAmount.toLocaleString()}</span></span>
                      <span>•</span>
                      <span>{bond.effectiveDate} → {bond.terminationDate}</span>
                      <span>•</span>
                      <span>{bond.frequency} / {bond.calendar}</span>
                    </div>
                  </div>
                  <div className={listStyles.hoverActions}>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDuplicate(bond); }}
                      className={listStyles.duplicateButton}
                      title="Duplicate"
                    >
                      <DuplicateIcon />
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDelete(bond.id, bond.name); }}
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
