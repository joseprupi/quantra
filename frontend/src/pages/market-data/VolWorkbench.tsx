import { ChangeEvent, memo, useEffect, useMemo, useRef, useState } from 'react';
import Plot from 'react-plotly.js';
import Header from '../../components/Header';
import BackLink from '../../components/ui/BackLink';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { useVolSurfaceCache } from '../../hooks/useVolSurfaceCache';
import { SampleVolSurfacesResponse, SwaptionVolDiagnostics, VolSurfaceSampleResult, type CalibrateSwaptionVolResponse } from '../../lib/quantra-types';
import { orchestratorPost } from '../../lib/api/orchestrator';
import { deleteVolSurface, exportVolSurfaces, getVolSurfaces, importVolSurfaces, saveVolSurface, VolSurfaceSpec, type VolShape } from '../../lib/storage/volSurfaces';
import { buildSwaptionVolWirePayload, VolSurfacePayloadError, yearsToPeriod } from '../../lib/storage/volSurfacePayload';
import type { Curve } from '../../lib/types';
import { getSavedCurveSets, resolveCurveSetCurves } from '../../lib/storage/curveSets';
import { indexStore, storedToRateIndexDef, type StoredIndexSpec } from '../../lib/storage/indices';
import { getLegacyFlatQuotes, getQuoteBook, getResolutionMode, resolveQuoteValue } from '../../lib/storage/quoteBook';

import { collectIndexRefIds, CurveSet, IndexDef, TimeUnit } from '../../lib/types';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { backfillSurfaceAxesFromLegacy, bpToStrike, buildCubeGeometry, buildEquityGeometry, cubeToNodesMatrix, decodeDiagnosticsPeriod, isStrictlyIncreasing, nodesMatrixToCube, pickDefaultFloatIndex, rateFloatIndexOptions, strikeToBp, swapIndexIdForIndex, type EditableSwaptionKind } from './vol-workbench-helpers';
import type { DisplayUnits } from './vol-workbench-types';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import Matrix2DEditor, { fillAll, type Matrix2D, type MatrixCell } from '../../components/ui/Matrix2DEditor';
import PricingTraceLink from '../../components/products/PricingTraceLink';

type SurfaceType = 'Swaption' | 'Optionlet' | 'EquityBlack';
type OutputMode = 'Cube' | 'SmileSlice' | 'TermSlice' | 'ExpirySlice';
type StrikeAxis = 'AbsoluteStrike' | 'SpreadFromATM';
type SwaptionSurfaceKind = 'Constant' | 'AtmMatrix' | 'SmileCube' | 'SabrParams' | 'SabrCalibrate';
type SabrParamName = 'alpha' | 'beta' | 'rho' | 'nu';
const SABR_PARAM_NAMES: SabrParamName[] = ['alpha', 'beta', 'rho', 'nu'];
const SABR_PARAM_LABELS: Record<SabrParamName, string> = {
  alpha: 'α (alpha)',
  beta: 'β (beta)',
  rho: 'ρ (rho)',
  nu: 'ν (nu)',
};
const SABR_PARAM_FIELDS: Record<SabrParamName, keyof VolSurfaceSpec> = {
  alpha: 'sabr_alpha',
  beta: 'sabr_beta',
  rho: 'sabr_rho',
  nu: 'sabr_nu',
};
const SABR_PARAM_DEFAULTS: Record<SabrParamName, number> = {
  alpha: NaN,
  beta: 0.5,
  rho: NaN,
  nu: NaN,
};
// Default strike spreads for a fresh SabrCalibrate surface, in rate units
// (decimals): -100bp, -50bp, 0, +50bp, +100bp. Spreads from per-node ATM
// forward, matching quantra_SwaptionSabrCalibrateSpec's strike-axis
// interpretation. Brokers typically quote 5 strikes (ATM±50bp/±100bp) as
// the v1 default.
const DEFAULT_SABR_CALIBRATE_STRIKES: number[] = [-0.01, -0.005, 0, 0.005, 0.01];
function shapeForKind(kind: SwaptionSurfaceKind): VolShape {
  if (kind === 'AtmMatrix') return 'AtmMatrix2D';
  if (kind === 'SmileCube') return 'SmileCube3D';
  if (kind === 'SabrParams') return 'SabrParams';
  if (kind === 'SabrCalibrate') return 'SabrCalibrate';
  return 'Constant';
}
type GridType = 'TenorGrid' | 'RangeGrid';
type Tab = 'heatmap' | 'table' | 'diagnostics' | 'compare' | 'export';
type SliceMode = 'Smile' | 'Term' | 'Expiry';

type Period = { n: number; unit: TimeUnit };

type SamplingPreset = 'Fast' | 'Standard' | 'HQ';
const FAST_EXPIRIES: Period[] = [
  { n: 1, unit: 'Months' }, { n: 6, unit: 'Months' }, { n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }, { n: 5, unit: 'Years' }, { n: 10, unit: 'Years' },
];
const FAST_TENORS: Period[] = [
  { n: 1, unit: 'Years' }, { n: 3, unit: 'Years' }, { n: 5, unit: 'Years' }, { n: 10, unit: 'Years' }, { n: 20, unit: 'Years' },
];
const FAST_STRIKES = [-0.01, -0.005, 0, 0.005, 0.01];
const STANDARD_EXPIRIES: Period[] = [
  { n: 1, unit: 'Months' }, { n: 3, unit: 'Months' }, { n: 6, unit: 'Months' },
  { n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }, { n: 3, unit: 'Years' }, { n: 5, unit: 'Years' }, { n: 7, unit: 'Years' }, { n: 10, unit: 'Years' },
];
const STANDARD_TENORS: Period[] = [
  { n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }, { n: 3, unit: 'Years' }, { n: 5, unit: 'Years' },
  { n: 7, unit: 'Years' }, { n: 10, unit: 'Years' }, { n: 15, unit: 'Years' }, { n: 20, unit: 'Years' },
];
const STANDARD_STRIKES = [-0.02, -0.015, -0.01, -0.0075, -0.005, -0.0025, 0, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02];
const HQ_EXPIRIES: Period[] = [...STANDARD_EXPIRIES, { n: 15, unit: 'Years' }];
const HQ_TENORS: Period[] = [...STANDARD_TENORS];
const HQ_STRIKES = STANDARD_STRIKES;
const PERIOD_UNITS: TimeUnit[] = ['Days', 'Weeks', 'Months', 'Years'];

function periodLabel(p: Period): string {
  const unit = p.unit === 'Months' ? 'M' : p.unit === 'Years' ? 'Y' : p.unit === 'Weeks' ? 'W' : 'D';
  return `${p.n}${unit}`;
}

/**
 * Format a wall-clock instant as a human relative time ("just now", "12s
 * ago", "3m ago", "1h ago"). Used by the workbench header status pill so
 * the user can tell, while in Edit mode, how stale the cached Sampler
 * view is. Pure function — caller is responsible for re-rendering.
 */
function formatRelativeTime(thenMs: number, nowMs: number = Date.now()): string {
  const sec = Math.max(0, Math.floor((nowMs - thenMs) / 1000));
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}

function formatStrikeAxisLabel(v: number | undefined, axis: StrikeAxis, surfaceType: SurfaceType): string {
  if (v === undefined || !Number.isFinite(v)) return '—';
  if (surfaceType === 'EquityBlack') {
    return `K = ${v.toFixed(4)}`;
  }
  if (axis === 'SpreadFromATM') {
    const bp = v * 10000;
    return `Spread = ${bp >= 0 ? '+' : ''}${bp.toFixed(0)} bp`;
  }
  return `Strike = ${(v * 100).toFixed(3)}%`;
}

function toDisplayVol(v: number, units: DisplayUnits): number {
  if (units === 'bps') return v * 10000;
  if (units === 'pct') return v * 100;
  return v;
}

function formatVolBoth(v: number | undefined, surfaceType: SurfaceType): string {
  if (v === undefined || !Number.isFinite(v)) return '—';
  if (surfaceType === 'EquityBlack') {
    return `${(v * 100).toFixed(4)}% (${v.toFixed(6)})`;
  }
  return `${v.toFixed(6)} (${(v * 10000).toFixed(1)} bps)`;
}

