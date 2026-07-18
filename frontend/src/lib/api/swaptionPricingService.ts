import type { components } from './_generated/orchestrator';
import { priceSwaption as orchPriceSwaption } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type SwaptionResultSchema = components['schemas']['SwaptionResult'];

// Map orchestrator SwaptionResult → legacy SwaptionResultData shape.
// npv, delta, vega are top-level in SwaptionResult; remaining fields come from extras
export function mapSwaptionOrchResult(r: SwaptionResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:                r.npv   ?? undefined,
    delta:              r.delta ?? undefined,
    vega:               r.vega  ?? undefined,
    implied_volatility: ex.implied_volatility,
    atm_forward:        ex.atm_forward,
    annuity:            ex.annuity,
    dv01:               ex.dv01,
    gamma:              ex.gamma,
    theta:              ex.theta,
  };
}

// Build the ``/v1/price/swaption`` POST body for whichever arm the caller is
// on. Inline and by-reference are the two arms of the same
// ``swaption_id ⊕ swaption`` discriminator the backend request model enforces;
// the portal chooses per call and the response handling is identical.
//
//   • by-reference — chosen when a saved swaption id is present. Emit a
//     minimal ``{ swaption_id, as_of, snapshot_id? }``; the orchestrator loads
//     the trade and chains to its saved curve_set (``pricing.curve_set_id``)
//     + vol_surface (``pricing.vol_surface_id``) + swaption_model
//     (``pricing.swaption_model_id``) server-side. No inline ``swaption`` /
//     ``curves`` / ``vol_surface`` / ``swaption_model`` is sent on this arm.
//       - ``snapshot_id`` is forwarded when present so the call prices against
//         the pinned market-data snapshot; omitted ⇒ current/live data.
//
//   • inline — every other call, byte-unchanged: pass a ready
//     SwaptionPriceRequest envelope (top-level ``swaption`` + ``as_of`` +
//     ``curves`` + ``vol_surface`` + ``swaption_model``) through verbatim, else
//     wrap the legacy fat body as ``{ swaption: <fat>, as_of }`` (the save-flow
//     round-trip shape preserved for ``saveSwaption``).
export function buildSwaptionPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;

  // By-reference arm: a saved swaption id discriminates it.
  if (req && typeof req.swaption_id === 'string' && req.swaption_id.length > 0) {
    const body: Record<string, unknown> = {
      swaption_id: req.swaption_id,
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
      'swaption' in req ||
      'swaption_id' in req ||
      'curves' in req ||
      'vol_surface' in req ||
      'swaption_model' in req
    );
  return looksLikeEnvelope ? req : { swaption: request as Record<string, unknown>, as_of: asOf };
}

// The orchestrator is the only path — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in ``buildSwaptionPriceBody``; from
// here down both arms share the same response mapping.
export async function priceSwaption(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ swaptions: Record<string, unknown>[] }>> {
  const payload = buildSwaptionPriceBody(request, asOf);
  const result = await orchPriceSwaption(payload as Parameters<typeof orchPriceSwaption>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { swaptions: [...] } shape.
  return {
    success: true,
    data: { swaptions: [mapSwaptionOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
