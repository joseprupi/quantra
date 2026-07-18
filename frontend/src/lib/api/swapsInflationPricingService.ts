import type { components } from './_generated/orchestrator';
import { priceSwapsInflation as orchPriceSwapsInflation } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type InflationSwapResultSchema = components['schemas']['InflationSwapResult'];

// Map orchestrator InflationSwapResult → legacy
// InflationSwapResultData shape consumed by InflationSwap.tsx. Top-level
// fields (npv / fair_rate / fair_spread / fixed_leg_bps / fixed_leg_npv /
// inflation_leg_npv / yoy_leg_bps / yoy_leg_npv) are populated by the
// engine_io decoder; the per-leg flow arrays (fixed_leg_flows /
// inflation_leg_flows / yoy_leg_flows) ride through extras (mirrors the
// swap_ir / cds shape).
export function mapSwapsInflationOrchResult(r: InflationSwapResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:                 r.npv                 ?? undefined,
    fair_rate:           r.fair_rate           ?? undefined,
    fair_spread:         r.fair_spread         ?? undefined,
    fixed_leg_bps:       r.fixed_leg_bps       ?? undefined,
    fixed_leg_npv:       r.fixed_leg_npv       ?? undefined,
    inflation_leg_npv:   r.inflation_leg_npv   ?? undefined,
    yoy_leg_bps:         r.yoy_leg_bps         ?? undefined,
    yoy_leg_npv:         r.yoy_leg_npv         ?? undefined,
    swap_kind:           r.swap_kind,
    fixed_leg_flows:     ex.fixed_leg_flows,
    inflation_leg_flows: ex.inflation_leg_flows,
    yoy_leg_flows:       ex.yoy_leg_flows,
  };
}

// Branch on the envelope code, never on prose. The catalog mirrors the error
// codes the backend documents for inflation swaps
// (swap_inflation_not_found / *_curve_not_found / *_index_not_found /
// *_snapshot_not_found / *_curve_resolution_failed /
// *_index_resolution_failed / *_quote_resolution_failed /
// engine_unavailable / engine_timeout / md_unreachable etc.).

// Build the ``/v1/price/swaps/inflation`` POST body for whichever arm the
// caller is on. Inline and by-reference are the two arms of the same
// ``swap_id ⊕ swap`` discriminator the backend request model enforces; the
// portal chooses per call and the response handling is identical.
//
//   • by-reference — chosen when a saved inflation-swap id is present. Emit a
//     minimal ``{ swap_id, as_of, snapshot_id? }``; the orchestrator loads the
//     trade and chains to its saved ``pricing.curves`` (role-tagged
//     nominal+inflation) + ``pricing.inflation_index_id`` + top-level
//     ``swap_kind`` server-side. No inline ``swap`` / ``curves`` /
//     ``inflation_index`` is sent on this arm.
//       - ``snapshot_id`` is forwarded when present so the call prices against
//         the pinned market-data snapshot; omitted ⇒ current/live data.
//       - a YYIIS by-ref call surfaces the engine's clean 502 (ABORT)
//         identically to the inline arm — by-ref does not paper over it.
//
//   • inline — every other call, byte-unchanged: pass a ready
//     InflationSwapPriceRequest envelope (top-level ``swap`` + ``as_of`` +
//     ``curves`` + ``inflation_index``) through verbatim, else wrap the legacy
//     fat body as ``{ swap: <fat>, as_of }`` (the save-flow round-trip shape
//     preserved for ``saveInflationSwap``).
export function buildSwapsInflationPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;

  // By-reference arm: a saved inflation-swap id discriminates it.
  if (req && typeof req.swap_id === 'string' && req.swap_id.length > 0) {
    const body: Record<string, unknown> = {
      swap_id: req.swap_id,
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
      'swap' in req ||
      'swap_id' in req ||
      'curves' in req ||
      'inflation_index' in req
    );
  return looksLikeEnvelope ? req : { swap: request as Record<string, unknown>, as_of: asOf };
}

// Calls the orchestrator unconditionally — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in ``buildSwapsInflationPriceBody``;
// from here down both arms share the same response mapping.
export async function priceSwapsInflation(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ swaps: Record<string, unknown>[] }>> {
  const payload = buildSwapsInflationPriceBody(request, asOf);
  const result = await orchPriceSwapsInflation(payload as Parameters<typeof orchPriceSwapsInflation>[0]);
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
    data: { swaps: [mapSwapsInflationOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
