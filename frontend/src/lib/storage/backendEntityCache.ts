// Generic backend-backed entity cache.
//
// The storage/curves.ts pattern factored out: the orchestrator's
// `app.*` CRUD is the single source of truth; this cache keeps the local
// business-id-keyed view so synchronous call sites keep working. NO
// localStorage anywhere.
//
// Identity model: unlike curves (whose page-level ids became backend UUIDs
// directly), these entities are referenced BY BUSINESS ID from other
// objects (curve points name an index id, swaption models name a
// vol_surface_id, curve sets carry credit-curve ids, sourceRefs...).
// Renaming them to UUIDs would break every soft ref, so the local `id` stays
// the business key: it is stored as the backend row's unique `name` (plus
// `body.local_id`), and the row UUID is tracked internally for PATCH/DELETE.
import type { ListParams, NamedCrudClient } from '../api/crud';
import type { OrchestratorResult } from '../api/types';

interface RowBase {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

const PAGE_LIMIT = 200;

function unwrap<T>(result: OrchestratorResult<T>): T {
  if (!result.ok) {
    const err = new Error(result.envelope.error) as Error & { code?: string };
    err.code = result.envelope.code;
    throw err;
  }
  return result.data;
}

export interface BackendEntityCache<TLocal> {
  /** Re-fetch every page from the backend and replace the cache. */
  refresh(): Promise<TLocal[]>;
  /** Await the first backend load (no-op afterwards). */
  ensureLoaded(): Promise<TLocal[]>;
  /** Synchronous cache read (empty before the first load). */
  getAll(): TLocal[];
  getById(id: string): TLocal | null;
  /** Create (unknown business id) or PATCH (known) the backend row. */
  save(item: TLocal): Promise<TLocal>;
  /** Delete the backend row behind a business id (no-op when unknown). */
  remove(id: string): Promise<void>;
  /** Delete every row of this entity owned by the caller. */
  removeAll(): Promise<void>;
  /** TEST-ONLY: replace the cache without touching the API. */
  __setForTests(items: TLocal[]): void;
}

export function createBackendEntityCache<TLocal, TRow extends RowBase>(config: {
  client: NamedCrudClient<any, any, TRow, any>;
  /** Row → local shape; return null to drop unreadable rows. */
  fromApi(row: TRow): TLocal | null;
  /** Local shape → Create/Update body ({ name, ...scalars, body/payload }). */
  toApi(item: TLocal): any;
  /** The business key; doubles as the backend row `name`. */
  localId(item: TLocal): string;
}): BackendEntityCache<TLocal> {
  const { client, fromApi, toApi, localId } = config;

  let cache: TLocal[] = [];
  let loaded = false;
  let inflight: Promise<TLocal[]> | null = null;
  const uuidByLocalId = new Map<string, string>();

  async function fetchAllRows(): Promise<TRow[]> {
    const rows: TRow[] = [];
    let offset = 0;
    for (;;) {
      const params: ListParams = { limit: PAGE_LIMIT, offset };
      const page = unwrap(await client.list(params)) as {
        items: TRow[];
        page: { has_more: boolean };
      };
      rows.push(...page.items);
      if (!page.page.has_more) break;
      offset += PAGE_LIMIT;
    }
    return rows;
  }

  function ingest(rows: TRow[]): TLocal[] {
    uuidByLocalId.clear();
    const items: TLocal[] = [];
    for (const row of rows) {
      const item = fromApi(row);
      if (item === null) continue;
      items.push(item);
      uuidByLocalId.set(localId(item), row.id);
    }
    cache = items;
    loaded = true;
    return cache;
  }

  return {
    async refresh(): Promise<TLocal[]> {
      if (!inflight) {
        inflight = fetchAllRows()
          .then(ingest)
          .finally(() => {
            inflight = null;
          });
      }
      return inflight;
    },

    async ensureLoaded(): Promise<TLocal[]> {
      if (loaded) return cache;
      return this.refresh();
    },

    getAll(): TLocal[] {
      return cache;
    },

    getById(id: string): TLocal | null {
      return cache.find(item => localId(item) === id) || null;
    },

    async save(item: TLocal): Promise<TLocal> {
      const key = localId(item);
      const uuid = uuidByLocalId.get(key);
      const row = uuid
        ? unwrap(await client.patch(uuid, toApi(item)))
        : unwrap(await client.create(toApi(item)));
      const saved = fromApi(row as TRow) ?? item;
      uuidByLocalId.set(localId(saved), (row as TRow).id);
      cache = [...cache.filter(existing => localId(existing) !== key), saved];
      return saved;
    },

    async remove(id: string): Promise<void> {
      const uuid = uuidByLocalId.get(id);
      if (uuid) {
        unwrap(await client.delete(uuid));
        uuidByLocalId.delete(id);
      }
      cache = cache.filter(item => localId(item) !== id);
    },

    async removeAll(): Promise<void> {
      for (const item of [...cache]) {
        await this.remove(localId(item));
      }
    },

    __setForTests(items: TLocal[]): void {
      cache = [...items];
      loaded = true;
      uuidByLocalId.clear();
    },
  };
}
