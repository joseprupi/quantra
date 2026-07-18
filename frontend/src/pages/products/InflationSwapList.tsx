import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import {
  deleteInflationSwap,
  deriveInflationSwapName,
  exportInflationSwaps,
  getInflationSwaps,
  importInflationSwaps,
  saveInflationSwap,
  type StoredInflationSwap,
} from '../../lib/storage/inflationSwaps';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';

type InflationSwapKind = 'All' | 'ZeroCoupon' | 'YearOnYear';

function detectKind(request: StoredInflationSwap['request']): Exclude<InflationSwapKind, 'All'> {
  const trade = request?.swaps?.[0] as any;
  return trade?.year_on_year_inflation_swap ? 'YearOnYear' : 'ZeroCoupon';
}

function getPricingInflation(pricing: Record<string, any> | undefined) {
  if (!pricing) return {};
  return pricing.inflation || {
    inflation_curves: pricing.inflation_curves,
    inflation_indices: pricing.inflation_indices,
  };
}

export default function InflationSwapList() {
  const navigate = useNavigate();
  const ui = entityUi.inflationSwap;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [swaps, setSwaps] = useState<StoredInflationSwap[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<InflationSwapKind>('All');

  const loadSwaps = async () => {
    setLoading(true);
    try {
      setSwaps(await getInflationSwaps());
    } catch {
      setError('Failed to load inflation swaps');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSwaps();
  }, []);

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    try {
      await deleteInflationSwap(id);
      setSwaps(swaps.filter(swap => swap.id !== id));
      setSuccess('Inflation swap deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch {
      setError('Failed to delete inflation swap');
    }
  };

  const handleDuplicate = async (swap: StoredInflationSwap) => {
    const baseName = swap.name || deriveInflationSwapName(swap.request);
    await saveInflationSwap(swap.request, { name: `${baseName} (copy)` });
    await loadSwaps();
    setSuccess('Inflation swap duplicated');
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const imported = await importInflationSwaps(file);
      await loadSwaps();
      setSuccess(`Imported ${imported.length} inflation swap(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleExportAll = () => {
    if (swaps.length === 0) {
      setError('No inflation swaps to export');
      return;
    }
    exportInflationSwaps(swaps.map(swap => swap.request));
    setSuccess('Inflation swaps exported');
    setTimeout(() => setSuccess(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={ui.plural}
          subtitle="Zero-coupon and year-on-year inflation swap pricing"
          actions={
            <div className="flex gap-2">
            <select
              value={typeFilter}
              onChange={e => setTypeFilter(e.target.value as InflationSwapKind)}
              className="px-3 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors"
            >
              <option value="All">All types</option>
              <option value="ZeroCoupon">Zero Coupon</option>
              <option value="YearOnYear">Year-on-Year</option>
            </select>
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
              <ImportIcon />
              Import
            </button>
            {swaps.length > 0 && (
              <button onClick={handleExportAll} className={listStyles.secondaryButton}>
                <ExportIcon />
                Export All
              </button>
            )}
            <button onClick={() => navigate('/products/inflation-swaps/new')} className={listStyles.primaryNewButton}>
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
            onAction={() => navigate('/products/inflation-swaps/new')}
          />
        ) : (
          <div className="space-y-3">
            {swaps
              .filter(swap => typeFilter === 'All' || detectKind(swap.request) === typeFilter)
              .map(swap => {
                const trade = swap.request?.swaps?.[0] as any;
                const kind = detectKind(swap.request);
                const pricing = swap.request?.pricing as Record<string, any> | undefined;
                const inflation = getPricingInflation(pricing);
                const inflationCurveId = inflation?.inflation_curves?.[0]?.id || inflation?.inflation?.inflation_curves?.[0]?.id || 'inflation';
                const zeroCouponTrade = trade?.zero_coupon_inflation_swap;
                const yoyTrade = trade?.year_on_year_inflation_swap;
                const effectiveDate = zeroCouponTrade?.start_date || yoyTrade?.fixed_schedule?.effective_date || '—';
                const maturityDate = zeroCouponTrade?.maturity_date || yoyTrade?.fixed_schedule?.termination_date || '—';
                const fixedRate = zeroCouponTrade?.fixed_rate ?? yoyTrade?.fixed_rate;
                const spread = yoyTrade?.spread;
                const displayName = swap.name || deriveInflationSwapName(swap.request);

                return (
                  <div key={swap.id} className={listStyles.listCard} onClick={() => navigate(`/products/inflation-swaps/${swap.id}`)}>
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <h3
                            className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
                            onClick={() => navigate(`/products/inflation-swaps/${swap.id}`)}
                          >
                            {displayName}
                          </h3>
                          <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                            {kind === 'ZeroCoupon' ? 'ZC' : 'YoY'}
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                          <span>Curve: <span className="font-medium text-[#525252]">{inflationCurveId}</span></span>
                          <span>Fixed: <span className="font-medium text-[#525252]">{fixedRate !== undefined ? fixedRate.toFixed(4) : '—'}</span></span>
                          {kind === 'YearOnYear' && (
                            <span>Spread: <span className="font-medium text-[#525252]">{spread !== undefined ? spread.toFixed(4) : '—'}</span></span>
                          )}
                          <span>•</span>
                          <span>{effectiveDate} → {maturityDate}</span>
                        </div>
                      </div>
                      <div className={listStyles.hoverActions}>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDuplicate(swap);
                          }}
                          className={listStyles.duplicateButton}
                          title="Duplicate"
                        >
                          <DuplicateIcon />
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDelete(swap.id, displayName);
                          }}
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
