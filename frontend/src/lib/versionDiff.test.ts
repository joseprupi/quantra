import { describe, expect, it } from 'vitest';
import {
  diffSnapshots,
  flattenPayload,
  restoreBodyFromSnapshot,
} from './versionDiff';

describe('flattenPayload', () => {
  it('flattens nested objects with dotted paths and arrays with indices', () => {
    const flat = flattenPayload({
      name: 'sw',
      request: {
        notional: 5000000,
        pricing: { curve_set_id: 'abc' },
        points: [{ rate: 0.02 }, { rate: 0.03 }],
      },
    });
    expect(flat).toEqual({
      name: 'sw',
      'request.notional': '5000000',
      'request.pricing.curve_set_id': 'abc',
      'request.points[0].rate': '0.02',
      'request.points[1].rate': '0.03',
    });
  });

  it('renders null / empty containers as single leaves', () => {
    expect(flattenPayload({ a: null, b: {}, c: [] })).toEqual({
      a: 'null',
      b: '{}',
      c: '[]',
    });
  });
});

describe('diffSnapshots', () => {
  it('reports only changed keys with old → new values', () => {
    const rows = diffSnapshots(
      { name: 'sw', request: { notional: 5000000, fixed_rate: 0.02 } },
      { name: 'sw', request: { notional: 6000000, fixed_rate: 0.02 } },
    );
    expect(rows).toEqual([
      { path: 'request.notional', kind: 'changed', oldValue: '5000000', newValue: '6000000' },
    ]);
  });

  it('reports added and removed nested keys', () => {
    const rows = diffSnapshots(
      { request: { legacy: { flag: true } } },
      { request: { pricing: { curve_set_id: 'abc' } } },
    );
    expect(rows).toEqual([
      { path: 'request.legacy.flag', kind: 'removed', oldValue: 'true' },
      { path: 'request.pricing.curve_set_id', kind: 'added', newValue: 'abc' },
    ]);
  });

  it('excludes server-managed fields (timestamps churn on every write)', () => {
    const rows = diffSnapshots(
      { id: 'u1', updated_at: '2026-01-01T00:00:00Z', created_at: 'x', owner_uid: 'a', name: 'n' },
      { id: 'u1', updated_at: '2026-02-02T00:00:00Z', created_at: 'y', owner_uid: 'b', name: 'n' },
    );
    expect(rows).toEqual([]);
  });

  it('identical payloads → empty diff', () => {
    const payload = { name: 'sw', request: { a: [1, 2, 3] } };
    expect(diffSnapshots(payload, payload)).toEqual([]);
  });
});

describe('restoreBodyFromSnapshot', () => {
  it('keeps exactly the editable keys and drops server-managed fields', () => {
    const body = restoreBodyFromSnapshot(
      {
        id: 'uuid-1',
        owner_uid: 'dev-user',
        name: 'sw',
        request: { notional: 5000000 },
        created_at: 'x',
        updated_at: 'y',
        deleted_at: null,
      },
      ['name', 'request'],
    );
    expect(body).toEqual({ name: 'sw', request: { notional: 5000000 } });
  });

  it('skips editable keys absent from the snapshot', () => {
    expect(restoreBodyFromSnapshot({ name: 'curve' }, ['name', 'points', 'body'])).toEqual({
      name: 'curve',
    });
  });

  it('never emits a server-managed key even if listed as editable', () => {
    expect(restoreBodyFromSnapshot({ id: 'u', name: 'n' }, ['id', 'name'])).toEqual({ name: 'n' });
  });
});
