// Global As-Of Date — persisted in localStorage, used across the portal
// This is the single source of truth for the pricing/valuation date.

import { getDefaultAsOf } from './runtimeConfig';
import { fetchLatestRealDate } from './latestRealDate';

const AS_OF_DATE_KEY = 'quantra_as_of_date';
// Marks the As-Of value we set automatically from the latest real-data date.
// When the stored As-Of equals this marker the current value is OUR auto
// default (not a user choice), so it may be refreshed to a newer real date on
// a later load; when it differs the user picked it and we never touch it.
const AS_OF_AUTO_KEY = 'quantra_as_of_auto';

function todayString(): string {
  return new Date().toISOString().split('T')[0];
}

/**
 * The initial As-Of when the user has not yet chosen one (no localStorage
 * value). A self-hosted image may inject an optional `DEFAULT_AS_OF` so a fresh
 * user's default click-Price anchors on the bundle's demo-data date; when that
 * is absent (dev / public hosted) this stays "today", exactly as before.
 */
function initialAsOf(): string {
  return getDefaultAsOf() ?? todayString();
}

export function getAsOfDate(): string {
  try {
    const saved = localStorage.getItem(AS_OF_DATE_KEY);
    if (saved && /^\d{4}-\d{2}-\d{2}$/.test(saved)) return saved;
    return initialAsOf();
  } catch {
    return initialAsOf();
  }
}

export function setAsOfDate(date: string): void {
  localStorage.setItem(AS_OF_DATE_KEY, date);
  // A user (or any explicit) set clears the auto marker: this is now a chosen
  // value we must not overwrite with a rolling real date.
  try {
    localStorage.removeItem(AS_OF_AUTO_KEY);
  } catch {
    /* ignore */
  }
  // Dispatch a storage event so other components can react
  window.dispatchEvent(new CustomEvent('quantra-asofdate-change', { detail: date }));
}

/**
 * True when the stored As-Of is an explicit USER choice — i.e. a stored value
 * exists that is NOT the one we auto-applied from the latest real-data date.
 * A fresh user (no stored value) is not a user choice, so the auto default may
 * apply; once auto-applied, the value equals the marker and is still eligible
 * to roll forward until the user overrides it.
 */
export function userHasChosenAsOf(): boolean {
  try {
    const saved = localStorage.getItem(AS_OF_DATE_KEY);
    if (!saved) return false;
    return saved !== localStorage.getItem(AS_OF_AUTO_KEY);
  } catch {
    return false;
  }
}

/** Set the As-Of from an automatic (real-data) source, tagging the marker. */
function setAsOfDateAuto(date: string): void {
  localStorage.setItem(AS_OF_DATE_KEY, date);
  localStorage.setItem(AS_OF_AUTO_KEY, date);
  window.dispatchEvent(new CustomEvent('quantra-asofdate-change', { detail: date }));
}

/**
 * Default the global As-Of to the latest REAL-data date when the user has not
 * made an explicit choice. Robust by design: if the user already picked an
 * As-Of, or the real-data endpoint returns no date / errors, this is a no-op
 * and the existing default (`DEFAULT_AS_OF`, else "today") stands — so hosted /
 * dev / fresh-synthetic bundles regress in no way.
 *
 * Returns the applied real date, or `null` when nothing was changed.
 */
export async function applyRealDataAsOfDefault(): Promise<string | null> {
  if (userHasChosenAsOf()) return null;
  const real = await fetchLatestRealDate();
  if (!real) return null;
  // Re-check after the await: a user may have picked a date meanwhile.
  if (userHasChosenAsOf()) return null;
  // Nothing to do if it's already the same real date.
  if (localStorage.getItem(AS_OF_DATE_KEY) === real) {
    localStorage.setItem(AS_OF_AUTO_KEY, real);
    return null;
  }
  setAsOfDateAuto(real);
  return real;
}
