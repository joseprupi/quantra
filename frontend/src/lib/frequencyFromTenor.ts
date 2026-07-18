import type { Frequency } from './types';

// The floating leg's coupon payment frequency should FOLLOW the picked
// index's tenor by default (a 3M index pays quarterly, a 6M index
// semi-annually, ...). This mirrors the orchestrator's own tenor→frequency
// derivation so the portal default and the
// backend-side derivation can never disagree. Keyed by the tenor's length in
// months so a `Years` tenor normalises through the same table (1Y -> 12 ->
// Annual). Tenors with no clean integer annual frequency (e.g. 5M, 3W) map to
// `null` so the caller keeps its current default instead of picking a wrong
// schedule.
const MONTHS_TO_FREQUENCY: Record<number, Frequency> = {
  1: 'Monthly',
  2: 'Bimonthly',
  3: 'Quarterly',
  4: 'EveryFourthMonth',
  6: 'Semiannual',
  12: 'Annual',
};

const WEEKS_TO_FREQUENCY: Record<number, Frequency> = {
  1: 'Weekly',
  2: 'Biweekly',
  4: 'EveryFourthWeek',
};

/**
 * Map an index tenor (n, unit) to the matching coupon payment frequency, or
 * `null` when the tenor has no exact integer annual frequency.
 */
export function frequencyFromIndexTenor(
  tenorNumber: number | null | undefined,
  tenorTimeUnit: string | null | undefined,
): Frequency | null {
  const n = Number(tenorNumber);
  if (!Number.isInteger(n) || n <= 0) return null;
  const unit = String(tenorTimeUnit ?? '').toLowerCase();
  if (unit === 'years') return MONTHS_TO_FREQUENCY[n * 12] ?? null;
  if (unit === 'months') return MONTHS_TO_FREQUENCY[n] ?? null;
  if (unit === 'weeks') return WEEKS_TO_FREQUENCY[n] ?? null;
  if (unit === 'days') return n === 1 ? 'Daily' : null;
  return null;
}

/**
 * Same derivation off an index definition shape — accepts both the canonical
 * `tenor: {n, unit}` and the legacy flat `tenor_number`/`tenor_time_unit`
 * fields (local index-store records still carry the flat pair).
 */
export function frequencyFromIndexDef(
  def:
    | {
        tenor?: { n?: number; unit?: string } | null;
        tenor_number?: number | null;
        tenor_time_unit?: string | null;
      }
    | null
    | undefined,
): Frequency | null {
  if (!def) return null;
  const n = def.tenor?.n ?? def.tenor_number;
  const unit = def.tenor?.unit ?? def.tenor_time_unit;
  return frequencyFromIndexTenor(n, unit);
}
