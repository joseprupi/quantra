import { providerLabel, isImported, isSyntheticLegacy } from '../../lib/provenance';

/**
 * Provenance chip for a market-data quote's `source` tag.
 *
 * Everything the platform serves is REAL public market data — Bank of England,
 * US Treasury, FRED and ECB — so all official feeds render identically (a green
 * dot + the publishing provider) to read as equally real. Two visually distinct
 * cases exist: user-supplied data (`manual`/`csv`) shows a neutral "Imported"
 * chip so it is clearly your own data, and leftover legacy `synthetic` rows
 * from older installs show an amber "Synthetic (legacy)" chip so they can
 * never pass for an official feed.
 */
export default function SourceChip({ source }: { source?: string | null }) {
  if (!source) return <span className="text-xs text-[#a3a3a3]">—</span>;
  const label = providerLabel(source);
  if (isSyntheticLegacy(source)) {
    return (
      <span
        className="inline-flex px-2 py-0.5 text-xs font-medium rounded bg-[#fdf3d6] text-[#7a5b12]"
        title="Legacy synthetic demo data — not real market prices"
      >
        {label}
      </span>
    );
  }
  const imported = isImported(source);
  return (
    <span
      className={
        imported
          ? 'inline-flex px-2 py-0.5 text-xs font-medium rounded bg-[#f5f5f5] text-[#525252]'
          : 'inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded bg-[#e6f6ec] text-[#1f7a44]'
      }
      title={
        imported
          ? 'Imported by you — user-supplied data'
          : `Real public market data · ${label} · updated daily`
      }
    >
      {!imported && <span aria-hidden="true" className="text-[#1f9d55]">&#9679;</span>}
      {label}
    </span>
  );
}
