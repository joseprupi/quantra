/**
 * Build the source-ref linkage stamped onto a
 * saved product record at SAVE time.
 *
 * Why this exists: the localStorage product *request*
 * is the post-resolution body — ``resolveCurveArrayQuoteIds`` has already
 * stripped every ``quote_id`` to a frozen value and relabelled curve ids to
 * ``'discount'``/``'forward'``/… So the request alone can no longer be migrated
 * to the backend faithfully (it has lost the quote-id leaves and
 * the link to the quote-id-bearing reference-data stores). ``sourceRefs`` records
 * the *store ids* the page selected, so the migration can reload the faithful
 * entities (with their ``quote_id`` leaves) from ``storage/curves`` etc. and
 * rebuild the persist-graph input.
 *
 * Robustness: a record loaded from storage and re-saved has its curve selector
 * id clobbered to the relabelled ``'discount'`` (the page rehydrates from the
 * inlined request curve, not the store). So a curve ref is only emitted when the
 * id actually resolves in ``storage/curves`` — a clobbered ``'discount'`` resolves
 * to nothing and is omitted, and the migration then skips that record with a
 * surfaced reason rather than persisting a dangling/frozen curve. Records
 * authored or re-selected after the linkage existed carry real store ids and
 * migrate cleanly.
 *
 * Purely additive + reversible (mirrors ``appId``/``appGraph``). Nothing
 * reads ``sourceRefs`` yet — the migration pass is the consumer.
 */

import type { StoredSourceRefs } from './wrappedStore';
import { getCurveById } from './curves';

/** A (role → curve-store-id) pair the caller wants to link, possibly unset. */
export interface CurveRefInput {
  role: string;
  curveId?: string | null;
}

/** Keep only the curve refs whose id resolves to a real ``storage/curves`` row. */
function resolveCurveRefs(curves: CurveRefInput[]): Array<{ role: string; curveId: string }> {
  const out: Array<{ role: string; curveId: string }> = [];
  for (const { role, curveId } of curves) {
    if (curveId && getCurveById(curveId)) out.push({ role, curveId });
  }
  return out;
}

/**
 * Assemble the ``sourceRefs`` to stamp onto a product record. Curve ids are
 * validated against ``storage/curves`` (clobbered relabels are dropped); the
 * other ids are passed through as supplied — the caller emits them only when a
 * saved reference entity is actually selected. Returns ``undefined`` when there
 * is nothing linkable, so the field is omitted entirely.
 */
export function buildSourceRefs(input: {
  curves?: CurveRefInput[];
  indexId?: string | null;
  creditCurveId?: string | null;
  volSurfaceId?: string | null;
  swaptionModelId?: string | null;
}): StoredSourceRefs | undefined {
  const refs: StoredSourceRefs = {};
  const curves = resolveCurveRefs(input.curves ?? []);
  if (curves.length > 0) refs.curves = curves;
  if (input.indexId) refs.indexId = input.indexId;
  if (input.creditCurveId) refs.creditCurveId = input.creditCurveId;
  if (input.volSurfaceId) refs.volSurfaceId = input.volSurfaceId;
  if (input.swaptionModelId) refs.swaptionModelId = input.swaptionModelId;
  return Object.keys(refs).length > 0 ? refs : undefined;
}
