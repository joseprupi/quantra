/**
 * Typed HTTP client for the Quantra orchestrator.
 *
 * Talks only to the orchestrator (VITE_ORCHESTRATOR_URL). Does not call the
 * pricing engine, MD service, or Postgres directly (invariant 1).
 *
 * Error handling branches on `envelope.code`, never on the `error` prose
 * string.
 */

import { auth } from '../firebase';
import { isDevAuthBypass } from '../devAuth';
import { getOrchestratorUrl } from '../runtimeConfig';
import type { components } from './_generated/orchestrator';
import type { ApiErrorEnvelope, OrchestratorResult } from './types';

// Portal end of the correlation thread: mint a UUID per
// orchestrator call and send it as `X-Request-ID`. The orchestrator honours an
// inbound id and propagates the same value to the engine (gRPC `x-request-id`)
// and the MD service (`X-Request-ID` header), so this single header is what
// stitches the portal console line to every backend log for one user action.
function newRequestId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  // Fallback for environments without crypto.randomUUID (older test runners).
  return 'rid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

// One structured line per orchestrator call (portal slice:
// {event, method, route, request_id, http_status, duration_ms}). Console-only,
// dev-grade — aggregation is deferred. `error` flags the failing path.
function logOrchestratorCall(entry: {
  method: HttpMethod;
  route: string;
  request_id: string;
  http_status: number;
  duration_ms: number;
  error?: string;
}): void {
  const line = {
    service: 'portal',
    event: entry.error ? 'orchestrator.call.failed' : 'orchestrator.call',
    method: entry.method,
    route: entry.route,
    request_id: entry.request_id,
    http_status: entry.http_status,
    duration_ms: Math.round(entry.duration_ms),
    ...(entry.error ? { error: entry.error } : {}),
  };
  if (entry.error) console.error('[orchestrator]', line);
  else console.info('[orchestrator]', line);
}

async function getAuthHeaders(): Promise<Record<string, string>> {
  // Dev bypass (mirrors the backend): the orchestrator pins every request to a
  // fixed `dev-user` principal and requires NO auth header. There is no real
  // Firebase user under bypass, so `getIdToken()` would throw — send the call
  // with no Authorization header instead.
  if (isDevAuthBypass()) {
    return { 'Content-Type': 'application/json' };
  }
  const user = auth.currentUser;
  if (!user) {
    throw new Error('Not authenticated. Please sign in.');
  }
  try {
    const token = await user.getIdToken();
    return {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    };
  } catch (error) {
    console.error('Failed to get auth token:', error);
    throw new Error('Authentication failed. Please sign in again.');
  }
}

type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE' | 'PUT';

interface FetchOptions {
  method: HttpMethod;
  /** When true, `body` is JSON-serialised and sent (POST/PATCH/PUT). */
  hasBody?: boolean;
  body?: unknown;
  /**
   * A `multipart/form-data` body (e.g. a CSV file upload). When present it is
   * sent verbatim and the `Content-Type` header is dropped so the browser sets
   * the correct `multipart/form-data; boundary=…` itself. Auth headers
   * (Authorization under a real login, none under dev-bypass) still apply.
   */
  formBody?: FormData;
  /**
   * When false, a successful response is assumed to carry no JSON body
   * (e.g. a 204 from DELETE) and `data` is resolved as `undefined`.
   */
  parseJson?: boolean;
  /** Extra request headers merged over the auth headers (e.g. `If-Match`). */
  extraHeaders?: Record<string, string>;
  /**
   * Suppress the `console.error('orchestrator.call.failed')` line on a failed
   * call. The call still fully executes and returns the same result — only the
   * error-level log is muted. Used by the cold-start provision probe
   * (bootstrap), which retries a not-yet-ready backend on purpose and does not
   * want each expected transient attempt to surface as a first-paint console
   * error; a genuine final failure is logged once by the caller instead.
   */
  quiet?: boolean;
}

