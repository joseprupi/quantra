import type { components } from './_generated/orchestrator';
import { priceSwapIr, type IrSwapResultWithFlows, type SwapFlow } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type IrSwapResultSchema = components['schemas']['IrSwapResult'];

// Prefer the new top-level `*_leg_flows` arrays on IrSwapResult; fall back to
// the legacy `extras` location; default to `[]` so an omitted field never
// breaks the result UI (back-compatible per the dual-surface guarantee).
function pickFlows(top: SwapFlow[] | undefined, fromExtras: unknown): SwapFlow[] {
  if (Array.isArray(top)) return top;
  if (Array.isArray(fromExtras)) return fromExtras as SwapFlow[];
  return [];
}

// Map orchestrator IrSwapResult → legacy SwapResultData shape via extras passthrough.
export function mapIrSwapOrchResult(r: IrSwapResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  const rf = r as IrSwapResultWithFlows;
  return {
    npv: r.npv ?? undefined,
    fair_rate: ex.fair_rate,
    fair_spread: ex.fair_spread,
    fixed_leg_bps: ex.fixed_leg_bps,
    floating_leg_bps: ex.floating_leg_bps,
    fixed_leg_npv: ex.fixed_leg_npv,
    floating_leg_npv: ex.floating_leg_npv,
    fixed_leg_flows: pickFlows(rf.fixed_leg_flows, ex.fixed_leg_flows),
    floating_leg_flows: pickFlows(rf.floating_leg_flows, ex.floating_leg_flows),
    overnight_leg_bps: ex.overnight_leg_bps,
    overnight_leg_npv: ex.overnight_leg_npv,
    overnight_leg_flows: ex.overnight_leg_flows,
    fair_spread_leg1: ex.fair_spread_leg1,
    fair_spread_leg2: ex.fair_spread_leg2,
    leg1_bps: ex.leg1_bps,
    leg2_bps: ex.leg2_bps,
    leg1_npv: ex.leg1_npv,
    leg2_npv: ex.leg2_npv,
    leg1_flows: ex.leg1_flows,
    leg2_flows: ex.leg2_flows,
    cms_leg_bps: ex.cms_leg_bps,
    cms_leg_npv: ex.cms_leg_npv,
    cms_leg_flows: ex.cms_leg_flows,
    used_cms_pricer_type: ex.used_cms_pricer_type,
    used_cms_yield_curve_model: ex.used_cms_yield_curve_model,
    used_cms_mean_reversion: ex.used_cms_mean_reversion,
    used_cms_hagan_lower_limit: ex.used_cms_hagan_lower_limit,
    used_cms_hagan_upper_limit: ex.used_cms_hagan_upper_limit,
    used_cms_hagan_precision: ex.used_cms_hagan_precision,
    used_cms_hagan_hard_upper_limit: ex.used_cms_hagan_hard_upper_limit,
  };
}

// Build the `/v1/price/swap/ir` POST body for whichever arm the caller is on.
// Inline and by-reference are the two arms of the same `swap_id ⊕ swap`
// discriminator the backend request model enforces; the portal chooses per
// call and the response handling is identical.
//
//   • by-reference — chosen when a saved swap id is present. Emit a minimal
//     `{ swap_id, as_of, snapshot_id?, curves? }`; the orchestrator loads the
//     trade + its curve chain from storage. No inline `swap` is sent on this
//     arm.
//       - `snapshot_id` is forwarded when present so the call prices against
//         the pinned market-data snapshot; omitted ⇒ current/live data.
//       - `curves` is forwarded only when present as an override; the backend
//         honours request-level curves over the saved chain.
//
//   • inline — every other call, byte-unchanged: pass a ready
//     `{ swap|curves, as_of }` envelope through verbatim, else wrap the legacy
//     fat body as `{ swap: <fat>, as_of }` (non-vanilla branches still rely
//     on the wrap; TODO: migrate them to ready inline envelopes).
function buildIrSwapPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;

  // By-reference arm: a saved swap id discriminates it.
  if (req && typeof req.swap_id === 'string' && req.swap_id.length > 0) {
    const body: Record<string, unknown> = {
      swap_id: req.swap_id,
      as_of: typeof req.as_of === 'string' ? req.as_of : asOf,
    };
    if (typeof req.snapshot_id === 'string' && req.snapshot_id.length > 0) {
      body.snapshot_id = req.snapshot_id;
    }
    if (req.curves !== undefined && req.curves !== null) {
      body.curves = req.curves;
    }
    return body;
  }

  // Inline arm (unchanged).
  const looksLikeEnvelope =
    !!req && 'as_of' in req && ('swap' in req || 'curves' in req);
  return looksLikeEnvelope ? req : { swap: request as Record<string, unknown>, as_of: asOf };
}

// The orchestrator is the only path — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in `buildIrSwapPriceBody`; from here
// down both arms share the same response mapping.
export async function priceIrSwap(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ swaps: Record<string, unknown>[] }>> {
  const payload = buildIrSwapPriceBody(request, asOf);
  const result = await priceSwapIr(payload as Parameters<typeof priceSwapIr>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { swaps: [...] } shape.
  return {
    success: true,
    data: { swaps: [mapIrSwapOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
