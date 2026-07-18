// Curves listing page - view saved yield or inflation curves
import { useState, useEffect, useMemo, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import Header from '../../components/Header';
import { Curve } from '../../lib/types';
import { refreshCurves, deleteCurve, exportCurves, importCurves, saveCurve } from '../../lib/storage/curves';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import CollectionEmptyState from '../../components/ui/CollectionEmptyState';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getCreateLabel, getEmptyTitle, getNewLabel } from '../../components/ui/entityUi';
import { isLivePublicCurve } from '../../lib/provenance';

export default function CurvesList() {
  const location = useLocation();
  const navigate = useNavigate();
  const [curves, setCurves] = useState<Curve[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mode = location.pathname.startsWith('/inflation-curves') ? 'inflation' : 'yield';
  const isInflationMode = mode === 'inflation';
  const ui = isInflationMode ? entityUi.inflationCurve : entityUi.yieldCurve;
  const title = ui.plural;
  const subtitle = isInflationMode
    ? 'Manage inflation curves, indices, and helper instruments'
    : 'Manage yield curves and nominal market data';
  const newPath = isInflationMode ? '/inflation-curves/new' : '/yield-curves/new';

  const loadCurves = async () => {
    setLoading(true);
    try {
      // Always re-fetch from the backend on mount: another browser/tab may
      // have created curves since the app-level bootstrap preload.
      setCurves(await refreshCurves());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load curves');
    } finally {
      setLoading(false);
    }
  };

  const visibleCurves = useMemo(() => (
    curves.filter((curve) => (
      isInflationMode ? curve.role === 'inflation' : curve.role !== 'inflation'
    ))
  ), [curves, isInflationMode]);

  useEffect(() => {
    void loadCurves();
  }, []);

  const handleDelete = async (curveId: string, curveName: string) => {
    if (!confirm(`Delete "${curveName}"? This cannot be undone.`)) return;

    try {
      await deleteCurve(curveId);
      setCurves(curves.filter(c => c.id !== curveId));
      setSuccess('Curve deleted');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete curve');
    }
  };

  const handleExportAll = () => {
    if (visibleCurves.length === 0) {
      setError('No curves to export');
      return;
    }
    exportCurves(visibleCurves);
    setSuccess(`${title} exported`);
    setTimeout(() => setSuccess(null), 3000);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    try {
      const imported = await importCurves(file);
      await loadCurves();
      setSuccess(`Imported ${imported.length} curve(s)`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import');
    }

    // Reset file input
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const handleDuplicate = async (curve: Curve) => {
    const now = new Date().toISOString();
    try {
      await saveCurve({
        ...JSON.parse(JSON.stringify(curve)),
        id: `${curve.id}_copy_${Date.now()}`,
        name: `${curve.name} (copy)`,
        createdAt: now,
        updatedAt: now,
      });
      await loadCurves();
      setSuccess(`Duplicated "${curve.name}"`);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to duplicate curve');
    }
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />

      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={title}
          subtitle={subtitle}
          actions={
            <div className="flex gap-2">
            <input
              ref={fileInputRef}
              type="file"
              accept=".json"
              onChange={handleImport}
              className="hidden"
            />

            <button
              onClick={() => fileInputRef.current?.click()}
              className={listStyles.secondaryButton}
            >
              <ImportIcon />
              Import
            </button>

            {visibleCurves.length > 0 && (
              <button
                onClick={handleExportAll}
                className={listStyles.secondaryButton}
              >
                <ExportIcon />
                Export All
              </button>
            )}

            <button
              onClick={() => navigate(newPath)}
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
        ) : visibleCurves.length === 0 ? (
          <CollectionEmptyState
            icon={ui.icon}
            title={getEmptyTitle(ui)}
            description={ui.emptyDescription}
            actionLabel={getCreateLabel(ui)}
            onAction={() => navigate(newPath)}
          />
        ) : (
          <div className="space-y-3">
            {visibleCurves.map(curve => (
              <CurveCard
                key={curve.id}
                curve={curve}
                onEdit={() => navigate(curve.role === 'inflation' ? `/inflation-curves/${curve.id}` : `/yield-curves/${curve.id}`)}
                onDuplicate={() => handleDuplicate(curve)}
                onDelete={() => handleDelete(curve.id, curve.name)}
              />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

function CurveCard({ curve, onEdit, onDuplicate, onDelete }: {
  curve: Curve;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const navigate = useNavigate();
  const isInflation = curve.role === 'inflation';
  const pointCounts = {
    deposits: curve.points.filter(p => p.point_type === 'DepositHelper').length,
    swaps: curve.points.filter(p => p.point_type === 'SwapHelper').length,
    fras: curve.points.filter(p => p.point_type === 'FRAHelper').length,
    ois: curve.points.filter(p => p.point_type === 'OISHelper' || p.point_type === 'DatedOISHelper').length,
  };
  const inflationHelpers = curve.inflation_curve?.points.length || 0;
  const inflationKind = curve.inflation_curve?.kind === 'YoYInflation' ? 'YoY' : 'Zero';
  const inflationIndexId = curve.inflation_curve?.index.id;

  const roleLabel = (curve as any).role === 'forward'
    ? 'Forward'
    : (curve as any).role === 'basis'
      ? 'Basis'
      : (curve as any).role === 'inflation'
        ? 'Inflation'
        : 'Discount';

  return (
    <div className={listStyles.listCard} onClick={onEdit}>
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <h3
              className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] cursor-pointer transition-colors"
              onClick={() => navigate(curve.role === 'inflation' ? `/inflation-curves/${curve.id}` : `/yield-curves/${curve.id}`)}
            >
              {curve.name}
            </h3>
            <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
              {curve.currency}
            </span>
            <span className={`px-2 py-0.5 text-xs font-medium rounded ${roleLabel === 'Discount' ? 'bg-teal-50 text-teal-700' :
                roleLabel === 'Forward' ? 'bg-blue-50 text-blue-700' :
                  roleLabel === 'Inflation' ? 'bg-rose-50 text-rose-700' :
                  'bg-amber-50 text-amber-700'
              }`}>
              {roleLabel}
            </span>
            {isLivePublicCurve(curve) && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded bg-[#e6f6ec] text-[#1f7a44]"
                title="Real public market data (Bank of England / US Treasury) · updated daily"
              >
                <span aria-hidden="true" className="text-[#1f9d55]">&#9679;</span>
                Real public data
              </span>
            )}
          </div>

          {curve.description && (
            <p className="text-sm text-[#737373] mb-3">{curve.description}</p>
          )}

          <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
            {isInflation ? (
              <>
                <span>
                  <span className="font-medium text-[#525252]">{inflationHelpers}</span> helpers
                </span>
                <span className="flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full bg-rose-500"></span>
                  {inflationKind} inflation
                </span>
                {inflationIndexId && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-fuchsia-500"></span>
                    {inflationIndexId}
                  </span>
                )}
                {curve.discount_curve_id && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                    Disc: {curve.discount_curve_id}
                  </span>
                )}
              </>
            ) : (
              <>
                <span>
                  <span className="font-medium text-[#525252]">{curve.points.length}</span> instruments
                </span>
                {pointCounts.deposits > 0 && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-[#8a6a2f]"></span>
                    {pointCounts.deposits} deposits
                  </span>
                )}
                {pointCounts.swaps > 0 && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                    {pointCounts.swaps} swaps
                  </span>
                )}
                {pointCounts.fras > 0 && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-green-500"></span>
                    {pointCounts.fras} FRAs
                  </span>
                )}
                {pointCounts.ois > 0 && (
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-teal-500"></span>
                    {pointCounts.ois} OIS
                  </span>
                )}
              </>
            )}
            <span>•</span>
            <span>{curve.interpolator}</span>
            <span>•</span>
            <span>Ref: {curve.reference_date}</span>
          </div>
        </div>

        <div className={listStyles.hoverActions}>
          <button
            onClick={(e) => { e.stopPropagation(); onDuplicate(); }}
            className={listStyles.duplicateButton}
            title="Duplicate"
          >
            <DuplicateIcon />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete(); }}
            className={listStyles.deleteButton}
            title="Delete"
          >
            <TrashButton />
          </button>
        </div>
      </div>
    </div>
  );
}
