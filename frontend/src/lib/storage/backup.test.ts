// Backup export/import round-trip tests.
//
// Primary guard: restoring a backup must route vol surfaces through
// the backend store (saveVolSurface), NOT write the dead
// localStorage['quantra_vol_surfaces'] key, which nothing reads and which used
// to silently drop every surface on restore.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  // curves
  getSavedCurves: vi.fn(() => []),
  saveCurve: vi.fn(async (c: any) => c),
  generateCurveId: vi.fn(() => 'curve-new-id'),
  // curveSets
  getSavedCurveSets: vi.fn(() => []),
  saveCurveSet: vi.fn(async (c: any) => c),
  generateCurveSetId: vi.fn(() => 'curveset-new-id'),
  // bonds
  fixedGetAll: vi.fn(async () => []),
  fixedSave: vi.fn(async () => undefined),
  floatingGetAll: vi.fn(async () => []),
  floatingSave: vi.fn(async () => undefined),
  // quoteBook
  getQuoteBook: vi.fn(() => [] as any[]),
  saveQuoteBook: vi.fn(),
  // indices
  indexSave: vi.fn(async () => undefined),
  // swaps
  getIrSwaps: vi.fn(async () => []),
  replaceIrSwaps: vi.fn(async () => undefined),
  // inflationSwaps
  getInflationSwaps: vi.fn(async () => []),
  replaceInflationSwaps: vi.fn(async () => undefined),
  // swaptions
  getSwaptions: vi.fn(async () => []),
  replaceSwaptions: vi.fn(async () => undefined),
  // swaptionModels
  getSwaptionModels: vi.fn(() => []),
  replaceSwaptionModels: vi.fn(),
  // cds
  getCdsItems: vi.fn(async () => []),
  replaceCds: vi.fn(async () => undefined),
  // volSurfaces
  getVolSurfaces: vi.fn(() => []),
  saveVolSurface: vi.fn(async (s: any) => s),
  // creditCurves
  getCreditCurves: vi.fn(() => []),
  replaceCreditCurves: vi.fn(async () => undefined),
  // equityOptions
  listEquityOptions: vi.fn(async () => []),
  replaceEquityOptions: vi.fn(async () => undefined),
}));

vi.mock('./curves', () => ({
  getSavedCurves: h.getSavedCurves,
  saveCurve: h.saveCurve,
  generateId: h.generateCurveId,
}));
vi.mock('./curveSets', () => ({
  getSavedCurveSets: h.getSavedCurveSets,
  saveCurveSet: h.saveCurveSet,
  generateCurveSetId: h.generateCurveSetId,
}));
vi.mock('./bonds', () => ({
  fixedBondStore: { getAll: h.fixedGetAll, save: h.fixedSave },
  floatingBondStore: { getAll: h.floatingGetAll, save: h.floatingSave },
}));
vi.mock('./quoteBook', () => ({
  getQuoteBook: h.getQuoteBook,
  saveQuoteBook: h.saveQuoteBook,
}));
vi.mock('./indices', () => ({
  indexStore: { save: h.indexSave },
}));
vi.mock('./swaps', () => ({
  getIrSwaps: h.getIrSwaps,
  replaceIrSwaps: h.replaceIrSwaps,
}));
vi.mock('./inflationSwaps', () => ({
  getInflationSwaps: h.getInflationSwaps,
  replaceInflationSwaps: h.replaceInflationSwaps,
}));
vi.mock('./swaptions', () => ({
  getSwaptions: h.getSwaptions,
  replaceSwaptions: h.replaceSwaptions,
}));
vi.mock('./swaptionModels', () => ({
  getSwaptionModels: h.getSwaptionModels,
  replaceSwaptionModels: h.replaceSwaptionModels,
}));
vi.mock('./cds', () => ({
  getCdsItems: h.getCdsItems,
  replaceCds: h.replaceCds,
}));
vi.mock('./volSurfaces', () => ({
  getVolSurfaces: h.getVolSurfaces,
  saveVolSurface: h.saveVolSurface,
}));
vi.mock('./creditCurves', () => ({
  getCreditCurves: h.getCreditCurves,
  replaceCreditCurves: h.replaceCreditCurves,
}));
vi.mock('./equityOptions', () => ({
  listEquityOptions: h.listEquityOptions,
  replaceEquityOptions: h.replaceEquityOptions,
}));

