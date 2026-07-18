// Wrapped store for named, user-saved product configurations.
//
// Backend-backed: each StoredEntry is ONE row in the
// product's `/v1/{product}` table, marked `request.__wrapper__ = true`.
// The save-graph rows (inline bodies the by-reference price arm
// reads via `appId`) live in the SAME tables but are NOT wrapper-marked,
// so reads filter them out — no double listing, no localStorage.
// Consolidating wrapper + graph rows into one row is an explicit
// follow-up; this keeps the page-facing contract byte-identical.
//
// Identity: the local entry id (route key, referenced by detail URLs)
// doubles as the backend row's unique `name`; the human
// display name rides inside the JSONB. Numeric-position lookup and
// bare-request import normalisation are preserved.

import { generateId } from './dataStore';
import type { NamedCrudClient } from '../api/crud';
import { createBackendEntityCache, type BackendEntityCache } from './backendEntityCache';

/**
 * Source-ref linkage for lazy migration.
 *
 * The localStorage product *request* (the `request` field below) is the
 * post-resolution inline/legacy-fat body: ``resolveCurveArrayQuoteIds`` has
 * already stripped every ``quote_id`` to a frozen value, and the curve ids were
 * relabelled to ``'discount'``/``'forward'``/… So the request alone can no
 * longer be migrated to the backend faithfully — it has lost both the quote-id
 * leaves (the frontend sends quote-ids, never values) and the
 * link back to the quote-id-bearing reference-data stores (``storage/curves``,
 * ``indices``, ``creditCurves``, ``volSurfaces``, ``swaptionModels``).
 *
 * ``sourceRefs`` closes that gap: at SAVE time the product page already knows
 * which saved reference entities it selected (the CurveSetSelector ids, the
 * vol-surface id, …), so we stamp those *store ids* onto the local record. The
 * migration then loads the faithful, quote-id-bearing entities from their
 * stores by id and rebuilds the persist-graph input — keeping that invariant
 * and by-ref↔inline parity. It is ADDITIVE and reversible (clearing it reverts
 * to pure-local / un-migratable), exactly like ``appId``/``appGraph``.
 *
 * Records saved BEFORE the linkage existed carry no ``sourceRefs``; the
 * migration skips them with a surfaced reason (no false success) until the
 * user re-saves.
 */
export interface StoredSourceRefs {
  /**
   * Saved-curve store ids by role. ``role`` matches the persist-graph curve key
   * (``discount``/``forward`` for swap_ir/swaption, ``discount``/``dividend``
   * for equity, ``nominal``/``inflation`` for swaps_inflation, ``discount`` for
   * cds). ``curveId`` is the ``storage/curves`` id (retains ``quote_id``s).
   */
  curves?: Array<{ role: string; curveId: string }>;
  /** Saved ``storage/indices`` id where the product references an index entity. */
  indexId?: string;
  /** Saved ``storage/creditCurves`` id (cds). */
  creditCurveId?: string;
  /** Saved ``storage/volSurfaces`` id (swaption / equity_options). */
  volSurfaceId?: string;
  /** Saved ``storage/swaptionModels`` id (swaption, when a saved model is referenced). */
  swaptionModelId?: string;
}

export interface StoredEntry<R> {
  id: string;
  name?: string;
  createdAt: string;
  updatedAt: string;
  request: R;
  /**
   * id↔uuid bridge. When this local record has been
   * persisted server-side via the CRUD surface, ``appId`` holds the server-
   * minted UUID of the root entity (e.g. the saved swap id) and the
   * price layer switches to the by-reference arm. ``appGraph``
   * carries the rest of the persisted dependency graph (curve-set + per-curve
   * UUIDs) so an idempotent re-save PATCHes rather than duplicates. Both are
   * ADDITIVE — the local ``id`` and ``request`` are untouched, so the
   * localStorage path keeps working as the dual-read fallback.
   */
  appId?: string;
  appGraph?: Record<string, unknown>;
  /** Source-ref linkage — see StoredSourceRefs. */
  sourceRefs?: StoredSourceRefs;
}

export interface SaveOptions {
  id?: string;
  name?: string;
  /** Stamp/refresh the server-side UUID bridge (see StoredEntry). */
  appId?: string;
  appGraph?: Record<string, unknown>;
  /** Stamp/refresh the source-ref linkage (see StoredSourceRefs). */
  sourceRefs?: StoredSourceRefs;
}

