/**
 * Client-side snapshot diffing for the entity audit trail (History panel).
 *
 * The backend stores full post-change row snapshots per version and no
 * server-side diff — the client flattens two snapshots into dotted-path
 * leaf maps and reports only the changed / added / removed keys.
 */

/** One leaf in a flattened snapshot: dotted path → display string. */
export type FlatSnapshot = Record<string, string>;

/**
 * Server-managed row fields excluded from the restore body AND from the
 * side-by-side diff's noise (they change on every write by construction).
 * `updated_at`/`created_at` still appear in the single-version snapshot view.
 */
export const SERVER_MANAGED_FIELDS = new Set([
  'id',
  'owner_uid',
  'created_at',
  'updated_at',
  'deleted_at',
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function leafDisplay(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

/**
 * Flatten an arbitrary JSON payload into `{ dotted.path: displayString }`.
 * Nested objects use dotted paths (`request.pricing.curve_set_id`), array
 * elements indexed paths (`points[2].rate`). Empty objects/arrays flatten to
 * a single leaf (`{}` / `[]`) so an emptied container still shows as a change.
 */
export function flattenPayload(value: unknown, prefix = ''): FlatSnapshot {
  const out: FlatSnapshot = {};
  const walk = (node: unknown, path: string): void => {
    if (isPlainObject(node)) {
      const keys = Object.keys(node);
      if (keys.length === 0) {
        out[path || '(root)'] = '{}';
        return;
      }
      for (const key of keys) walk(node[key], path ? `${path}.${key}` : key);
      return;
    }
    if (Array.isArray(node)) {
      if (node.length === 0) {
        out[path || '(root)'] = '[]';
        return;
      }
      node.forEach((item, i) => walk(item, `${path}[${i}]`));
      return;
    }
    out[path || '(root)'] = leafDisplay(node);
  };
  walk(value, prefix);
  return out;
}

/** One row in the side-by-side field diff. */
export interface DiffRow {
  path: string;
  kind: 'added' | 'removed' | 'changed';
  /** Value in the OLDER version (absent for `added`). */
  oldValue?: string;
  /** Value in the NEWER version (absent for `removed`). */
  newValue?: string;
}

function topLevelField(path: string): string {
  const dot = path.indexOf('.');
  const bracket = path.indexOf('[');
  const cut = Math.min(dot === -1 ? path.length : dot, bracket === -1 ? path.length : bracket);
  return path.slice(0, cut);
}

/**
 * Diff two version snapshots (older → newer). Only changed / added / removed
 * leaves are returned, in stable path order. Server-managed row fields
 * (`id`, timestamps, `owner_uid`) are excluded — they differ on every write
 * and carry no user-facing meaning in a field diff.
 */
export function diffSnapshots(
  olderPayload: Record<string, unknown>,
  newerPayload: Record<string, unknown>,
): DiffRow[] {
  const older = flattenPayload(olderPayload);
  const newer = flattenPayload(newerPayload);
  const paths = Array.from(new Set([...Object.keys(older), ...Object.keys(newer)])).sort();
  const rows: DiffRow[] = [];
  for (const path of paths) {
    if (SERVER_MANAGED_FIELDS.has(topLevelField(path))) continue;
    const inOld = path in older;
    const inNew = path in newer;
    if (inOld && inNew) {
      if (older[path] !== newer[path]) {
        rows.push({ path, kind: 'changed', oldValue: older[path], newValue: newer[path] });
      }
    } else if (inNew) {
      rows.push({ path, kind: 'added', newValue: newer[path] });
    } else {
      rows.push({ path, kind: 'removed', oldValue: older[path] });
    }
  }
  return rows;
}

/**
 * Derive the editable PATCH body from a version snapshot: keep exactly the
 * client-owned keys (the same fields the save flow sends — e.g.
 * `['name', 'request']` for the seven product entities), drop everything
 * server-managed. Keys absent from the snapshot are skipped, never sent as
 * `undefined`/`null`.
 */
export function restoreBodyFromSnapshot(
  payload: Record<string, unknown>,
  editableKeys: readonly string[],
): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const key of editableKeys) {
    if (SERVER_MANAGED_FIELDS.has(key)) continue;
    if (key in payload && payload[key] !== undefined) body[key] = payload[key];
  }
  return body;
}
