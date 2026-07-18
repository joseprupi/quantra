/**
 * Market-data provenance helpers.
 *
 * The platform now serves ONLY real, daily public market data — Bank of England
 * (SONIA OIS / gilt), US Treasury, FRED (US Federal Reserve) and ECB (European
 * Central Bank). New installs contain no synthetic data; the non-official
 * categories are user-supplied data brought in via the Import screen (`manual`
 * / `csv`) and any leftover `synthetic` rows from older installs, which must
 * render distinctly — never styled like an official feed. These helpers map a
 * quote's `source` tag to a friendly PROVIDER label so the UI can attribute
 * every series clearly, rather than splitting the real feeds into a misleading
 * "live vs not-live" pair.
 */

/** User-supplied `source` tags (imported by the user, not an official feed). */
export const IMPORTED_SOURCES = ['manual', 'csv'];

/** Legacy demo-data tag from pre-real-data installs. Not an official feed. */
const SYNTHETIC_SOURCE = 'synthetic';

/**
 * Real public-data `source` tags → friendly provider label. Case-insensitive.
 * Every official feed maps to the institution that publishes it; everything
 * else that is not `manual`/`csv` falls through to a capitalized raw tag.
 */
const PROVIDER_LABELS: Record<string, string> = {
  boe: 'Bank of England',
  boe_ois: 'Bank of England',
  gilt: 'Bank of England',
  treasury: 'US Treasury',
  fred: 'FRED',
  ecb: 'ECB',
  manual: 'Imported',
  csv: 'Imported',
  synthetic: 'Synthetic (legacy)',
};

function capitalize(s: string): string {
  return s.length === 0 ? s : s[0].toUpperCase() + s.slice(1);
}

/**
 * A friendly provider label for a quote `source` tag. Official feeds resolve to
 * the publishing institution (Bank of England / US Treasury / FRED / ECB);
 * user-supplied values (`manual`/`csv`) read "Imported"; legacy demo rows
 * (`synthetic`) read "Synthetic (legacy)" so they can never pass for an
 * official feed; anything unrecognised is shown as its capitalized raw tag
 * rather than dropped.
 */
export function providerLabel(source?: string | null): string {
  if (!source) return '—';
  const key = source.toLowerCase();
  return PROVIDER_LABELS[key] ?? capitalize(source);
}

/**
 * True when a `source` tag denotes user-supplied data (the Import screen /
 * per-row add-value) rather than an official public feed. This is the ONLY
 * "your data" category now that everything else is real public market data.
 */
export function isImported(source?: string | null): boolean {
  if (!source) return false;
  return IMPORTED_SOURCES.includes(source.toLowerCase());
}

/**
 * True when a `source` tag is the legacy `synthetic` demo tag (rows left over
 * from pre-real-data installs). Such rows are NOT official public data and NOT
 * user imports — the UI must style them distinctly (no official-feed styling).
 */
export function isSyntheticLegacy(source?: string | null): boolean {
  if (!source) return false;
  return source.toLowerCase() === SYNTHETIC_SOURCE;
}

// A curve carries no explicit `source` column, so recognise the real public
// curves by their (seeded) name/description: the BoE SONIA OIS curve and the
// US Treasury / UK gilt government curves.
const LIVE_CURVE_RE = /\bboe\b|sonia\s*ois|treasury|\bgilt\b|daily public/i;

/**
 * True when a curve is one of the real, daily-public feeds (BoE SONIA OIS,
 * US Treasury, UK gilt). Heuristic on the curve's name/description — additive
 * UI labelling only, never pricing logic.
 */
export function isLivePublicCurve(c: { name?: string; description?: string }): boolean {
  return LIVE_CURVE_RE.test(`${c.name ?? ''} ${c.description ?? ''}`);
}