export interface WrappedStore<R> {
  getAll(): Promise<StoredEntry<R>[]>;
  getById(id: string): Promise<StoredEntry<R> | null>;
  getRequestById(id: string): Promise<R | null>;
  save(request: R, opts?: SaveOptions): Promise<string>;
  delete(id: string): Promise<void>;
  clear(): Promise<void>;
  replaceAll(entries: Array<R | StoredEntry<R>>): Promise<void>;
  deriveName(request: R): string;
  exportJson(): Promise<string>;
  importJson(text: string): Promise<StoredEntry<R>[]>;
}

export interface WrappedStoreOptions<R> {
  /** Legacy localStorage key — retained only as a diagnostic label. */
  storageKey: string;
  /** The product's orchestrator CRUD client (`lib/api/crud.ts`). */
  client: NamedCrudClient<any, any, any, any>;
  deriveName: (request: R) => string;
  // Optional side effect run after any mutation that changes the persisted
  // contents. Used e.g. to backfill inflation indices when inflation swaps
  // change. Mirrors the original module-level hook.
  onChange?: (entries: StoredEntry<R>[]) => void | Promise<void>;
}

function isWrapped<R>(value: any): value is StoredEntry<R> {
  return (
    !!value &&
    typeof value === 'object' &&
    typeof value.id === 'string' &&
    'request' in value
  );
}

function nowIso(): string {
  return new Date().toISOString();
}

function normaliseInto<R>(value: unknown): { entries: StoredEntry<R>[]; migrated: boolean } {
  if (!Array.isArray(value)) return { entries: [], migrated: false };
  let migrated = false;
  const entries: StoredEntry<R>[] = [];
  for (const item of value) {
    if (isWrapped<R>(item)) {
      entries.push({
        id: item.id || generateId(),
        name: typeof item.name === 'string' && item.name.length > 0 ? item.name : undefined,
        createdAt: item.createdAt || nowIso(),
        updatedAt: item.updatedAt || item.createdAt || nowIso(),
        request: item.request,
        ...(typeof item.appId === 'string' && item.appId.length > 0 ? { appId: item.appId } : {}),
        ...(item.appGraph && typeof item.appGraph === 'object' ? { appGraph: item.appGraph } : {}),
        ...(item.sourceRefs && typeof item.sourceRefs === 'object' ? { sourceRefs: item.sourceRefs } : {}),
      });
      if (!item.id) migrated = true;
    } else if (item && typeof item === 'object') {
      // Legacy bare request — wrap it.
      entries.push({
        id: generateId(),
        createdAt: nowIso(),
        updatedAt: nowIso(),
        request: item as R,
      });
      migrated = true;
    }
  }
  return { entries, migrated };
}

// Backend row payload: the StoredEntry minus id (the id doubles as the row
// `name`; `__wrapper__` distinguishes these rows from save-graph rows
// sharing the same product table).
function entryToRowRequest<R>(entry: StoredEntry<R>): Record<string, unknown> {
  return {
    __wrapper__: true,
    ...(entry.name !== undefined ? { name: entry.name } : {}),
    createdAt: entry.createdAt,
    updatedAt: entry.updatedAt,
    request: entry.request,
    ...(entry.appId !== undefined ? { appId: entry.appId } : {}),
    ...(entry.appGraph !== undefined ? { appGraph: entry.appGraph } : {}),
    ...(entry.sourceRefs !== undefined ? { sourceRefs: entry.sourceRefs } : {}),
    local_id: entry.id,
  };
}

