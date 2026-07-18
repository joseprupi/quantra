/**
 * Stack endpoints for the FULL journey suite.
 *
 * The suite runs against a live self-hosted stack (root docker-compose.yml).
 * All backend calls go through the portal's same-origin reverse proxy
 * (`/v1/...`), exactly like the browser does, so a single URL describes the
 * whole stack.
 */

export const PORTAL_URL = process.env.E2E_PORTAL_URL || 'http://localhost:5173'

/** Orchestrator API base as the browser reaches it (same-origin proxy). */
export const API_URL = `${PORTAL_URL}/v1`

/** Unique-enough suffix so parallel workers / re-runs never collide on names. */
export function uniq(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

/**
 * A unique canonical id valid per the backend grammar
 * (`CCY.FAMILY.<tenor>` with FAMILY = 2–6 uppercase letters).
 */
export function uniqCanonicalId(tenor = '5Y'): string {
  const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  let fam = 'E'
  for (let i = 0; i < 5; i++) fam += letters[Math.floor(Math.random() * letters.length)]
  return `USD.${fam}.${tenor}`
}

/** Names of the two REAL seeded curves (see backend/scripts/seed_demo_entities.py). */
export const SEEDED = {
  gbpOisCurveName: 'GBP SONIA OIS (BoE, daily public)',
  usdTreasuryCurveName: 'USD Treasury (public, daily)',
  soniaIndexId: 'SONIA',
  sofrIndexId: 'SOFR',
  boeQuotePrefix: 'GBP.RATES.BOE.OIS.',
  ustQuotePrefix: 'USD.RATES.UST.OFFICIAL.',
}
