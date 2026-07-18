/**
 * Identity model for product-owned persisted curves (the save-graph rows).
 *
 * The save-graph persists each product's wire curves as real ``app.curves``
 * rows. Those rows live in the SAME namespace as the user's own saved curves,
 * so their names must be (a) unique — the backend enforces a per-owner
 * partial-unique constraint on ``name`` and 409s a duplicate — and
 * (b) recognisably owned by the product that created them.
 *
 * Historically the wire body's constant curve id (``'discount'`` /
 * ``'forward'``/…) leaked into the persisted row name: the first-ever product
 * save created a live curve literally named ``discount``, EVERY later save's
 * ``POST /v1/curves`` 409'd with ``name_conflict``, and a user's own unrelated
 * curve named ``discount`` could be silently clobbered through the backend's
 * name-matching restore arm. This module is the fix:
 *
 *   • CREATE: the persisted name is derived from the product name + curve
 *     role + a short unique suffix — never a bare role constant, never
 *     the wire id, unique across saves by construction.
 *   • UPDATE (re-save): the graph PATCHes the row by its REMEMBERED UUID
 *     (``appGraph.curveIds``) and the patch body carries NO ``name`` at all,
 *     so a re-save can only ever touch the product's own rows and never
 *     renames them into a conflict. Name matching is never used anywhere.
 *
 * Backward compatibility: rows persisted before this fix (e.g. one named
 * ``discount``) keep their name — the name-less PATCH body leaves it as-is —
 * and load/price paths are untouched (by-reference pricing resolves curves by
 * UUID + role tags, never by name).
 */

import type { components } from './_generated/orchestrator';

type CurveCreate = components['schemas']['CurveCreate'];
type CurveUpdate = components['schemas']['CurveUpdate'];

/** Cap the product-name part so derived names stay readable (and well under
 * any storage limit) even for very long product names. */
const MAX_PRODUCT_NAME_PART = 80;

/** Short collision-resistant suffix: base36 millis + 4 random base36 chars. */
function uniqueSuffix(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

/**
 * Derive the unique, human-readable name for a product-owned persisted curve:
 * ``"<product> — <role> (<suffix>)"``. The suffix makes collisions
 * impossible in practice even when two products share a display name.
 */
export function deriveGraphCurveName(productName: string, role: string): string {
  const trimmed = (productName ?? '').trim() || 'product';
  const clipped =
    trimmed.length > MAX_PRODUCT_NAME_PART
      ? trimmed.slice(0, MAX_PRODUCT_NAME_PART).trimEnd()
      : trimmed;
  const rolePart = (role ?? '').trim() || 'curve';
  return `${clipped} — ${rolePart} (${uniqueSuffix()})`;
}

/**
 * The CREATE body for a persisted graph curve: the wire-shaped body with the
 * name replaced by the derived unique name. The wire ``name`` (a role
 * constant like ``discount``, or a source-curve id) is never persisted.
 */
export function graphCurveCreateBody(
  body: CurveCreate,
  productName: string,
  role: string,
): CurveCreate {
  return { ...body, name: deriveGraphCurveName(productName, role) };
}

/**
 * The PATCH body for a re-saved graph curve: the wire-shaped body with the
 * ``name`` REMOVED entirely, so the row (addressed by its remembered UUID)
 * keeps whatever name it has — no rename, no rename-conflict, and legacy
 * rows created before the naming fix stay loadable unchanged.
 */
export function graphCurvePatchBody(body: CurveCreate): CurveUpdate {
  const { name: _name, ...rest } = body;
  return rest;
}
