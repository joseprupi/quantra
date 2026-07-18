import type { components } from './_generated/orchestrator';
import { priceBondsFixed, priceBondsFloating } from './orchestrator';
import { type PricingResult } from '../quantra-types';
import { mapEnvelopeToErrorInfo } from './errorEnvelope';

type FixedBondResultSchema = components['schemas']['FixedBondResult'];
type FloatingBondResultSchema = components['schemas']['FloatingBondResult'];

// Map FixedBondResult → BondResultData shape.
// Renames: accrued → accrued_amount, yield_to_maturity → yield, cashflows → flows
// Risk: macaulay_duration / modified_duration / convexity / bps / accrued_days rely
//       on the engine populating extras with these exact key names.
export function mapFixedBondOrchResult(r: FixedBondResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:               r.npv               ?? undefined,
    clean_price:       r.clean_price       ?? undefined,
    dirty_price:       r.dirty_price       ?? undefined,
    accrued_amount:    r.accrued           ?? undefined,  // rename
    yield:             r.yield_to_maturity ?? undefined,  // rename
    flows:             r.cashflows         ?? undefined,  // rename
    macaulay_duration: ex.macaulay_duration,
    modified_duration: ex.modified_duration,
    convexity:         ex.convexity,
    bps:               ex.bps,
    accrued_days:      ex.accrued_days,
  };
}

// Map FloatingBondResult → BondResultData shape.
// Differences from fixed: no yield_to_maturity top-level → yield from extras;
//                         forecast_rates present but not consumed by PricingResults.
export function mapFloatingBondOrchResult(r: FloatingBondResultSchema): Record<string, unknown> {
  const ex = (r.extras ?? {}) as Record<string, unknown>;
  return {
    npv:               r.npv         ?? undefined,
    clean_price:       r.clean_price ?? undefined,
    dirty_price:       r.dirty_price ?? undefined,
    accrued_amount:    r.accrued     ?? undefined,  // rename
    yield:             ex.yield,                    // from extras (no top-level YTM for floater)
    flows:             r.cashflows   ?? undefined,  // rename
    macaulay_duration: ex.macaulay_duration,
    modified_duration: ex.modified_duration,
    convexity:         ex.convexity,
    bps:               ex.bps,
    accrued_days:      ex.accrued_days,
  };
}

// Build the ``/v1/price/bonds/fixed`` POST body for whichever arm the caller
// is on. Inline and by-reference are the two arms of the same
// ``bond_id ⊕ bond`` discriminator the backend request model enforces.
//
//   • by-reference — chosen when a saved bond id is present. Emit a minimal
//     ``{ bond_id, as_of, snapshot_id? }``; the orchestrator loads the trade
//     and chains to its saved discount curve via
//     ``pricing.discount_curve_id`` / ``pricing.curve_set_id`` server-side.
//     No inline ``bond``/``curves`` is sent.
//   • inline — every other call, byte-unchanged: a ready envelope
//     (``bond``/``bond_id`` + ``as_of`` + ``curves``) passes through;
//     else the legacy fat body is wrapped as ``{ bond: <fat>, as_of }``.
export function buildFixedBondPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;
  if (req && typeof req.bond_id === 'string' && req.bond_id.length > 0) {
    const body: Record<string, unknown> = {
      bond_id: req.bond_id,
      as_of: typeof req.as_of === 'string' ? req.as_of : asOf,
    };
    if (typeof req.snapshot_id === 'string' && req.snapshot_id.length > 0) {
      body.snapshot_id = req.snapshot_id;
    }
    return body;
  }
  const looksLikeEnvelope =
    !!req &&
    'as_of' in req &&
    ('bond' in req || 'curves' in req);
  return looksLikeEnvelope ? req : { bond: request as Record<string, unknown>, as_of: asOf };
}

// The orchestrator is the only path — no flag, no legacy branch. The arm
// (inline vs by-reference) is decided in ``buildFixedBondPriceBody``;
// both arms share the same response mapping.
export async function priceFixedBond(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ bonds: Record<string, unknown>[] }>> {
  const payload = buildFixedBondPriceBody(request, asOf);
  const result = await priceBondsFixed(payload as Parameters<typeof priceBondsFixed>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { bonds: [...] } shape.
  return {
    success: true,
    data: { bonds: [mapFixedBondOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}

// Build the ``/v1/price/bonds/floating`` POST body.
//
//   • by-reference — a saved bond id → minimal
//     ``{ bond_id, as_of, snapshot_id? }``; the orchestrator loads the trade
//     and chains to its saved discount + projection curves
//     (``pricing.discount_curve_id`` / ``pricing.forecast_curve_id``) and
//     projection index (``pricing.index_id``) server-side. No inline
//     ``bond``/``curves``/``index`` is sent on this arm.
//   • inline — a ready envelope (``bond``/``bond_id`` + ``as_of``
//     + ``curves`` + ``index``) passes through; else legacy-wrap.
export function buildFloatingBondPriceBody(request: unknown, asOf: string): Record<string, unknown> {
  const req =
    request && typeof request === 'object' ? (request as Record<string, unknown>) : null;
  if (req && typeof req.bond_id === 'string' && req.bond_id.length > 0) {
    const body: Record<string, unknown> = {
      bond_id: req.bond_id,
      as_of: typeof req.as_of === 'string' ? req.as_of : asOf,
    };
    if (typeof req.snapshot_id === 'string' && req.snapshot_id.length > 0) {
      body.snapshot_id = req.snapshot_id;
    }
    return body;
  }
  const looksLikeEnvelope =
    !!req &&
    'as_of' in req &&
    ('bond' in req || 'curves' in req || 'index' in req);
  return looksLikeEnvelope ? req : { bond: request as Record<string, unknown>, as_of: asOf };
}

// The orchestrator is the only path — no flag, no legacy branch. Arm decided
// in ``buildFloatingBondPriceBody``; both arms share the same mapping.
export async function priceFloatingBond(
  request: unknown,
  asOf: string,
): Promise<PricingResult<{ bonds: Record<string, unknown>[] }>> {
  const payload = buildFloatingBondPriceBody(request, asOf);
  const result = await priceBondsFloating(payload as Parameters<typeof priceBondsFloating>[0]);
  if (!result.ok) {
    return {
      success: false,
      error: result.envelope.error,
      errorInfo: mapEnvelopeToErrorInfo(result.envelope.code, result.httpStatus, result.envelope),
      duration_ms: result.duration_ms,
    };
  }
  // Wrap the mapped result to match the legacy { bonds: [...] } shape.
  return {
    success: true,
    data: { bonds: [mapFloatingBondOrchResult(result.data.result)] },
    duration_ms: result.duration_ms,
    requestId: result.requestId,
  };
}