// Single fetch core shared by every verb. Error handling branches on
// `envelope.code`: on a non-2xx the orchestrator's structured error body is
// surfaced verbatim when present, otherwise a synthetic envelope is built from
// the HTTP status. Transport/auth failures map to `network_error` /
// `unauthenticated`.
async function orchestratorFetch<T>(
  path: string,
  opts: FetchOptions,
): Promise<OrchestratorResult<T>> {
  const startTime = performance.now();
  const requestId = newRequestId();
  try {
    const headers: Record<string, string> = {
      ...(await getAuthHeaders()),
      'X-Request-ID': requestId,
      ...(opts.extraHeaders ?? {}),
    };
    if (opts.formBody) {
      // Let the browser set `multipart/form-data; boundary=…`; a manual
      // Content-Type would omit the boundary and break server-side parsing.
      delete headers['Content-Type'];
    }
    const init: RequestInit = { method: opts.method, headers };
    if (opts.formBody) {
      init.body = opts.formBody;
    } else if (opts.hasBody) {
      init.body = JSON.stringify(opts.body);
    }
    const response = await fetch(`${getOrchestratorUrl()}${path}`, init);
    const duration_ms = performance.now() - startTime;

    if (!response.ok) {
      let envelope: ApiErrorEnvelope = {
        error: `HTTP ${response.status}`,
        code: `http_${response.status}`,
      };
      try {
        const raw = (await response.json()) as unknown;
        if (raw && typeof raw === 'object' && 'code' in raw && 'error' in raw) {
          envelope = raw as ApiErrorEnvelope;
        }
      } catch {
        // Body was not JSON; keep the synthesised envelope.
      }
      // The orchestrator echoes the id in the error envelope; fall back to the id
      // we sent so the support handle is always present even on a bare body.
      if (!envelope.request_id) envelope.request_id = requestId;
      if (!opts.quiet) {
        logOrchestratorCall({
          method: opts.method,
          route: path,
          request_id: envelope.request_id,
          http_status: response.status,
          duration_ms,
          error: envelope.code,
        });
      }
      return { ok: false, envelope, httpStatus: response.status, duration_ms, requestId };
    }

    const data = opts.parseJson === false ? (undefined as T) : ((await response.json()) as T);
    logOrchestratorCall({
      method: opts.method,
      route: path,
      request_id: requestId,
      http_status: response.status,
      duration_ms,
    });
    return { ok: true, data, duration_ms, requestId };
  } catch (error) {
    const duration_ms = performance.now() - startTime;
    const message = error instanceof Error ? error.message : 'Unknown error';
    const isAuth =
      message.startsWith('Not authenticated') || message.startsWith('Authentication failed');
    if (!opts.quiet) {
      logOrchestratorCall({
        method: opts.method,
        route: path,
        request_id: requestId,
        http_status: isAuth ? 401 : 0,
        duration_ms,
        error: isAuth ? 'unauthenticated' : 'network_error',
      });
    }
    return {
      ok: false,
      envelope: {
        error: message,
        code: isAuth ? 'unauthenticated' : 'network_error',
        request_id: requestId,
      },
      httpStatus: isAuth ? 401 : 0,
      duration_ms,
      requestId,
    };
  }
}

// Optional audit-reason plumbing: every mutating orchestrator route accepts an
// `X-Change-Reason` header which the backend records verbatim on the entity's
// audit-trail version row. Absent (the default) the header is not sent.
function changeReasonHeaders(changeReason?: string): Record<string, string> | undefined {
  const trimmed = changeReason?.trim();
  return trimmed ? { 'X-Change-Reason': trimmed } : undefined;
}

export function orchestratorPost<T>(
  path: string,
  body: unknown,
  opts?: { quiet?: boolean; changeReason?: string },
): Promise<OrchestratorResult<T>> {
  return orchestratorFetch<T>(path, {
    method: 'POST',
    hasBody: true,
    body,
    quiet: opts?.quiet,
    extraHeaders: changeReasonHeaders(opts?.changeReason),
  });
}

export function orchestratorGet<T>(path: string): Promise<OrchestratorResult<T>> {
  return orchestratorFetch<T>(path, { method: 'GET' });
}

