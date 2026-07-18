// Backend-backed curve-set store tests.
//
// The store is a thin adapter over the orchestrator CRUD client with an
// in-memory cache; these tests mock the crud module and assert the
// body-flattening mapping plus create-vs-patch routing.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Curve, CurveSet } from '../types';

const crudMock = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
  get: vi.fn(),
  restore: vi.fn(),
}));

vi.mock('../api/crud', () => ({ curveSets: crudMock }));

import {
  getSavedCurveSets,
  refreshCurveSets,
  resolveCurveSetCurves,
  saveCurveSet,
  __setCurveSetsCacheForTests,
} from './curveSets';
import { __setCurvesCacheForTests } from './curves';

const BACKEND_UUID = '7dae198d-af66-4f86-b879-a8cfb6d8b236';

const sampleCurve: Curve = {
  id: 'curve-uuid-1',
  name: 'EUR Discount',
  currency: 'EUR',
  role: 'discount',
  day_counter: 'Actual360',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: '2026-03-05',
  points: [],
  createdAt: '2026-03-05T00:00:00.000Z',
  updatedAt: '2026-03-05T00:00:00.000Z',
};

function apiRow(overrides: Record<string, unknown> = {}) {
  return {
    id: BACKEND_UUID,
    name: 'EUR Multi-Curve Set',
    currency: 'EUR',
    body: {
      description: 'EUR dual-curve framework',
      as_of_date: '2026-02-09',
      curve_refs: [{ curve_id: sampleCurve.id, role: 'discount', name: 'EUR Discount' }],
      quote_ids: [],
    },
    created_at: '2026-02-09T10:00:00Z',
    updated_at: '2026-02-09T10:00:00Z',
    deleted_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  __setCurveSetsCacheForTests([]);
  __setCurvesCacheForTests([]);
});

describe('curve set storage (backend-backed)', () => {
  it('flattens the JSONB body into the portal CurveSet shape on refresh', async () => {
    crudMock.list.mockResolvedValue({
      ok: true,
      data: { items: [apiRow()], page: { limit: 200, offset: 0, has_more: false } },
    });

    const sets = await refreshCurveSets();

    expect(sets).toHaveLength(1);
    const set = sets[0];
    expect(set.id).toBe(BACKEND_UUID);
    expect(set.description).toBe('EUR dual-curve framework');
    expect(set.as_of_date).toBe('2026-02-09');
    expect(set.curve_refs).toHaveLength(1);
    expect(set.curve_refs[0]).toEqual(
      expect.objectContaining({ curve_id: sampleCurve.id, role: 'discount', label: 'EUR Discount' }),
    );
    expect(getSavedCurveSets()).toEqual(sets);
  });

  it('creates on a local draft id and adopts the backend UUID', async () => {
    crudMock.create.mockResolvedValue({ ok: true, data: apiRow({ name: 'New Set' }) });

    const draft: CurveSet = {
      id: 'cset_123_abc',
      name: 'New Set',
      currency: 'EUR',
      as_of_date: '2026-02-09',
      curve_refs: [],
      quote_ids: [],
      createdAt: '',
      updatedAt: '',
    };
    const created = await saveCurveSet(draft);

    expect(crudMock.create).toHaveBeenCalledTimes(1);
    expect(crudMock.patch).not.toHaveBeenCalled();
    expect(created.id).toBe(BACKEND_UUID);
    // The body carries everything the scalar columns don't.
    const sent = crudMock.create.mock.calls[0][0];
    expect(sent.name).toBe('New Set');
    expect(sent.body.as_of_date).toBe('2026-02-09');
  });

  it('patches when the id is already a backend UUID', async () => {
    crudMock.patch.mockResolvedValue({ ok: true, data: apiRow() });

    await saveCurveSet({
      id: BACKEND_UUID,
      name: 'EUR Multi-Curve Set',
      currency: 'EUR',
      as_of_date: '2026-02-09',
      curve_refs: [],
      quote_ids: [],
      createdAt: '',
      updatedAt: '',
    });

    expect(crudMock.patch).toHaveBeenCalledTimes(1);
    expect(crudMock.patch.mock.calls[0][0]).toBe(BACKEND_UUID);
    expect(crudMock.create).not.toHaveBeenCalled();
  });

  it('surfaces the error envelope as a coded error', async () => {
    crudMock.create.mockResolvedValue({
      ok: false,
      envelope: { error: 'Name already in use.', code: 'name_conflict' },
      httpStatus: 409,
      duration_ms: 1,
    });

    await expect(
      saveCurveSet({
        id: 'cset_dup',
        name: 'Duplicate',
        currency: 'EUR',
        as_of_date: '',
        curve_refs: [],
        quote_ids: [],
        createdAt: '',
        updatedAt: '',
      }),
    ).rejects.toMatchObject({ message: 'Name already in use.', code: 'name_conflict' });
  });

  it('resolves only existing referenced curves for consumers', () => {
    __setCurvesCacheForTests([sampleCurve]);
    __setCurveSetsCacheForTests([
      {
        id: BACKEND_UUID,
        name: 'Reference Set',
        currency: 'EUR',
        as_of_date: '2026-03-05',
        curve_refs: [
          { id: 'ref_1', curve_id: sampleCurve.id, role: 'discount' },
          { id: 'ref_2', curve_id: 'missing_curve', role: 'forward' },
        ],
        quote_ids: [],
        createdAt: '2026-03-05T00:00:00.000Z',
        updatedAt: '2026-03-05T00:00:00.000Z',
      },
    ]);

    const set = getSavedCurveSets()[0];
    expect(resolveCurveSetCurves(set)).toEqual([
      expect.objectContaining({ id: sampleCurve.id, name: sampleCurve.name }),
    ]);
  });
});
