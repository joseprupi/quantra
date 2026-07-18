/**
 * Local development auth bypass.
 *
 * Mirrors the backend's dev bypass: when ON, the orchestrator resolves every
 * request to the fixed principal `uid="dev-user"`, `email="dev@quantra.local"`
 * with NO `Authorization`/`X-API-Key` header required. The portal must match
 * that contract so a developer can price locally WITHOUT a Google login.
 *
 * Two ways the bypass turns on:
 *   1. Local dev: a dev build (`import.meta.env.DEV`) with
 *      `VITE_DEV_AUTH_BYPASS=true`. A production build hard-disables this path.
 *   2. Self-hosted image: the container entrypoint injects a runtime
 *      `config.js` with `devAuthBypass: true`. This is the only signal that
 *      survives into a production build, and it exists solely for the
 *      operator-run self-hosted bundle (which pairs with the backend's own
 *      `DEV_AUTH_BYPASS` + `ENV=dev`). The public hosted build ships no such
 *      flag, so the bypass can never turn on there.
 */

import type { User } from './firebase';
import { isRuntimeDevAuthBypass } from './runtimeConfig';

/**
 * True when the dev-auth bypass is active: either the self-hosted runtime
 * config opts in, or a dev build has `VITE_DEV_AUTH_BYPASS=true`.
 * Evaluated per-call (not memoised) so tests can toggle it via `vi.stubEnv`.
 */
export function isDevAuthBypass(): boolean {
  if (isRuntimeDevAuthBypass()) return true;
  return (
    import.meta.env.DEV === true &&
    String(import.meta.env.VITE_DEV_AUTH_BYPASS) === 'true'
  );
}

/**
 * Gates developer-only tooling (the /investigate pricing-trace page and its
 * "View trace" deep links). True in a dev build, AND also true under the
 * self-hosted / demo dev-auth bypass (see `isDevAuthBypass`) so the operator
 * can "see the logs" in the single-user self-hosted bundle and public demo,
 * which are production builds running with runtime `devAuthBypass: true`.
 * Still OFF in the hosted real-auth production build, which ships no dev-bypass
 * flag (`import.meta.env.DEV === false` and no runtime bypass ⇒ false). Moving
 * all three gate sites together keeps the feature coherent: a visible trace
 * link always has a reachable /investigate page.
 */
export function isDevTooling(): boolean {
  return import.meta.env.DEV === true || isDevAuthBypass();
}

/**
 * Fixed mock principal matching the backend bypass identity. Cast to the
 * Firebase `User` type so it slots into the app's existing user shape; only
 * `uid`/`email` (and a no-op `getIdToken`) are ever read under bypass.
 */
export const DEV_USER = {
  uid: 'dev-user',
  email: 'dev@quantra.local',
  displayName: 'Dev User',
  emailVerified: true,
  isAnonymous: false,
  photoURL: null,
  providerData: [],
  // Never sent under bypass, but present so any incidental caller is safe.
  getIdToken: async () => 'dev-bypass-token',
} as unknown as User;