import { buildBackup, importBackup, ExportData } from './backup';

function makeSurface(id: string): any {
  return { id, payload_type: 'BlackVolSpec', base: {}, series: [] };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('importBackup — vol surface restore', () => {
  it('routes vol surfaces through saveVolSurface, never the dead localStorage key', async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    const data: ExportData = {
      version: '1.0',
      exportedAt: '2026-07-09T00:00:00Z',
      curves: [],
      fixedBonds: [],
      floatingBonds: [],
      volSurfaces: [makeSurface('swaptvol_eur'), makeSurface('capvol_usd')],
    };

    const counts = await importBackup(data, true);

    // Every surface persisted to the backend store...
    expect(h.saveVolSurface).toHaveBeenCalledTimes(2);
    expect(h.saveVolSurface).toHaveBeenCalledWith(makeSurface('swaptvol_eur'));
    expect(h.saveVolSurface).toHaveBeenCalledWith(makeSurface('capvol_usd'));
    expect(counts.volSurfaces).toBe(2);

    // ...and the dead localStorage key is NEVER written (the silent-drop bug).
    const wroteDeadKey = setItemSpy.mock.calls.some(([key]) => key === 'quantra_vol_surfaces');
    expect(wroteDeadKey).toBe(false);
    expect(localStorage.getItem('quantra_vol_surfaces')).toBeNull();
  });

  it('skips vol surfaces when includeMarketData is false', async () => {
    const data: ExportData = {
      version: '1.0',
      exportedAt: '2026-07-09T00:00:00Z',
      curves: [],
      fixedBonds: [],
      floatingBonds: [],
      volSurfaces: [makeSurface('swaptvol_eur')],
    };

    const counts = await importBackup(data, false);

    expect(h.saveVolSurface).not.toHaveBeenCalled();
    expect(counts.volSurfaces).toBe(0);
  });

  it('tolerates a per-surface save failure without aborting the import', async () => {
    h.saveVolSurface
      .mockRejectedValueOnce(new Error('409 name clash'))
      .mockResolvedValueOnce(makeSurface('ok'));
    const data: ExportData = {
      version: '1.0',
      exportedAt: '2026-07-09T00:00:00Z',
      curves: [],
      fixedBonds: [],
      floatingBonds: [],
      volSurfaces: [makeSurface('bad'), makeSurface('ok')],
    };

    const counts = await importBackup(data, true);

    expect(h.saveVolSurface).toHaveBeenCalledTimes(2);
    expect(counts.volSurfaces).toBe(1); // only the successful one counted
  });
});

describe('importBackup — other collections route to backend stores', () => {
  it('restores indices, curves and swaps through their stores', async () => {
    const data: ExportData = {
      version: '1.0',
      exportedAt: '2026-07-09T00:00:00Z',
      indices: [{ id: 'idx1' }, { id: 'idx2' }],
      curves: [{ id: 'c1', points: [{}] }],
      fixedBonds: [],
      floatingBonds: [],
      swaps: [{ id: 's1' } as any, { id: 's2' } as any],
    };

    const counts = await importBackup(data, true);

    expect(h.indexSave).toHaveBeenCalledTimes(2);
    expect(counts.indices).toBe(2);
    expect(h.saveCurve).toHaveBeenCalledTimes(1);
    expect(counts.curves).toBe(1);
    expect(h.replaceIrSwaps).toHaveBeenCalledWith(data.swaps);
    expect(counts.swaps).toBe(2);
  });
});

describe('buildBackup', () => {
  it('assembles a snapshot pulling vol surfaces from the backend cache', async () => {
    h.getVolSurfaces.mockReturnValueOnce([makeSurface('a'), makeSurface('b')] as any);
    h.getSavedCurves.mockReturnValueOnce([{ id: 'c1' }] as any);

    const snapshot = await buildBackup();

    expect(snapshot.version).toBe('1.0');
    expect(snapshot.volSurfaces).toHaveLength(2);
    expect(snapshot.curves).toHaveLength(1);
    expect(h.getVolSurfaces).toHaveBeenCalled();
  });
});