/**
 * POST a `multipart/form-data` body (e.g. a file upload) to the orchestrator.
 * Same-origin + dev-bypass-aware like every other verb; the browser owns the
 * `Content-Type`/boundary. Errors surface as the same code-branching
 * `OrchestratorResult` as the JSON verbs.
 */
export function orchestratorPostForm<T>(
  path: string,
  form: FormData,
): Promise<OrchestratorResult<T>> {
  return orchestratorFetch<T>(path, { method: 'POST', formBody: form });
}

// Market-data import

/** One user-supplied quote in the JSON import batch. */
export interface MarketDataImportQuote {
  canonical_id: string;
  /** Observation date, `YYYY-MM-DD`. */
  as_of: string;
  value: number;
  /** Optional per-row provenance override (defaults server-side to `manual`). */
  source?: string;
  /** Optional free-form per-row metadata. */
  meta?: Record<string, unknown>;
}

/** One soft, per-row failure surfaced in the import report body (HTTP 200). */
export interface MarketDataImportRowError {
  row: number;
  reason: string;
}

/** The `POST /v1/market-data/import` per-row report (HTTP 200). */
export interface MarketDataImportReport {
  imported: number;
  skipped: number;
  errors: MarketDataImportRowError[];
  /** Effective default provenance tag: `csv` or `manual`. */
  source: string;
}

/**
 * Import a manual JSON batch of quotes into the shared `md.*` catalog. Server
 * tags `source="manual"` (a per-row `source` overrides it). Per-row failures
 * come back SOFT in `report.errors` with a 200 — only whole-request problems
 * (empty batch, bad body) are a `!ok` result.
 */
export function importMarketDataJson(
  quotes: MarketDataImportQuote[],
): Promise<OrchestratorResult<MarketDataImportReport>> {
  return orchestratorPost<MarketDataImportReport>('/v1/market-data/import', { quotes });
}

/**
 * Import a CSV upload into the shared `md.*` catalog. Server tags
 * `source="csv"`. The CSV rows are
 * `canonical_id, as_of (YYYY-MM-DD), value[, source, meta_json]` with an
 * optional auto-detected header. Same soft/hard error split as the JSON path.
 */
export function importMarketDataCsv(
  file: File,
): Promise<OrchestratorResult<MarketDataImportReport>> {
  const form = new FormData();
  form.append('file', file);
  return orchestratorPostForm<MarketDataImportReport>('/v1/market-data/import', form);
}

// Optional `If-Match` precondition. Defaults to NOT sending the
// header — today's behaviour. The orchestrator PATCH does not yet honour
// `If-Match`; this is the client-side half of
// the optimistic-concurrency scaffolding and is a no-op server-side until the
// backend returns 412 on mismatch.
export function orchestratorPatch<T>(
  path: string,
  body: unknown,
  opts?: { ifMatch?: string; changeReason?: string },
): Promise<OrchestratorResult<T>> {
  const extraHeaders: Record<string, string> = {
    ...(opts?.ifMatch !== undefined ? { 'If-Match': opts.ifMatch } : {}),
    ...(changeReasonHeaders(opts?.changeReason) ?? {}),
  };
  return orchestratorFetch<T>(path, {
    method: 'PATCH',
    hasBody: true,
    body,
    extraHeaders: Object.keys(extraHeaders).length > 0 ? extraHeaders : undefined,
  });
}

// DELETE is soft on the named entities (reversible via `:restore`); returns
// 204 with no body.
export function orchestratorDelete(
  path: string,
  opts?: { changeReason?: string },
): Promise<OrchestratorResult<void>> {
  return orchestratorFetch<void>(path, {
    method: 'DELETE',
    parseJson: false,
    extraHeaders: changeReasonHeaders(opts?.changeReason),
  });
}

// Market-data SERIES CRUD (md.canonical_ids)

/**
 * One `md.canonical_ids` catalog row — the 10 structured catalog columns plus
 * the two audit timestamps. This is the authoritative shape returned by every
 * series CRUD route (`SeriesOut`), and the source for both the Quote Book's
 * series-management table and the Import screen's canonical_id dropdown.
 */