export function createWrappedStore<R>(opts: WrappedStoreOptions<R>): WrappedStore<R> {
  const { storageKey, client, deriveName, onChange } = opts;

  const cache: BackendEntityCache<StoredEntry<R>> = createBackendEntityCache<StoredEntry<R>, any>({
    client,
    fromApi(row) {
      const payload = (row.request ?? {}) as Record<string, any>;
      // Rows without the wrapper marker are save-graph bodies (the
      // by-reference price arm's target) — invisible to this store.
      if (payload.__wrapper__ !== true) return null;
      return {
        id: payload.local_id ?? row.name,
        name: typeof payload.name === 'string' && payload.name.length > 0 ? payload.name : undefined,
        createdAt: payload.createdAt || row.created_at,
        updatedAt: payload.updatedAt || row.updated_at,
        request: payload.request as R,
        ...(typeof payload.appId === 'string' && payload.appId.length > 0 ? { appId: payload.appId } : {}),
        ...(payload.appGraph && typeof payload.appGraph === 'object' ? { appGraph: payload.appGraph } : {}),
        ...(payload.sourceRefs && typeof payload.sourceRefs === 'object' ? { sourceRefs: payload.sourceRefs } : {}),
      };
    },
    toApi(entry) {
      return { name: entry.id, request: entryToRowRequest(entry) };
    },
    localId(entry) {
      return entry.id;
    },
  });

  async function loadAll(): Promise<StoredEntry<R>[]> {
    await cache.ensureLoaded();
    return cache.getAll();
  }

  async function fireOnChange(entries: StoredEntry<R>[]): Promise<void> {
    if (!onChange) return;
    try {
      await onChange(entries);
    } catch (err) {
      console.warn(`[${storageKey}] onChange hook failed`, err);
    }
  }

  return {
    async getAll() {
      const entries = await loadAll();
      await fireOnChange(entries);
      return entries;
    },

    async getById(id: string) {
      const entries = await loadAll();
      const direct = entries.find(e => e.id === id);
      if (direct) return direct;
      // Legacy URL fallback: numeric position lookup.
      const idx = Number(id);
      if (!Number.isNaN(idx) && idx >= 0 && idx < entries.length) {
        return entries[idx];
      }
      return null;
    },

    async getRequestById(id: string) {
      const entry = await this.getById(id);
      return entry?.request ?? null;
    },

    async save(request: R, options?: SaveOptions) {
      const entries = await loadAll();
      const now = nowIso();
      const incomingId = options?.id;
      const incomingName = options?.name?.trim() || undefined;

      let existing: StoredEntry<R> | undefined;
      if (incomingId) {
        existing = entries.find(e => e.id === incomingId);
        if (!existing) {
          const idx = Number(incomingId);
          if (!Number.isNaN(idx) && idx >= 0 && idx < entries.length) {
            existing = entries[idx];
          }
        }
      }

      // Server-id bridge: an explicit option stamps/overwrites; otherwise the
      // existing value is preserved across re-saves (additive — never cleared
      // implicitly).
      const nextAppId = options?.appId !== undefined ? options.appId : undefined;
      const nextAppGraph = options?.appGraph !== undefined ? options.appGraph : undefined;
      const nextSourceRefs = options?.sourceRefs !== undefined ? options.sourceRefs : undefined;

      const next: StoredEntry<R> = existing
        ? {
            id: existing.id,
            name: incomingName !== undefined ? incomingName : existing.name,
            createdAt: existing.createdAt || now,
            updatedAt: now,
            request,
            ...((nextAppId ?? existing.appId) !== undefined ? { appId: nextAppId ?? existing.appId } : {}),
            ...((nextAppGraph ?? existing.appGraph) !== undefined
              ? { appGraph: nextAppGraph ?? existing.appGraph }
              : {}),
            ...((nextSourceRefs ?? existing.sourceRefs) !== undefined
              ? { sourceRefs: nextSourceRefs ?? existing.sourceRefs }
              : {}),
          }
        : {
            id: incomingId && !entries.some(e => e.id === incomingId) ? incomingId : generateId(),
            name: incomingName,
            createdAt: now,
            updatedAt: now,
            request,
            ...(nextAppId !== undefined ? { appId: nextAppId } : {}),
            ...(nextAppGraph !== undefined ? { appGraph: nextAppGraph } : {}),
            ...(nextSourceRefs !== undefined ? { sourceRefs: nextSourceRefs } : {}),
          };

      const saved = await cache.save(next);
      await fireOnChange(cache.getAll());
      return saved.id;
    },

    async delete(id: string) {
      const entries = await loadAll();
      let targetId = id;
      if (!entries.some(e => e.id === id)) {
        const idx = Number(id);
        if (!Number.isNaN(idx) && idx >= 0 && idx < entries.length) {
          targetId = entries[idx].id;
        }
      }
      await cache.remove(targetId);
      await fireOnChange(cache.getAll());
    },

    async clear() {
      await loadAll();
      await cache.removeAll();
      await fireOnChange([]);
    },

    async replaceAll(items) {
      const { entries } = normaliseInto<R>(items);
      await loadAll();
      await cache.removeAll();
      for (const entry of entries) {
        await cache.save(entry);
      }
      await fireOnChange(cache.getAll());
    },

    deriveName,

    async exportJson() {
      const entries = await this.getAll();
      return JSON.stringify(entries, null, 2);
    },

    async importJson(text: string) {
      const data = JSON.parse(text);
      const { entries } = normaliseInto<R>(data);
      if (entries.length === 0) throw new Error('No entries found in import file');
      await loadAll();
      for (const entry of entries) {
        await cache.save(entry);
      }
      const all = cache.getAll();
      await fireOnChange(all);
      return entries;
    },
  };
}

// Helper used by storage modules that need to emit a one-line filename
// for a downloaded JSON file. Centralised so call sites don't repeat
// the date suffix pattern.
export function downloadJson(filename: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
