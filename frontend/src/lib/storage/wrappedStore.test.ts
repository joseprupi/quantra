import { beforeEach, describe, expect, it } from 'vitest';

import { createWrappedStore } from './wrappedStore';
import { makeFakeCrudClient } from './fakeCrudClient';

interface Req {
  v: number;
}

const KEY = 'quantra_test_wrapped';

function makeStore() {
  // Backend-backed: each store instance gets a fresh in-memory
  // fake of the product CRUD client.
  return createWrappedStore<Req>({
    storageKey: KEY,
    client: makeFakeCrudClient(),
    deriveName: (r) => `req-${r.v}`,
  });
}

describe('wrappedStore — Phase-5 appId/appGraph bridge (additive)', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('stamps appId + appGraph on save without touching the local id/request', async () => {
    const store = makeStore();
    const id = await store.save({ v: 1 }, { name: 'first' });
    const stamped = await store.save(
      { v: 2 },
      { id, appId: 'swap-uuid', appGraph: { swapId: 'swap-uuid', curveSetId: 'cs' } },
    );

    expect(stamped).toBe(id); // same local record — additive, not a new row
    const entry = await store.getById(id);
    expect(entry?.id).toBe(id);
    expect(entry?.request).toEqual({ v: 2 });
    expect(entry?.appId).toBe('swap-uuid');
    expect(entry?.appGraph).toEqual({ swapId: 'swap-uuid', curveSetId: 'cs' });
  });

  it('preserves a prior appId/appGraph across a re-save that omits them', async () => {
    const store = makeStore();
    const id = await store.save({ v: 1 }, { appId: 'a', appGraph: { swapId: 'a' } });
    await store.save({ v: 2 }, { id, name: 'renamed' });

    const entry = await store.getById(id);
    expect(entry?.appId).toBe('a');
    expect(entry?.appGraph).toEqual({ swapId: 'a' });
    expect(entry?.name).toBe('renamed');
  });

  it('round-trips appId/appGraph through the persisted rows (fromApi/toApi)', async () => {
    // Two store instances share ONE backend (the fake client) — the second
    // instance re-reads the rows the first wrote, proving the row payload
    // round-trips the bridge fields.
    const shared = makeFakeCrudClient();
    const storeA = createWrappedStore<Req>({ storageKey: KEY, client: shared, deriveName: (r) => `req-${r.v}` });
    const id = await storeA.save({ v: 1 }, { appId: 'a', appGraph: { swapId: 'a', curveIds: { d: 'c1' } } });

    const storeB = createWrappedStore<Req>({ storageKey: KEY, client: shared, deriveName: (r) => `req-${r.v}` });
    const reloaded = await storeB.getById(id);
    expect(reloaded?.appId).toBe('a');
    expect(reloaded?.appGraph).toEqual({ swapId: 'a', curveIds: { d: 'c1' } });
  });

  it('leaves appId undefined for an unsaved (local-only) entry', async () => {
    const store = makeStore();
    const id = await store.save({ v: 1 });
    const entry = await store.getById(id);
    expect(entry?.appId).toBeUndefined();
    expect(entry?.appGraph).toBeUndefined();
  });
});