function atmHeatmapColor(value: number): string {
  // Diverging blue→white→red scale tuned for typical Normal vol ranges
  // (0bp → 300bp). Anything beyond clamps to the endpoints. Pure literal
  // numbers only; quote/NaN cells get no background (Matrix2DEditor handles
  // that path).
  if (!Number.isFinite(value)) return 'transparent';
  const lo = 0;
  const mid = 0.01;
  const hi = 0.03;
  const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
  if (value <= mid) {
    const t = clamp01((value - lo) / (mid - lo));
    const r = Math.round(99 + (255 - 99) * t);
    const g = Math.round(102 + (255 - 102) * t);
    const b = Math.round(241 + (255 - 241) * t);
    return `rgb(${r}, ${g}, ${b})`;
  }
  const t = clamp01((value - mid) / (hi - mid));
  const r = Math.round(255 + (220 - 255) * t);
  const g = Math.round(255 + (38 - 255) * t);
  const b = Math.round(255 + (38 - 255) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

function normalizeStrikeGrid(values: number[]): number[] {
  return Array.from(new Set(
    values
      .filter(v => Number.isFinite(v))
      .map(v => Number(v.toFixed(10)))
  )).sort((a, b) => a - b);
}

function toHeatmap(
  sample: VolSurfaceSampleResult,
  strikeIdx: number,
  strikeFallback: number[] = [],
  forceNoTenor: boolean = false
): { rows: string[]; cols: string[]; values: number[][] } {
  const nE = sample.n_expiries || sample.expiries?.length || 0;
  const nT = sample.n_tenors || sample.tenors?.length || (sample.n_tenors === 0 ? 0 : 1);
  const hasTenor = !forceNoTenor && nT > 0;
  const inferredNoTenorNS = !hasTenor && nE > 0 && (sample.vols?.length || 0) >= nE
    ? Math.max(1, Math.floor((sample.vols?.length || 0) / nE))
    : 0;
  const declaredNS = sample.n_strikes || sample.strikes?.length || 0;
  const nS = hasTenor
    ? (declaredNS || strikeFallback.length || 0)
    : (declaredNS > 1
      ? declaredNS
      : (strikeFallback.length > 1 ? strikeFallback.length : (inferredNoTenorNS || declaredNS || 0)));
  const vols = sample.vols || [];
  const rows = sample.expiries || [];
  const cols = (sample.tenors || []).map(t => `${t.n}${t.unit?.[0] || ''}`);
  const strikeAxis = sample.strikes?.length === nS ? sample.strikes : (strikeFallback.length === nS ? strikeFallback : Array.from({ length: nS }, (_, i) => i + 1));
  const effectiveNS = !hasTenor && strikeFallback.length > 1 && nE > 0 && (vols.length === nE * strikeFallback.length)
    ? strikeFallback.length
    : nS;

  const matrix = Array.from({ length: Math.max(nE, 1) }, () => Array.from({ length: Math.max(nT, 1) }, () => NaN));
  if (!hasTenor) {
    if (effectiveNS > 1) {
      const strikeCols = (strikeAxis.length === effectiveNS ? strikeAxis : strikeFallback).map(v => Number.isFinite(v) ? v.toFixed(6) : String(v));
      const strikeMatrix = Array.from({ length: Math.max(nE, 1) }, () => Array.from({ length: Math.max(effectiveNS, 1) }, () => NaN));
      for (let i = 0; i < nE; i += 1) {
        for (let k = 0; k < effectiveNS; k += 1) {
          const idx = i * effectiveNS + k;
          strikeMatrix[i][k] = vols[idx] ?? NaN;
        }
      }
      return { rows, cols: strikeCols, values: strikeMatrix };
    }
    for (let i = 0; i < nE; i += 1) {
      const idx = i * Math.max(nS, 1) + Math.min(strikeIdx, Math.max(nS - 1, 0));
      matrix[i][0] = vols[idx] ?? NaN;
    }
    return { rows, cols: ['—'], values: matrix };
  }
  for (let i = 0; i < nE; i += 1) {
    for (let j = 0; j < nT; j += 1) {
      const idx = (i * nT + j) * Math.max(nS, 1) + Math.min(strikeIdx, Math.max(nS - 1, 0));
      matrix[i][j] = vols[idx] ?? NaN;
    }
  }
  return { rows, cols, values: matrix };
}

function toCsv(sample: VolSurfaceSampleResult): string {
  const nE = sample.n_expiries || sample.expiries?.length || 0;
  const nT = sample.n_tenors || sample.tenors?.length || (sample.n_tenors === 0 ? 0 : 1);
  const nS = sample.n_strikes || sample.strikes?.length || 0;
  const vols = sample.vols || [];
  const strikes = sample.strikes || [];
  const rows = ['expiry,tenor,strike,vol,atm,start,end'];
  const hasTenor = nT > 0;
  for (let i = 0; i < nE; i += 1) {
    for (let j = 0; j < Math.max(nT, 1); j += 1) {
      for (let k = 0; k < Math.max(nS, 1); k += 1) {
        const idx = hasTenor ? ((i * nT + j) * Math.max(nS, 1) + k) : (i * Math.max(nS, 1) + k);
        const expiry = sample.expiries?.[i] || '';
        const tenor = hasTenor ? `${sample.tenors?.[j]?.n ?? ''}${sample.tenors?.[j]?.unit?.[0] || ''}` : '';
        const strike = strikes[k] ?? '';
        const vol = vols[idx] ?? '';
        const atm = sample.atm_levels?.[hasTenor ? (i * nT + j) : i] ?? '';
        const start = sample.effective_swap_starts?.[hasTenor ? (i * nT + j) : i] ?? '';
        const end = sample.effective_swap_ends?.[hasTenor ? (i * nT + j) : i] ?? '';
        rows.push(`${expiry},${tenor},${strike},${vol},${atm},${start},${end}`);
      }
    }
  }
  return rows.join('\n');
}

export default function VolWorkbench() {
  const surfaceUi = entityUi.surface;
  const { asOfDate } = useAsOfDate();
  const cache = useVolSurfaceCache<SampleVolSurfacesResponse>();

  const [volSurfaces, setVolSurfaces] = useState<VolSurfaceSpec[]>([]);
  const [curveSets, setCurveSets] = useState<CurveSet[]>([]);
  const [allIndices, setAllIndices] = useState<StoredIndexSpec[]>([]);
  const [surfaceType, setSurfaceType] = useState<SurfaceType>('Swaption');
  const [swaptionKind, setSwaptionKind] = useState<SwaptionSurfaceKind>('Constant');
  const [outputMode, setOutputMode] = useState<OutputMode>('Cube');
  const [strikeAxis, setStrikeAxis] = useState<StrikeAxis>('AbsoluteStrike');
  const [volId, setVolId] = useState('');
  const [selectedCurveSetId, setSelectedCurveSetId] = useState('');
  const [swapIndexId, setSwapIndexId] = useState('EUR_SWAP_6M');
  const [floatIndexId, setFloatIndexId] = useState('EUR_6M');
  const [discountingCurveId, setDiscountingCurveId] = useState('');
  const [forwardingCurveId, setForwardingCurveId] = useState('');
  const [expiryGridType, setExpiryGridType] = useState<GridType>('TenorGrid');
  const [samplingPreset, setSamplingPreset] = useState<SamplingPreset>('Standard');
  const [expiryTenors, setExpiryTenors] = useState<Period[]>(STANDARD_EXPIRIES);
  const [rangeEndDate, setRangeEndDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() + 5);
    return d.toISOString().split('T')[0];
  });
  const [rangeStepN, setRangeStepN] = useState(1);
  const [rangeStepUnit, setRangeStepUnit] = useState<TimeUnit>('Months');
  const [rangeBusinessOnly, setRangeBusinessOnly] = useState(false);
  const [tenorGrid, setTenorGrid] = useState<Period[]>(STANDARD_TENORS);
  const [strikeGrid, setStrikeGrid] = useState<number[]>(STANDARD_STRIKES);
  const [newExpiryN, setNewExpiryN] = useState(1);
  const [newExpiryUnit, setNewExpiryUnit] = useState<TimeUnit>('Years');
  const [newTenorN, setNewTenorN] = useState(5);
  const [newTenorUnit, setNewTenorUnit] = useState<TimeUnit>('Years');
  const [newStrike, setNewStrike] = useState(0);
  const [allowExtrapolation, setAllowExtrapolation] = useState(true);
  const [maxPoints, setMaxPoints] = useState(2000);
  const [sliceExpiryIndex, setSliceExpiryIndex] = useState(0);
  const [sliceTenorIndex, setSliceTenorIndex] = useState(0);
  const [sliceStrike, setSliceStrike] = useState(0);
  const [probeExpiryIdx, setProbeExpiryIdx] = useState(0);
  const [probeTenorIdx, setProbeTenorIdx] = useState(0);
  const [sliceMode, setSliceMode] = useState<SliceMode>('Smile');
  const [lockSliceRangeToGlobal, setLockSliceRangeToGlobal] = useState(false);
  // Default ON so the term-structure plot renders for the shipped SABR
  // example out of the box. Without trim, sampling at the rightmost (expiry,
  // tenor) combos can roll past the curve's max time and the sampler returns
  // QuantLib's "1st leg: time (...) is past max curve time (...)" error.
  // Users can still turn it off via the checkbox to see the raw expiries.
  const [autoTrimHorizon, setAutoTrimHorizon] = useState(true);
  const [showAdvancedSlice, setShowAdvancedSlice] = useState(false);
  // Two-mode workbench layout: 'edit' shows the surface inputs + sampling
  // configuration full-width; 'sampler' shows the result tabs full-width.
  // Replaces the prior side-by-side 4/8 grid which left both Matrix2DEditor
  // and the heatmap cramped on typical 1440px screens. Auto-flips to
  // 'sampler' after a user-initiated Sample succeeds (see runSample's
  // autoFlipOnSuccess opt). Auto-run / cache rehits do NOT flip — they may
  // fire while the user is still editing.
  const [viewMode, setViewMode] = useState<'edit' | 'sampler'>('edit');
  // Wall-clock of the last successful sample (cache hit or live response).
  // Drives the "Sampled Xs ago" status pill in the header so the user can
  // tell, while in Edit mode, whether the cached Sampler view is fresh.
  const [lastSampleAt, setLastSampleAt] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>('heatmap');
  // Forces the "Sampled Xs ago" pill to re-render every 5s so the relative
  // time stays roughly accurate without per-second churn. The interval is
  // only mounted while a sample exists.
  const [, setSampleAgoTick] = useState(0);
  useEffect(() => {
    if (lastSampleAt === null) return;
    const id = setInterval(() => setSampleAgoTick(x => x + 1), 5000);
    return () => clearInterval(id);
  }, [lastSampleAt]);
  const [selectedStrikeIdx, setSelectedStrikeIdx] = useState(0);
  const [showSmallMultiples, setShowSmallMultiples] = useState(false);
  const [displayUnits, setDisplayUnits] = useState<DisplayUnits>('bps');
  const [autoRun, setAutoRun] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);
  // Correlation id of the most recent /v1/vol-surfaces/sample
  // call, held so the Sampler can offer the same "View trace" doorway into
  // /investigate that the product pricing pages do. Set on BOTH the success
  // and error branches of runSample (a failed sample is exactly when someone
  // wants the pipeline logs); cleared at the start of each run.
  const [requestId, setRequestId] = useState<string | undefined>();
  const [cacheHit, setCacheHit] = useState(false);
  const [useCache, setUseCache] = useState(false);
  const [result, setResult] = useState<SampleVolSurfacesResponse | null>(null);
  const [lastRequest, setLastRequest] = useState<any>(null);
  const [baseline, setBaseline] = useState<VolSurfaceSampleResult | null>(null);
  const [requestNote, setRequestNote] = useState<string | null>(null);
  const [showSurfaceList, setShowSurfaceList] = useState(true);
  const [strikeGridTemplate, setStrikeGridTemplate] = useState<'custom' | 'normal' | 'lognormal'>('custom');
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const surfaces = getVolSurfaces();
    setVolSurfaces(surfaces);
    setCurveSets(getSavedCurveSets());
    if (surfaces.length > 0) {
      setVolId(surfaces[0].id);
    }
    indexStore.getAll().then(setAllIndices).catch(() => setAllIndices([]));
  }, []);

  useEffect(() => {
    if (!showSurfaceList) {
      // Pick up surface edits/imports without full page reload.
      setVolSurfaces(getVolSurfaces());
    }
  }, [showSurfaceList]);

  useEffect(() => {
    if (curveSets.length === 0) {
      if (selectedCurveSetId) setSelectedCurveSetId('');
      return;
    }
    if (!selectedCurveSetId || !curveSets.some(cs => cs.id === selectedCurveSetId)) {
      setSelectedCurveSetId(curveSets[0].id);
    }
  }, [curveSets, selectedCurveSetId]);

  const curveSet = useMemo(() => curveSets.find(cs => cs.id === selectedCurveSetId) || null, [curveSets, selectedCurveSetId]);
  const resolvedCurves = useMemo(() => (curveSet ? resolveCurveSetCurves(curveSet) : []), [curveSet]);
  const availableCurveIds = useMemo(() => resolvedCurves.map(c => c.id), [resolvedCurves]);
  const discountCurveCandidates = useMemo(() => {
    if (!curveSet) return [];
    const discount = resolvedCurves.filter(c => c.role === 'discount').map(c => c.id);
    return discount.length ? discount : availableCurveIds;
  }, [curveSet, resolvedCurves, availableCurveIds]);
  const forwardCurveCandidates = useMemo(() => {
    if (!curveSet) return [];
    const forward = resolvedCurves.filter(c => c.role === 'forward').map(c => c.id);
    return forward.length ? forward : availableCurveIds;
  }, [curveSet, resolvedCurves, availableCurveIds]);
  const strikes = useMemo(
    () => strikeGrid.filter(v => Number.isFinite(v)).sort((a, b) => a - b),
    [strikeGrid]
  );
  const strikeGridError = useMemo(() => {
    if (strikeGrid.length === 0) return 'Provide at least one strike.';
    const finite = strikeGrid.filter(v => Number.isFinite(v));
    if (finite.length !== strikeGrid.length) return 'Strike grid has non-finite values.';
    if (surfaceType === 'EquityBlack' && finite.some(v => v <= 0)) return 'Equity strike grid must be strictly positive.';
    if (new Set(finite.map(v => v.toFixed(12))).size !== finite.length) return 'Strike grid has duplicate values.';
    const sorted = [...finite].sort((a, b) => a - b);
    if (!isStrictlyIncreasing(sorted)) return 'Strike grid must be strictly increasing.';
    return null;
  }, [strikeGrid, surfaceType]);
  // Only RATE indices can serve as a swap's floating leg. Inflation indices
  // (e.g. EUHICP_YY) are NOT valid float legs — the engine returns
  // "Unknown index id" for them and yields an empty surface. Restrict the
  // options (and thus the auto-select fallback below) to IBOR + Overnight.
  const floatIndexOptions = useMemo(() => rateFloatIndexOptions(allIndices), [allIndices]);
  const swapIndexOptions = useMemo(() => {
    const options = new Set<string>();
    for (const idx of allIndices) {
      options.add(swapIndexIdForIndex(idx));
    }
    if (swapIndexId) options.add(swapIndexId);
    return Array.from(options).sort();
  }, [allIndices, swapIndexId]);
  const estimatedPoints = useMemo(() => {
    const surfaceForEstimate = volSurfaces.find(v => v.id === volId) || null;
    const nE = expiryGridType === 'TenorGrid' ? expiryTenors.length : 16;
    const nT = surfaceType === 'Swaption' ? Math.max(1, tenorGrid.length) : 1;
    const nS = Math.max(1, (surfaceType === 'Swaption' && surfaceForEstimate?.strikes?.length ? surfaceForEstimate.strikes.length : strikes.length));
    if (outputMode === 'Cube') return nE * nT * nS;
    if (outputMode === 'SmileSlice') return nS;
    if (outputMode === 'TermSlice') return nT;
    return nE;
  }, [expiryGridType, expiryTenors.length, surfaceType, tenorGrid.length, strikes.length, volSurfaces, volId, outputMode]);

  const surfaceMatchesType = (surface: VolSurfaceSpec, type: SurfaceType) => {
    if (type === 'Swaption') return (surface as any).payload_type === 'SwaptionVolSpec';
    if (type === 'Optionlet') return (surface as any).payload_type === 'OptionletVolSpec';
    return (surface as any).payload_type === 'BlackVolSpec';
  };
  const filteredVolSurfaces = useMemo(
    () => volSurfaces.filter(v => surfaceMatchesType(v, surfaceType)),
    [volSurfaces, surfaceType]
  );
  const selectedSurface = useMemo(() => volSurfaces.find(v => v.id === volId) || null, [volSurfaces, volId]);
  const updateSelectedSurface = (updater: (surface: VolSurfaceSpec) => VolSurfaceSpec) => {
    if (!selectedSurface) return;
    const updated = updater({
      ...selectedSurface,
      updatedAt: new Date().toISOString(),
    });
    // Optimistic UI; the backend PATCH runs behind it (last write wins).
    setVolSurfaces(prev => prev.map(s => (s.id === updated.id ? updated : s)));
    void saveVolSurface(updated)
      .then(() => setVolSurfaces(getVolSurfaces()))
      .catch(err => setError(err instanceof Error ? err.message : 'Failed to save surface'));
  };
  const quoteBookEntries = useMemo(() => getQuoteBook(), []);
  const flatQuotes = useMemo(() => getLegacyFlatQuotes(), []);
  const quoteIdOptions = useMemo(
    () => Array.from(new Set([...quoteBookEntries.map(q => q.id), ...flatQuotes.map(q => q.id)])).sort(),
    [quoteBookEntries, flatQuotes]
  );
  const getEquityMatrixDims = (surface: VolSurfaceSpec) => {
    const nRows = surface.price_expiries?.length || surface.surface_prices?.n_rows || 0;
    const nCols = surface.price_strikes?.length || surface.surface_prices?.n_cols || 0;
    return { nRows, nCols };
  };
  const resizeEquityMatrix = (surface: VolSurfaceSpec, nRows: number, nCols: number): VolSurfaceSpec => {
    const prevCols = surface.surface_prices?.n_cols || 0;
    const prevValues = surface.surface_prices?.values || [];
    const prevQuoteIds = surface.surface_price_quote_ids || [];
    const values: number[] = [];
    const quoteIds: string[] = [];
    for (let i = 0; i < nRows; i += 1) {
      for (let j = 0; j < nCols; j += 1) {
        values.push(prevValues[i * prevCols + j] ?? 0);
        quoteIds.push(prevQuoteIds[i * prevCols + j] || '');
      }
    }
    return {
      ...surface,
      surface_prices: { n_rows: nRows, n_cols: nCols, values },
      surface_price_quote_ids: quoteIds,
    };
  };
  const setEquityMatrixCellQuote = (surface: VolSurfaceSpec, row: number, col: number, quoteId: string): VolSurfaceSpec => {
    const { nRows, nCols } = getEquityMatrixDims(surface);
    const safe = resizeEquityMatrix(surface, nRows, nCols);
    const idx = row * nCols + col;
    const nextQuoteIds = [...(safe.surface_price_quote_ids || Array.from({ length: nRows * nCols }, () => ''))];
    nextQuoteIds[idx] = quoteId;
    return {
      ...safe,
      surface_price_quote_ids: nextQuoteIds,
    };
  };
  useEffect(() => {
    if (filteredVolSurfaces.length === 0) return;
    if (!filteredVolSurfaces.some(v => v.id === volId)) {
      setVolId(filteredVolSurfaces[0].id);
    }
  }, [filteredVolSurfaces, volId]);
  const sample = result?.results?.[0] || null;
  const spreadAxisSupported = false;
  const resultErrors = useMemo(
    () => (result?.results || []).map(r => r.error?.error_message).filter((x): x is string => !!x),
    [result]
  );

  useEffect(() => {
    setDisplayUnits(prev => {
      if (surfaceType === 'EquityBlack') {
        return prev === 'bps' ? 'pct' : prev;
      }
      return prev === 'pct' ? 'bps' : prev;
    });
  }, [surfaceType]);
  const derivedCurveHorizonYears = useMemo(() => {
    if (!curveSet) return null;
    const years: number[] = [];
    for (const c of resolvedCurves) {
      for (const pt of c.points || []) {
        const p: any = (pt as any)?.point || {};
        if (p.tenor && typeof p.tenor.n === 'number' && p.tenor.unit) {
          years.push(periodToYears({ n: p.tenor.n, unit: p.tenor.unit as TimeUnit }));
          continue;
        }
        if (typeof p.tenor_number === 'number' && p.tenor_time_unit) {
          years.push(periodToYears({ n: p.tenor_number, unit: p.tenor_time_unit as TimeUnit }));
        }
      }
    }
    if (years.length === 0) return null;
    return Math.max(...years);
  }, [curveSet, resolvedCurves]);

  const inferSwaptionKindFromSurface = (surface: VolSurfaceSpec): SwaptionSurfaceKind => {
    const shape = surface.base?.shape;
    if (shape === 'SabrParams') return 'SabrParams';
    if (shape === 'SabrCalibrate') return 'SabrCalibrate';
    if (shape === 'SmileCube3D') return 'SmileCube';
    if (shape === 'AtmMatrix2D') return 'AtmMatrix';
    return 'Constant';
  };

  // Convert a possibly-heterogeneous MatrixCell grid (or undefined) to a dense
  // (nRows × nCols) grid for SABR/AtmMatrix editors; missing cells default to
  // `fallback` (NaN literal by default, 0.5 for β).
  const seedMatrix = (
    src: MatrixCell[][] | undefined,
    nRows: number,
    nCols: number,
    fallback: number,
  ): Matrix2D => {
    const out: Matrix2D = [];
    for (let i = 0; i < nRows; i += 1) {
      const row: MatrixCell[] = [];
      for (let j = 0; j < nCols; j += 1) {
        const existing = src?.[i]?.[j];
        if (existing === undefined) {
          row.push(Number.isFinite(fallback) ? (fallback as MatrixCell) : (NaN as MatrixCell));
        } else {
          row.push(existing);
        }
      }
      out.push(row);
    }
    return out;
  };

  // Apply a Swaption kind change to the surface: update `base.shape`, seed
  // surface axes if missing, and seed any SABR matrices that don't yet exist
  // so the editor renders cells without forcing the user to also resize. We
  // intentionally do NOT overwrite existing data, so flipping between kinds
  // and back keeps user edits intact. updatedAt is bumped via the shared
  // `updateSelectedSurface` so dirty-state tracking picks the change up.
  const applyKindChangeToSurface = (kind: SwaptionSurfaceKind) => {
    if (!selectedSurface) return;
    updateSelectedSurface((s) => {
      const next: VolSurfaceSpec = { ...s, base: { ...s.base, shape: shapeForKind(kind) } };
      // Both SabrParams and SabrCalibrate are lognormal-only in v1 — the
      // backend rejects Normal vol type (parser/vol_surface_parsers.cpp).
      // Coerce automatically on transition INTO SABR from a Normal surface
      // so users can't accidentally save an invalid state from the editor.
      // We touch only the Normal case; ShiftedLognormal is left alone.
      if ((kind === 'SabrParams' || kind === 'SabrCalibrate') && next.base.volatility_type === 'Normal') {
        next.base = { ...next.base, volatility_type: 'Lognormal' };
      }
      // SmileCube also needs axes — the wire payload masks SmileCube3D as
      // AtmMatrix2D and ships `surface.grid` indexed by axes_expiries ×
      // axes_tenors (see volSurfacePayload.ts). Without seeded axes the
      // Matrix2DEditor would render an empty grid for legacy SmileCube
      // surfaces that only carry `expiries/tenors: number[]`.
      const needsAxes = kind === 'AtmMatrix' || kind === 'SabrParams' || kind === 'SmileCube' || kind === 'SabrCalibrate';
      if (needsAxes) {
        // Legacy `expiries: number[]` is years and may be fractional (e.g.
        // 1/12 for 1 month). yearsToPeriod emits the smallest integer-`n`
        // Period that represents the value exactly, matching the
        // quantra_Period.n:int OpenAPI contract.
        if (!next.axes_expiries || next.axes_expiries.length === 0) {
          next.axes_expiries = next.expiries && next.expiries.length > 0
            ? next.expiries.map(yearsToPeriod)
            : STANDARD_EXPIRIES.slice();
        }
        if (!next.axes_tenors || next.axes_tenors.length === 0) {
          next.axes_tenors = next.tenors && next.tenors.length > 0
            ? next.tenors.map(yearsToPeriod)
            : STANDARD_TENORS.slice();
        }
      }
      if (kind === 'SabrParams') {
        const nE = next.axes_expiries?.length || 0;
        const nT = next.axes_tenors?.length || 0;
        for (const pname of SABR_PARAM_NAMES) {
          const field = SABR_PARAM_FIELDS[pname];
          const current = next[field] as MatrixCell[][] | undefined;
          if (!current || current.length === 0) {
            next[field] = seedMatrix(undefined, nE, nT, SABR_PARAM_DEFAULTS[pname]) as never;
          }
        }
      }
      if (kind === 'SabrCalibrate') {
        // Seed a default strike axis if missing. The default of ±100bp / ±50bp
        // / ATM matches typical broker swaption-cube quoting conventions and
        // is what the backend test fixtures use.
        if (!next.axes_strikes || next.axes_strikes.length === 0) {
          next.axes_strikes = DEFAULT_SABR_CALIBRATE_STRIKES.slice();
        }
        // Seed an empty market-vols cube sized (nE × nT × nS) of NaN literals.
        // The Matrix2DEditor renders NaN as an empty input the user fills in.
        const nE = next.axes_expiries?.length || 0;
        const nT = next.axes_tenors?.length || 0;
        const nS = next.axes_strikes.length;
        const existing = next.sabr_market_vols;
        if (!existing || existing.length === 0) {
          const cube: MatrixCell[][][] = [];
          for (let i = 0; i < nE; i += 1) {
            const eRow: MatrixCell[][] = [];
            for (let j = 0; j < nT; j += 1) {
              const tRow: MatrixCell[] = [];
              for (let k = 0; k < nS; k += 1) tRow.push(NaN as MatrixCell);
              eRow.push(tRow);
            }
            cube.push(eRow);
          }
          next.sabr_market_vols = cube;
        }
        // Seed a sensible default options block: beta_fixed=true / beta=0.5 is
        // the textbook anchoring choice for swaption SABR (lets the
        // calibrator move only α/ρ/ν, which is well-posed for sparse data).
        if (!next.sabr_calibration_options) {
          next.sabr_calibration_options = { beta_fixed: true, beta_value: 0.5, vega_weighted_smile_fit: false };
        }
      }
      return next;
    });
  };

  const inferSurfaceTypeFromSurface = (surface: VolSurfaceSpec): SurfaceType => {
    if ((surface as any).payload_type === 'BlackVolSpec') return 'EquityBlack';
    if ((surface as any).payload_type === 'OptionletVolSpec') return 'Optionlet';
    return 'Swaption';
  };

  const openSurface = (surface: VolSurfaceSpec) => {
    setVolId(surface.id);
    const inferredType = inferSurfaceTypeFromSurface(surface);
    setSurfaceType(inferredType);
    if (inferredType === 'Swaption') {
      const inferredKind = inferSwaptionKindFromSurface(surface);
      setSwaptionKind(inferredKind);
      // Backfill `axes_expiries`/`axes_tenors` from the legacy
      // `expiries`/`tenors: number[]` (years) for editable kinds whose
      // saved surface predates the period-axes field. Without this the
      // Matrix2DEditor mounted by Step 1.5 renders its empty-state for
      // legacy surfaces like `swaptvol_dense`. Pure helper lives in
      // vol-workbench-helpers.ts; tested in vol-workbench-helpers.test.ts.
      if (inferredKind !== 'Constant') {
        const seeded = backfillSurfaceAxesFromLegacy(surface, inferredKind as EditableSwaptionKind);
        if (seeded) {
          void saveVolSurface(seeded)
            .then(() => setVolSurfaces(getVolSurfaces()))
            .catch(err => setError(err instanceof Error ? err.message : 'Failed to save surface'));
        }
      }
    }
    if (inferredType === 'EquityBlack') {
      setStrikeAxis('AbsoluteStrike');
      setOutputMode('Cube');
      const preferredStrikes = normalizeStrikeGrid(
        (surface.price_strikes && surface.price_strikes.length > 0)
          ? surface.price_strikes
          : ((surface.strikes && surface.strikes.length > 0) ? surface.strikes : [80, 90, 100, 110, 120])
      );
      setStrikeGrid(preferredStrikes);
    }
    setShowSurfaceList(false);
    setError(null);
    setRequestNote(null);
    setResult(null);
    setBaseline(null);
    setLastRequest(null);
    setLatencyMs(null);
    setCacheHit(false);
    setLastSampleAt(null);
    setViewMode('edit');
  };

  const handleDeleteSurface = async (surface: VolSurfaceSpec) => {
    if (!confirm(`Delete "${surface.id}"? This cannot be undone.`)) return;
    try {
      await deleteVolSurface(surface.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete surface');
    }
    const next = getVolSurfaces();
    setVolSurfaces(next);
    if (volId === surface.id) {
      setVolId(next[0]?.id || '');
    }
  };

  const handleDuplicateSurface = async (surface: VolSurfaceSpec) => {
    const now = new Date().toISOString();
    try {
      await saveVolSurface({
        ...JSON.parse(JSON.stringify(surface)),
        id: `${surface.id}_copy_${Date.now()}`,
        createdAt: now,
        updatedAt: now,
      } as VolSurfaceSpec);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to duplicate surface');
    }
    setVolSurfaces(getVolSurfaces());
  };

  const handleExportAllSurfaces = () => {
    if (volSurfaces.length === 0) return;
    exportVolSurfaces(volSurfaces);
  };

  const handleImportSurfaces = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      await importVolSurfaces(file);
      setVolSurfaces(getVolSurfaces());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import surfaces');
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleCreateSurface = async (nextType?: SurfaceType) => {
    const targetType = nextType || surfaceType;
    const id = `vol_${Date.now()}`;
    await saveVolSurface({
      id,
      payload_type: targetType === 'EquityBlack' ? 'BlackVolSpec' : 'SwaptionVolSpec',
      base: {
        reference_date: asOfDate,
        calendar: targetType === 'EquityBlack' ? 'NullCalendar' : 'TARGET',
        business_day_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
        ...(targetType === 'EquityBlack' ? {} : { volatility_type: 'Normal' }),
        shape: 'Constant',
        constant_vol: targetType === 'EquityBlack' ? 0.2 : 0.01,
      },
      expiries: [],
      tenors: targetType === 'EquityBlack' ? [] : [],
      strikes: targetType === 'EquityBlack' ? [80, 100, 120] : [],
      grid: [],
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    } as VolSurfaceSpec);
    setVolSurfaces(getVolSurfaces());
    setVolId(id);
    setSurfaceType(targetType);
    setShowSurfaceList(false);
    setError(null);
    setRequestNote(null);
    setResult(null);
    setBaseline(null);
    setLastRequest(null);
    setLatencyMs(null);
    setCacheHit(false);
    setLastSampleAt(null);
    setViewMode('edit');
  };

  useEffect(() => {
    if (!selectedSurface) return;
    if (surfaceType === 'Swaption') {
      const inferred = inferSwaptionKindFromSurface(selectedSurface);
      if (swaptionKind !== inferred) {
        setSwaptionKind(inferred);
      }
    }
    if (strikeAxis !== 'AbsoluteStrike' || surfaceType === 'EquityBlack') {
      setStrikeAxis('AbsoluteStrike');
      if (surfaceType !== 'EquityBlack') {
        setRequestNote('SpreadFromATM is temporarily disabled in sampler mode; using AbsoluteStrike for compatibility.');
      }
    }
    if (surfaceType === 'EquityBlack' && outputMode !== 'Cube') {
      setOutputMode('Cube');
    }
    if (surfaceType === 'EquityBlack' && sliceMode === 'Expiry') {
      setSliceMode('Term');
    }
  }, [selectedSurface?.id, surfaceType, outputMode, strikeAxis, swaptionKind, sliceMode]);

  useEffect(() => {
    if (surfaceType === 'EquityBlack') {
      const preferredStrikes = normalizeStrikeGrid(
        (selectedSurface?.price_strikes && selectedSurface.price_strikes.length > 0)
          ? selectedSurface.price_strikes
          : ((selectedSurface?.strikes && selectedSurface.strikes.length > 0)
            ? selectedSurface.strikes
            : [80, 90, 100, 110, 120])
      );
      setStrikeGrid(preferredStrikes);
      if (samplingPreset === 'Fast' || samplingPreset === 'Standard' || samplingPreset === 'HQ') {
        setMaxPoints(samplingPreset === 'Fast' ? 1500 : samplingPreset === 'HQ' ? 30000 : 10000);
      }
      return;
    }
    if (samplingPreset === 'Fast') {
      setExpiryTenors(FAST_EXPIRIES);
      setTenorGrid(FAST_TENORS);
      setStrikeGrid(normalizeStrikeGrid(FAST_STRIKES));
      setMaxPoints(1500);
      return;
    }
    if (samplingPreset === 'HQ') {
      setExpiryTenors(HQ_EXPIRIES);
      setTenorGrid(HQ_TENORS);
      setStrikeGrid(normalizeStrikeGrid(HQ_STRIKES));
      setMaxPoints(30000);
      return;
    }
    setExpiryTenors(STANDARD_EXPIRIES);
    setTenorGrid(STANDARD_TENORS);
    setStrikeGrid(normalizeStrikeGrid(STANDARD_STRIKES));
    setMaxPoints(10000);
  }, [samplingPreset, surfaceType, selectedSurface?.id]);

  useEffect(() => {
    if (!curveSet) return;
    if (!discountingCurveId || !availableCurveIds.includes(discountingCurveId)) {
      setDiscountingCurveId(discountCurveCandidates[0] || '');
    }
    if (!forwardingCurveId || !availableCurveIds.includes(forwardingCurveId)) {
      setForwardingCurveId(forwardCurveCandidates[0] || discountCurveCandidates[0] || '');
    }
  }, [
    curveSet,
    discountingCurveId,
    forwardingCurveId,
    availableCurveIds,
    discountCurveCandidates,
    forwardCurveCandidates,
  ]);

  useEffect(() => {
    if (floatIndexOptions.length === 0) return;
    // Auto-switch to a non-OIS float index when the active surface is
    // SabrCalibrate. The backend rejects OIS underlyings for SabrCalibrate
    // in v1 and our pre-flight guard would otherwise fire on the first
    // auto-sample after opening such a surface — landing the user in an
    // error state before they've touched the dropdown. Prefer the first
    // IBOR-family index from the registry; users can still override.
    const surfaceIsSabrCalibrate = selectedSurface?.base?.shape === 'SabrCalibrate';
    const currentIdx = allIndices.find((i) => i.id === floatIndexId);
    const needsSwitchForOis = surfaceIsSabrCalibrate && currentIdx?.type === 'Overnight';
    if (!floatIndexOptions.includes(floatIndexId) || needsSwitchForOis) {
      // floatIndexOptions is already restricted to RATE indices (IBOR +
      // Overnight), so any pick here is a valid swap float leg — never an
      // inflation index. The float leg must also be COHERENT with the active
      // swap index (EUR_SWAP_6M ⇒ EURIBOR_6M, not the first IBOR EURIBOR_1M,
      // which has no forwarding curve → "Unknown index id" → empty surface);
      // pickDefaultFloatIndex matches on swapIndexId first, then falls back to
      // prefer-IBOR-then-Overnight. This also satisfies SabrCalibrate's
      // OIS-avoidance: it only lands on Overnight when no IBOR exists.
      const fallback = pickDefaultFloatIndex(allIndices, floatIndexOptions, swapIndexId);
      if (fallback && fallback !== floatIndexId) {
        setFloatIndexId(fallback);
      }
    }
  }, [floatIndexOptions, floatIndexId, allIndices, selectedSurface?.base?.shape, swapIndexId]);

  useEffect(() => {
    if (swapIndexOptions.length > 0 && !swapIndexOptions.includes(swapIndexId)) {
      setSwapIndexId(swapIndexOptions[0]);
    }
  }, [swapIndexOptions, swapIndexId]);

  useEffect(() => {
    if (!spreadAxisSupported && strikeAxis === 'SpreadFromATM') {
      setStrikeAxis('AbsoluteStrike');
      setRequestNote('Switched strike axis to AbsoluteStrike: SpreadFromATM is supported only for Swaption SmileCube.');
    }
  }, [spreadAxisSupported, strikeAxis]);

  const buildRequest = async (overrides?: Partial<{ outputMode: OutputMode; sliceExpiryIndex: number; sliceTenorIndex: number; sliceStrike: number }>) => {
    if (!selectedSurface) throw new Error('Select a stored vol surface ID');
    if (!surfaceMatchesType(selectedSurface, surfaceType)) throw new Error('Selected surface is incompatible with selected surface type');
    if (strikeGridError) throw new Error(strikeGridError);
    if (strikes.length === 0) throw new Error('Provide at least one strike');
    if (estimatedPoints > maxPoints) throw new Error('Estimated points exceed max_points guardrail');

    const sourceStrikes = surfaceType === 'EquityBlack'
      ? ((selectedSurface.price_strikes && selectedSurface.price_strikes.length > 0)
        ? selectedSurface.price_strikes
        : ((selectedSurface.strikes && selectedSurface.strikes.length > 0) ? selectedSurface.strikes : strikes))
      : ((surfaceType === 'Swaption' && selectedSurface.strikes && selectedSurface.strikes.length > 0)
        ? selectedSurface.strikes
        : strikes);
    const sortedStrikes = [...sourceStrikes].sort((a, b) => a - b);
    for (let i = 1; i < sortedStrikes.length; i += 1) {
      if (!(sortedStrikes[i] > sortedStrikes[i - 1])) {
        throw new Error('Strike grid must be strictly increasing');
      }
    }
    if (surfaceType === 'EquityBlack' && sortedStrikes.some(k => !(k > 0))) {
      throw new Error('EquityBlack requires strictly positive absolute strikes.');
    }

    const inferredSwaptionKind = surfaceType === 'Swaption' ? inferSwaptionKindFromSurface(selectedSurface) : 'Constant';
    const effectiveSwaptionKind: SwaptionSurfaceKind = surfaceType === 'Swaption'
      ? (inferredSwaptionKind === 'SmileCube' ? 'SmileCube' : swaptionKind)
      : 'Constant';
    const effectiveStrikeAxis: StrikeAxis = strikeAxis === 'SpreadFromATM' ? 'AbsoluteStrike' : strikeAxis;
    // SpreadFromATM is temporarily disabled in sampler mode for backend compatibility.

    // Guard against curve horizon boundary errors (e.g. time past max curve
    // time). For swaption sampling, keep expiry+tenor within a conservative
    // cap. SABR-params surfaces always go through the swap-rate forward
    // resolution path on the backend, so the guard must apply to every
    // output mode for SABR — not just Cube — otherwise a TermSlice or
    // single-point query trips QuantLib's "1st leg: time (X) is past max
    // curve time (Y)" error.
    //
    // The 0.5Y safety margin trims a half-year off the derived horizon so
    // calendar-rolled swap ends (settlement-days + business-day adjustment)
    // don't roll past the curve's interpolation domain. Without it, a
    // (10Y expiry × 20Y tenor) sample against a curve whose helpers max out
    // at 30Y rolls to ~30.29Y and trips the same QuantLib error.
    const HORIZON_SAFETY_MARGIN_YEARS = 0.5;
    const maxTenorYears = Math.max(...tenorGrid.map(periodToYears), 0);
    const horizonRaw = derivedCurveHorizonYears ?? 29.5;
    const horizonCapYears = Math.max(0, horizonRaw - HORIZON_SAFETY_MARGIN_YEARS);
    const horizonSource = derivedCurveHorizonYears !== null ? 'curve-derived' : 'fallback';
    const maxExpiryYearsAllowed = horizonCapYears - maxTenorYears;
    // Both SabrParams and SabrCalibrate go through the same forward-resolution
    // path on the backend (per-node SabrSmileSection or
    // XabrSwaptionVolatilityCube instantiation in
    // finalizeSwaptionVolEntryForPricing), so the horizon guard applies to
    // both kinds in every output mode — not just Cube. SmileSlice / TermSlice
    // queries against a SABR surface still walk the full surface-axes grid
    // before sampling, and trip QuantLib's "1st leg: time (X) is past max
    // curve time (Y)" the moment any (axes_expiry × axes_tenor) node walks
    // past the curve's max time.
    const isSabrParamsSurface =
      surfaceType === 'Swaption' && selectedSurface.base?.shape === 'SabrParams';
    const isSabrCalibrateSurface =
      surfaceType === 'Swaption' && selectedSurface.base?.shape === 'SabrCalibrate';
    const isSabrSurface = isSabrParamsSurface || isSabrCalibrateSurface;
    const shouldGuardHorizon =
      surfaceType === 'Swaption' && (outputMode === 'Cube' || isSabrSurface);
    const droppedExpiries = shouldGuardHorizon ? expiryTenors.filter(p => periodToYears(p) > maxExpiryYearsAllowed) : [];
    const keptExpiries = shouldGuardHorizon ? expiryTenors.filter(p => periodToYears(p) <= maxExpiryYearsAllowed) : expiryTenors;
    const expiryExceedsHorizon = droppedExpiries.length > 0;
    const effectiveExpiryTenors = (shouldGuardHorizon && autoTrimHorizon)
      ? keptExpiries
      : expiryTenors;
    if (shouldGuardHorizon && autoTrimHorizon && droppedExpiries.length > 0) {
      const droppedLabels = droppedExpiries.map(periodLabel).join(', ');
      setRequestNote(
        `Auto-trim (${horizonSource} horizon ${horizonCapYears.toFixed(2)}Y): max tenor=${maxTenorYears.toFixed(2)}Y, max expiry=${maxExpiryYearsAllowed.toFixed(2)}Y, dropped ${droppedExpiries.length} (${droppedLabels}).`
      );
    } else if (expiryExceedsHorizon) {
      setRequestNote(
        `Curve horizon exceeded (${horizonSource} horizon ${horizonCapYears.toFixed(2)}Y): max tenor=${maxTenorYears.toFixed(2)}Y, max expiry=${maxExpiryYearsAllowed.toFixed(2)}Y. ` +
        `Reduce tenor grid, reduce expiries, or enable auto-trim.`
      );
    } else {
      setRequestNote(null);
    }
    if (shouldGuardHorizon && autoTrimHorizon && effectiveExpiryTenors.length === 0) {
      throw new Error('All expiries exceed safe curve horizon for selected tenor grid. Reduce max tenor or expiries.');
    }

    // SABR surfaces (Params AND Calibrate) instantiate one
    // SabrSmileSection (Params) or one full XabrSwaptionVolatilityCube
    // (Calibrate) per (axes_expiry × axes_tenor) node BEFORE sampling runs,
    // inside finalizeSwaptionVolEntryForPricing on the backend. The
    // sampler then trips QuantLib's "1st leg: time (X) is past max curve
    // time (Y)" the moment any of those nodes walks past the curve's max
    // time — even if the sampling grid itself is conservative. The
    // sampling-grid horizon guard above doesn't help here because it only
    // trims the sampling tenor_grid, not the surface's own axes. Check
    // axes against the same conservative cap and throw early so users get
    // a clear, actionable message instead of the QuantLib internal error.
    if (isSabrSurface) {
      const axesExpiries = selectedSurface.axes_expiries || [];
      const axesTenors = selectedSurface.axes_tenors || [];
      if (axesExpiries.length > 0 && axesTenors.length > 0) {
        let worstExpiry = axesExpiries[0];
        let worstTenor = axesTenors[0];
        let worstYears = -Infinity;
        for (const e of axesExpiries) {
          const ey = periodToYears(e);
          for (const t of axesTenors) {
            const total = ey + periodToYears(t);
            if (total > worstYears) {
              worstYears = total;
              worstExpiry = e;
              worstTenor = t;
            }
          }
        }
        if (worstYears > horizonCapYears) {
          throw new Error(
            `SABR surface "${selectedSurface.id}" has a ${isSabrCalibrateSurface ? 'calibrate-cube' : 'calibration'} node (` +
            `${periodLabel(worstExpiry)} expiry × ${periodLabel(worstTenor)} tenor = ${worstYears.toFixed(2)}Y) ` +
            `that walks past the ${horizonSource} curve horizon (${horizonCapYears.toFixed(2)}Y, ` +
            `safety margin ${HORIZON_SAFETY_MARGIN_YEARS}Y below the curve's max time). ` +
            `Trim the surface's axes_expiries/axes_tenors or use a curve set that bootstraps further out.`
          );
        }
      }
    }

    // Filter to yield-rate curves only. Inflation curves carry their bootstrap
    // spec under `inflation_curve` and arrive with `points: []`, which trips
    // the backend's "Empty list of points for term structure" check inside
    // PricingRegistryBuilder.build() — that failure happens before the
    // per-query loop, so its error gets replicated into every query's
    // error_message and nothing plots. Also defensively drop any other curve
    // with empty/missing points (same failure mode).
    //
    // Inflation curves belong in `pricing.inflation.inflation_curves`; vol
    // surface sampling doesn't need them so we simply omit them here.
    const yieldRateCurves = resolvedCurves.filter(c =>
      c.role !== 'inflation' &&
      Array.isArray(c.points) &&
      c.points.length > 0
    );
    const droppedNonRateCurveIds = resolvedCurves
      .filter(c => !yieldRateCurves.includes(c))
      .map(c => c.id);
    const curves = yieldRateCurves.map(c => normalizeCurveForApi({
      id: c.id,
      day_counter: c.day_counter,
      interpolator: c.interpolator,
      bootstrap_trait: c.bootstrap_trait,
      reference_date: c.reference_date || asOfDate,
      points: c.points,
    }));
    if (curves.length === 0) {
      const droppedSuffix = droppedNonRateCurveIds.length
        ? ` Curve set "${curveSet?.name || curveSet?.id}" only contains non-yield or pointless curves (${droppedNonRateCurveIds.join(', ')}).`
        : '';
      throw new Error(`Curve Set is required and must contain at least one yield curve with points.${droppedSuffix}`);
    }
    if (droppedNonRateCurveIds.length > 0 && !requestNote) {
      setRequestNote(
        `Omitting non-rate curves from pricing.curves: ${droppedNonRateCurveIds.join(', ')}.`
      );
    }

    const indexIds = collectIndexRefIds(resolvedCurves.flatMap(c => c.points || []));
    if (surfaceType === 'Swaption') indexIds.push(floatIndexId);
    const uniqueIndexIds = Array.from(new Set(indexIds.filter(Boolean)));
    const storedIndices = await indexStore.getAll();
    const resolvedIndices: IndexDef[] = storedIndices
      .filter(i => uniqueIndexIds.includes(i.id))
      .map(i => storedToRateIndexDef(i))
      .filter((index): index is IndexDef => !!index);

    const idx = resolvedIndices.find(i => i.id === floatIndexId);
    const idxIsOvernight = idx?.index_type === 'Overnight';
    const swapIndexDef = surfaceType === 'Swaption' ? [{
      id: swapIndexId,
      kind: idxIsOvernight ? 'OisSwapIndex' : 'IborSwapIndex',
      spot_days: 2,
      calendar: idx?.calendar || 'TARGET',
      business_day_convention: idx?.business_day_convention || 'ModifiedFollowing',
      end_of_month: false,
      float_index_id: floatIndexId,
      fixed_leg: {
        fixed_frequency: 'Annual',
        fixed_day_counter: 'Thirty360',
        fixed_calendar: idx?.calendar || 'TARGET',
        fixed_bdc: idx?.business_day_convention || 'ModifiedFollowing',
        fixed_term_bdc: idx?.business_day_convention || 'ModifiedFollowing',
        fixed_date_rule: 'Forward',
        fixed_eom: false,
      },
      float_leg: {
        float_tenor: idxIsOvernight ? { n: 1, unit: 'Days' } : { n: idx?.tenor?.n || 6, unit: idx?.tenor?.unit || 'Months' },
        float_calendar: idx?.calendar || 'TARGET',
        float_bdc: idx?.business_day_convention || 'ModifiedFollowing',
        float_term_bdc: idx?.business_day_convention || 'ModifiedFollowing',
        float_date_rule: 'Forward',
        float_eom: false,
      },
    }] : undefined;

    // The backend's strict-mode check requires
    // `pricing.as_of_date == surface.base.reference_date`. The surface's
    // reference_date is its calibration date and is the only date the
    // surface's quotes/grids are guaranteed to be consistent with. Prefer
    // it for every surface type (EquityBlack already did this) and fall
    // back to the workbench's global as-of date only when the surface
    // does not carry one. Otherwise loading a stored example calibrated
    // on a past date and sampling it today produces:
    //   "Strict mode: pricing.as_of_date (today) must equal swaption vol
    //    referenceDate (calibration date)".
    const pricingAsOfDate = selectedSurface.base?.reference_date || asOfDate;

    const base = {
      reference_date: pricingAsOfDate,
      calendar: selectedSurface.base?.calendar || 'TARGET',
      business_day_convention: selectedSurface.base?.business_day_convention || 'ModifiedFollowing',
      day_counter: selectedSurface.base?.day_counter || 'Actual365Fixed',
      constant_vol: selectedSurface.base?.constant_vol ?? 0.01,
      ...(surfaceType === 'EquityBlack' ? {} : { volatility_type: selectedSurface.base?.volatility_type || 'Normal' }),
      ...(selectedSurface.base?.displacement !== undefined ? { displacement: selectedSurface.base.displacement } : {}),
    };

    const mode = getResolutionMode();
    const asOf = pricingAsOfDate;
    const bookById = new Map(getQuoteBook().map(q => [q.id, q]));
    const flatById = new Map(getLegacyFlatQuotes().map(q => [q.id, q]));
    const resolveQuoteValueById = (id?: string): number | null => {
      if (!id) return null;
      const b = bookById.get(id);
      if (b) {
        const val = resolveQuoteValue(b.series, asOf, mode);
        if (val !== null) return val;
      }
      const q = flatById.get(id);
      if (q && Number.isFinite(q.value)) return q.value;
      return null;
    };

    // Swaption surfaces route through the shared helper so the wire shape is
    // driven by `surface.base.shape` (not the dropdown), the surface owns its
    // own expiries/tenors (not the sampling grid), and SabrParams gets the
    // correct envelope. Optionlet and EquityBlack stay inline; the helper is
    // Swaption-scoped by name and Step 2 / Step 4 will revisit the others.
    let volSurfacePayload: any;
    if (surfaceType === 'Swaption') {
      try {
        volSurfacePayload = buildSwaptionVolWirePayload(
          selectedSurface,
          swapIndexId,
          pricingAsOfDate,
          resolveQuoteValueById,
        );
      } catch (err) {
        if (err instanceof VolSurfacePayloadError) throw err;
        throw new Error(err instanceof Error ? err.message : 'Failed to build swaption vol payload');
      }
    } else if (surfaceType === 'Optionlet') {
      volSurfacePayload = {
        id: selectedSurface.id,
        payload_type: 'OptionletVolSpec',
        payload: { base: { ...base, shape: 'Constant' } },
      };
    } else {
      volSurfacePayload = {
        id: selectedSurface.id,
        payload_type: 'BlackVolSpec',
        payload: (selectedSurface.base as any)?.shape === 'SurfaceFromPrices'
          ? {
            base: {
              reference_date: selectedSurface.base?.reference_date || asOfDate,
              calendar: selectedSurface.base?.calendar || 'NullCalendar',
              business_day_convention: selectedSurface.base?.business_day_convention || 'Following',
              day_counter: selectedSurface.base?.day_counter || 'Actual365Fixed',
              shape: 'SurfaceFromPrices',
            },
            ...(selectedSurface.surface_prices ? (() => {
              const nRows = selectedSurface.surface_prices?.n_rows || 0;
              const nCols = selectedSurface.surface_prices?.n_cols || 0;
              const qids = selectedSurface.surface_price_quote_ids || [];
              const vals = selectedSurface.surface_prices.values || [];
              const resolvedValues = Array.from({ length: nRows * nCols }, (_, idx) => {
                const qid = qids[idx];
                const qv = resolveQuoteValueById(qid);
                return qv ?? vals[idx] ?? 0;
              });
              return {
                surface_prices: {
                  n_rows: nRows,
                  n_cols: nCols,
                  values: resolvedValues,
                  ...(qids.some(Boolean) ? { quote_ids: qids } : {}),
                },
              };
            })() : {}),
            price_expiries: selectedSurface.price_expiries,
            price_strikes: selectedSurface.price_strikes,
            price_option_type: selectedSurface.price_option_type || 'Call',
            ...(selectedSurface.spot_quote_id
              ? { spot_quote_id: selectedSurface.spot_quote_id }
              : (selectedSurface.spot !== undefined ? { spot: selectedSurface.spot } : {})),
            ...(selectedSurface.use_flat_discount_rate
              ? { use_flat_discount_rate: true, flat_discount_rate: selectedSurface.flat_discount_rate ?? 0.02 }
              : (selectedSurface.discount_curve_id ? { discount_curve_id: selectedSurface.discount_curve_id } : {})),
            ...(selectedSurface.use_flat_dividend_rate
              ? { use_flat_dividend_rate: true, flat_dividend_rate: selectedSurface.flat_dividend_rate ?? 0 }
              : (selectedSurface.dividend_curve_id ? { dividend_curve_id: selectedSurface.dividend_curve_id } : {})),
            ...(selectedSurface.surface_interpolator ? { surface_interpolator: selectedSurface.surface_interpolator } : {}),
          }
          : {
            base: {
              reference_date: pricingAsOfDate,
              calendar: selectedSurface.base?.calendar || 'NullCalendar',
              business_day_convention: selectedSurface.base?.business_day_convention || 'Following',
              day_counter: selectedSurface.base?.day_counter || 'Actual365Fixed',
              shape: 'Constant',
              constant_vol: selectedSurface.base?.constant_vol ?? 0.2,
            },
          },
      };
    }

    if (surfaceType === 'Swaption' && effectiveSwaptionKind === 'SmileCube' && !requestNote) {
      setRequestNote('Stored surfaces are model specs; cube is generated by sampling. Embedded cube tensors are ignored for compatibility.');
    }

    const expiryGrid = expiryGridType === 'TenorGrid'
      ? {
        grid_type: 'TenorGrid',
        grid: {
          tenors: effectiveExpiryTenors,
          calendar: selectedSurface.base?.calendar || 'TARGET',
          business_day_convention: selectedSurface.base?.business_day_convention || 'ModifiedFollowing',
        },
      }
      : {
        grid_type: 'RangeGrid',
        grid: {
          start_date: pricingAsOfDate,
          end_date: rangeEndDate,
          step_number: rangeStepN,
          step_time_unit: rangeStepUnit,
          business_days_only: rangeBusinessOnly,
          calendar: selectedSurface.base?.calendar || 'TARGET',
          business_day_convention: selectedSurface.base?.business_day_convention || 'ModifiedFollowing',
        },
      };

    const effectiveMode = surfaceType === 'EquityBlack' ? 'Cube' : (overrides?.outputMode || outputMode);
    const effExpiryIdx = overrides?.sliceExpiryIndex ?? sliceExpiryIndex;
    const effTenorIdx = overrides?.sliceTenorIndex ?? sliceTenorIndex;
    const effStrike = overrides?.sliceStrike ?? sliceStrike;

    // SABR surfaces (Params AND Calibrate) need the sampler to resolve
    // F(expiry, tenor) before sampling so it can instantiate a per-node
    // SabrSmileSection (Params) or run XabrSwaptionVolatilityCube
    // (Calibrate) from the forward. The backend enforces this at
    // request/sample_vol_surfaces_request.cpp:432-438 and errors out with
    // "SpreadFromATM/SABR swaption sampling requires discounting_curve_id
    // and forwarding_curve_id" when they're missing. SpreadFromATM strike
    // axes would trigger the same path, but they're pinned to AbsoluteStrike
    // here so SABR is the only trigger we actually wire today; add an
    // `effectiveStrikeAxis === 'SpreadFromATM'` term if/when that pin is
    // lifted.
    const surfaceNeedsForwardResolution =
      surfaceType === 'Swaption' && (selectedSurface.base?.shape === 'SabrParams' || selectedSurface.base?.shape === 'SabrCalibrate');
    if (surfaceNeedsForwardResolution) {
      if (!discountingCurveId) {
        throw new Error('SABR sampling requires a Discounting Curve selection.');
      }
      if (!forwardingCurveId) {
        throw new Error('SABR sampling requires a Forwarding Curve selection.');
      }
      if (!availableCurveIds.includes(discountingCurveId)) {
        throw new Error(`Discounting curve "${discountingCurveId}" is not in the selected curve set.`);
      }
      if (!availableCurveIds.includes(forwardingCurveId)) {
        throw new Error(`Forwarding curve "${forwardingCurveId}" is not in the selected curve set.`);
      }
    }
    // Pre-flight OIS check for SabrCalibrate. The engine rejects OIS
    // underlyings for SabrCalibrate at parse time. Detect via the
    // underlying float index's overnight flag —
    // more accurate than the wire-helper's `_OIS` suffix sniff because the
    // user might have overridden the swap-index id.
    if (isSabrCalibrateSurface && idxIsOvernight) {
      throw new Error(
        `SabrCalibrate cannot calibrate against an Overnight (OIS) float index "${floatIndexId}". The backend rejects OIS underlyings for SabrCalibrate in v1; switch the Float Index to an IBOR-family index (e.g. EURIBOR_6M) or use a SabrParams surface.`
      );
    }

    const query: any = {
      vol_id: selectedSurface.id,
      surface_type: surfaceType,
      expiry_grid: expiryGrid,
      strike_grid: { axis: surfaceType === 'EquityBlack' ? 'AbsoluteStrike' : effectiveStrikeAxis, strikes: sortedStrikes },
      output_mode: effectiveMode,
      options: { allow_extrapolation: allowExtrapolation, max_points: maxPoints },
      ...(surfaceType === 'Swaption' ? { tenor_grid: { grid_type: 'TenorGrid', grid: { tenors: tenorGrid } }, swap_index_id: swapIndexId } : {}),
      ...(surfaceType === 'EquityBlack' ? {} : {}),
      ...(effectiveMode === 'SmileSlice' ? { slice_expiry_index: effExpiryIdx, slice_tenor_index: effTenorIdx } : {}),
      ...(effectiveMode === 'TermSlice' ? { slice_expiry_index: effExpiryIdx, slice_strike: effStrike, slice_strike_is_set: true } : {}),
      ...(effectiveMode === 'ExpirySlice' ? { slice_tenor_index: effTenorIdx, slice_strike: effStrike, slice_strike_is_set: true } : {}),
      ...(surfaceNeedsForwardResolution ? {
        discounting_curve_id: discountingCurveId,
        forwarding_curve_id: forwardingCurveId,
      } : {}),
    };

    const pricingCurves = curves;
    const pricingIndices = resolvedIndices.map(normalizeIndexDefForApi);
    const request = {
      pricing: {
        as_of_date: pricingAsOfDate,
        curves: pricingCurves,
        indices: pricingIndices,
        ...(swapIndexDef ? { swap_indices: swapIndexDef } : {}),
        ...(() => {
          const referencedQuoteIds = Array.from(new Set(
            [
              ...resolvedCurves
                .flatMap(c => c.points || [])
                .map((p: any) => p?.point?.quote_id)
                .filter((id: any) => typeof id === 'string' && id.length > 0),
              ...(surfaceType === 'EquityBlack' && selectedSurface?.spot_quote_id ? [selectedSurface.spot_quote_id] : []),
              ...(surfaceType === 'EquityBlack' ? (selectedSurface?.surface_price_quote_ids || []).filter(Boolean) : []),
            ]
          ));
          if (referencedQuoteIds.length === 0) return {};

          const quotes = referencedQuoteIds
            .map(id => {
              const b = bookById.get(id);
              if (b) {
                const val = resolveQuoteValue(b.series, asOf, mode);
                if (val !== null) {
                  return {
                    id,
                    kind: b.kind || 'Rate',
                    value: val,
                    ...(b.quote_type ? { quote_type: b.quote_type } : {}),
                  };
                }
              }
              const q = flatById.get(id);
              if (q && Number.isFinite(q.value)) {
                return {
                  id,
                  kind: q.kind || 'Rate',
                  value: q.value,
                  ...(q.quote_type ? { quote_type: q.quote_type } : {}),
                };
              }
              return null;
            })
            .filter(q => q !== null) as Array<{ id: string; kind: string; value: number; quote_type?: any }>;

          return quotes.length > 0 ? { quotes } : {};
        })(),
        vol_surfaces: [volSurfacePayload],
      },
      queries: [query],
    };
    return request;
  };

  const runSample = async (
    overrides?: Partial<{ outputMode: OutputMode; sliceExpiryIndex: number; sliceTenorIndex: number; sliceStrike: number }>,
    opts?: { autoFlipOnSuccess?: boolean },
  ) => {
    setError(null);
    setRequestId(undefined);
    let succeeded = false;
    try {
      setVolSurfaces(getVolSurfaces());
      const request = await buildRequest(overrides);
      setLastRequest(request);
      if (useCache) {
        const cached = cache.get(request);
        if (cached) {
          setResult(cached);
          setCacheHit(true);
          setLatencyMs(0);
          succeeded = true;
          return;
        }
      }
      setLoading(true);
      const response = await orchestratorPost<SampleVolSurfacesResponse>('/v1/vol-surfaces/sample', request);
      // Capture the correlation id BEFORE the ok-check throw so the trace
      // doorway is available on the error path too (that's when the logs
      // matter most), exactly as on the product pricing pages.
      setRequestId(response.requestId);
      setLatencyMs(response.duration_ms);
      if (!response.ok || !response.data) throw new Error(response.ok ? 'Sampling failed' : response.envelope.error);
      setResult(response.data);
      if (useCache) cache.set(request, response.data);
      setCacheHit(false);
      succeeded = true;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Sampling failed');
    } finally {
      setLoading(false);
      if (succeeded) {
        setLastSampleAt(Date.now());
        // Only flip the view when the user explicitly clicked "Sample" — the
        // auto-run useEffect (background sampling on every edit) MUST NOT
        // pull focus out of Edit mode, otherwise editing a cell would jerk
        // the user away from the matrix on each keystroke.
        if (opts?.autoFlipOnSuccess) setViewMode('sampler');
      }
    }
  };

  const probeCell = async (expiryIdx: number, tenorIdx: number) => {
    setSliceExpiryIndex(expiryIdx);
    setSliceTenorIndex(tenorIdx);
    setProbeExpiryIdx(expiryIdx);
    setProbeTenorIdx(tenorIdx);
  };

  useEffect(() => {
    if (!autoRun) return;
    const timer = setTimeout(() => {
      runSample({ outputMode: outputMode === 'SmileSlice' ? 'SmileSlice' : 'Cube' });
    }, 350);
    return () => clearTimeout(timer);
  }, [
    autoRun,
    tab,
    volId,
    surfaceType,
    swaptionKind,
    strikeAxis,
    selectedCurveSetId,
    discountingCurveId,
    forwardingCurveId,
    floatIndexId,
    swapIndexId,
    expiryGridType,
    expiryTenors,
    tenorGrid,
    strikeGrid,
    outputMode,
    allowExtrapolation,
    maxPoints,
    autoTrimHorizon,
  ]);

  const exportJson = () => {
    if (!result) return;
    const payload = {
      request: lastRequest,
      response: result,
      ui: {
        outputMode,
        sliceMode,
        strikeAxis,
        displayUnits,
        selectedStrikeIdx,
        probeExpiryIdx,
        probeTenorIdx,
        samplingPreset,
      },
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vol-sample-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const exportCsv = () => {
    if (!sample) return;
    const blob = new Blob([toCsv(sample)], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vol-sample-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const exportStrikeLayerCsv = () => {
    if (!sample || !heatmap) return;
    const rows = ['expiry,tenor,vol'];
    for (let i = 0; i < heatmap.rows.length; i += 1) {
      for (let j = 0; j < heatmap.cols.length; j += 1) {
        const v = heatmap.values[i]?.[j];
        if (Number.isFinite(v)) rows.push(`${heatmap.rows[i]},${heatmap.cols[j]},${v}`);
      }
    }
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vol-strike-layer-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const exportSliceCsv = () => {
    const rows = ['x,vol'];
    unifiedSlice.x.forEach((x, i) => rows.push(`${x},${unifiedSlice.y[i] ?? ''}`));
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vol-slice-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  const equityStrikeFallback = useMemo(() => {
    if (selectedSurface?.price_strikes && selectedSurface.price_strikes.length > 1) {
      return normalizeStrikeGrid(selectedSurface.price_strikes);
    }
    if (selectedSurface?.strikes && selectedSurface.strikes.length > 1) {
      return normalizeStrikeGrid(selectedSurface.strikes);
    }
    const reqStrikes = (lastRequest as any)?.queries?.[0]?.strike_grid?.strikes;
    if (Array.isArray(reqStrikes) && reqStrikes.length > 1) {
      return normalizeStrikeGrid(reqStrikes.filter((v: any) => Number.isFinite(v)).map((v: any) => Number(v)));
    }
    if (surfaceType === 'EquityBlack' && strikes.length > 1) {
      return normalizeStrikeGrid(strikes);
    }
    if (surfaceType === 'EquityBlack') {
      return [80, 90, 100, 110, 120];
    }
    return [];
  }, [selectedSurface?.id, selectedSurface?.price_strikes, selectedSurface?.strikes, lastRequest, surfaceType, strikes]);
  const heatmap = sample ? toHeatmap(
    sample,
    surfaceType === 'EquityBlack' ? 0 : selectedStrikeIdx,
    surfaceType === 'EquityBlack' ? equityStrikeFallback : [],
    surfaceType === 'EquityBlack'
  ) : null;
  const baselineHeatmap = baseline ? toHeatmap(
    baseline,
    surfaceType === 'EquityBlack' ? 0 : selectedStrikeIdx,
    surfaceType === 'EquityBlack' ? equityStrikeFallback : [],
    surfaceType === 'EquityBlack'
  ) : null;
  const canCompare = !!heatmap && !!baselineHeatmap
    && heatmap.rows.length === baselineHeatmap.rows.length
    && heatmap.cols.length === baselineHeatmap.cols.length;
  const sampleVolStats = useMemo(() => {
    const vals = (sample?.vols || []).filter((v): v is number => Number.isFinite(v));
    if (vals.length === 0) return null;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    return { min, max, spread: max - min };
  }, [sample]);
  const smallMultiplesWindow = useMemo(() => {
    if (!sample || !sample.strikes?.length) return { start: 0, end: 0 };
    const total = sample.strikes.length;
    const half = 5;
    let start = Math.max(0, selectedStrikeIdx - half);
    let end = Math.min(total - 1, selectedStrikeIdx + half);
    const need = Math.min(11, total);
    while ((end - start + 1) < need && start > 0) start -= 1;
    while ((end - start + 1) < need && end < total - 1) end += 1;
    return { start, end };
  }, [sample?.strikes?.length, selectedStrikeIdx]);
  const smallMultiplesScale = useMemo(() => {
    if (!sample || !showSmallMultiples) return null;
    const vals: number[] = [];
    for (let idx = smallMultiplesWindow.start; idx <= smallMultiplesWindow.end; idx += 1) {
      const hm = toHeatmap(sample, idx, surfaceType === 'EquityBlack' ? equityStrikeFallback : [], surfaceType === 'EquityBlack');
      hm.values.forEach(r => r.forEach(v => { if (Number.isFinite(v)) vals.push(v); }));
    }
    if (vals.length === 0) return null;
    return { min: Math.min(...vals), max: Math.max(...vals) };
  }, [sample, showSmallMultiples, smallMultiplesWindow.start, smallMultiplesWindow.end]);
  const cubeGeometry = useMemo(() => {
    if (!sample) return null;
    return buildCubeGeometry(sample, selectedStrikeIdx, displayUnits, asOfDate);
  }, [sample, selectedStrikeIdx, displayUnits, asOfDate]);
  const unifiedSlice = useMemo(() => {
    if (!sample) return { title: 'No slice', x: [] as string[], y: [] as number[] };
    const nE = sample.n_expiries || sample.expiries?.length || 0;
    const nT = sample.n_tenors || sample.tenors?.length || 0;
    const nS = sample.n_strikes || sample.strikes?.length || 0;
    const vols = sample.vols || [];
    const hasTenor = surfaceType !== 'EquityBlack' && nT > 0;
    const nTEff = hasTenor ? nT : 1;
    const clampE = Math.max(0, Math.min(probeExpiryIdx, Math.max(nE - 1, 0)));
    const clampT = Math.max(0, Math.min(probeTenorIdx, Math.max(nT - 1, 0)));
    const clampK = Math.max(0, Math.min(selectedStrikeIdx, Math.max(nS - 1, 0)));
    if (nE === 0 || nS === 0) return { title: 'No slice', x: [] as string[], y: [] as number[] };

    if (sliceMode === 'Smile') {
      const x = (sample.strikes || []).map(v => strikeAxis === 'SpreadFromATM' ? `${(v * 10000).toFixed(1)} bp` : v.toFixed(6));
      const y = Array.from({ length: nS }, (_, k) =>
        hasTenor ? (vols[(clampE * nT + clampT) * nS + k] ?? NaN) : (vols[clampE * nS + k] ?? NaN)
      );
      const expiryLbl = sample.expiries?.[clampE] || String(clampE);
      const tenorLbl = hasTenor
        ? (sample.tenors?.[clampT] ? `${sample.tenors[clampT].n}${sample.tenors[clampT].unit?.[0] || ''}` : String(clampT))
        : null;
      return { title: tenorLbl ? `Smile @ expiry=${expiryLbl} tenor=${tenorLbl}` : `Smile @ expiry=${expiryLbl}`, x, y };
    }
    if (sliceMode === 'Term') {
      if (hasTenor) {
        const x = (sample.tenors || []).map(t => `${t.n}${t.unit?.[0] || ''}`);
        const y = Array.from({ length: nT }, (_, j) => vols[(clampE * nT + j) * nS + clampK] ?? NaN);
        const expiryLbl = sample.expiries?.[clampE] || String(clampE);
        return { title: `Term slice @ expiry=${expiryLbl}, strike=${formatStrikeAxisLabel(sample.strikes?.[clampK], strikeAxis, surfaceType)}`, x, y };
      }
      const x = sample.expiries || [];
      const y = Array.from({ length: nE }, (_, i) => vols[i * nS + clampK] ?? NaN);
      return { title: `Term structure @ strike=${formatStrikeAxisLabel(sample.strikes?.[clampK], strikeAxis, surfaceType)}`, x, y };
    }
    const x = sample.expiries || [];
    const y = Array.from({ length: nE }, (_, i) =>
      hasTenor ? (vols[(i * nTEff + clampT) * nS + clampK] ?? NaN) : (vols[i * nS + clampK] ?? NaN)
    );
    const tenorLbl = sample.tenors?.[clampT] ? `${sample.tenors[clampT].n}${sample.tenors[clampT].unit?.[0] || ''}` : String(clampT);
    return {
      title: hasTenor
        ? `Expiry slice @ tenor=${tenorLbl}, strike=${formatStrikeAxisLabel(sample.strikes?.[clampK], strikeAxis, surfaceType)}`
        : `Expiry profile @ strike=${formatStrikeAxisLabel(sample.strikes?.[clampK], strikeAxis, surfaceType)}`,
      x,
      y,
    };
  }, [sample, sliceMode, probeExpiryIdx, probeTenorIdx, selectedStrikeIdx, strikeAxis, surfaceType]);
  const sliceStats = useMemo(() => {
    const vals = unifiedSlice.y.filter((v): v is number => Number.isFinite(v));
    if (vals.length === 0) return null;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    return { min, max, spread: max - min };
  }, [unifiedSlice]);

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-[1400px] mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="mb-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h1 className="text-2xl font-semibold text-[#0a0a0a]">IR Vol Sampler</h1>
              <p className="text-[#737373] mt-1">Stored surfaces define the model/spec. Sampling generates cube/slice results for exploration, diagnostics, compare, and export.</p>
            </div>
            {showSurfaceList && (
              <div className="flex gap-2">
                <input ref={fileInputRef} type="file" accept=".json" onChange={handleImportSurfaces} className="hidden" />
                <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
                  <ImportIcon />
                  Import
                </button>
                {volSurfaces.length > 0 && (
                  <button onClick={handleExportAllSurfaces} className={listStyles.secondaryButton}>
                    <ExportIcon />
                    Export All
                  </button>
                )}
                <button onClick={() => handleCreateSurface()} className={listStyles.primaryNewButton}>
                  <NewIcon />
                  New Surface
                </button>
              </div>
            )}
          </div>
        </div>

        {showSurfaceList ? (
          volSurfaces.length === 0 ? (
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-10 text-center">
              <h3 className="text-lg font-medium text-[#0a0a0a] mb-2">No surfaces available</h3>
              <p className="text-[#737373]">Create or import surfaces in Settings, then open them here.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {volSurfaces.map(surface => {
                const rows = surface.axes_expiries?.length
                  || surface.expiries?.length
                  || surface.grid?.length
                  || 0;
                const cols = surface.axes_tenors?.length
                  || surface.tenors?.length
                  || surface.grid?.[0]?.length
                  || 0;
                return (
                  <div
                    key={surface.id}
                    className={listStyles.listCard}
                    onClick={() => openSurface(surface)}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="text-lg font-medium text-[#0a0a0a] hover:text-[#8a6a2f] transition-colors">{surface.id}</h3>
                          <span className="px-2 py-0.5 text-xs font-medium bg-[#f5f5f5] text-[#737373] rounded">
                            {surface.base?.volatility_type || 'Normal'}
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-4 text-xs text-[#737373]">
                          <span>Shape: <span className="font-mono">{surface.base?.shape || 'Constant'}</span></span>
                          <span>Grid: <span className="font-mono">{rows} x {cols}</span></span>
                        </div>
                      </div>
                      <div className={listStyles.hoverActions}>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDuplicateSurface(surface); }}
                          className={listStyles.duplicateButton}
                          title="Duplicate"
                        >
                          <DuplicateIcon />
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDeleteSurface(surface); }}
                          className={listStyles.deleteButton}
                          title="Delete"
                        >
                          <TrashButton />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )
        ) : null}

        {!showSurfaceList && (
          <>
            <div className="mb-4 flex items-center justify-between gap-3 flex-wrap">
              <BackLink onClick={() => setShowSurfaceList(true)} label={getBackLabel(surfaceUi.plural)} />
              {selectedSurface && (
                <div className="flex items-center gap-3 flex-wrap">
                  {/* Sampled-status pill: shows whether the Sampler view is
                      fresh, stale, or empty. Visible in BOTH modes so an
                      editing user can tell at a glance whether their cached
                      sampler output reflects their current edits. */}
                  <div
                    className="text-[11px] px-2 py-1 rounded border border-[#e5e5e5] bg-white text-[#525252]"
                    title={lastSampleAt ? `Last sample at ${new Date(lastSampleAt).toLocaleTimeString()}` : 'Press Sample to compute the surface output.'}
                  >
                    {loading
                      ? <><span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 mr-1.5 align-middle animate-pulse" />Sampling…</>
                      : lastSampleAt
                        ? <>
                            <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle ${resultErrors.length > 0 ? 'bg-red-500' : 'bg-emerald-500'}`} />
                            Sampled {formatRelativeTime(lastSampleAt)}
                            {resultErrors.length > 0 && <span className="ml-1 text-red-700">• {resultErrors.length} err</span>}
                          </>
                        : <><span className="inline-block w-1.5 h-1.5 rounded-full bg-[#a3a3a3] mr-1.5 align-middle" />No sample yet</>}
                  </div>
                  {/* Trace doorway for the most recent sample — same
                      affordance the product pricing pages carry (dev-gated
                      inside PricingTraceLink). One click from the sampler to
                      the /investigate pipeline logs for this request. */}
                  <PricingTraceLink requestId={requestId} />
                  {/* Two-mode segmented control. Replaces the prior side-by-
                      side 4/8 column grid so each view gets full canvas. */}
                  <div className="inline-flex rounded-lg border border-[#d4d4d4] overflow-hidden text-xs">
                    <button
                      type="button"
                      onClick={() => setViewMode('edit')}
                      aria-pressed={viewMode === 'edit'}
                      className={`px-3 py-1.5 ${viewMode === 'edit' ? 'bg-[#0a0a0a] text-white' : 'bg-white text-[#525252] hover:bg-[#f5f5f5]'}`}
                    >
                      ✎ Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => setViewMode('sampler')}
                      aria-pressed={viewMode === 'sampler'}
                      className={`px-3 py-1.5 border-l border-[#d4d4d4] ${viewMode === 'sampler' ? 'bg-[#0a0a0a] text-white' : 'bg-white text-[#525252] hover:bg-[#f5f5f5]'}`}
                    >
                      ▶ Sampler
                    </button>
                  </div>
                  <div className="text-xs text-[#737373]">
                    Working on: <span className="font-medium text-[#0a0a0a]">{selectedSurface.id}</span>
                  </div>
                </div>
              )}
            </div>
            {error && (
              <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700">
                <div className="text-sm">{error}</div>
                {/* A failed sample is exactly when the pipeline logs matter —
                    expose the same trace doorway as on a success. */}
                {requestId && (
                  <div className="mt-2">
                    <PricingTraceLink requestId={requestId} />
                  </div>
                )}
              </div>
            )}
            {requestNote && <div className="mb-4 p-3 text-sm bg-amber-50 border border-amber-200 rounded-lg text-amber-800">{requestNote}</div>}
            {resultErrors.length > 0 && (
              <div className="mb-4 p-3 text-sm bg-red-50 border border-red-200 rounded-lg text-red-700 space-y-1">
                {resultErrors.map((msg, i) => <div key={`${msg}-${i}`}>{msg}</div>)}
              </div>
            )}
          </>
        )}

        {!showSurfaceList && (
          <div className="space-y-4">
            {/* Edit view: surface inputs + sampling configuration. Mounted
                only when viewMode === 'edit' so the Matrix2DEditor and the
                Sampling Definition form get the full page width. */}
            {viewMode === 'edit' && (
            <section className="bg-white border border-[#e5e5e5] rounded-xl p-4 space-y-4">
              <h2 className="text-sm font-semibold text-[#0a0a0a]">Sampling Definition</h2>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={labelClass}>Surface Type</label>
                  <select className={inputClass} value={surfaceType} onChange={e => setSurfaceType(e.target.value as SurfaceType)}>
                    <option value="Swaption">Swaption</option>
                    <option value="Optionlet">Optionlet</option>
                    <option value="EquityBlack">EquityBlack</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Vol Surface Spec</label>
                  <select className={inputClass} value={volId} onChange={e => setVolId(e.target.value)}>
                    <option value="">Select…</option>
                    {filteredVolSurfaces.map(v => <option key={v.id} value={v.id}>{v.id}</option>)}
                  </select>
                </div>
              </div>
              <p className="text-xs text-[#737373] -mt-2">Stored surfaces define the model. Sampling generates the cube.</p>
              {surfaceType === 'EquityBlack' && selectedSurface && (
                <div className="border border-[#e5e5e5] rounded-lg p-3 space-y-3 bg-[#fafafa]">
                  <h3 className="text-xs font-semibold text-[#0a0a0a]">EquityBlack Surface Editor</h3>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className={labelClass}>Shape</label>
                      <select
                        className={inputClass}
                        value={selectedSurface.base?.shape === 'SurfaceFromPrices' ? 'SurfaceFromPrices' : 'Constant'}
                        onChange={e => updateSelectedSurface(s => ({
                          ...s,
                          payload_type: 'BlackVolSpec',
                          base: {
                            ...s.base,
                            shape: e.target.value as any,
                            ...(e.target.value === 'Constant' ? { constant_vol: s.base?.constant_vol ?? 0.24 } : {}),
                          },
                        }))}
                      >
                        <option value="Constant">Constant</option>
                        <option value="SurfaceFromPrices">SurfaceFromPrices</option>
                      </select>
                    </div>
                    {selectedSurface.base?.shape === 'Constant' ? (
                      <div>
                        <label className={labelClass}>constant_vol</label>
                        <input
                          type="number"
                          step="0.0001"
                          className={inputClass}
                          value={selectedSurface.base?.constant_vol ?? 0.24}
                          onChange={e => updateSelectedSurface(s => ({
                            ...s,
                            payload_type: 'BlackVolSpec',
                            base: { ...s.base, shape: 'Constant', constant_vol: Number(e.target.value) || 0.0 },
                          }))}
                        />
                      </div>
                    ) : (
                      <div>
                        <label className={labelClass}>price_option_type</label>
                        <select
                          className={inputClass}
                          value={selectedSurface.price_option_type || 'Call'}
                          onChange={e => updateSelectedSurface(s => ({ ...s, price_option_type: e.target.value as 'Call' | 'Put' }))}
                        >
                          <option value="Call">Call</option>
                          <option value="Put">Put</option>
                        </select>
                      </div>
                    )}
                  </div>

                  {selectedSurface.base?.shape === 'SurfaceFromPrices' && (
                    <>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>surface_interpolator</label>
                          <select
                            className={inputClass}
                            value={selectedSurface.surface_interpolator || 'Bilinear'}
                            onChange={e => updateSelectedSurface(s => ({ ...s, surface_interpolator: e.target.value as 'Bilinear' | 'Bicubic' }))}
                          >
                            <option value="Bilinear">Bilinear</option>
                            <option value="Bicubic">Bicubic</option>
                          </select>
                        </div>
                      </div>

                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>Spot source</label>
                          <select
                            className={inputClass}
                            value={selectedSurface.spot_quote_id ? 'quote' : 'inline'}
                            onChange={e => updateSelectedSurface(s => e.target.value === 'quote'
                              ? { ...s, spot: undefined, spot_quote_id: s.spot_quote_id || '' }
                              : { ...s, spot: s.spot ?? 100, spot_quote_id: undefined })}
                          >
                            <option value="inline">Inline spot</option>
                            <option value="quote">Spot quote id</option>
                          </select>
                        </div>
                        {selectedSurface.spot_quote_id ? (
                          <div>
                            <label className={labelClass}>spot_quote_id</label>
                            <select
                              className={inputClass}
                              value={selectedSurface.spot_quote_id || ''}
                              onChange={e => updateSelectedSurface(s => ({ ...s, spot_quote_id: e.target.value || undefined }))}
                            >
                              <option value="">Select quote…</option>
                              {quoteIdOptions.map(id => <option key={id} value={id}>{id}</option>)}
                            </select>
                          </div>
                        ) : (
                          <div>
                            <label className={labelClass}>spot</label>
                            <input
                              type="number"
                              step="0.0001"
                              className={inputClass}
                              value={selectedSurface.spot ?? 100}
                              onChange={e => updateSelectedSurface(s => ({ ...s, spot: Number(e.target.value) || 0 }))}
                            />
                          </div>
                        )}
                      </div>

                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>Discount source</label>
                          <select
                            className={inputClass}
                            value={selectedSurface.use_flat_discount_rate ? 'flat' : 'curve'}
                            onChange={e => updateSelectedSurface(s => e.target.value === 'flat'
                              ? { ...s, use_flat_discount_rate: true, discount_curve_id: undefined, flat_discount_rate: s.flat_discount_rate ?? 0.02 }
                              : { ...s, use_flat_discount_rate: false, flat_discount_rate: undefined, discount_curve_id: s.discount_curve_id || (discountCurveCandidates[0] || '') })}
                          >
                            <option value="curve">Curve id</option>
                            <option value="flat">Flat rate</option>
                          </select>
                        </div>
                        {selectedSurface.use_flat_discount_rate ? (
                          <div>
                            <label className={labelClass}>flat_discount_rate</label>
                            <input
                              type="number"
                              step="0.0001"
                              className={inputClass}
                              value={selectedSurface.flat_discount_rate ?? 0.02}
                              onChange={e => updateSelectedSurface(s => ({ ...s, use_flat_discount_rate: true, flat_discount_rate: Number(e.target.value) || 0 }))}
                            />
                          </div>
                        ) : (
                          <div>
                            <label className={labelClass}>discount_curve_id</label>
                            <select
                              className={inputClass}
                              value={selectedSurface.discount_curve_id || ''}
                              onChange={e => updateSelectedSurface(s => ({ ...s, use_flat_discount_rate: false, discount_curve_id: e.target.value || undefined }))}
                            >
                              <option value="">Select curve…</option>
                              {availableCurveIds.map(id => <option key={id} value={id}>{id}</option>)}
                            </select>
                          </div>
                        )}
                      </div>

                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelClass}>Dividend source</label>
                          <select
                            className={inputClass}
                            value={selectedSurface.use_flat_dividend_rate ? 'flat' : 'curve'}
                            onChange={e => updateSelectedSurface(s => e.target.value === 'flat'
                              ? { ...s, use_flat_dividend_rate: true, dividend_curve_id: undefined, flat_dividend_rate: s.flat_dividend_rate ?? 0.0 }
                              : { ...s, use_flat_dividend_rate: false, flat_dividend_rate: undefined, dividend_curve_id: s.dividend_curve_id || (forwardCurveCandidates[0] || '') })}
                          >
                            <option value="curve">Curve id</option>
                            <option value="flat">Flat yield</option>
                          </select>
                        </div>
                        {selectedSurface.use_flat_dividend_rate ? (
                          <div>
                            <label className={labelClass}>flat_dividend_rate</label>
                            <input
                              type="number"
                              step="0.0001"
                              className={inputClass}
                              value={selectedSurface.flat_dividend_rate ?? 0}
                              onChange={e => updateSelectedSurface(s => ({ ...s, use_flat_dividend_rate: true, flat_dividend_rate: Number(e.target.value) || 0 }))}
                            />
                          </div>
                        ) : (
                          <div>
                            <label className={labelClass}>dividend_curve_id</label>
                            <select
                              className={inputClass}
                              value={selectedSurface.dividend_curve_id || ''}
                              onChange={e => updateSelectedSurface(s => ({ ...s, use_flat_dividend_rate: false, dividend_curve_id: e.target.value || undefined }))}
                            >
                              <option value="">Select curve…</option>
                              {availableCurveIds.map(id => <option key={id} value={id}>{id}</option>)}
                            </select>
                          </div>
                        )}
                      </div>

                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <label className={labelClass}>price_expiries</label>
                          <button
                            type="button"
                            className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-[#f5f5f5]"
                            onClick={() => updateSelectedSurface(s => {
                              const nextExp = [...(s.price_expiries || []), asOfDate];
                              return resizeEquityMatrix({ ...s, price_expiries: nextExp }, nextExp.length, s.price_strikes?.length || 0);
                            })}
                          >
                            Add expiry
                          </button>
                        </div>
                        {(selectedSurface.price_expiries || []).map((exp, i) => (
                          <div key={`exp-${i}`} className="grid grid-cols-[1fr_auto] gap-2">
                            <input
                              type="date"
                              className={inputClass}
                              value={exp}
                              onChange={e => updateSelectedSurface(s => {
                                const next = [...(s.price_expiries || [])];
                                next[i] = e.target.value;
                                return { ...s, price_expiries: next };
                              })}
                            />
                            <button
                              type="button"
                              className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-red-50 hover:text-red-600"
                              onClick={() => updateSelectedSurface(s => {
                                const nextExp = [...(s.price_expiries || [])];
                                nextExp.splice(i, 1);
                                return resizeEquityMatrix({ ...s, price_expiries: nextExp }, nextExp.length, s.price_strikes?.length || 0);
                              })}
                            >
                              Delete
                            </button>
                          </div>
                        ))}
                      </div>

                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <label className={labelClass}>price_strikes</label>
                          <button
                            type="button"
                            className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-[#f5f5f5]"
                            onClick={() => updateSelectedSurface(s => {
                              const nextStr = [...(s.price_strikes || []), 100];
                              return resizeEquityMatrix({ ...s, price_strikes: nextStr }, s.price_expiries?.length || 0, nextStr.length);
                            })}
                          >
                            Add strike
                          </button>
                        </div>
                        {(selectedSurface.price_strikes || []).map((kVal, j) => (
                          <div key={`k-${j}`} className="grid grid-cols-[1fr_auto] gap-2">
                            <input
                              type="number"
                              step="0.0001"
                              className={inputClass}
                              value={kVal}
                              onChange={e => updateSelectedSurface(s => {
                                const next = [...(s.price_strikes || [])];
                                next[j] = Number(e.target.value) || 0;
                                return { ...s, price_strikes: next };
                              })}
                            />
                            <button
                              type="button"
                              className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-red-50 hover:text-red-600"
                              onClick={() => updateSelectedSurface(s => {
                                const nextStr = [...(s.price_strikes || [])];
                                nextStr.splice(j, 1);
                                return resizeEquityMatrix({ ...s, price_strikes: nextStr }, s.price_expiries?.length || 0, nextStr.length);
                              })}
                            >
                              Delete
                            </button>
                          </div>
                        ))}
                      </div>

                      <div className="space-y-2">
                        <label className={labelClass}>surface_prices quote matrix (row=expiry, col=strike)</label>
                        <div className="overflow-auto border border-[#e5e5e5] rounded-lg p-2 bg-white">
                          <table className="text-xs w-full">
                            <thead className="bg-[#fafafa]">
                              <tr>
                                <th className="px-2 py-1 text-left">Expiry \ Strike</th>
                                {(selectedSurface.price_strikes || []).map((k, j) => (
                                  <th key={`hk-${j}`} className="px-2 py-1 text-left">{k}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {(selectedSurface.price_expiries || []).map((exp, i) => {
                                const nCols = selectedSurface.price_strikes?.length || 0;
                                return (
                                  <tr key={`r-${i}`} className="border-t border-[#f5f5f5]">
                                    <td className="px-2 py-1 font-mono">{exp}</td>
                                    {(selectedSurface.price_strikes || []).map((_, j) => {
                                      const idx = i * nCols + j;
                                      const qid = selectedSurface.surface_price_quote_ids?.[idx] || '';
                                      return (
                                        <td key={`c-${i}-${j}`} className="px-2 py-1">
                                          <select
                                            className="w-full px-2 py-1 border border-[#d4d4d4] rounded bg-white"
                                            value={qid}
                                            onChange={e => updateSelectedSurface(s => setEquityMatrixCellQuote(s, i, j, e.target.value))}
                                          >
                                            <option value="">Select quote…</option>
                                            {quoteIdOptions.map(id => <option key={id} value={id}>{id}</option>)}
                                          </select>
                                        </td>
                                      );
                                    })}
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                        <p className="text-xs text-[#737373]">Matrix cells store quote IDs. Values are resolved from quote book/saved quotes at as-of date.</p>
                      </div>
                    </>
                  )}
                </div>
              )}
              {surfaceType === 'Swaption' && (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className={labelClass}>Swaption Surface Kind</label>
                    <select
                      className={inputClass}
                      value={swaptionKind}
                      onChange={e => {
                        const nextKind = e.target.value as SwaptionSurfaceKind;
                        setSwaptionKind(nextKind);
                        // Write through to surface.base.shape and seed axes /
                        // SABR grids on transition. updatedAt is bumped via
                        // updateSelectedSurface so dirty-state tracking sees
                        // the change.
                        applyKindChangeToSurface(nextKind);
                      }}
                    >
                      <option value="Constant">Constant</option>
                      <option value="AtmMatrix">ATM Matrix</option>
                      <option value="SmileCube">Smile Cube</option>
                      <option value="SabrParams">SABR Params</option>
                      <option value="SabrCalibrate">SABR Calibrate</option>
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Swap Index ID</label>
                    <select className={inputClass} value={swapIndexId} onChange={e => setSwapIndexId(e.target.value)} disabled={swapIndexOptions.length === 0}>
                      <option value="">{swapIndexOptions.length === 0 ? 'No swap indices available' : 'Select…'}</option>
                      {swapIndexOptions.map(id => <option key={id} value={id}>{id}</option>)}
                    </select>
                  </div>
                </div>
              )}
              {surfaceType === 'Swaption' && (swaptionKind === 'AtmMatrix' || swaptionKind === 'SmileCube') && selectedSurface && (
                <div className="border border-[#e5e5e5] rounded-lg p-3 space-y-3 bg-[#fafafa]">
                  <SurfaceAxesEditor
                    surface={selectedSurface}
                    onChange={updateSelectedSurface}
                    title={swaptionKind === 'SmileCube'
                      ? 'Surface axes — define the ATM anchor grid'
                      : 'Surface axes — define the ATM matrix dimensions'}
                    helperText={swaptionKind === 'SmileCube'
                      ? 'These periods are the (expiry, tenor) nodes where the ATM σ values are anchored. The per-strike smile shape is generated by the backend at sampling time and not stored here.'
                      : 'These periods become the rows (expiry) and columns (tenor) of the saved matrix and are what the wire payload sends. They are independent of the sampling axes below.'}
                  />
                  <div className="flex items-baseline justify-between">
                    <h3 className="text-xs font-semibold text-[#0a0a0a]">
                      {swaptionKind === 'SmileCube' ? 'ATM σ anchors (decimal vols)' : 'ATM Matrix (decimal vols)'}
                    </h3>
                    <span className="text-[11px] text-[#737373]">rows = surface expiries, cols = surface tenors</span>
                  </div>
                  <Matrix2DEditor
                    value={(selectedSurface.grid || []) as MatrixCell[][]}
                    onChange={(next) => updateSelectedSurface(s => ({ ...s, grid: next }))}
                    rowLabels={(selectedSurface.axes_expiries || []).map(periodLabel)}
                    colLabels={(selectedSurface.axes_tenors || []).map(periodLabel)}
                    rowAxisLabel="Expiry"
                    colAxisLabel="Tenor"
                    quoteIdOptions={quoteIdOptions}
                    validateCell={(cell) => {
                      if (cell && typeof cell === 'object' && 'quoteId' in cell) {
                        return cell.quoteId.trim().length === 0 ? 'quote id required' : null;
                      }
                      if (typeof cell === 'number') {
                        if (!Number.isFinite(cell)) return null;
                        if (cell < 0) return 'vol must be ≥ 0';
                      }
                      return null;
                    }}
                    colorScale={(v) => atmHeatmapColor(v)}
                    ariaLabel={swaptionKind === 'SmileCube' ? 'SmileCube ATM anchor grid editor' : 'ATM matrix grid editor'}
                  />
                  <p className="text-[11px] text-[#a3a3a3]">
                    {swaptionKind === 'SmileCube'
                      ? 'Edit ATM σ at each (expiry, tenor) anchor. The smile/skew across the strike axis is generated by the backend smile model at sampling time. Paste TSV/CSV, use the row/column fill handles, or toggle a cell to a quote reference.'
                      : 'Edit cells directly, paste TSV/CSV from a spreadsheet, or use the column/row fill handles. Resize via the surface-axes editor above; cells outside the new dimensions are dropped on the next save.'}
                  </p>
                </div>
              )}
              {surfaceType === 'Swaption' && swaptionKind === 'SabrParams' && selectedSurface && (
                <SabrParamsEditorBlock
                  surface={selectedSurface}
                  onChange={updateSelectedSurface}
                  quoteIdOptions={quoteIdOptions}
                />
              )}
              {surfaceType === 'Swaption' && swaptionKind === 'SabrCalibrate' && selectedSurface && (
                <SabrCalibrateEditorBlock
                  surface={selectedSurface}
                  onChange={updateSelectedSurface}
                  quoteIdOptions={quoteIdOptions}
                  swapIndexId={swapIndexId}
                  discountingCurveId={discountingCurveId}
                  forwardingCurveId={forwardingCurveId}
                  curveSet={curveSet}
                  resolvedCurves={resolvedCurves}
                  floatIndexId={floatIndexId}
                  asOfDate={asOfDate}
                  sampleErrorHint={error}
                  onCalibrationSaved={(calibrated) => {
                    void saveVolSurface(calibrated)
                      .then(() => {
                        setVolSurfaces(getVolSurfaces());
                        setVolId(calibrated.id);
                        setSwaptionKind('SabrParams');
                      })
                      .catch(err => setError(err instanceof Error ? err.message : 'Failed to save calibrated surface'));
                  }}
                />
              )}

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={labelClass}>Sampling Preset</label>
                  <select className={inputClass} value={samplingPreset} onChange={e => setSamplingPreset(e.target.value as SamplingPreset)}>
                    <option value="Fast">Fast</option>
                    <option value="Standard">Standard</option>
                    <option value="HQ">HQ</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Output Mode</label>
                  <select className={inputClass} value={surfaceType === 'EquityBlack' ? 'Cube' : outputMode} onChange={e => setOutputMode(e.target.value as OutputMode)} disabled={surfaceType === 'EquityBlack'}>
                    <option value="Cube">Cube</option>
                    <option value="SmileSlice">SmileSlice</option>
                  </select>
                </div>
                <div>
                  <label className={labelClass}>Strike Axis</label>
                  <select className={inputClass} value={surfaceType === 'EquityBlack' ? 'AbsoluteStrike' : strikeAxis} onChange={e => setStrikeAxis(e.target.value as StrikeAxis)} disabled={!spreadAxisSupported || surfaceType === 'EquityBlack'}>
                    <option value="AbsoluteStrike">AbsoluteStrike</option>
                    <option value="SpreadFromATM" disabled={!spreadAxisSupported}>SpreadFromATM</option>
                  </select>
                  {!spreadAxisSupported && (
                    <p className="text-xs text-[#a3a3a3] mt-1">SpreadFromATM is temporarily disabled in sampler compatibility mode; changes here will not affect requests.</p>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={labelClass}>Curve Set</label>
                  <select className={inputClass} value={selectedCurveSetId} onChange={e => setSelectedCurveSetId(e.target.value)}>
                    <option value="">Select…</option>
                    {curveSets.map(cs => <option key={cs.id} value={cs.id}>{cs.name}</option>)}
                  </select>
                  {curveSets.length === 0 && (
                    <p className="text-xs text-red-600 mt-1">No curve sets loaded. Import or create one to run sampling.</p>
                  )}
                </div>
                {surfaceType === 'Swaption' ? (
                  <div>
                    <label className={labelClass}>Float Index ID</label>
                    <select className={inputClass} value={floatIndexId} onChange={e => setFloatIndexId(e.target.value)} disabled={floatIndexOptions.length === 0}>
                      <option value="">{floatIndexOptions.length === 0 ? 'No indices available' : 'Select…'}</option>
                      {floatIndexOptions.map(id => <option key={id} value={id}>{id}</option>)}
                    </select>
                  </div>
                ) : (
                  <div />
                )}
              </div>

              {spreadAxisSupported && (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className={labelClass}>Discounting Curve ID</label>
                    <select className={inputClass} value={discountingCurveId} onChange={e => setDiscountingCurveId(e.target.value)} disabled={availableCurveIds.length === 0}>
                      <option value="">{availableCurveIds.length === 0 ? 'Select a curve set first' : 'Select curve…'}</option>
                      {discountCurveCandidates.map(id => <option key={id} value={id}>{id}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className={labelClass}>Forwarding Curve ID</label>
                    <select className={inputClass} value={forwardingCurveId} onChange={e => setForwardingCurveId(e.target.value)} disabled={availableCurveIds.length === 0}>
                      <option value="">{availableCurveIds.length === 0 ? 'Select a curve set first' : 'Select curve…'}</option>
                      {forwardCurveCandidates.map(id => <option key={id} value={id}>{id}</option>)}
                    </select>
                  </div>
                </div>
              )}
              {spreadAxisSupported && strikeAxis === 'SpreadFromATM' && (
                <p className="text-xs text-[#a3a3a3] -mt-2">
                  Curve IDs are required when strike axis is <span className="font-medium">SpreadFromATM</span>.
                </p>
              )}

              <div className="border-t border-[#f0f0f0] pt-3">
                <h3 className="text-xs font-semibold text-[#0a0a0a]">Sampling axes — where to evaluate σ</h3>
                <p className="text-[11px] text-[#a3a3a3] mb-2">
                  These grids drive the sampler request (cube/slice queries), not the wire shape of the saved surface.
                  For non-constant Swaption kinds the surface uses its own axes (configured above the matrix editor).
                </p>
                <label className={labelClass}>Expiry Grid</label>
                <div className="grid grid-cols-2 gap-3 mb-2">
                  <select className={inputClass} value={expiryGridType} onChange={e => setExpiryGridType(e.target.value as GridType)}>
                    <option value="TenorGrid">TenorGrid</option>
                    <option value="RangeGrid">RangeGrid</option>
                  </select>
                  {expiryGridType === 'RangeGrid' && (
                    <input className={inputClass} type="date" value={rangeEndDate} onChange={e => setRangeEndDate(e.target.value)} />
                  )}
                </div>
                {expiryGridType === 'TenorGrid' && (
                  <PeriodGridEditor
                    rows={expiryTenors}
                    setRows={setExpiryTenors}
                    newN={newExpiryN}
                    setNewN={setNewExpiryN}
                    newUnit={newExpiryUnit}
                    setNewUnit={setNewExpiryUnit}
                  />
                )}
                {expiryGridType === 'RangeGrid' && (
                  <div className="grid grid-cols-3 gap-2">
                    <input type="number" className={inputClass} value={rangeStepN} onChange={e => setRangeStepN(Number(e.target.value) || 1)} />
                    <select className={inputClass} value={rangeStepUnit} onChange={e => setRangeStepUnit(e.target.value as TimeUnit)}>
                      <option value="Days">Days</option><option value="Weeks">Weeks</option><option value="Months">Months</option><option value="Years">Years</option>
                    </select>
                    <label className="text-xs text-[#525252] flex items-center gap-2"><input type="checkbox" checked={rangeBusinessOnly} onChange={e => setRangeBusinessOnly(e.target.checked)} /> business days</label>
                  </div>
                )}
              </div>

              {surfaceType === 'Swaption' && (
                <div>
                  <label className={labelClass}>Tenor Grid (Swaption sampling)</label>
                  <PeriodGridEditor
                    rows={tenorGrid}
                    setRows={setTenorGrid}
                    newN={newTenorN}
                    setNewN={setNewTenorN}
                    newUnit={newTenorUnit}
                    setNewUnit={setNewTenorUnit}
                  />
                </div>
              )}

              <div>
                <label className={labelClass}>Strike Grid</label>
                <div className="grid grid-cols-2 gap-2 mb-2">
                  <select
                    className={inputClass}
                    value={strikeGridTemplate}
                    onChange={(e) => {
                      const mode = e.target.value as 'custom' | 'normal' | 'lognormal';
                      setStrikeGridTemplate(mode);
                      if (mode === 'normal') setStrikeGrid(normalizeStrikeGrid([-0.02, -0.015, -0.01, -0.0075, -0.005, -0.0025, 0, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02]));
                      if (mode === 'lognormal') setStrikeGrid(normalizeStrikeGrid([0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]));
                    }}
                  >
                    <option value="custom">Common grids (custom)</option>
                    <option value="normal">Normal (spread grid)</option>
                    <option value="lognormal">Lognormal (absolute rate grid)</option>
                  </select>
                </div>
                <div className="grid grid-cols-2 gap-2 mb-2">
                  <button className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]" onClick={() => setStrikeGrid(normalizeStrikeGrid([-0.002, -0.001, 0, 0.001, 0.002]))}>ATM ±100/200bp</button>
                  <button className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]" onClick={() => setStrikeGrid(normalizeStrikeGrid([0.01, 0.015, 0.02, 0.025, 0.03]))}>Absolute rates</button>
                </div>
                <StrikeGridEditor strikes={strikeGrid} setStrikes={setStrikeGrid} newStrike={newStrike} setNewStrike={setNewStrike} />
                {strikeGridError && <p className="text-xs text-red-600 mt-1">{strikeGridError}</p>}
                <p className="text-xs text-[#a3a3a3] mt-1">Structured list, sorted automatically.</p>
              </div>

              {outputMode !== 'Cube' && (
                <label className="text-xs text-[#525252] flex items-center gap-2">
                  <input type="checkbox" checked={showAdvancedSlice} onChange={e => setShowAdvancedSlice(e.target.checked)} />
                  Advanced slice coordinates
                </label>
              )}
              {outputMode !== 'Cube' && showAdvancedSlice && (
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <label className={labelClass}>slice_expiry_index</label>
                    <input type="number" className={inputClass} value={sliceExpiryIndex} onChange={e => setSliceExpiryIndex(Number(e.target.value) || 0)} />
                  </div>
                  <div>
                    <label className={labelClass}>slice_tenor_index</label>
                    <input type="number" className={inputClass} value={sliceTenorIndex} onChange={e => setSliceTenorIndex(Number(e.target.value) || 0)} />
                  </div>
                  <div>
                    <label className={labelClass}>slice_strike</label>
                    <input type="number" className={inputClass} value={sliceStrike} onChange={e => setSliceStrike(Number(e.target.value) || 0)} />
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-3 items-end">
                <div>
                  <label className={labelClass}>max_points</label>
                  <input type="number" className={inputClass} value={maxPoints} onChange={e => setMaxPoints(Number(e.target.value) || 1000)} />
                </div>
                <label className="text-xs text-[#525252] flex items-center gap-2"><input type="checkbox" checked={allowExtrapolation} onChange={e => setAllowExtrapolation(e.target.checked)} />allow extrapolation</label>
              </div>
              <label className="text-xs text-[#525252] flex items-center gap-2">
                <input type="checkbox" checked={autoTrimHorizon} onChange={e => setAutoTrimHorizon(e.target.checked)} />
                Auto-trim to curve horizon (29.5Y cap)
              </label>

              <div className="text-xs text-[#737373]">
                Sampling points: <span className={estimatedPoints > maxPoints ? 'text-red-600 font-semibold' : 'font-semibold'}>{estimatedPoints}</span>
                <span className="ml-2">({expiryGridType === 'TenorGrid' ? expiryTenors.length : 16} x {surfaceType === 'Swaption' ? tenorGrid.length : 1} x {strikes.length || 1})</span>
                {estimatedPoints < 200 ? <span className="text-amber-700"> - Surface may look blocky</span> : null}
              </div>
              <div className="text-xs text-[#737373] border border-[#e5e5e5] rounded-lg p-2 bg-[#fafafa]">
                <div>Quality checklist</div>
                <div>points count: <span className="font-mono">{estimatedPoints}</span> {estimatedPoints < 100 ? <span className="text-amber-700"> - Grid too sparse; increase expiries/tenors/strikes.</span> : null}</div>
                <div>min/max vol: <span className="font-mono">{sampleVolStats ? `${sampleVolStats.min.toFixed(6)} / ${sampleVolStats.max.toFixed(6)}` : '—'}</span></div>
                <div>cube cardinality: <span className="font-mono">{sample ? `${(sample.n_expiries || 0) * (sample.n_tenors || 0) * (sample.n_strikes || 0)} vs ${(sample.vols || []).length}` : '—'}</span></div>
                <div>% missing/NaN: <span className="font-mono">{sample ? (() => {
                  const vals = sample.vols || [];
                  const miss = vals.filter(v => !Number.isFinite(v)).length;
                  return `${((miss / Math.max(vals.length, 1)) * 100).toFixed(2)}%`;
                })() : '—'}</span></div>
              </div>

              <div className="flex items-center gap-3">
                <button
                  disabled={loading || estimatedPoints > maxPoints || !!strikeGridError}
                  onClick={() => runSample(
                    { outputMode: outputMode === 'SmileSlice' ? 'SmileSlice' : 'Cube' },
                    { autoFlipOnSuccess: true },
                  )}
                  className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] disabled:opacity-60"
                  title="Run the sampler with the current inputs and switch to the Sampler view."
                >
                  Sample → Sampler
                </button>
                <label className="text-xs text-[#525252] flex items-center gap-2">
                  <input type="checkbox" checked={autoRun} onChange={e => setAutoRun(e.target.checked)} />
                  Auto-run on change
                </label>
              </div>

              <div className="text-xs text-[#737373]">
                {latencyMs !== null && <span>Latency: {latencyMs.toFixed(1)}ms</span>}
                {cacheHit && <span className="ml-2 text-green-700">served from cache</span>}
                <label className="ml-3 text-xs text-[#525252] inline-flex items-center gap-2">
                  <input type="checkbox" checked={useCache} onChange={e => setUseCache(e.target.checked)} />
                  Use cache
                </label>
                <button
                  type="button"
                  onClick={() => { cache.clear(); setCacheHit(false); }}
                  className="ml-3 px-2 py-1 border border-[#d4d4d4] rounded text-xs hover:bg-[#f5f5f5]"
                >
                  Clear cache
                </button>
              </div>
            </section>
            )}

            {/* Sampler view: full-width result tabs (heatmap, table,
                diagnostics, compare, export). Mounted only when viewMode
                === 'sampler' so the heatmap gets the entire page width.
                The "Re-sample" button avoids round-tripping back to Edit. */}
            {viewMode === 'sampler' && (
            <section className="bg-white border border-[#e5e5e5] rounded-xl p-4">
              <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
                <div className="flex gap-2">
                  {(['heatmap', 'table', 'diagnostics', 'compare', 'export'] as Tab[]).map(t => (
                    <button key={t} onClick={() => setTab(t)} className={`px-3 py-1.5 text-xs rounded-lg border ${tab === t ? 'bg-[#0a0a0a] text-white border-[#0a0a0a]' : 'bg-white text-[#525252] border-[#d4d4d4]'}`}>
                      {t === 'heatmap' ? 'Sampler' : t[0].toUpperCase() + t.slice(1)}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  disabled={loading || estimatedPoints > maxPoints || !!strikeGridError}
                  onClick={() => runSample({ outputMode: outputMode === 'SmileSlice' ? 'SmileSlice' : 'Cube' })}
                  className="px-3 py-1.5 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] disabled:opacity-60"
                  title="Re-run the sampler with the current inputs and sampling grid"
                >
                  🔁 Re-sample
                </button>
              </div>

              {!sample && (
                <PanelPlaceholder
                  compact
                  icon={surfaceUi.icon}
                  title="No sample yet"
                  description={getInstructionText('the query', 'Sample')}
                />
              )}

              {sample && tab === 'heatmap' && (
                <div className="space-y-3">
                  {heatmap && (
                    <>
                      <div className="flex items-center gap-3 flex-wrap">
                        {surfaceType !== 'EquityBlack' && (
                          <>
                            <label className="text-xs text-[#737373]">Strike</label>
                            <input type="range" min={0} max={Math.max((sample.strikes?.length || 1) - 1, 0)} value={selectedStrikeIdx} onChange={e => setSelectedStrikeIdx(Number(e.target.value))} />
                            <span className="text-xs font-mono">{formatStrikeAxisLabel(sample.strikes?.[selectedStrikeIdx], strikeAxis, surfaceType)}</span>
                          </>
                        )}
                        <label className="text-xs text-[#737373]">Probe expiry</label>
                        <input type="range" min={0} max={Math.max((sample.n_expiries || 1) - 1, 0)} value={probeExpiryIdx} onChange={e => setProbeExpiryIdx(Number(e.target.value))} />
                        {surfaceType !== 'EquityBlack' && (
                          <>
                            <label className="text-xs text-[#737373]">Probe tenor</label>
                            <input type="range" min={0} max={Math.max((sample.n_tenors || 1) - 1, 0)} value={probeTenorIdx} onChange={e => setProbeTenorIdx(Number(e.target.value))} />
                          </>
                        )}
                        <label className="text-xs text-[#737373]">Display units</label>
                        <select
                          className="px-2 py-1 text-xs border border-[#d4d4d4] rounded-lg bg-white"
                          value={displayUnits}
                          onChange={e => setDisplayUnits(e.target.value as DisplayUnits)}
                        >
                          {surfaceType === 'EquityBlack' ? (
                            <option value="pct">%</option>
                          ) : (
                            <option value="bps">bps</option>
                          )}
                          <option value="decimal">decimal</option>
                        </select>
                        <label className="text-xs text-[#525252] flex items-center gap-2">
                          <input type="checkbox" checked={showSmallMultiples} onChange={e => setShowSmallMultiples(e.target.checked)} />
                          Small multiples
                        </label>
                      </div>
                      {sampleVolStats && (
                        <div className="text-xs text-[#737373] border border-[#e5e5e5] rounded-lg p-2 bg-[#fafafa]">
                          min: <span className="font-mono">{formatVolBoth(sampleVolStats.min, surfaceType)}</span>
                          <span className="mx-2">|</span>
                          max: <span className="font-mono">{formatVolBoth(sampleVolStats.max, surfaceType)}</span>
                          <span className="mx-2">|</span>
                          spread: <span className="font-mono">{surfaceType === 'EquityBlack' ? `${(sampleVolStats.spread * 100).toFixed(3)}%` : `${(sampleVolStats.spread * 10000).toFixed(1)} bps`}</span>
                        </div>
                      )}
                      {!showSmallMultiples ? (
                        <HeatmapTable
                          rows={heatmap.rows}
                          cols={heatmap.cols}
                          values={heatmap.values}
                          displayUnits={displayUnits}
                          colAxisLabel={surfaceType === 'EquityBlack' ? 'Strike' : 'Tenor'}
                          surfaceType={surfaceType}
                          colorScale={smallMultiplesScale || undefined}
                          selectedRowIdx={probeExpiryIdx}
                          selectedColIdx={surfaceType === 'EquityBlack' ? selectedStrikeIdx : probeTenorIdx}
                          onCellClick={(i, j) => {
                            if (surfaceType === 'EquityBlack') {
                              setProbeExpiryIdx(i);
                              setSelectedStrikeIdx(j);
                            } else {
                              probeCell(i, j);
                            }
                          }}
                        />
                      ) : (
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                          {(sample.strikes || []).slice(smallMultiplesWindow.start, smallMultiplesWindow.end + 1).map((_, localIdx) => {
                            const idx = localIdx + smallMultiplesWindow.start;
                            const hm = toHeatmap(sample, idx, surfaceType === 'EquityBlack' ? equityStrikeFallback : [], surfaceType === 'EquityBlack');
                            return (
                              <div key={idx} className="border border-[#e5e5e5] rounded-lg p-2">
                                <div className="text-xs font-mono mb-1">{formatStrikeAxisLabel(sample.strikes?.[idx], strikeAxis, surfaceType)}</div>
                                <HeatmapTable
                                  rows={hm.rows}
                                  cols={hm.cols}
                                  values={hm.values}
                                  displayUnits={displayUnits}
                                  colAxisLabel={surfaceType === 'EquityBlack' ? 'Strike' : 'Tenor'}
                                  surfaceType={surfaceType}
                                  colorScale={smallMultiplesScale || undefined}
                                  selectedRowIdx={probeExpiryIdx}
                                  selectedColIdx={surfaceType === 'EquityBlack' ? selectedStrikeIdx : probeTenorIdx}
                                  onCellClick={(i, j) => {
                                    if (surfaceType === 'EquityBlack') {
                                      setProbeExpiryIdx(i);
                                      setSelectedStrikeIdx(j);
                                    } else {
                                      probeCell(i, j);
                                    }
                                  }}
                                />
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {surfaceType === 'EquityBlack' ? (
                        <EquitySurfaceViewer
                          sample={sample}
                          displayUnits={displayUnits}
                          asOfDate={asOfDate}
                          strikeFallback={equityStrikeFallback}
                          sliceMode={sliceMode}
                          probeExpiryIdx={probeExpiryIdx}
                          probeStrikeIdx={selectedStrikeIdx}
                          onProbeChange={(i, k) => {
                            setProbeExpiryIdx(i);
                            setSelectedStrikeIdx(k);
                            setSliceExpiryIndex(i);
                          }}
                        />
                      ) : (
                        <VolCubeViewer
                          sample={sample}
                          selectedStrikeIdx={selectedStrikeIdx}
                          probeExpiryIdx={probeExpiryIdx}
                          probeTenorIdx={probeTenorIdx}
                          displayUnits={displayUnits}
                          strikeAxis={strikeAxis}
                          sliceMode={sliceMode}
                          asOfDate={asOfDate}
                          onProbeChange={(i, j) => {
                            setProbeExpiryIdx(i);
                            setProbeTenorIdx(j);
                            setSliceExpiryIndex(i);
                            setSliceTenorIndex(j);
                          }}
                        />
                      )}
                      <div className="border border-[#e5e5e5] rounded-lg p-3 bg-[#fafafa] space-y-2">
                        <div className="flex items-center gap-3">
                          <label className="text-xs text-[#737373]">Slice mode</label>
                          <select className="px-2 py-1 text-xs border border-[#d4d4d4] rounded-lg bg-white" value={sliceMode} onChange={e => setSliceMode(e.target.value as SliceMode)}>
                            <option value="Smile">Smile (vol vs strike)</option>
                            <option value="Term">{surfaceType === 'EquityBlack' ? 'Term structure (vol vs expiry)' : 'Term slice (vol vs tenor)'}</option>
                            {surfaceType !== 'EquityBlack' && (
                              <option value="Expiry">Expiry slice (vol vs expiry)</option>
                            )}
                          </select>
                          <label className="text-xs text-[#525252] flex items-center gap-2">
                            <input type="checkbox" checked={lockSliceRangeToGlobal} onChange={e => setLockSliceRangeToGlobal(e.target.checked)} />
                            Lock slice y-range to global cube min/max
                          </label>
                        </div>
                        {sliceStats && (
                          <div className="text-xs text-[#737373]">
                            slice min/max/spread: <span className="font-mono">{formatVolBoth(sliceStats.min, surfaceType)} / {formatVolBoth(sliceStats.max, surfaceType)} / {surfaceType === 'EquityBlack' ? `${(sliceStats.spread * 100).toFixed(3)}%` : `${(sliceStats.spread * 10000).toFixed(1)} bps`}</span>
                          </div>
                        )}
                        <LocalSlicePlot
                          title={unifiedSlice.title}
                          x={unifiedSlice.x}
                          y={unifiedSlice.y}
                          displayUnits={displayUnits}
                          lockToGlobalRange={lockSliceRangeToGlobal}
                          globalRange={sampleVolStats ? { min: sampleVolStats.min, max: sampleVolStats.max } : null}
                        />
                      </div>
                      <p className="text-xs text-[#737373]">
                        {surfaceType === 'EquityBlack'
                          ? 'Probe mode: click a heatmap cell to move the equity surface probe.'
                          : 'Probe mode: click a heatmap cell to move 3D probe lines and update the slice instantly.'}
                      </p>
                    </>
                  )}
                </div>
              )}

              {sample && tab === 'table' && (
                <div className="space-y-3">
                  {heatmap && (
                    <HeatmapTable
                      rows={heatmap.rows}
                      cols={heatmap.cols}
                      values={heatmap.values}
                      displayUnits={displayUnits}
                      colAxisLabel={surfaceType === 'EquityBlack' ? 'Strike' : 'Tenor'}
                      surfaceType={surfaceType}
                      selectedRowIdx={probeExpiryIdx}
                      selectedColIdx={surfaceType === 'EquityBlack' ? selectedStrikeIdx : probeTenorIdx}
                      onCellClick={(i, j) => {
                        if (surfaceType === 'EquityBlack') {
                          setProbeExpiryIdx(i);
                          setSelectedStrikeIdx(j);
                        } else {
                          probeCell(i, j);
                        }
                      }}
                    />
                  )}
                  <div className="flex gap-2">
                    <button
                      onClick={async () => {
                        const text = toCsv(sample);
                        await navigator.clipboard.writeText(text);
                      }}
                      className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]"
                    >
                      Copy CSV
                    </button>
                  </div>
                  <pre className="p-3 bg-[#fafafa] border border-[#e5e5e5] rounded-lg text-[11px] overflow-auto max-h-[420px]">{toCsv(sample)}</pre>
                </div>
              )}

              {sample && tab === 'diagnostics' && (
                <div className="space-y-3 text-xs">
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                    <Diag label="ql_vol_type" value={sample.ql_vol_type} />
                    <Diag label="canonical_strike_kind" value={sample.canonical_strike_kind} />
                    <Diag label="requested_strike_axis" value={sample.requested_strike_axis} />
                    <Diag label="allow_extrapolation_used" value={String(sample.allow_extrapolation_used)} />
                    <Diag label="calendar_used" value={sample.calendar_used} />
                    <Diag label="bdc_used" value={sample.business_day_convention_used} />
                    <Diag label="expiry_kind" value={sample.expiry_kind} />
                    <Diag label="n_expiries" value={String(sample.n_expiries ?? 0)} />
                    <Diag label="n_tenors" value={String(sample.n_tenors ?? 0)} />
                    <Diag label="n_strikes" value={String(sample.n_strikes ?? 0)} />
                    <Diag label="x_axis_kind" value="tenorYears" />
                    <Diag label="y_axis_kind" value="expiryYearFraction" />
                    <Diag label="x_strictly_increasing" value={cubeGeometry ? String(cubeGeometry.xStrictlyIncreasing) : '—'} />
                    <Diag label="y_strictly_increasing" value={cubeGeometry ? String(cubeGeometry.yStrictlyIncreasing) : '—'} />
                    <Diag label="y_all_zero" value={cubeGeometry ? String(cubeGeometry.yAllZero) : '—'} />
                    <Diag label="y_has_duplicates" value={cubeGeometry ? String(cubeGeometry.yHasDuplicates) : '—'} />
                  </div>
                  {cubeGeometry && (cubeGeometry.yAllZero || !cubeGeometry.yStrictlyIncreasing) && (
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-red-700">
                      Expiry axis parsing failed (likely ISO date parsing) or produced non-increasing values.
                    </div>
                  )}
                  {cubeGeometry && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      <Diag label="x_values" value={cubeGeometry.x.map(v => v.toFixed(6)).join(', ')} />
                      <Diag label="y_values" value={cubeGeometry.y.map(v => v.toFixed(6)).join(', ')} />
                    </div>
                  )}
                  {sample.error?.error_message && (
                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-red-700">{sample.error.error_message}</div>
                  )}
                  <div className="overflow-auto border border-[#e5e5e5] rounded-lg">
                    <table className="w-full text-xs">
                      <thead className="bg-[#fafafa]"><tr><th className="px-2 py-1 text-left">requested_expiry_grid_points</th><th className="px-2 py-1 text-left">expiries</th></tr></thead>
                      <tbody>
                        {(sample.requested_expiry_grid_points || []).map((x, i) => (
                          <tr key={i} className="border-t border-[#f5f5f5]"><td className="px-2 py-1">{x}</td><td className="px-2 py-1">{sample.expiries?.[i] || ''}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {sample && tab === 'compare' && (
                <div className="space-y-3">
                  <div className="flex gap-2">
                    <button onClick={() => setBaseline(sample)} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Set current as baseline</button>
                  </div>
                  {!baseline && <p className="text-xs text-[#737373]">Set a baseline, then sample again to see delta heatmap.</p>}
                  {baseline && !canCompare && <p className="text-xs text-red-600">Baseline/current dimensions differ; cannot compare heatmap.</p>}
                  {baseline && canCompare && heatmap && baselineHeatmap && (
                    <DeltaHeatmap current={heatmap.values} base={baselineHeatmap.values} />
                  )}
                </div>
              )}

              {sample && tab === 'export' && (
                <div className="space-y-3">
                  <div className="flex gap-2">
                    <button onClick={exportJson} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Export JSON</button>
                    <button onClick={exportCsv} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Export CSV</button>
                    <button onClick={exportStrikeLayerCsv} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Export strike layer CSV</button>
                    <button onClick={exportSliceCsv} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Export slice CSV</button>
                  </div>
                  {lastRequest && (
                    <pre className="p-3 bg-[#fafafa] border border-[#e5e5e5] rounded-lg text-[11px] overflow-auto max-h-[220px]">{JSON.stringify(lastRequest, null, 2)}</pre>
                  )}
                  <pre className="p-3 bg-[#fafafa] border border-[#e5e5e5] rounded-lg text-[11px] overflow-auto max-h-[350px]">{JSON.stringify(sample, null, 2)}</pre>
                </div>
              )}
            </section>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

function PeriodGridEditor({
  rows,
  setRows,
  newN,
  setNewN,
  newUnit,
  setNewUnit,
}: {
  rows: Period[];
  setRows: (rows: Period[]) => void;
  newN: number;
  setNewN: (n: number) => void;
  newUnit: TimeUnit;
  setNewUnit: (u: TimeUnit) => void;
}) {
  const add = () => {
    if (!Number.isFinite(newN) || newN <= 0) return;
    const next = [...rows, { n: Math.floor(newN), unit: newUnit }];
    const uniq = Array.from(new Map(next.map(p => [`${p.n}-${p.unit}`, p])).values());
    uniq.sort((a, b) => periodToYears(a) - periodToYears(b));
    setRows(uniq);
  };
  const remove = (idx: number) => setRows(rows.filter((_, i) => i !== idx));

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-2">
        <input type="number" min={1} className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm" value={newN} onChange={e => setNewN(Number(e.target.value) || 1)} />
        <select className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm" value={newUnit} onChange={e => setNewUnit(e.target.value as TimeUnit)}>
          {PERIOD_UNITS.map(u => <option key={u} value={u}>{u}</option>)}
        </select>
        <button type="button" onClick={add} className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5]">Add</button>
      </div>
      <div className="space-y-1 max-h-28 overflow-auto border border-[#e5e5e5] rounded-lg p-2 bg-[#fafafa]">
        {rows.map((p, i) => (
          <div key={`${p.n}-${p.unit}-${i}`} className="flex items-center justify-between text-xs">
            <span className="font-mono">{periodLabel(p)}</span>
            <button type="button" className="p-1 text-[#737373] hover:text-red-500 hover:bg-red-50 rounded" onClick={() => remove(i)} title="Remove">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        ))}
        {rows.length === 0 && <div className="text-xs text-[#a3a3a3]">No rows</div>}
      </div>
    </div>
  );
}

/**
 * Surface-axes editor: a thin wrapper around `PeriodGridEditor` that writes
 * directly to `surface.axes_expiries` and `surface.axes_tenors`. These are
 * the axes that drive the matrix editor's dimensions AND what
 * buildSwaptionVolWirePayload emits — keeping them paired in one block makes
 * the relationship visually obvious.
 */
function SurfaceAxesEditor({
  surface,
  onChange,
  title,
  helperText,
}: {
  surface: VolSurfaceSpec;
  onChange: (updater: (s: VolSurfaceSpec) => VolSurfaceSpec) => void;
  title: string;
  helperText: string;
}) {
  const [expiryN, setExpiryN] = useState(1);
  const [expiryUnit, setExpiryUnit] = useState<TimeUnit>('Years');
  const [tenorN, setTenorN] = useState(5);
  const [tenorUnit, setTenorUnit] = useState<TimeUnit>('Years');
  const expiries = surface.axes_expiries || [];
  const tenors = surface.axes_tenors || [];
  return (
    <div className="border border-[#d4d4d4] rounded-lg p-3 space-y-2 bg-white">
      <div>
        <h3 className="text-xs font-semibold text-[#0a0a0a]">{title}</h3>
        <p className="text-[11px] text-[#a3a3a3]">{helperText}</p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-[11px] font-medium text-[#525252] mb-1">Surface expiries</label>
          <PeriodGridEditor
            rows={expiries}
            setRows={(next) => onChange((s) => ({ ...s, axes_expiries: next }))}
            newN={expiryN}
            setNewN={setExpiryN}
            newUnit={expiryUnit}
            setNewUnit={setExpiryUnit}
          />
        </div>
        <div>
          <label className="block text-[11px] font-medium text-[#525252] mb-1">Surface tenors</label>
          <PeriodGridEditor
            rows={tenors}
            setRows={(next) => onChange((s) => ({ ...s, axes_tenors: next }))}
            newN={tenorN}
            setNewN={setTenorN}
            newUnit={tenorUnit}
            setNewUnit={setTenorUnit}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * SABR-calibrate editor block: market-vol cube editor + calibration options
 * panel + "Calibrate now" / "Save as SabrParams" actions.
 *
 * Layout mirrors `SabrParamsEditorBlock`:
 *   - SurfaceAxesEditor (shared) for axes_expiries / axes_tenors.
 *   - StrikeAxisEditor (thin wrapper around the existing StrikeGridEditor)
 *     for surface.axes_strikes — strike spreads from per-node ATM forward
 *     (rate units, e.g. -0.02 = -200bp), matching the backend
 *     SwaptionSabrCalibrateSpec strike-axis interpretation.
 *   - Tabbed Matrix2DEditor where each tab is a strike level; rows × cols =
 *     expiries × tenors. Reuses the same component as SabrParams; cells
 *     accept literal numbers or quote references with the same per-cell
 *     validation pattern (must be > 0, finite).
 *   - Calibration options: beta_fixed checkbox, beta_value numeric (only
 *     editable when fixed), vega_weighted_smile_fit toggle. NO weights[]
 *     vector — the engine rejects non-empty weights.
 *   - Calibrate-now button + diagnostics display + Save-as-SabrParams.
 */
function SabrCalibrateEditorBlock({
  surface,
  onChange,
  quoteIdOptions,
  swapIndexId,
  discountingCurveId,
  forwardingCurveId,
  curveSet,
  resolvedCurves,
  floatIndexId,
  asOfDate,
  sampleErrorHint,
  onCalibrationSaved,
}: {
  surface: VolSurfaceSpec;
  onChange: (updater: (s: VolSurfaceSpec) => VolSurfaceSpec) => void;
  quoteIdOptions: string[];
  swapIndexId: string;
  discountingCurveId: string;
  forwardingCurveId: string;
  curveSet: CurveSet | null;
  resolvedCurves: Curve[];
  floatIndexId: string;
  asOfDate: string;
  sampleErrorHint: string | null;
  onCalibrationSaved: (newSurface: VolSurfaceSpec) => void;
}) {
  const expiries = surface.axes_expiries || [];
  const tenors = surface.axes_tenors || [];
  const strikes = surface.axes_strikes || [];
  const nE = expiries.length;
  const nT = tenors.length;
  const nS = strikes.length;
  const opts = surface.sabr_calibration_options || {};
  const cube = (surface.sabr_market_vols || []) as MatrixCell[][][];
  const [newStrike, setNewStrike] = useState(0);
  const [calibrating, setCalibrating] = useState(false);
  const [calibrateError, setCalibrateError] = useState<string | null>(null);
  // Local mirror of the cached diagnostics so the just-completed calibrate
  // result renders immediately without needing the parent to round-trip a
  // new VolSurfaceSpec read.
  const [diagnostics, setDiagnostics] = useState<SwaptionVolDiagnostics | null>(() => {
    const cached = surface.sabr_last_calibration;
    if (!cached) return null;
    return {
      vol_id: surface.id,
      kind: 'SabrCalibrate',
      n_expiries: cached.n_expiries,
      n_tenors: cached.n_tenors,
      alpha_per_node: cached.alpha_per_node,
      beta_per_node: cached.beta_per_node,
      rho_per_node: cached.rho_per_node,
      nu_per_node: cached.nu_per_node,
      forward_per_node: cached.forward_per_node,
      atm_vol_per_node: cached.atm_vol_per_node,
      time_to_expiry_per_node: cached.time_to_expiry_per_node,
      calibration: {
        per_node_rmse: cached.per_node_rmse,
        per_node_max_abs_error: cached.per_node_max_abs_error,
        overall_rmse: cached.overall_rmse,
        converged: cached.converged,
        strikes: cached.strikes,
        per_strike_fit_error: cached.per_strike_fit_error,
      },
      warnings: cached.warnings,
    } as SwaptionVolDiagnostics;
  });
  // Reset the local diagnostics view when the user opens a different surface
  // so we don't show stale α/ρ/ν grids from the previous SABR-calibrate
  // surface mid-edit.
  useEffect(() => {
    const cached = surface.sabr_last_calibration;
    if (!cached) {
      setDiagnostics(null);
      return;
    }
    setDiagnostics({
      vol_id: surface.id,
      kind: 'SabrCalibrate',
      n_expiries: cached.n_expiries,
      n_tenors: cached.n_tenors,
      alpha_per_node: cached.alpha_per_node,
      beta_per_node: cached.beta_per_node,
      rho_per_node: cached.rho_per_node,
      nu_per_node: cached.nu_per_node,
      forward_per_node: cached.forward_per_node,
      atm_vol_per_node: cached.atm_vol_per_node,
      time_to_expiry_per_node: cached.time_to_expiry_per_node,
      calibration: {
        per_node_rmse: cached.per_node_rmse,
        per_node_max_abs_error: cached.per_node_max_abs_error,
        overall_rmse: cached.overall_rmse,
        converged: cached.converged,
        strikes: cached.strikes,
        per_strike_fit_error: cached.per_strike_fit_error,
      },
      warnings: cached.warnings,
    } as SwaptionVolDiagnostics);
  }, [surface.id]);

  // The vols editor renders the cube as ONE flat table: rows = (expiry,
  // tenor) calibration nodes in row-major order, cols = strike spreads. A
  // whole broker-style smile table therefore pastes in a single go.
  const writeNodesMatrix = (next: MatrixCell[][]) => {
    onChange((s) => ({ ...s, sabr_market_vols: nodesMatrixToCube(next, nE, nT, nS) }));
  };

  const writeStrikeAxis = (next: number[]) => {
    const sorted = normalizeStrikeGrid(next);
    onChange((s) => {
      const oldStrikes = s.axes_strikes || [];
      const old = (s.sabr_market_vols || []) as MatrixCell[][][];
      // Map old strike index to new strike index by exact-value match (after
      // normalize). Cells whose strike is dropped are forgotten; new strikes
      // get NaN literals. This way deleting/adding a strike doesn't corrupt
      // the rest of the cube.
      const strikeIdxLookup = sorted.map((s2) =>
        oldStrikes.findIndex((o) => Math.abs(o - s2) < 1e-12)
      );
      const newCube: MatrixCell[][][] = [];
      for (let i = 0; i < nE; i += 1) {
        const eRow: MatrixCell[][] = [];
        for (let j = 0; j < nT; j += 1) {
          const tRow: MatrixCell[] = [];
          for (let k = 0; k < sorted.length; k += 1) {
            const oldK = strikeIdxLookup[k];
            tRow.push(oldK >= 0 ? (old?.[i]?.[j]?.[oldK] ?? (NaN as MatrixCell)) : (NaN as MatrixCell));
          }
          eRow.push(tRow);
        }
        newCube.push(eRow);
      }
      return { ...s, axes_strikes: sorted, sabr_market_vols: newCube };
    });
  };

  const updateOptions = (patch: Partial<NonNullable<VolSurfaceSpec['sabr_calibration_options']>>) => {
    onChange((s) => ({
      ...s,
      sabr_calibration_options: {
        ...(s.sabr_calibration_options || {}),
        ...patch,
      },
    }));
  };

  // Project the cube onto the flat nodes-by-strikes matrix for the editor.
  // `null` cells (NaN literals that went through a JSON save/reload round
  // trip) are coerced back to NaN so they render as empty inputs.
  const nodesMatrix: MatrixCell[][] = useMemo(
    () => cubeToNodesMatrix(cube, nE, nT, nS),
    [cube, nE, nT, nS],
  );
  const nodeRowLabels = useMemo(() => {
    const out: string[] = [];
    for (let i = 0; i < nE; i += 1) {
      for (let j = 0; j < nT; j += 1) {
        out.push(`${periodLabel(expiries[i])} x ${periodLabel(tenors[j])}`);
      }
    }
    return out;
  }, [expiries, tenors, nE, nT]);

  const validateMarketVolCell = (cell: MatrixCell) => {
    if (cell && typeof cell === 'object' && 'quoteId' in cell) {
      return cell.quoteId.trim().length === 0 ? 'quote id required' : null;
    }
    if (typeof cell !== 'number') return null;
    if (!Number.isFinite(cell)) return 'must be a finite number';
    if (!(cell > 0)) return 'σ must be > 0';
    return null;
  };

  const formatStrikeTab = (s: number) => {
    const bp = s * 10000;
    if (Math.abs(bp) < 0.5) return 'ATM';
    return `${bp >= 0 ? '+' : ''}${bp.toFixed(0)} bp`;
  };

  // ---------- Calibrate-now ----------
  const handleCalibrateNow = async () => {
    setCalibrateError(null);
    setCalibrating(true);
    try {
      // Pre-flight UI guards mirror the wire helper's parse-time rejections
      // so we surface a clear message before kicking off a spinner.
      if (!swapIndexId) throw new Error('Pick a Swap Index ID first.');
      if (!discountingCurveId) throw new Error('Pick a Discounting Curve first.');
      if (!forwardingCurveId) throw new Error('Pick a Forwarding Curve first.');
      if (!curveSet) throw new Error('Pick a Curve Set first.');
      // Build the wire vol-surface payload via the shared helper. This
      // throws VolSurfacePayloadError on any pre-flight rejection (Normal
      // vol type, OIS swap-index, dimension mismatch, non-positive vols)
      // — surface those messages directly.
      const bookById = new Map(getQuoteBook().map((q) => [q.id, q]));
      const flatById = new Map(getLegacyFlatQuotes().map((q) => [q.id, q]));
      const mode = getResolutionMode();
      const resolveQuoteValueById = (id?: string): number | null => {
        if (!id) return null;
        const b = bookById.get(id);
        if (b) {
          const val = resolveQuoteValue(b.series, asOfDate, mode);
          if (val !== null) return val;
        }
        const q = flatById.get(id);
        if (q && Number.isFinite(q.value)) return q.value;
        return null;
      };
      const volSurfacePayload = buildSwaptionVolWirePayload(
        surface,
        swapIndexId,
        surface.base?.reference_date || asOfDate,
        resolveQuoteValueById,
      );
      const yieldRateCurves = resolvedCurves.filter(
        (c) => c.role !== 'inflation' && Array.isArray(c.points) && c.points.length > 0,
      );
      const curves = yieldRateCurves.map((c) =>
        normalizeCurveForApi({
          id: c.id,
          day_counter: c.day_counter,
          interpolator: c.interpolator,
          bootstrap_trait: c.bootstrap_trait,
          reference_date: c.reference_date || asOfDate,
          points: c.points,
        }),
      );
      if (curves.length === 0) {
        throw new Error('Curve set has no usable yield curves.');
      }
      const indexIds = collectIndexRefIds(resolvedCurves.flatMap((c) => c.points || []));
      indexIds.push(floatIndexId);
      const uniqueIndexIds = Array.from(new Set(indexIds.filter(Boolean)));
      const storedIndices = await indexStore.getAll();
      const resolvedIndices: IndexDef[] = storedIndices
        .filter((i) => uniqueIndexIds.includes(i.id))
        .map((i) => storedToRateIndexDef(i))
        .filter((idx): idx is IndexDef => !!idx);
      const idxRecord = resolvedIndices.find((i) => i.id === floatIndexId);
      const idxIsOvernight = idxRecord?.index_type === 'Overnight';
      // The OIS rejection lives in two places: (a) buildSwaptionVolWirePayload
      // sniffs the swap-index id's `_OIS` suffix; (b) here we also check the
      // underlying float index because a user could have manually overridden
      // the swap-index id. Keep both — the editor message points at the float
      // index (more actionable for a UI fix), the helper guards the wire.
      if (idxIsOvernight) {
        throw new Error(
          `SabrCalibrate cannot calibrate against an Overnight (OIS) float index "${floatIndexId}". Switch the Float Index to an IBOR-family index (e.g. EURIBOR_6M).`
        );
      }
      const swapIndexDef = [{
        id: swapIndexId,
        kind: 'IborSwapIndex',
        spot_days: idxRecord?.fixing_days ?? 2,
        calendar: idxRecord?.calendar || 'TARGET',
        business_day_convention: idxRecord?.business_day_convention || 'ModifiedFollowing',
        end_of_month: false,
        float_index_id: floatIndexId,
        fixed_leg: {
          fixed_frequency: 'Annual',
          fixed_day_counter: 'Thirty360',
          fixed_calendar: idxRecord?.calendar || 'TARGET',
          fixed_bdc: idxRecord?.business_day_convention || 'ModifiedFollowing',
          fixed_term_bdc: idxRecord?.business_day_convention || 'ModifiedFollowing',
          fixed_date_rule: 'Forward',
          fixed_eom: false,
        },
        float_leg: {
          float_tenor: { n: idxRecord?.tenor?.n || 6, unit: idxRecord?.tenor?.unit || 'Months' },
          float_calendar: idxRecord?.calendar || 'TARGET',
          float_bdc: idxRecord?.business_day_convention || 'ModifiedFollowing',
          float_term_bdc: idxRecord?.business_day_convention || 'ModifiedFollowing',
          float_date_rule: 'Forward',
          float_eom: false,
        },
      }];
      const referencedQuoteIds = Array.from(new Set(
        resolvedCurves
          .flatMap((c) => c.points || [])
          .map((p: any) => p?.point?.quote_id)
          .filter((id: any): id is string => typeof id === 'string' && id.length > 0),
      ));
      const quotes = referencedQuoteIds
        .map((id) => {
          const b = bookById.get(id);
          if (b) {
            const val = resolveQuoteValue(b.series, asOfDate, mode);
            if (val !== null) {
              return {
                id,
                kind: b.kind || 'Rate',
                value: val,
                ...(b.quote_type ? { quote_type: b.quote_type } : {}),
              };
            }
          }
          const q = flatById.get(id);
          if (q && Number.isFinite(q.value)) {
            return {
              id,
              kind: q.kind || 'Rate',
              value: q.value,
              ...(q.quote_type ? { quote_type: q.quote_type } : {}),
            };
          }
          return null;
        })
        .filter((q) => q !== null) as Array<{ id: string; kind: string; value: number; quote_type?: any }>;
      const pricingAsOfDate = surface.base?.reference_date || asOfDate;
      const request = {
        pricing: {
          as_of_date: pricingAsOfDate,
          curves,
          indices: resolvedIndices.map(normalizeIndexDefForApi),
          swap_indices: swapIndexDef,
          ...(quotes.length > 0 ? { quotes } : {}),
          vol_surfaces: [volSurfacePayload],
        },
        vol_id: surface.id,
        discounting_curve_id: discountingCurveId,
        forwarding_curve_id: forwardingCurveId,
      };
      const resp = await orchestratorPost<CalibrateSwaptionVolResponse>('/v1/calibrate-swaption-vol', request);
      if (!resp.ok || !resp.data) {
        throw new Error(resp.ok ? 'Calibration failed' : resp.envelope.error);
      }
      const diag = resp.data.diagnostics;
      setDiagnostics(diag);
      // Cache the diagnostics on the surface so reopening it later shows the
      // last calibration without firing the endpoint. Mirrors the backend
      // diagnostics field names so the cache is byte-for-byte the response.
      onChange((s) => ({
        ...s,
        sabr_last_calibration: {
          cached_at: new Date().toISOString(),
          n_expiries: diag.n_expiries ?? nE,
          n_tenors: diag.n_tenors ?? nT,
          alpha_per_node: diag.alpha_per_node || [],
          beta_per_node: diag.beta_per_node || [],
          rho_per_node: diag.rho_per_node || [],
          nu_per_node: diag.nu_per_node || [],
          forward_per_node: diag.forward_per_node,
          atm_vol_per_node: diag.atm_vol_per_node,
          time_to_expiry_per_node: diag.time_to_expiry_per_node,
          per_node_rmse: diag.calibration?.per_node_rmse || [],
          per_node_max_abs_error: diag.calibration?.per_node_max_abs_error,
          overall_rmse: diag.calibration?.overall_rmse ?? 0,
          converged: diag.calibration?.converged,
          strikes: diag.calibration?.strikes,
          per_strike_fit_error: diag.calibration?.per_strike_fit_error,
          warnings: diag.warnings,
        },
      }));
    } catch (e) {
      // The /calibrate-swaption-vol endpoint wraps QuantLib's QL_FAIL text
      // into a generic gRPC ABORTED ("QuantLib error") with empty details,
      // so the message is uninformative on its own. /sample-vol-surfaces
      // walks the same calibration code path but propagates the rich text
      // through each query's per-result error field — when the parent's
      // auto-sample produced an error, prefer that as the more informative
      // message and append the raw calibrate error for completeness.
      const raw = e instanceof Error ? e.message : 'Calibration failed';
      const isGenericBackendError = /^(QuantLib error|gRPC error|ABORTED|UNKNOWN)\b/i.test(raw);
      const sampleHintIsRicher = sampleErrorHint && sampleErrorHint.length > raw.length + 20;
      const composed = isGenericBackendError && sampleHintIsRicher
        ? `${sampleErrorHint}\n\n(Backend returned a generic "${raw}" — message above is from the parallel sample request, which exercises the same SABR calibration internally and propagates QuantLib's exception text.)`
        : raw;
      setCalibrateError(composed);
    } finally {
      setCalibrating(false);
    }
  };

  // ---------- Save calibrated parameters as new SabrParams surface ----------
  const handleSaveAsParams = () => {
    if (!diagnostics) return;
    const today = new Date().toISOString().split('T')[0];
    const newId = `${surface.id}_calibrated_${today}`;
    const calNE = diagnostics.n_expiries ?? nE;
    const calNT = diagnostics.n_tenors ?? nT;
    // Reshape the per-node flat arrays into (nE × nT) MatrixCell grids. The
    // diagnostics block uses row-major (nExp × nTen) indexing per the
    // SwaptionVolDiagnostics schema.
    const reshape = (flat?: number[]): MatrixCell[][] => {
      const out: MatrixCell[][] = [];
      for (let i = 0; i < calNE; i += 1) {
        const row: MatrixCell[] = [];
        for (let j = 0; j < calNT; j += 1) {
          const v = flat?.[i * calNT + j];
          row.push(Number.isFinite(v) ? (v as MatrixCell) : (NaN as MatrixCell));
        }
        out.push(row);
      }
      return out;
    };
    // Use the diagnostics block's own axes (which equal the surface axes
    // post-finalize) so the new SabrParams surface is dimensionally
    // consistent with the calibrated parameter grids even if the user
    // edited the surface axes after calibration.
    const newAxesExpiries = diagnostics.expiries?.map((p) => ({ n: p.n, unit: p.unit as TimeUnit })) || expiries;
    const newAxesTenors = diagnostics.tenors?.map((p) => ({ n: p.n, unit: p.unit as TimeUnit })) || tenors;
    const newSurface: VolSurfaceSpec = {
      id: newId,
      payload_type: 'SwaptionVolSpec',
      base: {
        ...(surface.base || {}),
        shape: 'SabrParams',
        // SabrParams is lognormal-only; mirror SabrCalibrate's existing
        // (already-coerced) volatility_type. ShiftedLognormal carries
        // displacement; copy the value through too.
        volatility_type: surface.base?.volatility_type === 'ShiftedLognormal' ? 'ShiftedLognormal' : 'Lognormal',
      },
      axes_expiries: newAxesExpiries,
      axes_tenors: newAxesTenors,
      sabr_alpha: reshape(diagnostics.alpha_per_node),
      sabr_beta: reshape(diagnostics.beta_per_node),
      sabr_rho: reshape(diagnostics.rho_per_node),
      sabr_nu: reshape(diagnostics.nu_per_node),
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    onCalibrationSaved(newSurface);
  };

  // ---------- Diagnostics UI helpers ----------
  const cal = diagnostics?.calibration;
  // Backend warns at per-node RMSE in (50bp, 100bp) and QL throws above 100bp;
  // mirror that band in the UI. Anything above 50bp is yellow; the backend
  // warnings array fires above 50bp too.
  const RMSE_WARN_BP = 50;
  const RMSE_ERROR_BP = 100;
  const overallRmseBp = (cal?.overall_rmse ?? NaN) * 10000;
  const overallTone = !Number.isFinite(overallRmseBp)
    ? 'neutral'
    : overallRmseBp > RMSE_ERROR_BP
      ? 'error'
      : overallRmseBp > RMSE_WARN_BP
        ? 'warn'
        : 'ok';
  const reshapeForTable = (flat?: number[]): number[][] => {
    const calNE = diagnostics?.n_expiries ?? nE;
    const calNT = diagnostics?.n_tenors ?? nT;
    const out: number[][] = [];
    for (let i = 0; i < calNE; i += 1) {
      const row: number[] = [];
      for (let j = 0; j < calNT; j += 1) row.push(flat?.[i * calNT + j] ?? NaN);
      out.push(row);
    }
    return out;
  };
  // diagnostics.expiries/tenors come back with the NUMERIC FlatBuffers
  // TimeUnit enum (e.g. {n: 1, unit: 8} = 1 Year) — decode to the string
  // unit before labelling, else every node would label as Days.
  const toLocalPeriod = (p: { n: number; unit: number | string }): Period => {
    const d = decodeDiagnosticsPeriod(p);
    return { n: d.n, unit: d.unit as TimeUnit };
  };
  const calRowLabels = (diagnostics?.expiries
    ? diagnostics.expiries.map(toLocalPeriod)
    : expiries).map(periodLabel);
  const calColLabels = (diagnostics?.tenors
    ? diagnostics.tenors.map(toLocalPeriod)
    : tenors).map(periodLabel);

  return (
    <div className="border border-[#e5e5e5] rounded-lg p-3 space-y-3 bg-[#fafafa]">
      <SurfaceAxesEditor
        surface={surface}
        onChange={onChange}
        title="Surface axes — define the SABR calibration grid dimensions"
        helperText="Calibration runs one SABR fit per (expiry, tenor) node. Strikes axis below holds the spreads from per-node ATM forward (rate units, e.g. -0.005 = -50bp)."
      />
      <div className="border border-[#d4d4d4] rounded-lg p-3 space-y-2 bg-white">
        <h3 className="text-xs font-semibold text-[#0a0a0a]">Strike axis (spreads from ATM forward, in basis points)</h3>
        <p className="text-[11px] text-[#a3a3a3]">
          The same spread vector applies at every (expiry, tenor) node. ATM = 0; a conventional broker grid is -200, -100, 0, 100, 200.
        </p>
        <StrikeGridEditor
          strikes={strikes}
          setStrikes={writeStrikeAxis}
          newStrike={newStrike}
          setNewStrike={setNewStrike}
          displayUnit="bp"
        />
      </div>
      {nS > 0 && (
        <div>
          <div className="flex items-baseline justify-between mt-2 mb-2">
            <h3 className="text-xs font-semibold text-[#0a0a0a]">Market vols (lognormal σ)</h3>
            <span className="text-[11px] text-[#737373]">rows = expiry x tenor nodes, cols = strike spreads</span>
          </div>
          <Matrix2DEditor
            value={nodesMatrix}
            onChange={writeNodesMatrix}
            rowLabels={nodeRowLabels}
            colLabels={strikes.map(formatStrikeTab)}
            rowAxisLabel="Node"
            colAxisLabel="Strike"
            quoteIdOptions={quoteIdOptions}
            validateCell={validateMarketVolCell}
            ariaLabel="SABR market vol cube editor (rows are expiry x tenor nodes, cols are strike spreads)"
          />
          <p className="text-[11px] text-[#a3a3a3] mt-2">
            Every cell must be a positive lognormal σ (or a quote reference resolving to one). Paste a whole nodes-by-strikes table straight from a spreadsheet.
          </p>
        </div>
      )}
      <div className="border border-[#d4d4d4] rounded-lg p-3 space-y-2 bg-white">
        <h3 className="text-xs font-semibold text-[#0a0a0a]">Calibration options</h3>
        <div className="grid grid-cols-3 gap-3 items-end">
          <label className="flex items-center gap-2 text-xs text-[#525252] select-none">
            <input
              type="checkbox"
              checked={opts.beta_fixed === true}
              onChange={(e) => updateOptions({ beta_fixed: e.target.checked })}
            />
            Fix β
          </label>
          <div>
            <label className="block text-[11px] font-medium text-[#525252] mb-1">β value (when fixed)</label>
            <input
              type="number"
              step="0.01"
              min="0"
              max="1"
              className="w-full px-2 py-1 bg-white border border-[#d4d4d4] rounded text-sm font-mono disabled:bg-[#f5f5f5] disabled:text-[#a3a3a3]"
              value={opts.beta_value ?? 0.5}
              disabled={opts.beta_fixed !== true}
              onChange={(e) => {
                const v = Number(e.target.value);
                updateOptions({ beta_value: Number.isFinite(v) ? v : 0.5 });
              }}
            />
          </div>
          <label className="flex items-center gap-2 text-xs text-[#525252] select-none">
            <input
              type="checkbox"
              checked={opts.vega_weighted_smile_fit === true}
              onChange={(e) => updateOptions({ vega_weighted_smile_fit: e.target.checked })}
            />
            Vega-weighted smile fit
          </label>
        </div>
        <p className="text-[11px] text-[#a3a3a3]">
          Per-strike weights[] vector is intentionally NOT exposed — the backend rejects non-empty weights (QuantLib 1.41 only exposes vega-weighted fit as a single boolean flag).
        </p>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={handleCalibrateNow}
          disabled={calibrating || nE === 0 || nT === 0 || nS === 0}
          className="px-3 py-1.5 text-xs font-medium bg-[#0a0a0a] text-white rounded hover:bg-[#262626] disabled:opacity-50"
        >
          {calibrating ? 'Calibrating…' : '▶ Calibrate now'}
        </button>
        {diagnostics && (
          <button
            type="button"
            onClick={handleSaveAsParams}
            className="px-3 py-1.5 text-xs font-medium border border-[#d4d4d4] rounded hover:bg-[#f5f5f5]"
            title="Create a new SabrParams surface with the calibrated α/β/ρ/ν grids. The original SabrCalibrate surface stays unchanged."
          >
            Save calibrated parameters as new SabrParams surface
          </button>
        )}
        {diagnostics && surface.sabr_last_calibration?.cached_at && (
          <span className="text-[11px] text-[#737373]">
            Cached at {new Date(surface.sabr_last_calibration.cached_at).toLocaleTimeString()}
          </span>
        )}
      </div>
      {calibrateError && (
        <div className="space-y-1">
          <div className="p-2 text-xs bg-red-50 border border-red-200 rounded text-red-700 whitespace-pre-wrap">{calibrateError}</div>
          {/^(QuantLib error|gRPC error|ABORTED|UNKNOWN)\b/i.test(calibrateError) && (
            <div className="p-2 text-[11px] bg-amber-50 border border-amber-200 rounded text-amber-900">
              <div className="font-semibold mb-1">Common causes for a generic QuantLib error here:</div>
              <ul className="list-disc list-inside space-y-0.5">
                <li>Market σ cube too rough/peaky for SABR to fit within QuantLib&apos;s 15bp RMSE tolerance — try smoothing the smile or reducing per-strike dispersion.</li>
                <li>One or more strikes pushes K = forward + spread below zero (or near zero). For low-rate currencies (EUR), shrink the negative-spread wing or switch the surface to <code>ShiftedLognormal</code>.</li>
                <li>β fixed at an extreme (0 or 1). β = 0.5 is the safest starting point for most rate cubes.</li>
                <li>Calibration node sits at the very end of the curve horizon, leaving QuantLib no extrapolation room. Trim the longest expiry/tenor by 1Y.</li>
                <li>Open the <strong>Sampler</strong> tab — sampling exercises the same SABR fit and prints the failing node + RMSE inline (the calibrate endpoint is unfortunately gRPC-only and loses the verbose text).</li>
              </ul>
            </div>
          )}
        </div>
      )}
      {diagnostics && (
        <div className="border border-[#d4d4d4] rounded-lg p-3 space-y-3 bg-white">
          <div className="flex items-baseline justify-between flex-wrap gap-2">
            <h3 className="text-xs font-semibold text-[#0a0a0a]">Calibration diagnostics</h3>
            <div className="flex items-center gap-3 text-[11px]">
              <span className="text-[#737373]">
                Overall RMSE:{' '}
                <span className={`font-mono font-medium ${overallTone === 'error' ? 'text-red-700' : overallTone === 'warn' ? 'text-amber-700' : 'text-[#0a0a0a]'}`}>
                  {Number.isFinite(overallRmseBp) ? `${overallRmseBp.toFixed(2)} bp` : '—'}
                </span>
              </span>
              {cal?.converged !== undefined && (
                <span className={`px-1.5 py-0.5 rounded ${cal.converged ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-amber-50 text-amber-800 border border-amber-200'}`}>
                  {cal.converged ? 'converged' : 'not converged'}
                </span>
              )}
            </div>
          </div>
          {overallTone === 'warn' && (
            <div className="p-2 text-xs bg-amber-50 border border-amber-200 rounded text-amber-800">
              ⚠ Overall RMSE is between {RMSE_WARN_BP}bp and {RMSE_ERROR_BP}bp — fit quality is degraded. Inspect per-node RMSE to find the offending nodes; consider adjusting β anchoring or the strike grid.
            </div>
          )}
          {overallTone === 'error' && (
            <div className="p-2 text-xs bg-red-50 border border-red-200 rounded text-red-700">
              ✕ Overall RMSE exceeds {RMSE_ERROR_BP}bp — calibration is unreliable. The QuantLib calibrator's internal tolerance is 1%; values near or above that band typically indicate inconsistent market quotes.
            </div>
          )}
          {(diagnostics.warnings || []).length > 0 && (
            <div className="p-2 text-xs bg-amber-50 border border-amber-200 rounded text-amber-800 space-y-0.5">
              {(diagnostics.warnings || []).map((w, i) => (
                <div key={`warn-${i}`}>⚠ {w}</div>
              ))}
            </div>
          )}
          <SabrPerNodeResultsTable
            diagnostics={diagnostics}
            expiryLabels={calRowLabels}
            tenorLabels={calColLabels}
          />
          <div className="grid grid-cols-2 gap-3">
            <CalibratedMatrixView
              title="α (alpha) per node"
              rows={calRowLabels}
              cols={calColLabels}
              values={reshapeForTable(diagnostics.alpha_per_node)}
              precision={6}
            />
            <CalibratedMatrixView
              title="β (beta) per node"
              rows={calRowLabels}
              cols={calColLabels}
              values={reshapeForTable(diagnostics.beta_per_node)}
              precision={4}
            />
            <CalibratedMatrixView
              title="ρ (rho) per node"
              rows={calRowLabels}
              cols={calColLabels}
              values={reshapeForTable(diagnostics.rho_per_node)}
              precision={4}
              colorMode="diverging"
            />
            <CalibratedMatrixView
              title="ν (nu) per node"
              rows={calRowLabels}
              cols={calColLabels}
              values={reshapeForTable(diagnostics.nu_per_node)}
              precision={4}
            />
          </div>
          <CalibratedMatrixView
            title="Per-node fit error (RMSE, bp)"
            rows={calRowLabels}
            cols={calColLabels}
            values={reshapeForTable(cal?.per_node_rmse).map((row) => row.map((v) => v * 10000))}
            precision={2}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Per-node calibration results table for the SabrCalibrate diagnostics
 * block: one row per (expiry, tenor) node with the node's ATM forward, ATM
 * market vol, the four calibrated SABR parameters and the per-node fit RMSE.
 * Forward and ATM vol render in percent; parameters render raw at 4 decimal
 * places; RMSE renders in basis points.
 */
function SabrPerNodeResultsTable({
  diagnostics,
  expiryLabels,
  tenorLabels,
}: {
  diagnostics: SwaptionVolDiagnostics;
  expiryLabels: string[];
  tenorLabels: string[];
}) {
  const calNE = diagnostics.n_expiries ?? expiryLabels.length;
  const calNT = diagnostics.n_tenors ?? tenorLabels.length;
  const at = (arr: number[] | undefined, idx: number): number =>
    typeof arr?.[idx] === 'number' ? (arr[idx] as number) : NaN;
  const fmtPct = (v: number, dp: number) => (Number.isFinite(v) ? `${(v * 100).toFixed(dp)}%` : '-');
  const fmtParam = (v: number) => (Number.isFinite(v) ? v.toFixed(4) : '-');
  const fmtBp = (v: number) => (Number.isFinite(v) ? (v * 10000).toFixed(2) : '-');
  const rows: Array<{ key: string; cells: string[] }> = [];
  for (let i = 0; i < calNE; i += 1) {
    for (let j = 0; j < calNT; j += 1) {
      const n = i * calNT + j;
      rows.push({
        key: `node-${i}-${j}`,
        cells: [
          expiryLabels[i] ?? `${i}`,
          tenorLabels[j] ?? `${j}`,
          fmtPct(at(diagnostics.forward_per_node, n), 3),
          fmtPct(at(diagnostics.atm_vol_per_node, n), 2),
          fmtParam(at(diagnostics.alpha_per_node, n)),
          fmtParam(at(diagnostics.beta_per_node, n)),
          fmtParam(at(diagnostics.rho_per_node, n)),
          fmtParam(at(diagnostics.nu_per_node, n)),
          fmtBp(at(diagnostics.calibration?.per_node_rmse, n)),
        ],
      });
    }
  }
  const headers = ['Expiry', 'Tenor', 'Forward', 'ATM vol', 'α', 'β', 'ρ', 'ν', 'RMSE (bp)'];
  return (
    <div className="space-y-1">
      <div className="text-[11px] font-medium text-[#525252]">Per-node calibrated parameters</div>
      <div className="overflow-auto border border-[#e5e5e5] rounded">
        <table className="text-xs w-full" aria-label="Per-node SABR calibration results">
          <thead className="bg-[#fafafa]">
            <tr>
              {headers.map((h, i) => (
                <th
                  key={`pn-h-${i}`}
                  className={`px-2 py-1 ${i < 2 ? 'text-left' : 'text-right'} text-[#737373] font-medium whitespace-nowrap`}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-[#f5f5f5]">
                {r.cells.map((c, ci) => (
                  <td
                    key={`${r.key}-c-${ci}`}
                    className={`px-2 py-1 font-mono whitespace-nowrap ${ci < 2 ? 'text-left text-[#0a0a0a]' : 'text-right text-[#0a0a0a]'}`}
                  >
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Read-only matrix view used by the SabrCalibrate diagnostics block. Thin
 * wrapper around HeatmapTable that doesn't need the surfaceType plumbing —
 * the per-node calibration values are dimensionless (α, ρ, ν) or in bp
 * (RMSE), not σ values.
 */
function CalibratedMatrixView({
  title,
  rows,
  cols,
  values,
  precision = 4,
  colorMode = 'sequential',
}: {
  title: string;
  rows: string[];
  cols: string[];
  values: number[][];
  precision?: number;
  colorMode?: 'sequential' | 'diverging';
}) {
  const flat = values.flat().filter((v) => Number.isFinite(v));
  const min = flat.length ? Math.min(...flat) : 0;
  const max = flat.length ? Math.max(...flat) : 1;
  const span = (max - min) || 1;
  const bucketColors = colorMode === 'diverging'
    ? ['#053061', '#2166ac', '#4393c3', '#92c5de', '#d1e5f0', '#f7f7f7', '#fddbc7', '#f4a582', '#d6604d', '#b2182b', '#67001f']
    : ['#f7fbff', '#e3eef9', '#cfe1f2', '#b8d2ea', '#98bddd', '#74a4cf', '#4e87bf', '#2f6ca9', '#1e528f', '#103b73', '#08285a'];
  return (
    <div className="space-y-1">
      <div className="text-[11px] font-medium text-[#525252]">{title}</div>
      <div className="overflow-auto border border-[#e5e5e5] rounded">
        <table className="text-xs w-full">
          <thead className="bg-[#fafafa]">
            <tr>
              <th className="px-2 py-1 text-left text-[#737373] font-medium">Expiry \ Tenor</th>
              {cols.map((c, j) => <th key={`h-${j}`} className="px-2 py-1 text-right text-[#737373] font-medium">{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {values.map((row, i) => (
              <tr key={`r-${i}`} className="border-t border-[#f5f5f5]">
                <td className="px-2 py-1 font-mono text-[#525252]">{rows[i] || i}</td>
                {row.map((v, j) => {
                  const t = Number.isFinite(v)
                    ? (colorMode === 'diverging'
                      ? ((v + Math.max(Math.abs(min), Math.abs(max))) / (2 * Math.max(Math.abs(min), Math.abs(max)) || 1))
                      : (v - min) / span)
                    : 0;
                  const bucket = Math.max(0, Math.min(Math.round(t * (bucketColors.length - 1)), bucketColors.length - 1));
                  const bg = Number.isFinite(v) ? bucketColors[bucket] : '#f5f5f5';
                  // Pick the text color from the ACTUAL background luminance
                  // (the diverging palette is dark at BOTH ends, so a simple
                  // "high bucket = white" rule left dark-on-dark cells).
                  const r = parseInt(bg.slice(1, 3), 16) / 255;
                  const g = parseInt(bg.slice(3, 5), 16) / 255;
                  const b = parseInt(bg.slice(5, 7), 16) / 255;
                  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
                  const fg = luminance < 0.5 ? '#ffffff' : '#0f172a';
                  return (
                    <td
                      key={`c-${i}-${j}`}
                      className="px-2 py-1 text-right font-mono"
                      style={{ backgroundColor: bg, color: fg }}
                    >
                      {Number.isFinite(v) ? v.toFixed(precision) : '—'}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * SABR-params editor block: shared surface-axes editor + tabbed Matrix2DEditor
 * for the four parameter grids. Each tab carries its own validation closure.
 * The β tab has a "Fill all with 0.5" affordance that delegates to the same
 * `fillAll` helper used by Matrix2DEditor's generic "Fill all…" button — no
 * forked fill logic.
 */
function SabrParamsEditorBlock({
  surface,
  onChange,
  quoteIdOptions,
}: {
  surface: VolSurfaceSpec;
  onChange: (updater: (s: VolSurfaceSpec) => VolSurfaceSpec) => void;
  quoteIdOptions: string[];
}) {
  const [activeParam, setActiveParam] = useState<SabrParamName>('alpha');
  const expiries = surface.axes_expiries || [];
  const tenors = surface.axes_tenors || [];
  const nE = expiries.length;
  const nT = tenors.length;
  const rowLabels = expiries.map(periodLabel);
  const colLabels = tenors.map(periodLabel);
  const grid = (surface[SABR_PARAM_FIELDS[activeParam]] as MatrixCell[][] | undefined) || [];

  const writeGrid = (param: SabrParamName, next: MatrixCell[][]) => {
    const field = SABR_PARAM_FIELDS[param];
    onChange((s) => ({ ...s, [field]: next } as VolSurfaceSpec));
  };

  const validateForParam = (param: SabrParamName) => (cell: MatrixCell) => {
    if (cell && typeof cell === 'object' && 'quoteId' in cell) {
      return cell.quoteId.trim().length === 0 ? 'quote id required' : null;
    }
    if (typeof cell !== 'number') return null;
    if (!Number.isFinite(cell)) return 'must be a finite number';
    if (param === 'alpha' && !(cell > 0)) return 'α must be > 0';
    if (param === 'beta' && !(cell >= 0 && cell <= 1)) return 'β must be in [0, 1]';
    if (param === 'rho' && !(cell > -1 && cell < 1)) return 'ρ must be in (−1, 1)';
    if (param === 'nu' && !(cell > 0)) return 'ν must be > 0';
    return null;
  };

  const handleBetaFillHalf = () => {
    // Uses the same fillAll helper as the editor's generic "Fill all…" button
    // (imported from Matrix2DEditor) so the fill semantics stay consistent.
    const next = fillAll(grid as Matrix2D, nE, nT, 0.5 as MatrixCell);
    writeGrid('beta', next);
  };

  return (
    <div className="border border-[#e5e5e5] rounded-lg p-3 space-y-3 bg-[#fafafa]">
      <SurfaceAxesEditor
        surface={surface}
        onChange={onChange}
        title="Surface axes — define the SABR matrix dimensions"
        helperText="All four SABR parameter grids (α, β, ρ, ν) share these axes. They are independent of the sampling axes below."
      />
      <div>
        <div className="flex items-center gap-2 border-b border-[#e5e5e5]">
          {SABR_PARAM_NAMES.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setActiveParam(p)}
              className={
                p === activeParam
                  ? 'px-3 py-1.5 text-xs font-medium border-b-2 border-[#8a6a2f] text-[#0a0a0a]'
                  : 'px-3 py-1.5 text-xs font-medium border-b-2 border-transparent text-[#737373] hover:text-[#0a0a0a]'
              }
              aria-selected={p === activeParam}
              role="tab"
            >
              {SABR_PARAM_LABELS[p]}
            </button>
          ))}
        </div>
        <div className="flex items-baseline justify-between mt-2 mb-2">
          <h3 className="text-xs font-semibold text-[#0a0a0a]">
            {SABR_PARAM_LABELS[activeParam]} — per-node literals or quote references
          </h3>
          <span className="text-[11px] text-[#737373]">rows = surface expiries, cols = surface tenors</span>
        </div>
        {activeParam === 'beta' && (
          <div className="mb-2">
            <button
              type="button"
              onClick={handleBetaFillHalf}
              className="px-2 py-1 text-xs border border-[#d4d4d4] rounded hover:bg-[#f5f5f5]"
              title="Fill the entire β matrix with the textbook default 0.5"
            >
              Fill all with 0.5
            </button>
          </div>
        )}
        <Matrix2DEditor
          value={grid}
          onChange={(next) => writeGrid(activeParam, next)}
          rowLabels={rowLabels}
          colLabels={colLabels}
          rowAxisLabel="Expiry"
          colAxisLabel="Tenor"
          quoteIdOptions={quoteIdOptions}
          validateCell={validateForParam(activeParam)}
          ariaLabel={`SABR ${activeParam} grid editor`}
        />
        <p className="text-[11px] text-[#a3a3a3] mt-2">
          Resize via the surface-axes editor above; β defaults to 0.5 across the whole grid. Sampling against the backend
          requires every cell to be a finite literal or a resolvable quote reference.
        </p>
      </div>
    </div>
  );
}

function StrikeGridEditor({
  strikes,
  setStrikes,
  newStrike,
  setNewStrike,
  displayUnit = 'rate',
}: {
  strikes: number[];
  setStrikes: (values: number[]) => void;
  newStrike: number;
  setNewStrike: (v: number) => void;
  /** 'rate' shows/accepts rate decimals (0.005); 'bp' shows/accepts basis
   * points (50). Stored values are ALWAYS rate decimals; 'bp' only changes
   * the display/entry unit. */
  displayUnit?: 'rate' | 'bp';
}) {
  const toDisplay = (s: number) => (displayUnit === 'bp' ? strikeToBp(s) : Number(s.toFixed(8)));
  const fromDisplay = (v: number) => (displayUnit === 'bp' ? bpToStrike(v) : v);
  const add = () => {
    if (!Number.isFinite(newStrike)) return;
    setStrikes(normalizeStrikeGrid([...strikes, fromDisplay(newStrike)]));
  };
  const update = (idx: number, v: number) => {
    const next = [...strikes];
    next[idx] = fromDisplay(v);
    setStrikes(normalizeStrikeGrid(next));
  };
  const remove = (idx: number) => setStrikes(strikes.filter((_, i) => i !== idx));

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-2">
        <input type="number" step={displayUnit === 'bp' ? '1' : '0.0001'} aria-label={displayUnit === 'bp' ? 'New strike spread (bp)' : 'New strike'} className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm" value={newStrike} onChange={e => setNewStrike(Number(e.target.value) || 0)} />
        <button type="button" className="px-3 py-2 text-xs border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] col-span-2" onClick={add}>Add Strike</button>
      </div>
      <div className="space-y-1 max-h-28 overflow-auto border border-[#e5e5e5] rounded-lg p-2 bg-[#fafafa]">
        {strikes.map((s, i) => (
          <div key={`${s}-${i}`} className="grid grid-cols-3 gap-2 items-center">
            <input type="number" step={displayUnit === 'bp' ? '1' : '0.0001'} className="col-span-2 w-full px-2 py-1 bg-white border border-[#d4d4d4] rounded text-xs font-mono" value={toDisplay(s)} onChange={e => update(i, Number(e.target.value))} />
            <button type="button" className="p-1 text-[#737373] hover:text-red-500 hover:bg-red-50 rounded justify-self-start" onClick={() => remove(i)} title="Remove">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        ))}
        {strikes.length === 0 && <div className="text-xs text-[#a3a3a3]">No strikes</div>}
      </div>
    </div>
  );
}

function periodToYears(p: Period): number {
  switch (p.unit) {
    case 'Days': return p.n / 365;
    case 'Weeks': return (p.n * 7) / 365;
    case 'Months': return p.n / 12;
    case 'Years': return p.n;
    default: return p.n;
  }
}

const VolCubeViewer = memo(function VolCubeViewer({
  sample,
  selectedStrikeIdx,
  probeExpiryIdx,
  probeTenorIdx,
  displayUnits,
  strikeAxis,
  sliceMode,
  asOfDate,
  onProbeChange,
}: {
  sample: VolSurfaceSampleResult;
  selectedStrikeIdx: number;
  probeExpiryIdx: number;
  probeTenorIdx: number;
  displayUnits: DisplayUnits;
  strikeAxis: StrikeAxis;
  sliceMode: SliceMode;
  asOfDate: string;
  onProbeChange?: (expiryIdx: number, tenorIdx: number) => void;
}) {
  const geometry = useMemo(
    () => buildCubeGeometry(sample, selectedStrikeIdx, displayUnits, asOfDate),
    [sample, selectedStrikeIdx, displayUnits, asOfDate]
  );
  const [camera, setCamera] = useState<any>(null);
  if (!geometry.ok) {
    return <div className="text-xs text-[#a3a3a3]">{geometry.error || 'No cube values'}</div>;
  }
  const { nE, nT, nS, k, x, y, z, zMin, zMax, expiryLabels, tenorLabels } = geometry;
  const vols = sample.vols || [];
  const iProbe = Math.max(0, Math.min(probeExpiryIdx, nE - 1));
  const jProbe = Math.max(0, Math.min(probeTenorIdx, nT - 1));
  const span = zMax - zMin || 1;
  const zPadMin = zMin - span * 0.05;
  const zPadMax = zMax + span * 0.05;
  const probe = {
    probeX: x,
    probeY: Array.from({ length: nT }, () => y[iProbe] ?? 0),
    probeZTenor: Array.from({ length: nT }, (_, j) => z[iProbe][j]),
    probeXExpiry: Array.from({ length: nE }, () => x[jProbe] ?? 0),
    probeYExpiry: y,
    probeZExpiry: Array.from({ length: nE }, (_, i) => z[i][jProbe]),
    markerZ: z[iProbe][jProbe],
  };

  const extractCamera = (relayout: any): any | null => {
    if (!relayout || typeof relayout !== 'object') return null;
    if (relayout['scene.camera']) return relayout['scene.camera'];
    const hasSceneCameraKeys = Object.keys(relayout).some(k => k.startsWith('scene.camera.'));
    if (!hasSceneCameraKeys) return null;
    return {
      eye: {
        x: relayout['scene.camera.eye.x'] ?? 1.6,
        y: relayout['scene.camera.eye.y'] ?? 1.6,
        z: relayout['scene.camera.eye.z'] ?? 0.9,
      },
      center: {
        x: relayout['scene.camera.center.x'] ?? 0,
        y: relayout['scene.camera.center.y'] ?? 0,
        z: relayout['scene.camera.center.z'] ?? 0,
      },
      up: {
        x: relayout['scene.camera.up.x'] ?? 0,
        y: relayout['scene.camera.up.y'] ?? 0,
        z: relayout['scene.camera.up.z'] ?? 1,
      },
    };
  };

  const strikeVal = sample.strikes?.[k];
  const hoverSuffix = strikeAxis === 'SpreadFromATM'
    ? `spread=${(strikeVal || 0).toFixed(6)} (${((strikeVal || 0) * 10000).toFixed(1)} bps)`
    : `strike=${(strikeVal || 0).toFixed(6)}`;
  const activeColor = '#2563eb';
  const inactiveColor = '#94a3b8';
  const tenorLineColor = sliceMode === 'Term' ? activeColor : (sliceMode === 'Smile' ? activeColor : inactiveColor);
  const expiryLineColor = sliceMode === 'Expiry' ? activeColor : (sliceMode === 'Smile' ? activeColor : inactiveColor);
  const tenorLineWidth = sliceMode === 'Term' ? 10 : 6;
  const expiryLineWidth = sliceMode === 'Expiry' ? 10 : 6;

  return (
    <div className="border border-[#e5e5e5] rounded-lg p-2 bg-white">
      <Plot
        data={[
          {
            type: 'surface',
            x,
            y,
            z,
            surfacecolor: z,
            colorscale: 'Turbo',
            hoverinfo: 'skip',
            contours: { z: { show: true, usecolormap: true, project: { z: true } } },
            lighting: { ambient: 0.5, diffuse: 0.9, roughness: 0.8, specular: 0.2, fresnel: 0.1 },
            lightposition: { x: 100, y: 200, z: 0 },
          } as any,
          {
            type: 'scatter3d',
            mode: 'lines',
            x: probe.probeX,
            y: probe.probeY,
            z: probe.probeZTenor,
            line: { width: tenorLineWidth, color: tenorLineColor },
            hoverinfo: 'skip',
            showlegend: false,
          } as any,
          {
            type: 'scatter3d',
            mode: 'lines',
            x: probe.probeXExpiry,
            y: probe.probeYExpiry,
            z: probe.probeZExpiry,
            line: { width: expiryLineWidth, color: expiryLineColor },
            hoverinfo: 'skip',
            showlegend: false,
          } as any,
          {
            type: 'scatter3d',
            mode: 'markers',
            x: [x[jProbe] ?? 0],
            y: [y[iProbe] ?? 0],
            z: [probe.markerZ],
            marker: { size: 7, color: '#dc2626' },
            hovertemplate: `expiry=${expiryLabels[iProbe] || iProbe}<br>tenor=${tenorLabels[jProbe] || jProbe}<br>vol=${(vols[(iProbe * nT + jProbe) * nS + k] ?? NaN).toFixed(6)} (${(toDisplayVol(vols[(iProbe * nT + jProbe) * nS + k] ?? NaN, 'bps')).toFixed(1)} bps)<br>${hoverSuffix}<extra></extra>`,
            showlegend: false,
          } as any,
        ]}
        layout={{
          uirevision: 'volcube-keep-camera',
          autosize: true,
          height: 470,
          margin: { l: 0, r: 0, t: 20, b: 0 },
          showlegend: false,
          scene: {
            uirevision: 'volcube-scene-keep-camera',
            aspectmode: 'cube',
            ...(camera ? { camera } : {}),
            xaxis: { title: 'Tenor', tickmode: 'array', tickvals: x, ticktext: tenorLabels },
            yaxis: { title: 'Expiry', tickmode: 'array', tickvals: y, ticktext: expiryLabels },
            zaxis: { title: `Vol (${displayUnits})`, range: [zPadMin, zPadMax] },
          },
        } as any}
        onRelayout={(ev: any) => {
          const next = extractCamera(ev);
          if (next) setCamera(next);
        }}
        onClick={(ev: any) => {
          const p = ev?.points?.[0];
          if (!p) return;
          const xi = x.findIndex(v => Math.abs(v - p.x) < 1e-9);
          const yi = y.findIndex(v => Math.abs(v - p.y) < 1e-9);
          if (yi >= 0 && xi >= 0) onProbeChange?.(yi, xi);
        }}
        config={{ displaylogo: false, responsive: true }}
        style={{ width: '100%' }}
      />
    </div>
  );
});

function EquitySurfaceViewer({
  sample,
  displayUnits,
  asOfDate,
  strikeFallback,
  sliceMode,
  probeExpiryIdx,
  probeStrikeIdx,
  onProbeChange,
}: {
  sample: VolSurfaceSampleResult;
  displayUnits: DisplayUnits;
  asOfDate: string;
  strikeFallback?: number[];
  sliceMode: SliceMode;
  probeExpiryIdx: number;
  probeStrikeIdx: number;
  onProbeChange?: (expiryIdx: number, strikeIdx: number) => void;
}) {
  const geometry = useMemo(
    () => buildEquityGeometry(sample, displayUnits, asOfDate, strikeFallback),
    [sample, displayUnits, asOfDate, strikeFallback]
  );
  const [camera, setCamera] = useState<any>(null);
  if (!geometry.ok) {
    return <div className="text-xs text-[#a3a3a3]">{geometry.error || 'No equity surface values'}</div>;
  }
  const { x, y, z, zMin, zMax, expiryLabels } = geometry;
  const iProbe = Math.max(0, Math.min(probeExpiryIdx, y.length - 1));
  const kProbe = Math.max(0, Math.min(probeStrikeIdx, x.length - 1));
  const probeX = x;
  const probeY = Array.from({ length: x.length }, () => y[iProbe] ?? 0);
  const probeZStrike = Array.from({ length: x.length }, (_, k) => z[iProbe]?.[k] ?? NaN);
  const probeXExpiry = Array.from({ length: y.length }, () => x[kProbe] ?? 0);
  const probeYExpiry = y;
  const probeZExpiry = Array.from({ length: y.length }, (_, i) => z[i]?.[kProbe] ?? NaN);
  const markerZ = z[iProbe]?.[kProbe] ?? NaN;
  const activeColor = '#2563eb';
  const inactiveColor = '#94a3b8';
  const strikeLineColor = sliceMode === 'Smile' ? activeColor : inactiveColor;
  const expiryLineColor = sliceMode === 'Term' ? activeColor : inactiveColor;
  const strikeLineWidth = sliceMode === 'Smile' ? 10 : 6;
  const expiryLineWidth = sliceMode === 'Term' ? 10 : 6;
  const span = zMax - zMin || 1;
  const zPadMin = zMin - span * 0.05;
  const zPadMax = zMax + span * 0.05;

  return (
    <div className="border border-[#e5e5e5] rounded-lg p-2 bg-white">
      <Plot
        data={[
          {
            type: 'surface',
            x,
            y,
            z,
            surfacecolor: z,
            colorscale: 'Turbo',
            hovertemplate: 'K=%{x}<br>Expiry=%{y}<br>Vol=%{z}<extra></extra>',
            contours: { z: { show: true, usecolormap: true, project: { z: true } } },
          } as any,
          {
            type: 'scatter3d',
            mode: 'lines',
            x: probeX,
            y: probeY,
            z: probeZStrike,
            line: { width: strikeLineWidth, color: strikeLineColor },
            hoverinfo: 'skip',
            showlegend: false,
          } as any,
          {
            type: 'scatter3d',
            mode: 'lines',
            x: probeXExpiry,
            y: probeYExpiry,
            z: probeZExpiry,
            line: { width: expiryLineWidth, color: expiryLineColor },
            hoverinfo: 'skip',
            showlegend: false,
          } as any,
          {
            type: 'scatter3d',
            mode: 'markers',
            x: [x[kProbe] ?? 0],
            y: [y[iProbe] ?? 0],
            z: [markerZ],
            marker: { size: 7, color: '#111827' },
            hoverinfo: 'skip',
            showlegend: false,
          } as any,
        ]}
        layout={{
          uirevision: 'equity-surface-keep-camera',
          autosize: true,
          height: 470,
          margin: { l: 0, r: 0, t: 20, b: 0 },
          showlegend: false,
          scene: {
            uirevision: 'equity-surface-scene-keep-camera',
            aspectmode: 'cube',
            ...(camera ? { camera } : {}),
            xaxis: { title: 'Strike' },
            yaxis: { title: 'Expiry', tickmode: 'array', tickvals: y, ticktext: expiryLabels },
            zaxis: { title: `Vol (${displayUnits})`, range: [zPadMin, zPadMax] },
          },
        } as any}
        onRelayout={(ev: any) => {
          if (ev?.['scene.camera']) setCamera(ev['scene.camera']);
        }}
        onClick={(ev: any) => {
          const p = ev?.points?.[0];
          if (!p) return;
          const yi = y.findIndex(v => Math.abs(v - p.y) < 1e-9);
          const xi = x.findIndex(v => Math.abs(v - p.x) < 1e-9);
          if (yi >= 0 && xi >= 0) onProbeChange?.(yi, xi);
        }}
        config={{ displaylogo: false, responsive: true }}
        style={{ width: '100%' }}
      />
    </div>
  );
}

const LocalSlicePlot = memo(function LocalSlicePlot({
  title,
  x,
  y,
  displayUnits,
  lockToGlobalRange,
  globalRange,
}: {
  title: string;
  x: string[];
  y: number[];
  displayUnits: DisplayUnits;
  lockToGlobalRange: boolean;
  globalRange: { min: number; max: number } | null;
}) {
  if (!x.length || !y.length) return <p className="text-xs text-[#737373]">No slice points.</p>;
  const color = '#2563eb';
  const shown = y.map(v => toDisplayVol(v, displayUnits)).filter(v => Number.isFinite(v));
  const sMin = shown.length ? Math.min(...shown) : 0;
  const sMax = shown.length ? Math.max(...shown) : 0;
  const sSpan = sMax - sMin || 1;
  const sPad = sSpan * 0.08;
  const gMin = globalRange ? toDisplayVol(globalRange.min, displayUnits) : sMin;
  const gMax = globalRange ? toDisplayVol(globalRange.max, displayUnits) : sMax;
  const yRange = lockToGlobalRange ? [gMin, gMax] : [sMin - sPad, sMax + sPad];
  return (
    <div className="space-y-2">
      <div className="text-xs text-[#737373]">{title}</div>
      <Plot
        data={[
          {
            type: 'scatter',
            mode: 'lines+markers',
            x,
            y: y.map(v => toDisplayVol(v, displayUnits)),
            line: { color, width: 2.5 },
            marker: { size: 6 },
          } as any,
        ]}
        layout={{
          autosize: true,
          height: 260,
          margin: { l: 40, r: 10, t: 10, b: 40 },
          yaxis: { title: `Vol (${displayUnits})`, range: yRange },
          xaxis: { title: 'Slice axis' },
        } as any}
        config={{ displaylogo: false, responsive: true }}
        style={{ width: '100%' }}
      />
    </div>
  );
});

function Diag({ label, value }: { label: string; value?: string }) {
  return (
    <div className="p-2 border border-[#e5e5e5] rounded">
      <div className="text-[#a3a3a3]">{label}</div>
      <div className="font-mono text-[#0a0a0a] break-all">{value || '—'}</div>
    </div>
  );
}

function HeatmapTable({
  rows,
  cols,
  values,
  displayUnits,
  surfaceType,
  colAxisLabel = 'Tenor',
  colorScale,
  colorMode = 'sequential',
  selectedRowIdx,
  selectedColIdx,
  onCellClick,
}: {
  rows: string[];
  cols: string[];
  values: number[][];
  displayUnits: DisplayUnits;
  surfaceType: SurfaceType;
  colAxisLabel?: string;
  colorScale?: { min: number; max: number };
  colorMode?: 'sequential' | 'diverging';
  selectedRowIdx?: number;
  selectedColIdx?: number;
  onCellClick?: (rowIdx: number, colIdx: number) => void;
}) {
  const [min, max] = useMemo(() => {
    if (colorScale) return [colorScale.min, colorScale.max] as const;
    const flat = values.flat().filter(v => Number.isFinite(v));
    if (!flat.length) return [0, 1] as const;
    return [Math.min(...flat), Math.max(...flat)] as const;
  }, [values, colorScale]);
  const span = max - min || 1;
  const bucketColors = colorMode === 'diverging'
    ? ['#053061', '#2166ac', '#4393c3', '#92c5de', '#d1e5f0', '#f7f7f7', '#fddbc7', '#f4a582', '#d6604d', '#b2182b', '#67001f']
    : ['#f7fbff', '#e3eef9', '#cfe1f2', '#b8d2ea', '#98bddd', '#74a4cf', '#4e87bf', '#2f6ca9', '#1e528f', '#103b73', '#08285a'];
  return (
    <div className="overflow-auto border border-[#e5e5e5] rounded-lg">
      <table className="text-xs w-full">
        <thead className="bg-[#fafafa]">
          <tr>
            <th className="px-2 py-1 text-left">Expiry \ {colAxisLabel}</th>
            {cols.map((c, i) => <th key={i} className="px-2 py-1 text-right">{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {values.map((row, i) => (
            <tr key={i} className="border-t border-[#f5f5f5]">
              <td className="px-2 py-1">{rows[i] || i}</td>
              {row.map((v, j) => {
                const t = Number.isFinite(v)
                  ? (colorMode === 'diverging' ? ((v + Math.max(Math.abs(min), Math.abs(max))) / (2 * Math.max(Math.abs(min), Math.abs(max)) || 1)) : (v - min) / span)
                  : 0;
                const bucket = Math.max(0, Math.min(Math.round(t * (bucketColors.length - 1)), bucketColors.length - 1));
                const bg = Number.isFinite(v) ? bucketColors[bucket] : '#f5f5f5';
                const textColor = bucket >= 6 ? '#ffffff' : '#0f172a';
                const selected = selectedRowIdx === i && selectedColIdx === j;
                return (
                  <td
                    key={j}
                    className={`px-2 py-1 text-right font-mono cursor-pointer ${selected ? 'ring-2 ring-[#2563eb] ring-inset font-semibold' : ''}`}
                    style={{ backgroundColor: bg, color: textColor }}
                    onClick={() => onCellClick?.(i, j)}
                    title={Number.isFinite(v) ? `vol: ${formatVolBoth(v, surfaceType)}` : '—'}
                  >
                    {Number.isFinite(v) ? toDisplayVol(v, displayUnits).toFixed(displayUnits === 'bps' ? 2 : displayUnits === 'pct' ? 4 : 6) : '—'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DeltaHeatmap({ current, base }: { current: number[][]; base: number[][] }) {
  const delta = current.map((r, i) => r.map((v, j) => (v ?? 0) - (base[i]?.[j] ?? 0)));
  const flat = delta.flat().filter(v => Number.isFinite(v));
  const maxAbs = Math.max(...flat.map(v => Math.abs(v)));
  const meanAbs = flat.reduce((s, v) => s + Math.abs(v), 0) / Math.max(flat.length, 1);
  return (
    <div className="space-y-2">
      <div className="text-xs text-[#737373]">max abs diff: <span className="font-mono">{maxAbs.toFixed(6)} ({(maxAbs * 10000).toFixed(2)} bps)</span> | mean abs diff: <span className="font-mono">{meanAbs.toFixed(6)} ({(meanAbs * 10000).toFixed(2)} bps)</span></div>
      <HeatmapTable
        rows={delta.map((_, i) => String(i))}
        cols={delta[0]?.map((_, j) => String(j)) || []}
        values={delta}
        displayUnits="decimal"
        surfaceType="Swaption"
        colorMode="diverging"
        colorScale={{ min: -maxAbs, max: maxAbs }}
      />
    </div>
  );
}

