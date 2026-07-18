import { beforeEach, describe, expect, it, vi } from 'vitest';

// The index store is backend-backed; mock the crud client so
// normalization-on-read is exercised through the API row shape.
const crudMock = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
  get: vi.fn(),
  restore: vi.fn(),
}));
vi.mock('../api/crud', () => ({ indices: crudMock }));

import {
  indexStore,
  refreshIndices,
  storedToInflationIndexSpec,
  storedToRateIndexDef,
  type StoredIndexSpec,
} from './indices';

const storage = new Map<string, string>();

Object.defineProperty(globalThis, 'localStorage', {
  value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storage.set(key, value);
    },
    removeItem: (key: string) => {
      storage.delete(key);
    },
    clear: () => {
      storage.clear();
    },
  },
  configurable: true,
});

describe('index storage converters', () => {
  beforeEach(() => {
    storage.clear();
    vi.clearAllMocks();
  });

  it('maps saved IBOR indices to rate index defs', () => {
    const saved: StoredIndexSpec = {
      id: 'EURIBOR_6M',
      type: 'IBOR',
      family: 'Euribor',
      tenor_number: 6,
      tenor_time_unit: 'Months',
      fixing_days: 2,
      calendar: 'TARGET',
      business_day_convention: 'ModifiedFollowing',
      day_counter: 'Actual360',
      createdAt: '2026-03-07T00:00:00.000Z',
      updatedAt: '2026-03-07T00:00:00.000Z',
    };

    expect(storedToRateIndexDef(saved)).toEqual(expect.objectContaining({
      id: 'EURIBOR_6M',
      index_type: 'Ibor',
      currency: 'EUR',
      tenor: { n: 6, unit: 'Months' },
    }));
  });

  it('does not treat inflation indices as rate indices', () => {
    const saved: StoredIndexSpec = {
      id: 'EUHICP',
      type: 'Inflation',
      family_name: 'EU HICP',
      currency: 'EUR',
      frequency: 'Monthly',
      availability_lag: { n: 2, unit: 'Months' },
      observation_lag: { n: 3, unit: 'Months' },
      interpolated: true,
      revised: false,
      kind: 'ZeroInflation',
      fixing_days: 0,
      calendar: 'TARGET',
      day_counter: 'Actual365Fixed',
      createdAt: '2026-03-07T00:00:00.000Z',
      updatedAt: '2026-03-07T00:00:00.000Z',
    };

    expect(storedToRateIndexDef(saved)).toBeNull();
    expect(storedToInflationIndexSpec(saved)).toEqual(expect.objectContaining({
      id: 'EUHICP',
      family_name: 'EU HICP',
      kind: 'ZeroInflation',
      currency: 'EUR',
    }));
  });

  it('normalizes legacy-shaped backend rows on read', async () => {
    // Rows as the demo seeder writes them: unique business id in `name`,
    // legacy portal fields riding in `body` (incl. `source_name` carrying
    // the human family name).
    crudMock.list.mockResolvedValue({
      ok: true,
      data: {
        items: [
          {
            id: '11111111-1111-4111-8111-111111111111',
            name: 'EURIBOR_6M',
            kind: 'IBOR',
            currency: 'EUR',
            calendar: 'TARGET',
            day_counter: 'Actual360',
            body: {
              index_type: 'Ibor',
              source_name: 'Euribor',
              tenor_number: 6,
              tenor_time_unit: 'Months',
              fixing_days: 2,
              local_id: 'EURIBOR_6M',
            },
            created_at: '2026-02-09T10:00:00Z',
            updated_at: '2026-02-09T10:00:00Z',
          },
          {
            id: '22222222-2222-4222-8222-222222222222',
            name: 'EUHICP',
            kind: 'Inflation',
            currency: 'EUR',
            calendar: 'TARGET',
            day_counter: 'Actual365Fixed',
            body: {
              family_name: 'EU HICP',
              frequency: 'Monthly',
              availability_lag: { n: 2, unit: 'Months' },
              observation_lag: { n: 3, unit: 'Months' },
              interpolated: true,
              revised: false,
              kind: 'ZeroInflation',
              fixings: [{ date: '2025-10-01', value: 108.4 }],
              local_id: 'EUHICP',
            },
            created_at: '2026-02-09T10:00:00Z',
            updated_at: '2026-02-09T10:00:00Z',
          },
        ],
        page: { limit: 200, offset: 0, has_more: false },
      },
    });

    await refreshIndices();
    const normalized = await indexStore.getAll();
    expect(normalized).toEqual([
      expect.objectContaining({
        id: 'EURIBOR_6M',
        type: 'IBOR',
        family: 'Euribor',
      }),
      expect.objectContaining({
        id: 'EUHICP',
        type: 'Inflation',
        family_name: 'EU HICP',
      }),
    ]);
  });
});
