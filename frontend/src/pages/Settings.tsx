// Settings page - Data management, export/import, preferences
import { useEffect, useRef, useState } from 'react';
import Header from '../components/Header';
import { clearCurves } from '../lib/storage/curves';
import { clearCurveSets } from '../lib/storage/curveSets';
import { fixedBondStore, floatingBondStore } from '../lib/storage/bonds';
import { indexStore } from '../lib/storage/indices';
import { clearIrSwaps } from '../lib/storage/swaps';
import { clearInflationSwaps } from '../lib/storage/inflationSwaps';
import { clearSwaptions } from '../lib/storage/swaptions';
import { clearSwaptionModels } from '../lib/storage/swaptionModels';
import { clearCds } from '../lib/storage/cds';
import { clearVolSurfaces } from '../lib/storage/volSurfaces';
import { clearCreditCurves } from '../lib/storage/creditCurves';
import { clearEquityOptions } from '../lib/storage/equityOptions';
import { buildBackup, importBackup, ExportData } from '../lib/storage/backup';
import { DataOverviewStats, loadDataOverviewStats } from '../lib/storage/dataOverview';

export default function Settings() {
  const [importing, setImporting] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Stats
  const [stats, setStats] = useState<DataOverviewStats | null>(null);

  // Load stats on mount
  useEffect(() => {
    loadStats();
  }, []);

  async function loadStats() {
    setStats(await loadDataOverviewStats());
  }

  async function handleExportAll() {
    try {
      const exportData = await buildBackup();

      const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `quantra-backup-${new Date().toISOString().split('T')[0]}.json`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      setMessage({
        type: 'success',
        text:
          `Exported ${exportData.curves.length} curves, ${exportData.curveSets?.length ?? 0} curve sets, ` +
          `${exportData.indices?.length ?? 0} indices, ${exportData.volSurfaces?.length ?? 0} vol surfaces, ` +
          `${exportData.creditCurves?.length ?? 0} credit curves, ${exportData.fixedBonds.length} fixed bonds, ` +
          `${exportData.floatingBonds.length} floating bonds, ${exportData.swaps?.length ?? 0} swaps, ` +
          `${exportData.inflationSwaps?.length ?? 0} inflation swaps, ${exportData.swaptions?.length ?? 0} swaptions, ` +
          `${exportData.swaptionModels?.length ?? 0} swaption models, ${exportData.cds?.length ?? 0} cds, ` +
          `${exportData.equityOptions?.length ?? 0} equity options`,
      });
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to export data' });
    }
  }

  async function handleImportAll(file: File, skipConfirm = false, includeMarketData = true) {
    if (!skipConfirm && !confirm('Importing a backup will overwrite existing data. Continue?')) {
      return;
    }
    setImporting(true);
    setMessage(null);

    try {
      const content = await file.text();
      const data = JSON.parse(content) as ExportData;

      const c = await importBackup(data, includeMarketData);

      await loadStats();
      setMessage({
        type: 'success',
        text: `Imported ${c.curves} curves, ${c.curveSets} curve sets, ${c.indices} indices, ${c.quoteBook} quote book entries, ${c.volSurfaces} vol surfaces, ${c.creditCurves} credit curves, ${c.fixedBonds} fixed bonds, ${c.floatingBonds} floating bonds, ${c.swaps} swaps, ${c.inflationSwaps} inflation swaps, ${c.swaptions} swaptions, ${c.swaptionModels} swaption models, ${c.cds} cds, ${c.equityOptions} equity options`
      });
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to import data. Make sure it\'s a valid Quantra backup file.' });
    } finally {
      setImporting(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  }

  async function handleLoadFullExample() {
    if (!confirm('Load full example (portfolio + market data)? This will overwrite existing data.')) {
      return;
    }
    try {
      const backupResponse = await fetch('/example-backup.json');
      if (!backupResponse.ok) throw new Error('Failed to load example backup');
      const backupBlob = await backupResponse.blob();
      const backupFile = new File([backupBlob], 'example-backup.json', { type: 'application/json' });

      // Load portfolio/structures first
      await handleImportAll(backupFile, true, false);

      const marketResponse = await fetch('/example-market-data.json');
      if (!marketResponse.ok) throw new Error('Failed to load example market data');
      const marketBlob = await marketResponse.blob();
      const marketFile = new File([marketBlob], 'example-market-data.json', { type: 'application/json' });

      // Then load quote book + quotes + vols
      await handleImportAll(marketFile, true, true);
      setMessage({ type: 'success', text: 'Loaded full example (portfolio + market data)' });
    } catch {
      setMessage({ type: 'error', text: 'Failed to load full example' });
    }
  }

  async function handleClearAll() {
    // Strong, typed guard: this deletes SERVER rows and cannot be undone.
    const typed = window.prompt(
      'This permanently deletes ALL your entities FROM THE SERVER — curves, curve sets, ' +
        'indices, credit curves, vol surfaces, swaption models, fixed & floating bonds, ' +
        'swaps, inflation swaps, swaptions, CDS, and equity options. This cannot be undone.\n\n' +
        'Type DELETE to confirm.',
    );
    if (typed !== 'DELETE') {
      setMessage({ type: 'error', text: 'Clear all cancelled.' });
      return;
    }

    try {
      // Delete every owner-scoped entity type through the backend-backed stores
      // (owner isolation #5 holds — each store deletes only the caller's rows).
      // Curve sets before curves so a set is never briefly left referencing a
      // deleted curve.
      await clearCurveSets();
      await clearCurves();
      await indexStore.clear();
      await clearCreditCurves();
      await clearVolSurfaces();
      await clearSwaptionModels();
      await fixedBondStore.clear();
      await floatingBondStore.clear();
      await clearIrSwaps();
      await clearInflationSwaps();
      await clearSwaptions();
      await clearCds();
      await clearEquityOptions();

      await loadStats();
      setMessage({ type: 'success', text: 'Cleared all server data.' });
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to clear data' });
    }
  }

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />

      <main className="max-w-3xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <h1 className="text-2xl font-semibold text-[#0a0a0a] mb-2">Settings</h1>
        <p className="text-[#737373] mb-8">Manage your data, preferences, and account</p>

        {/* Message */}
        {message && (
          <div className={`mb-6 p-4 rounded-lg ${message.type === 'success'
              ? 'bg-green-50 border border-green-200 text-green-700'
              : 'bg-red-50 border border-red-200 text-red-700'
            }`}>
            <p className="text-sm">{message.text}</p>
          </div>
        )}

        {/* Data Overview */}
        <section className="bg-white border border-[#e5e5e5] rounded-xl p-6 mb-6">
          <h2 className="text-lg font-semibold text-[#0a0a0a] mb-4">Data Overview</h2>

          {stats && (
            <div className="overflow-hidden border border-[#e5e5e5] rounded-lg mb-6">
              <table className="w-full text-sm">
                <thead className="bg-[#fafafa] border-b border-[#e5e5e5]">
                  <tr>
                    <th className="text-left px-3 py-2 text-xs font-semibold text-[#525252] uppercase tracking-wide">Entity</th>
                    <th className="text-right px-3 py-2 text-xs font-semibold text-[#525252] uppercase tracking-wide">Count</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ['Indices', stats.indices],
                    ['Curves', stats.curves],
                    ['Curve Sets', stats.curveSets],
                    ['Quote Book Entries', stats.quoteBookEntries],
                    ['Vol Surfaces', stats.volSurfaces],
                    ['Credit Curves', stats.creditCurves],
                    ['Fixed Bonds', stats.fixedBonds],
                    ['Floating Bonds', stats.floatingBonds],
                    ['Swaps', stats.swaps],
                    ['Inflation Swaps', stats.inflationSwaps],
                    ['Swaptions', stats.swaptions],
                    ['Swaption Models', stats.swaptionModels],
                    ['CDS', stats.cds],
                    ['Equity Options', stats.equityOptions],
                    ['Total', stats.total],
                  ].map(([label, value]) => (
                    <tr key={String(label)} className="border-t border-[#f0f0f0]">
                      <td className="px-3 py-1.5 text-[#525252]">{label}</td>
                      <td className="px-3 py-1.5 text-right font-medium text-[#0a0a0a]">{value as number}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-sm text-[#737373]">
            Your entities are stored on the Quantra server (owner-scoped) and are the same from any browser you sign in to. This browser keeps only UI preferences locally. Market data (quotes) lives in the Quantra market-data server and is browsable from the Quote Book.
          </p>
        </section>

        {/* Export & Import */}
        <section className="bg-white border border-[#e5e5e5] rounded-xl p-6 mb-6">
          <h2 className="text-lg font-semibold text-[#0a0a0a] mb-4">Export & Import</h2>

          <div className="space-y-4">
            {/* Export All */}
            <div className="flex items-center justify-between p-4 bg-[#f5f5f5] rounded-lg">
              <div>
                <p className="font-medium text-[#0a0a0a]">Download Everything</p>
                <p className="text-sm text-[#737373]">Export all curves, indices, and bonds as a JSON backup file</p>
              </div>
              <button
                onClick={handleExportAll}
                className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] transition-colors flex items-center gap-2"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
                Export
              </button>
            </div>

            {/* Import */}
            <div className="flex items-center justify-between p-4 bg-[#f5f5f5] rounded-lg">
              <div>
                <p className="font-medium text-[#0a0a0a]">Import Backup</p>
                <p className="text-sm text-[#737373]">Restore data from a Quantra backup file</p>
              </div>
              <div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".json"
                  onChange={e => e.target.files?.[0] && handleImportAll(e.target.files[0])}
                  className="hidden"
                  id="import-file"
                />
                <label
                  htmlFor="import-file"
                  className={`px-4 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors cursor-pointer flex items-center gap-2 ${importing ? 'opacity-50 pointer-events-none' : ''}`}
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                  </svg>
                  {importing ? 'Importing...' : 'Import'}
                </label>
              </div>
            </div>

            {/* Load full example */}
            <div className="flex items-center justify-between p-4 bg-[#f5f5f5] rounded-lg">
              <div>
                <p className="font-medium text-[#0a0a0a]">Load Full Example</p>
                <p className="text-sm text-[#737373]">Import portfolio + market data in one click</p>
              </div>
              <button
                onClick={handleLoadFullExample}
                className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] transition-colors flex items-center gap-2"
              >
                Load Full Example
              </button>
            </div>
          </div>
        </section>

        {/* Danger Zone */}
        <section className="bg-white border border-red-200 rounded-xl p-6">
          <h2 className="text-lg font-semibold text-red-600 mb-4">Danger Zone</h2>

          <div className="flex items-center justify-between p-4 bg-red-50 rounded-lg">
            <div>
              <p className="font-medium text-red-700">Clear All Data</p>
              <p className="text-sm text-red-600">Permanently delete every entity you own FROM THE SERVER — curves, curve sets, indices, credit curves, vol surfaces, swaption models, bonds, swaps, inflation swaps, swaptions, CDS, and equity options. Requires typing DELETE. This cannot be undone.</p>
            </div>
            <button
              onClick={handleClearAll}
              className="px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700 transition-colors"
            >
              Clear All
            </button>
          </div>
        </section>

        {/* Storage Info */}
        <section className="mt-6 text-center">
          <p className="text-xs text-[#a3a3a3]">
            Storage: Quantra server (Postgres) • Version {__APP_VERSION__}
          </p>
        </section>
      </main>
    </div>
  );
}
