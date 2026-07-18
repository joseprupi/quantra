import { beforeEach, describe, expect, it } from 'vitest';

import { buildSourceRefs } from './sourceRefs';
import { __setCurvesCacheForTests } from './curves';
import { createWrappedStore } from './wrappedStore';
import { makeFakeCrudClient } from './fakeCrudClient';
import type { Curve } from '../types';

// The curves store is backend-backed; tests seed the in-memory
// cache directly instead of round-tripping the API.
let seeded: Curve[] = [];

function seedCurve(id: string) {
  seeded.push({ id, name: id, currency: 'EUR', points: [] } as unknown as Curve);
  __setCurvesCacheForTests(seeded);
}

beforeEach(() => {
  localStorage.clear();
  seeded = [];
  __setCurvesCacheForTests([]);
});

describe('buildSourceRefs', () => {
  it('keeps curve refs whose id resolves in storage/curves', () => {
    seedCurve('curve_real_1');
    const refs = buildSourceRefs({ curves: [{ role: 'discount', curveId: 'curve_real_1' }] });
    expect(refs).toEqual({ curves: [{ role: 'discount', curveId: 'curve_real_1' }] });
  });

  it('drops a clobbered relabel (e.g. "discount") that does not resolve in the store', () => {
    // No curve with id 'discount' is saved — this is the on-load relabel case.
    const refs = buildSourceRefs({
      curves: [
        { role: 'discount', curveId: 'discount' },
        { role: 'forward', curveId: undefined },
      ],
    });
    // Nothing linkable → field omitted entirely.
    expect(refs).toBeUndefined();
  });

  it('keeps only the resolvable curves in a mixed set', () => {
    seedCurve('curve_real_disc');
    const refs = buildSourceRefs({
      curves: [
        { role: 'discount', curveId: 'curve_real_disc' },
        { role: 'forward', curveId: 'forward' }, // clobbered relabel — dropped
      ],
    });
    expect(refs?.curves).toEqual([{ role: 'discount', curveId: 'curve_real_disc' }]);
  });

  it('passes through index / credit / vol / model ids as supplied (page-gated)', () => {
    seedCurve('c1');
    const refs = buildSourceRefs({
      curves: [{ role: 'discount', curveId: 'c1' }],
      indexId: 'idx_1',
      creditCurveId: 'cc_1',
      volSurfaceId: 'vs_1',
      swaptionModelId: 'sm_1',
    });
    expect(refs).toEqual({
      curves: [{ role: 'discount', curveId: 'c1' }],
      indexId: 'idx_1',
      creditCurveId: 'cc_1',
      volSurfaceId: 'vs_1',
      swaptionModelId: 'sm_1',
    });
  });

  it('returns undefined when nothing is linkable', () => {
    expect(buildSourceRefs({})).toBeUndefined();
    expect(buildSourceRefs({ curves: [] })).toBeUndefined();
  });
});

describe('wrappedStore — sourceRefs persistence (additive)', () => {
  interface Req { v: number }
  const KEY = 'quantra_test_sourcerefs';
  const makeStore = () =>
    createWrappedStore<Req>({
      storageKey: KEY,
      client: makeFakeCrudClient(),
      deriveName: (r) => `req-${r.v}`,
    });

  it('persists sourceRefs on save and returns it via getById', async () => {
    const store = makeStore();
    const refs = { curves: [{ role: 'discount', curveId: 'c1' }], volSurfaceId: 'vs_1' };
    const id = await store.save({ v: 1 }, { name: 'one', sourceRefs: refs });
    const entry = await store.getById(id);
    expect(entry?.sourceRefs).toEqual(refs);
  });

  it('preserves an existing sourceRefs across a re-save that does not pass it', async () => {
    const store = makeStore();
    const refs = { curves: [{ role: 'discount', curveId: 'c1' }] };
    const id = await store.save({ v: 1 }, { name: 'one', sourceRefs: refs });
    await store.save({ v: 2 }, { id }); // re-save without sourceRefs
    const entry = await store.getById(id);
    expect(entry?.request.v).toBe(2);
    expect(entry?.sourceRefs).toEqual(refs); // additive — never implicitly cleared
  });

  it('leaves sourceRefs unset when never provided', async () => {
    const store = makeStore();
    const id = await store.save({ v: 9 }, { name: 'none' });
    const entry = await store.getById(id);
    expect(entry?.sourceRefs).toBeUndefined();
  });
});
