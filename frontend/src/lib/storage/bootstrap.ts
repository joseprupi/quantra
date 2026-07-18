// Entity-store bootstrap.
//
// Runs once after auth resolves, before the protected routes render:
//
// 1. `POST /auth/provision` — makes sure the caller's `app.users` row exists
//    (every entity table FKs `owner_uid`; a fresh identity's first write
//    would otherwise fail). Idempotent upsert.
// 2. Preloads the backend-backed entity caches (curves, curve sets) so the
//    many synchronous `getSavedCurves()` / `getSavedCurveSets()` call sites
//    across pages, selectors and the save-graph reconstructors read real
//    data instead of an empty pre-load cache.
//
// Failure policy: never blocks the app hard. A failed preload leaves the
// caches empty (pages surface their own load errors / empty states) and a
// failed provision only matters on the first-ever write, which then surfaces
// its own error. Both are logged for diagnosis.
import { orchestratorPost } from '../api/orchestrator';
import type { OrchestratorResult } from '../api/types';
import { ensureCurvesLoaded } from './curves';
import { ensureCurveSetsLoaded } from './curveSets';
import { ensureIndicesLoaded } from './indices';
import { ensureCreditCurvesLoaded } from './creditCurves';
import { ensureVolSurfacesLoaded } from './volSurfaces';
import { ensureSwaptionModelsLoaded } from './swaptionModels';

let bootstrapPromise: Promise<void> | null = null;

// Cold-start race (self-hosted bundle): nginx serves the portal the instant the
// container is up, but the orchestrator upstream it reverse-proxies may still be
// warming. The first `/auth/provision` (and, because they wait on it, the entity
// preloads) then fire against a not-yet-ready backend and log a transient
// `orchestrator.call.failed` on first paint — `Failed to fetch` (transport) or a
// gateway 502/503/504 from nginx — before everything loads fine a moment later.
//
// Fix: retry ONLY the provision, and ONLY on those transient "backend not ready"
// signals, with a bounded budget. Because the preloads already await provision,
// once provision succeeds the orchestrator is warm and the preloads land clean —
// no first-paint error. A genuine failure (auth, 4xx, real 5xx) is NOT retried
// and still surfaces exactly as before. The retry never blocks hard: after the
// budget is spent it gives up and proceeds, same as the pre-fix path.
// Budget sized to cover a realistic cold orchestrator (measured ~3s from
// process start to first served request) with margin, while staying bounded so
// a genuine outage degrades (LoadingScreen → app with its own empty/error
// states) after ~10s rather than hanging forever. Because ProtectedRoute only
// renders the app AFTER bootstrap resolves, waiting here for provision to
// succeed means the preloads AND the first page's own list-GETs all fire
// against an already-warm backend — so the whole first paint is clean.
export const PROVISION_MAX_ATTEMPTS = 20;
const PROVISION_RETRY_DELAY_MS = 500;

/** Transient cold-start signals: transport failure or a gateway not-ready code. */
function isColdStartTransient(result: OrchestratorResult<unknown>): boolean {
  if (result.ok) return false;
  if (result.envelope.code === 'network_error') return true;
  return result.httpStatus === 502 || result.httpStatus === 503 || result.httpStatus === 504;
}

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function provisionWithColdStartRetry(): Promise<OrchestratorResult<{ uid: string }>> {
  // `quiet`: expected cold-start transients must NOT surface as first-paint
  // console errors. A genuine final failure is logged once by runBootstrap.
  let result = await orchestratorPost<{ uid: string }>('/auth/provision', {}, { quiet: true });
  for (let attempt = 2; attempt <= PROVISION_MAX_ATTEMPTS && isColdStartTransient(result); attempt++) {
    await delay(PROVISION_RETRY_DELAY_MS);
    result = await orchestratorPost<{ uid: string }>('/auth/provision', {}, { quiet: true });
  }
  return result;
}

async function runBootstrap(): Promise<void> {
  const provision = await provisionWithColdStartRetry();
  if (!provision.ok) {
    console.error(
      'Entity bootstrap: /auth/provision failed:',
      provision.envelope.code,
      provision.envelope.error,
    );
  }
  const results = await Promise.allSettled([
    ensureCurvesLoaded(),
    ensureCurveSetsLoaded(),
    ensureIndicesLoaded(),
    ensureCreditCurvesLoaded(),
    ensureVolSurfacesLoaded(),
    ensureSwaptionModelsLoaded(),
  ]);
  for (const result of results) {
    if (result.status === 'rejected') {
      console.error('Entity bootstrap: preload failed:', result.reason);
    }
  }
}

/**
 * Idempotent app-level bootstrap; safe to call from multiple mount points.
 * Resolves (never rejects) once provision + preloads have been attempted.
 */
export function bootstrapEntityStores(): Promise<void> {
  if (!bootstrapPromise) {
    bootstrapPromise = runBootstrap().catch(err => {
      console.error('Entity bootstrap failed:', err);
    });
  }
  return bootstrapPromise;
}

