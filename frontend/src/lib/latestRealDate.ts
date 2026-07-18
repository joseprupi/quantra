/**
 * Latest real (public) market-data date.
 *
 * The self-hosted bundle now ingests REAL, daily public market data alongside
 * the synthetic seed — a genuine Bank of England SONIA OIS curve (series under
 * `GBP.RATES.BOE.OIS.`) plus US Treasury / UK gilt government curves. The
 * real BoE OIS curve's `reference_date` auto-rolls daily to the freshest
 * ingested business date, and QuantLib couples the pricing As-Of to the
 * selected curve's `reference_date` — so to price a real GBP swap out of the
 * box the pricing As-Of must default to that latest real date, NOT the frozen
 * synthetic `DEFAULT_AS_OF`.
 *
 * `GET /v1/market-data/latest-date?prefix=…` returns the freshest ingested
 * date for a given canonical-id prefix, or `latest_date: null` (HTTP 200) when
 * nothing has been ingested yet. Every helper here degrades to the existing
 * default when the endpoint is empty, errors, or is unreachable — so hosted /
 * dev builds (no real feed) behave exactly as before.
 */

import { orchestratorGet } from './api/orchestrator';
import { getDefaultAsOf } from './runtimeConfig';

/** Canonical-id prefix for the real BoE SONIA OIS series. */
export const GBP_OIS_PREFIX = 'GBP.RATES.BOE.OIS.';

/** Matches a strict ISO calendar date `YYYY-MM-DD`. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** `GET /v1/market-data/latest-date` response body. */
export interface LatestDateResponse {
  /** Freshest ingested business date, or `null` when there's no data yet. */
  latest_date: string | null;
  source?: string | null;
  prefix?: string | null;
}

function todayString(): string {
  return new Date().toISOString().split('T')[0];
}

/**
 * Fetch the freshest ingested real-data business date for `prefix` (default:
 * the BoE SONIA OIS series). Returns the ISO date string, or `null` when the
 * endpoint reports no data (`latest_date: null`), returns a malformed value,
 * errors, or is unreachable — callers then fall back to the existing default.
 */
export async function fetchLatestRealDate(
  prefix: string = GBP_OIS_PREFIX,
): Promise<string | null> {
  const res = await orchestratorGet<LatestDateResponse>(
    `/v1/market-data/latest-date?prefix=${encodeURIComponent(prefix)}`,
  );
  if (
    res.ok &&
    res.data &&
    typeof res.data.latest_date === 'string' &&
    ISO_DATE.test(res.data.latest_date)
  ) {
    return res.data.latest_date;
  }
  return null;
}

/**
 * Resolve the pricing As-Of default: the latest real-data date when available,
 * otherwise the existing static default (`DEFAULT_AS_OF` runtime override, else
 * "today"). This is the coherence anchor for pricing a real MD-backed curve —
 * the As-Of equals the real curve's auto-rolling `reference_date`.
 */
export async function resolveDefaultAsOf(): Promise<string> {
  const real = await fetchLatestRealDate();
  if (real) return real;
  return getDefaultAsOf() ?? todayString();
}