export interface MarketDataSeries {
  canonical_id: string;
  asset_class: string;
  family: string | null;
  instrument: string;
  currency: string;
  tenor: string | null;
  field: string;
  frequency: string;
  units: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

/** `GET /v1/market-data/series` → `{series, count}`. */
export interface MarketDataSeriesList {
  series: MarketDataSeries[];
  count: number;
}

/**
 * Body for `POST /v1/market-data/series`. `canonical_id`/`asset_class`/
 * `currency` are required; every other structural field is optional and, when
 * omitted, is derived server-side from the canonical-id grammar (`frequency`
 * defaults `daily`, `units` `decimal_rate`).
 */
export interface MarketDataSeriesCreate {
  canonical_id: string;
  asset_class: string;
  currency: string;
  family?: string;
  instrument?: string;
  tenor?: string;
  field?: string;
  frequency?: string;
  units?: string;
  description?: string;
}

/**
 * Body for `PATCH /v1/market-data/series/{id}`. Every field is optional and
 * only the supplied fields change; the canonical_id itself is immutable. A
 * required metadata field blanked to `""` is rejected server-side (422).
 */
export type MarketDataSeriesPatch = Partial<Omit<MarketDataSeriesCreate, 'canonical_id'>>;

/** `DELETE /v1/market-data/series/{id}` → outcome + cascaded value count. */
export interface MarketDataSeriesDelete {
  canonical_id: string;
  deleted: boolean;
  deleted_quote_points: number;
}

/** List every series (management table + Import dropdown source). */
export function listSeries(): Promise<OrchestratorResult<MarketDataSeriesList>> {
  return orchestratorGet<MarketDataSeriesList>('/v1/market-data/series');
}

/**
 * Create a series definition. **201** `SeriesOut` on success; **409**
 * `market_data_series_exists` on a duplicate id; **422**
 * `market_data_series_invalid` on bad canonical-id grammar.
 */
export function createSeries(
  body: MarketDataSeriesCreate,
): Promise<OrchestratorResult<MarketDataSeries>> {
  return orchestratorPost<MarketDataSeries>('/v1/market-data/series', body);
}

/**
 * Edit a series' metadata (never its id). Only supplied fields change; 404
 * `market_data_series_not_found` if the id is gone, 422 if a required field is
 * blanked.
 */
export function updateSeries(
  canonicalId: string,
  patch: MarketDataSeriesPatch,
): Promise<OrchestratorResult<MarketDataSeries>> {
  return orchestratorPatch<MarketDataSeries>(
    `/v1/market-data/series/${encodeURIComponent(canonicalId)}`,
    patch,
  );
}

/**
 * Delete a series AND its `md.quote_points` values (reports the cascaded
 * `deleted_quote_points` count). Returns a JSON body with 200, so — unlike the
 * generic 204 `orchestratorDelete` — the response is parsed. 404
 * `market_data_series_not_found` if the id is gone.
 */
export function deleteSeries(
  canonicalId: string,
): Promise<OrchestratorResult<MarketDataSeriesDelete>> {
  return orchestratorFetch<MarketDataSeriesDelete>(
    `/v1/market-data/series/${encodeURIComponent(canonicalId)}`,
    { method: 'DELETE' },
  );
}

// Version / build info (About panel)

/**
 * `GET /v1/version` response. Reports the backend orchestrator's version +
 * build SHA and the pricing engine's version. `engine.source` is a backend
 * implementation detail (e.g. how the engine version was obtained) and is NOT
 * surfaced as user-facing text.
 */
export interface VersionInfo {
  orchestrator: { version: string; build_sha: string };
  engine: { version: string; source?: string };
}

/**
 * Fetch backend + engine versions for the About panel. Same-origin/dev-bypass
 * aware like every other GET. On any failure the caller shows a graceful
 * "unavailable" — the web-client row (a build-time constant) renders regardless.
 */
export function getVersion(): Promise<OrchestratorResult<VersionInfo>> {
  return orchestratorGet<VersionInfo>('/v1/version');
}

// In-app pricing trace

/** One pipeline stage as recorded by the orchestrator for a pricing call. */
export interface TraceStage {
  ts: string;
  stage: string;
  level: string;
  duration_ms?: number | null;
  /** Plain-English one-liner interpreting the stage. */
  summary?: string | null;
  payload?: unknown;
}

/** The `GET /v1/traces/{request_id}` response body. */
export interface PricingTrace {
  request_id: string;
  stages: TraceStage[];
}

/**
 * Fetch the orchestrator's pipeline trace for one pricing call by request id.
 * Owner-scoped on the server: a foreign / unknown id returns a 404 error
 * envelope with `code === 'trace_not_found'` (404, not 403 — ids are not
 * probeable).
 */
export function getTrace(requestId: string): Promise<OrchestratorResult<PricingTrace>> {
  return orchestratorGet<PricingTrace>(`/v1/traces/${encodeURIComponent(requestId)}`);
}

// Per-product result enrichments

/**
 * One per-period cashflow on an IR-swap leg, as returned by
 * `POST /v1/price/swap/ir` (IrSwapResult.fixed_leg_flows /
 * .floating_leg_flows). Dates are ISO strings; numbers are floats. Floating
 * legs carry `fixing_date` / `index_fixing`; fixed legs typically do not.
 * Every field is optional so the portal can render whatever the engine
 * supplies without coupling to a fixed column set.
 */
export interface SwapFlow {
  payment_date?: string;
  accrual_start_date?: string;
  accrual_end_date?: string;
  amount?: number;
  accrual_year_fraction?: number;
  gearing?: number;
  discount?: number;
  present_value?: number;
  fixing_date?: string;
  index_fixing?: number;
  has_cms_swap_rate?: boolean;
  cms_swap_rate?: number;
  spread?: number;
  rate?: number;
}

/**
 * IrSwapResult plus the two per-leg cashflow arrays the orchestrator now
 * returns at the top level (alongside `npv` / `leg_npvs` / `extras`). Kept as
 * a local widening of the generated schema so the portal consumes the new
 * fields before the OpenAPI types are regenerated. Both arrays are optional
 * and default to `[]` on the consuming side, so a response that omits them
 * (or carries them under `extras`, the legacy location) still maps cleanly.
 */
export type IrSwapResultWithFlows = components['schemas']['IrSwapResult'] & {
  fixed_leg_flows?: SwapFlow[];
  floating_leg_flows?: SwapFlow[];
};

// Per-product typed functions

type Schemas = components['schemas'];

export function priceSwapIr(
  req: Schemas['IrSwapPriceRequest'],
): Promise<OrchestratorResult<Schemas['IrSwapPriceResponse']>> {
  return orchestratorPost('/v1/price/swap/ir', req);
}

export function priceSwaption(
  req: Schemas['SwaptionPriceRequest'],
): Promise<OrchestratorResult<Schemas['SwaptionPriceResponse']>> {
  return orchestratorPost('/v1/price/swaption', req);
}

export function priceBondsFixed(
  req: Schemas['FixedBondPriceRequest'],
): Promise<OrchestratorResult<Schemas['FixedBondPriceResponse']>> {
  return orchestratorPost('/v1/price/bonds/fixed', req);
}

export function priceBondsFloating(
  req: Schemas['FloatingBondPriceRequest'],
): Promise<OrchestratorResult<Schemas['FloatingBondPriceResponse']>> {
  return orchestratorPost('/v1/price/bonds/floating', req);
}

export function priceCds(
  req: Schemas['CdsPriceRequest'],
): Promise<OrchestratorResult<Schemas['CdsPriceResponse']>> {
  return orchestratorPost('/v1/price/cds', req);
}

export function priceEquityOption(
  req: Schemas['EquityOptionPriceRequest'],
): Promise<OrchestratorResult<Schemas['EquityOptionPriceResponse']>> {
  return orchestratorPost('/v1/price/equity-option', req);
}

export function priceSwapsInflation(
  req: Schemas['InflationSwapPriceRequest'],
): Promise<OrchestratorResult<Schemas['InflationSwapPriceResponse']>> {
  return orchestratorPost('/v1/price/swaps/inflation', req);
}
