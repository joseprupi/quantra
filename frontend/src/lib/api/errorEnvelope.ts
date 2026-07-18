/**
 * Shared error-envelope → actionable error mapping.
 *
 * The orchestrator returns a structured error envelope `{ error, code, request_id?,
 * details? }`. This module is the ONE place that turns that envelope into the
 * `PricingErrorInfo` the UI renders. Two rules:
 *
 *   1. **Branch on `code`, never on the prose `error` string**.
 *      Category + guidance are derived from the stable machine code (or, as a
 *      fallback, the HTTP status) — never by sniffing `envelope.error`.
 *   2. **Keep the real upstream text.** `message` stays the orchestrator's
 *      `error` (the real reason — once the engine stops collapsing to "Quantra
 *      error" this is the true cause); the code→guidance lives in `suggestion`
 *      so a user can self-serve, and `requestId` is surfaced as a support
 *      handle an admin can grep across the stack.
 *
 * Previously each pricing service carried its own near-identical copy of this
 * mapper; they now all delegate here so the catalog stays consistent.
 */
import type { PricingErrorInfo } from '../quantra-types';
import type { ApiErrorEnvelope } from './types';

// Category drives the icon/colour/title in PricingErrorCard. Derived from the
// envelope code first (code wins over HTTP status), falling back to the
// HTTP status only for codes we don't recognise. The suffix rules cover every
// product's `*_not_found` (stale by-ref id) and `*_resolution_failed` (curve /
// surface / spot couldn't be resolved) code without enumerating all ~20.
export function categoryForCode(
  code: string,
  httpStatus: number,
): PricingErrorInfo['category'] {
  if (code === 'unauthenticated') return 'auth';
  if (code === 'network_error') return 'network';
  if (code === 'engine_timeout' || code === 'engine_unavailable') return 'unavailable';
  if (code.endsWith('_not_found')) return 'not_found';
  if (code.endsWith('_resolution_failed')) return 'validation';
  if (httpStatus >= 500) return 'server';
  if (httpStatus === 400 || httpStatus === 422) return 'validation';
  if (httpStatus === 404) return 'not_found';
  if (httpStatus === 401 || httpStatus === 403) return 'auth';
  return 'client';
}

// Exact code → human guidance. Keyed on the stable code only.
const EXACT_GUIDANCE: Record<string, string> = {
  unauthenticated: 'Your session has expired. Sign in again, then retry.',
  network_error:
    'Could not reach the pricing service. Check your connection and that the orchestrator is running.',
  engine_timeout:
    'The pricing engine took too long to respond. Retry in a moment; if it persists the engine may be overloaded.',
  engine_unavailable:
    'The pricing engine is unreachable (it may be down or restarting). Retry shortly.',
};

// A short, actionable "what to do next" for a given code. Suffix families cover
// the product-specific not-found / resolution-failed codes; otherwise we fall
// back to category-level guidance. Returns undefined when we have nothing more
// useful to add than the raw message already shown.
export function suggestionForCode(code: string, httpStatus: number): string | undefined {
  const exact = EXACT_GUIDANCE[code];
  if (exact) return exact;
  if (code.endsWith('_not_found')) {
    return 'A referenced record (trade, curve, surface, model, or snapshot) was not found — it may have been deleted or not saved yet. Re-save it or pick another, then retry.';
  }
  if (code.endsWith('_resolution_failed')) {
    return 'A referenced curve or surface could not be resolved for this as-of date. Check it is configured for that date and references registered/configured indices.';
  }
  const category = categoryForCode(code, httpStatus);
  if (category === 'validation') {
    return 'The request was rejected as invalid. Review the highlighted inputs and retry.';
  }
  if (category === 'unavailable') {
    return 'A backend service is temporarily unavailable. Retry shortly.';
  }
  if (category === 'server') {
    return 'The pricing engine hit an internal error. If it recurs, quote the request id below to support.';
  }
  return undefined;
}

// The single envelope → PricingErrorInfo mapper used by every pricing service.
// `message` is the real upstream text; `suggestion` + `requestId` make it
// actionable + greppable; `codeName` surfaces the stable code in the UI meta.
export function mapEnvelopeToErrorInfo(
  code: string,
  httpStatus: number,
  envelope: ApiErrorEnvelope,
): PricingErrorInfo {
  return {
    message: envelope.error,
    httpStatus,
    category: categoryForCode(code, httpStatus),
    codeName: code,
    suggestion: suggestionForCode(code, httpStatus),
    requestId: envelope.request_id ?? undefined,
    raw: envelope,
  };
}
