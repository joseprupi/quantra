// Backup export/import — assembles a portable JSON snapshot of the user's
// entities and restores it. Every entity type below is
// BACKEND-BACKED (app.* via the CRUD API); the only localStorage-resident
// collections still carried are the legacy `indices`/`quoteBook` stores.
//
// Extracted from Settings.tsx so the round-trip is unit-testable. The one
// behavioural fix vs the old inline code: vol surfaces are restored
// through the backend store (`saveVolSurface`) instead of being written to a
// dead `localStorage['quantra_vol_surfaces']` key that nothing reads — which
// used to silently drop every surface on restore.
import { getSavedCurves, saveCurve, generateId as generateCurveId } from './curves';
import { getSavedCurveSets, saveCurveSet, generateCurveSetId } from './curveSets';
import { fixedBondStore, floatingBondStore, SavedFixedRateBond, SavedFloatingRateBond } from './bonds';
import { getQuoteBook, saveQuoteBook, QuoteBookEntry } from './quoteBook';
import { indexStore } from './indices';
import { IrSwapRequest, StoredSwap, getIrSwaps, replaceIrSwaps } from './swaps';
import { InflationSwapRequest, StoredInflationSwap, getInflationSwaps, replaceInflationSwaps } from './inflationSwaps';
import { SwaptionRequest, StoredSwaption, getSwaptions, replaceSwaptions } from './swaptions';
import { getSwaptionModels, replaceSwaptionModels, SwaptionModelRecord } from './swaptionModels';
import { CdsRequest, StoredCds, getCdsItems, replaceCds } from './cds';
import { VolSurfaceSpec, getVolSurfaces, saveVolSurface } from './volSurfaces';
import { CreditCurveSpec, getCreditCurves, replaceCreditCurves } from './creditCurves';
import { EquityOptionRequest, StoredEquityOption, listEquityOptions, replaceEquityOptions } from './equityOptions';

export interface ExportData {
  version: string;
  exportedAt: string;
  indices?: any[];
  curves: any[];
  curveSets?: any[];
  quotes?: any[];
  quoteBook?: QuoteBookEntry[];
  volSurfaces?: VolSurfaceSpec[];
  creditCurves?: CreditCurveSpec[];
  fixedBonds: SavedFixedRateBond[];
  floatingBonds: SavedFloatingRateBond[];
  // The product collections below accept either wrapped entries
  // (preferred) or legacy bare requests, so older backup files keep
  // restoring cleanly while new ones round-trip user-defined names.
  swaps?: Array<IrSwapRequest | StoredSwap>;
  inflationSwaps?: Array<InflationSwapRequest | StoredInflationSwap>;
  swaptions?: Array<SwaptionRequest | StoredSwaption>;
  swaptionModels?: SwaptionModelRecord[];
  cds?: Array<CdsRequest | StoredCds>;
  equityOptions?: Array<EquityOptionRequest | StoredEquityOption>;
}

export interface ImportCounts {
  curves: number;
  curveSets: number;
  indices: number;
  quoteBook: number;
  volSurfaces: number;
  creditCurves: number;
  fixedBonds: number;
  floatingBonds: number;
  swaps: number;
  inflationSwaps: number;
  swaptions: number;
  swaptionModels: number;
  cds: number;
  equityOptions: number;
}

