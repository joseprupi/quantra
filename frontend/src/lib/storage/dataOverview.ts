import { fixedBondStore, floatingBondStore } from './bonds';
import { getCdsItems } from './cds';
import { getCreditCurves } from './creditCurves';
import { getSavedCurves } from './curves';
import { getSavedCurveSets } from './curveSets';
import { indexStore } from './indices';
import { getQuoteBook } from './quoteBook';
import { getIrSwaps } from './swaps';
import { getInflationSwaps } from './inflationSwaps';
import { getSwaptionModels } from './swaptionModels';
import { getSwaptions } from './swaptions';
import { getVolSurfaces } from './volSurfaces';
import { listEquityOptions } from './equityOptions';

export interface DataOverviewStats {
  indices: number;
  curves: number;
  curveSets: number;
  quoteBookEntries: number;
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
  total: number;
}

export async function loadDataOverviewStats(): Promise<DataOverviewStats> {
  const [indices, fixedBonds, floatingBonds, swaps, inflationSwaps, swaptions, cds, equityOptions] = await Promise.all([
    indexStore.getAll(),
    fixedBondStore.getAll(),
    floatingBondStore.getAll(),
    getIrSwaps(),
    getInflationSwaps(),
    getSwaptions(),
    getCdsItems(),
    listEquityOptions(),
  ]);

  const counts = {
    indices: indices.length,
    curves: getSavedCurves().length,
    curveSets: getSavedCurveSets().length,
    quoteBookEntries: getQuoteBook().length,
    volSurfaces: getVolSurfaces().length,
    creditCurves: getCreditCurves().length,
    fixedBonds: fixedBonds.length,
    floatingBonds: floatingBonds.length,
    swaps: swaps.length,
    inflationSwaps: inflationSwaps.length,
    swaptions: swaptions.length,
    swaptionModels: getSwaptionModels().length,
    cds: cds.length,
    equityOptions: equityOptions.length,
  };

  return {
    ...counts,
    total: Object.values(counts).reduce((acc, v) => acc + v, 0),
  };
}
