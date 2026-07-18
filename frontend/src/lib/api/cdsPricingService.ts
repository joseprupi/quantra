import type { components } from './_generated/orchestrator';
import { priceCds as pricingOrchCds } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type CdsResultSchema = components['schemas']['CdsResult'];

// Map orchestrator CdsResult → legacy CdsResultData shape.
// The orchestrator field protection_leg_npv renames to default_leg_npv;
// extras["default_leg_npv"] is the wire-name mirror fallback.
export function mapCdsOrchResult(r: CdsResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:             r.npv          ?? undefined,
    fair_spread:     r.fair_spread  ?? undefined,
    fair_upfront:    r.fair_upfront ?? undefined,
    default_leg_npv: r.protection_leg_npv ?? (ex.default_leg_npv as number | undefined),
    premium_leg_npv: r.premium_leg_npv ?? undefined,
  };
}

// Build the ``/v1/price/cds`` POST body for whichever arm the caller is on.
// Inline and by-reference are the two arms of the same ``cds_id ⊕ cds``
// discriminator the backend request model enforces; the portal chooses per
// call and the response handling is identical.
//
//   • by-reference — chosen when a saved cds id is present. Emit a minimal
//     ``{ cds_id, as_of, snapshot_id? }``; the orchestrator loads the trade
//     and chains to its saved discount curve (``pricing.discount_curve_id``) +
//     credit curve (``pricing.credit_curve_id``) server-side. No inline
//     ``cds`` / ``curves`` / ``credit_curve`` is sent on this arm (the cds
//     by-reference body is strictly by-reference — distinct from swap_ir,
//     which permits a ``curves`` override).
//       - ``snapshot_id`` is forwarded when present so the call prices against
//         the pinned market-data snapshot; omitted ⇒ current/live data.
//         (Credit curves never consult market data.)
//
//   • inline — every other call, byte-unchanged: pass a ready
//     CdsPriceRequest envelope (top-level ``cds`` + ``as_of`` + ``curves`` +
//     ``credit_curve``/``credit_curve_id``) through verbatim, else wrap the
//     legacy fat body as ``{ cds: <fat>, as_of }`` (the save-flow shape).
export function buildCdsPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;

  // By-reference arm: a saved cds id discriminates it.
  if (req && typeof req.cds_id === 'string' && req.cds_id.length > 0) {
    const body: Record<string, unknown> = {
      cds_id: req.cds_id,
      as_of: typeof req.as_of === 'string' ? req.as_of : asOf,
    };
    if (typeof req.snapshot_id === 'string' && req.snapshot_id.length > 0) {
      body.snapshot_id = req.snapshot_id;
    }
    return body;
  }

  // Inline arm (unchanged).
  const looksLikeEnvelope =
    !!req &&
    'as_of' in req &&
    (
      'cds' in req ||
      'curves' in req ||
      'credit_curve' in req ||
      'credit_curve_id' in req
    );
  return looksLikeEnvelope ? req : { cds: request as Record<string, unknown>, as_of: asOf };
}

// The orchestrator is the only path — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in ``buildCdsPriceBody``; from here
// down both arms share the same response mapping.
export async function priceCds(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ cds_list: Record<string, unknown>[] }>> {
  const payload = buildCdsPriceBody(request, asOf);
  const result = await pricingOrchCds(payload as Parameters<typeof pricingOrchCds>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { cds_list: [...] } shape.
  return {
    success: true,
    data: { cds_list: [mapCdsOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
