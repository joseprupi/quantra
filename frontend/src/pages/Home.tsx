import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import Header from '../components/Header';
import { DataOverviewStats, loadDataOverviewStats } from '../lib/storage/dataOverview';

export default function Home() {
  const [stats, setStats] = useState<DataOverviewStats | null>(null);

  useEffect(() => {
    void loadDataOverviewStats().then(setStats);
  }, []);

  // "Load full example" is gone: example data are ordinary backend
  // rows seeded server-side (`quantra-backend/scripts/seed_demo_entities.py`),
  // visible from any browser — no browser-local fixtures.

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />

      <main className="max-w-5xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold text-[#0a0a0a]">Home</h1>
          <p className="text-[#737373] mt-1">Workspace snapshot and quick entry points.</p>
        </div>

        <section className="bg-white border border-[#e5e5e5] rounded-xl p-5 mb-6">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-[#0a0a0a]">Loaded Entities</h2>
            <Link to="/settings" className="text-xs text-[#8a6a2f] hover:underline">View in Settings</Link>
          </div>
          {!stats ? (
            <p className="text-sm text-[#737373]">Loading...</p>
          ) : (
            <div className="overflow-hidden border border-[#e5e5e5] rounded-lg">
              <table className="w-full text-sm">
                <thead className="bg-[#fafafa] border-b border-[#e5e5e5]">
                  <tr>
                    <th className="text-left px-3 py-2 text-xs font-semibold text-[#525252] uppercase tracking-wide">Entity</th>
                    <th className="text-right px-3 py-2 text-xs font-semibold text-[#525252] uppercase tracking-wide">Count</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ['Curves', stats.curves],
                    ['Curve Sets', stats.curveSets],
                    ['Vol Surfaces', stats.volSurfaces],
                    ['Credit Curves', stats.creditCurves],
                    ['Products (all)', stats.fixedBonds + stats.floatingBonds + stats.swaps + stats.inflationSwaps + stats.swaptions + stats.cds + stats.equityOptions],
                    ['Swaption Models', stats.swaptionModels],
                    ['Market Data (indices + quote book)', stats.indices + stats.quoteBookEntries],
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

          {stats && stats.total === 0 && (
            <div className="mt-4 border border-[#e5e5e5] rounded-lg p-4 bg-[#fafafa]">
              <p className="text-sm text-[#525252]">
                No entities yet. Demo data is seeded server-side — run{' '}
                <code className="text-xs bg-[#f0f0f0] px-1 py-0.5 rounded">
                  uv run python scripts/seed_demo_entities.py
                </code>{' '}
                in the backend repo, then reload. Or create your first curve
                under Yield Curves.
              </p>
            </div>
          )}
        </section>

        <section className="bg-white border border-[#e5e5e5] rounded-xl p-5">
          <h2 className="text-sm font-semibold text-[#0a0a0a] mb-3">Quick Start</h2>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {[
              ['/yield-curves', 'Yield Curves'],
              ['/inflation-curves', 'Inflation Curves'],
              ['/curve-sets', 'Curve Sets'],
              ['/vol-workbench', 'Volatilities'],
              ['/products/ir-swap', 'Swaps'],
              ['/products/inflation-swaps', 'Inflation Swaps'],
              ['/products/equity-options', 'Equity Options'],
              ['/products/swaption', 'Swaptions'],
              ['/models/swaption', 'Swaption Models'],
            ].map(([to, label]) => (
              <Link
                key={to}
                to={to}
                className="px-3 py-2 text-sm border border-[#e5e5e5] rounded-lg hover:bg-[#f5f5f5] text-[#525252] hover:text-[#0a0a0a] transition-colors"
              >
                {label}
              </Link>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
