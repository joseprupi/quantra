// TEST-ONLY in-memory stand-in for a `lib/api/crud.ts` NamedCrudClient.
//
// Backs the wrappedStore/product-store unit tests now that the stores are
// backend-backed: rows live in a Map keyed by a fake UUID, and
// every verb resolves an `ok` OrchestratorResult synchronously. Never import
// this from production code.
import type { NamedCrudClient } from '../api/crud';

interface FakeRow {
  id: string;
  name: string;
  request: unknown;
  created_at: string;
  updated_at: string;
  deleted_at: null;
}

export function makeFakeCrudClient(): NamedCrudClient<any, any, any, any> & {
  rows: Map<string, FakeRow>;
  reset(): void;
} {
  const rows = new Map<string, FakeRow>();
  let seq = 0;

  const ok = <T>(data: T) => ({ ok: true as const, data, duration_ms: 0 });

  return {
    rows,
    reset() {
      rows.clear();
    },
    async create(body: any) {
      const now = new Date().toISOString();
      const id = `00000000-0000-4000-8000-${String(++seq).padStart(12, '0')}`;
      const row: FakeRow = {
        id,
        name: body.name,
        request: body.request,
        created_at: now,
        updated_at: now,
        deleted_at: null,
      };
      rows.set(id, row);
      return ok(row);
    },
    async list() {
      return ok({
        items: Array.from(rows.values()),
        page: { limit: 200, offset: 0, has_more: false },
      });
    },
    async get(id: string) {
      return ok(rows.get(id));
    },
    async patch(id: string, body: any) {
      const row = rows.get(id)!;
      const next: FakeRow = {
        ...row,
        ...(body.name !== undefined ? { name: body.name } : {}),
        ...(body.request !== undefined ? { request: body.request } : {}),
        updated_at: new Date().toISOString(),
      };
      rows.set(id, next);
      return ok(next);
    },
    async delete(id: string) {
      rows.delete(id);
      return ok(undefined as void);
    },
    async restore(id: string) {
      return ok(rows.get(id));
    },
  };
}
