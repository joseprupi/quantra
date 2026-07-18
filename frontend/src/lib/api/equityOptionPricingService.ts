import type { components } from './_generated/orchestrator';
import { priceEquityOption as pricingOrchEquityOption } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type EquityOptionResultSchema = components['schemas']['EquityOptionResult'];

// Map orchestrator EquityOptionResult → legacy result shape consumed by EquityOptions.tsx.
// Key rename: implied_volatility (orchestrator) → implied_vol (the legacy normalized name the page renders).
// engine_used: not in typed schema; passed through from extras if populated by the engine.
export function mapEquityOptionOrchResult(r: EquityOptionResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:             r.npv             ?? undefined,
    delta:           r.delta           ?? undefined,
    gamma:           r.gamma           ?? undefined,
    vega:            r.vega            ?? undefined,
    theta:           r.theta           ?? undefined,
    rho:             r.rho             ?? undefined,
    implied_vol:     r.implied_volatility ?? undefined,
    used_spot:       r.used_spot       ?? undefined,
    used_strike:     r.used_strike     ?? undefined,
    used_settlement: r.used_settlement ?? undefined,
    engine_used:     ex.engine_used,
  };
}

// Branch on the envelope code, never on prose.
// errorInfo is returned for consistency with the other pricing services but is
// not consumed by EquityOptions.tsx today (the component uses a plain
// try/catch error string).

// Build the ``/v1/price/equity-option`` POST body for whichever arm the
// caller is on. Inline and by-reference are the two arms of the same
// ``equity_option_id ⊕ equity_option`` discriminator the backend request model
// enforces; the portal chooses per call and the response handling is
// identical.
//
//   • by-reference — chosen when a saved equity-option id is present. Emit a
//     minimal ``{ equity_option_id, as_of, snapshot_id? }``; the orchestrator
//     loads the trade and chains to its saved ``pricing.curves`` +
//     ``pricing.vol_surface_id`` + inline ``pricing.spot`` server-side. No
//     inline ``equity_option`` / ``curves`` / ``vol_surface`` / ``spot`` is
//     sent on this arm.
//       - ``snapshot_id`` is forwarded when present so the call prices against
//         the pinned market-data snapshot; omitted ⇒ current/live data.
//
//   • inline — every other call, byte-unchanged: pass a ready
//     EquityOptionPriceRequest envelope (top-level ``equity_option`` +
//     ``as_of`` + ``curves`` + ``vol_surface`` + ``spot``) through verbatim,
//     else wrap the legacy fat body as ``{ equity_option: <fat>, as_of }``
//     (the save-flow round-trip shape preserved for ``saveEquityOption``).
export function buildEquityOptionPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;

  // By-reference arm: a saved equity-option id discriminates it.
  if (req && typeof req.equity_option_id === 'string' && req.equity_option_id.length > 0) {
    const body: Record<string, unknown> = {
      equity_option_id: req.equity_option_id,
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
      'equity_option' in req ||
      'equity_option_id' in req ||
      'curves' in req ||
      'vol_surface' in req ||
      'spot' in req
    );
  return looksLikeEnvelope ? req : { equity_option: request as Record<string, unknown>, as_of: asOf };
}

// Calls the orchestrator unconditionally — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in ``buildEquityOptionPriceBody``; from
// here down both arms share the same response mapping.
export async function priceEquityOption(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ options: Record<string, unknown>[] }>> {
  const payload = buildEquityOptionPriceBody(request, asOf);
  const result = await pricingOrchEquityOption(payload as Parameters<typeof pricingOrchEquityOption>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { options: [...] } shape.
  return {
    success: true,
    data: { options: [mapEquityOptionOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