/** Legacy localStorage index store (not yet backend-backed). */
function getIndices(): any[] {
  try {
    const raw = localStorage.getItem('quantra_indices');
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

/** Assemble a full backup snapshot from every entity store. */
export async function buildBackup(): Promise<ExportData> {
  const curves = getSavedCurves();
  const fixedBonds = await fixedBondStore.getAll();
  const floatingBonds = await floatingBondStore.getAll();
  const indices = getIndices();
  const volSurfaces = getVolSurfaces();
  const creditCurves = getCreditCurves();
  const curveSets = getSavedCurveSets();
  // Export the full wrappers (id + name + request) so saved names round-trip
  // cleanly. Import accepts either shape so old backup files with bare
  // requests still restore correctly.
  const swaps = await getIrSwaps();
  const inflationSwaps = await getInflationSwaps();
  const swaptions = await getSwaptions();
  const swaptionModels = getSwaptionModels();
  const cds = await getCdsItems();
  const equityOptions = await listEquityOptions();

  return {
    version: '1.0',
    exportedAt: new Date().toISOString(),
    indices,
    curves,
    curveSets,
    quoteBook: getQuoteBook(),
    volSurfaces,
    creditCurves,
    fixedBonds,
    floatingBonds,
    swaps,
    inflationSwaps,
    swaptions,
    swaptionModels,
    cds,
    equityOptions,
  };
}

/**
 * Restore a backup snapshot into the entity stores.
 * `includeMarketData` gates the quote-book + vol-surface collections so the
 * "Load full example" flow can restore structures and market data in phases.
 */
export async function importBackup(data: ExportData, includeMarketData = true): Promise<ImportCounts> {
  const counts: ImportCounts = {
    curves: 0,
    curveSets: 0,
    indices: 0,
    quoteBook: 0,
    volSurfaces: 0,
    creditCurves: 0,
    fixedBonds: 0,
    floatingBonds: 0,
    swaps: 0,
    inflationSwaps: 0,
    swaptions: 0,
    swaptionModels: 0,
    cds: 0,
    equityOptions: 0,
  };

  // Import indices
  if (data.indices && Array.isArray(data.indices)) {
    for (const idx of data.indices) {
      if (!idx?.id) continue;
      await indexStore.save(idx as any);
      counts.indices++;
    }
  }

  // Import curves — backend-backed: each becomes a stored curve
  // row. Foreign ids get fresh creates; name clashes 409.
  if (data.curves && Array.isArray(data.curves)) {
    for (const curve of data.curves) {
      if (curve.id && curve.points) {
        try {
          await saveCurve({ ...curve, id: generateCurveId() });
          counts.curves++;
        } catch (err) {
          console.warn('Backup import: curve skipped:', curve?.name, err);
        }
      }
    }
  }

  // `data.quotes` (the legacy static-quote store) is ignored on import:
  // the surface was dropped — market data lives in the market-data
  // server (md.*), loaded by the ingester, not in user backups.

  // Import curve sets — backend-backed. Legacy backups embed
  // foreign curve ids; those soft refs will not resolve here.
  if (data.curveSets && Array.isArray(data.curveSets)) {
    for (const cs of data.curveSets) {
      if (cs.id) {
        try {
          await saveCurveSet({ ...cs, id: generateCurveSetId() });
          counts.curveSets++;
        } catch (err) {
          console.warn('Backup import: curve set skipped:', cs?.name, err);
        }
      }
    }
  }

  // Import fixed bonds
  if (data.fixedBonds && Array.isArray(data.fixedBonds)) {
    for (const bond of data.fixedBonds) {
      if (bond.id) {
        await fixedBondStore.save(bond);
        counts.fixedBonds++;
      }
    }
  }

  // Import floating bonds
  if (data.floatingBonds && Array.isArray(data.floatingBonds)) {
    for (const bond of data.floatingBonds) {
      if (bond.id) {
        await floatingBondStore.save(bond);
        counts.floatingBonds++;
      }
    }
  }

  // Import swaps (API-compliant request objects)
  if (data.swaps && Array.isArray(data.swaps)) {
    await replaceIrSwaps(data.swaps);
    counts.swaps = data.swaps.length;
  }

  // Import inflation swaps (API-compliant request objects)
  if (data.inflationSwaps && Array.isArray(data.inflationSwaps)) {
    await replaceInflationSwaps(data.inflationSwaps);
    counts.inflationSwaps = data.inflationSwaps.length;
  }

  // Import swaptions (API-compliant request objects)
  if (data.swaptions && Array.isArray(data.swaptions)) {
    await replaceSwaptions(data.swaptions);
    counts.swaptions = data.swaptions.length;
  }

  // Import saved swaption models
  if (data.swaptionModels && Array.isArray(data.swaptionModels)) {
    replaceSwaptionModels(data.swaptionModels);
    counts.swaptionModels = data.swaptionModels.length;
  }

  // Import CDS requests
  if (data.cds && Array.isArray(data.cds)) {
    await replaceCds(data.cds);
    counts.cds = data.cds.length;
  }
  if (data.equityOptions && Array.isArray(data.equityOptions)) {
    await replaceEquityOptions(data.equityOptions);
    counts.equityOptions = data.equityOptions.length;
  }

  // Import quoteBook (still localStorage-resident; merged by id)
  if (includeMarketData && data.quoteBook && Array.isArray(data.quoteBook)) {
    const existingBook = getQuoteBook();
    for (const entry of data.quoteBook) {
      if (entry.id && Array.isArray(entry.series)) {
        const idx = existingBook.findIndex(e => e.id === entry.id);
        if (idx === -1) {
          existingBook.push(entry);
        } else {
          existingBook[idx] = entry;
        }
        counts.quoteBook++;
      }
    }
    saveQuoteBook(existingBook);
  }

  // Import vol surfaces — backend-backed. FIX: route each
  // surface through saveVolSurface (app.vol_surfaces) instead of writing the
  // dead localStorage['quantra_vol_surfaces'] key, which nothing reads and so
  // silently dropped every surface on restore.
  if (includeMarketData && data.volSurfaces && Array.isArray(data.volSurfaces)) {
    for (const surface of data.volSurfaces) {
      if (!surface?.id) continue;
      try {
        await saveVolSurface(surface);
        counts.volSurfaces++;
      } catch (err) {
        console.warn('Backup import: vol surface skipped:', surface?.id, err);
      }
    }
  }

  if (data.creditCurves && Array.isArray(data.creditCurves)) {
    await replaceCreditCurves(data.creditCurves);
    counts.creditCurves = data.creditCurves.length;
  }

  return counts;
}
