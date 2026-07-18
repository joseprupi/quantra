import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { StoredCds, deriveCdsName, deleteCds, exportCds, getCdsItems, importCds, saveCds } from '../../lib/storage/cds';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

export default function CdsList() {
  const navigate = useNavigate();
  const ui = entityUi.cds;
  const [items, setItems] = useState<StoredCds[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems(await getCdsItems());
    } catch {
      setError('Failed to load CDS requests');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this CDS request? This cannot be undone.')) return;
    try {
      await deleteCds(id);
      setItems(items.filter(i => i.id !== id));
      setSuccess('CDS request deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Failed to delete CDS request');
    }
  };

  const handleExportAll = () => {
    if (items.length === 0) {
      setError('No CDS requests to export');
      return;
    }
    exportCds(items.map(i => i.request));
    setSuccess('CDS requests exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importCds(file);
      await load();
      setSuccess(`Imported ${imported.length} CDS request(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleDuplicate = async (item: StoredCds) => {
    const baseName = item.name || deriveCdsName(item.request);
    await saveCds(item.request, { name: `${baseName} (copy)` });
    await load();
    setSuccess('CDS request duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Price credit default swaps with flat or quoted credit curves"
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
            {items.length > 0 && (
              <button
                onClick={handleExportAll}
                className={listStyles.secondaryButton}
              >
                <ExportIcon />
                Export All
              </button>
            )}
            <button
              onClick={() => navigate('/products/cds/new')}
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
        ) : items.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate('/products/cds/new')}
          />
        ) : (
          <div className="space-y-3">
            {items.map(item => {
              const cds = item.request?.cds_list?.[0]?.cds || {};
              const notional = cds.notional ?? 0;
              const spread = cds.running_coupon ?? cds.spread;
              const endDate = cds?.schedule?.termination_date || '—';
              const pricingCreditCurves = item.request?.pricing?.credit_curves || [];
              const linkedCurveId = item.request?.cds_list?.[0]?.credit_curve_id;
              const linkedCurve = pricingCreditCurves.find((c: any) => c.id === linkedCurveId) || pricingCreditCurves[0];
              const legacyCurve = item.request?.cds_list?.[0]?.credit_curve;
              const curveMode = (linkedCurve?.flat_hazard_rate !== undefined || legacyCurve?.flat_hazard_rate !== undefined)
                ? 'Flat hazard'
                : 'Spread quotes';
              const displayName = item.name || deriveCdsName(item.request);
              return (
                <div key={item.id} className={listStyles.listCard} onClick={() => navigate(`/products/cds/${item.id}`)}>
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <h3
                        className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                        onClick={() => navigate(`/products/cds/${item.id}`)}
                      >
                        {displayName}
                      </h3>
                      <div className="flex flex-wrap gap-4 text-xs text-[#737373] mt-1">
                        <span>Notional: <span className="font-medium text-[#525252]">{Number(notional).toLocaleString()}</span></span>
                        <span>Spread: <span className="font-medium text-[#525252]">{spread !== undefined ? (spread * 10000).toFixed(1) : '—'} bps</span></span>
                        <span>End: <span className="font-medium text-[#525252]">{endDate}</span></span>
                        <span>Curve: <span className="font-medium text-[#525252]">{curveMode}</span></span>
                      </div>
                    </div>
                    <div className={listStyles.hoverActions}>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDuplicate(item); }}
                        className={listStyles.duplicateButton}
                        title="Duplicate"
                      >
                        <DuplicateIcon />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
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
