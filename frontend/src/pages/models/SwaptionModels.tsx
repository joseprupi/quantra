import { ChangeEvent, useEffect, useRef, useState } from 'react';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import IndexPicker from '../../components/curves/IndexPicker';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { Curve, CurvePoint, IndexDef, IndexRef, Period, collectIndexRefIds } from '../../lib/types';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { orchestratorPost } from '../../lib/api/orchestrator';
import type { CalibrateSwaptionModelResult } from '../../lib/quantra-types';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import { getLegacyFlatQuotes } from '../../lib/storage/quoteBook';
import { getVolSurfaces, VolSurfaceSpec } from '../../lib/storage/volSurfaces';
import {
  buildSwaptionVolWirePayload,
  getSurfaceAxes,
  VolSurfacePayloadError,
} from '../../lib/storage/volSurfacePayload';
import {
  deleteSwaptionModel,
  getSwaptionModels,
  saveSwaptionModel,
  replaceSwaptionModels,
  SwaptionModelRecord,
} from '../../lib/storage/swaptionModels';
import { DuplicateIcon, ExportIcon, ImportIcon, listStyles, NewIcon, TrashButton } from '../../components/lists/listStyles';
import { formStyles } from '../../components/ui/formStyles';

function collectIndexIdsFromCurves(curves: Array<{ points?: CurvePoint[] }>): string[] {
  const allPoints = curves.flatMap((c) => c.points || []);
  return collectIndexRefIds(allPoints);
}

function collectQuoteIdsFromCurves(curves: Array<{ points?: CurvePoint[] }>): string[] {
  const ids = new Set<string>();
  for (const curve of curves) {
    for (const point of curve.points || []) {
      const quoteId = (point.point as any)?.quote_id;
      if (typeof quoteId === 'string' && quoteId.trim().length > 0) {
        ids.add(quoteId);
      }
    }
  }
  return Array.from(ids);
}

function periodLikeToYears(value: any): number {
  if (!value) return NaN;
  if (typeof value === 'number') return value;
  if (typeof value === 'object') {
    const n = Number((value as any).n ?? (value as any).tenor_number);
    const unit = String((value as any).unit ?? (value as any).tenor_time_unit ?? '');
    if (!Number.isFinite(n)) return NaN;
    if (unit === 'Days') return n / 365.0;
    if (unit === 'Weeks') return n / 52.0;
    if (unit === 'Months') return n / 12.0;
    if (unit === 'Years') return n;
  }
  return NaN;
}

function estimateCurveMaxYears(curves: Array<{ points?: CurvePoint[] }>): number {
  let maxYears = 0;
  for (const curve of curves) {
    for (const wrapper of curve.points || []) {
      const point: any = wrapper.point || {};
      const candidates = [
        periodLikeToYears(point.tenor),
        periodLikeToYears({ n: point.tenor_number, unit: point.tenor_time_unit }),
      ];
      for (const years of candidates) {
        if (Number.isFinite(years) && years > maxYears) maxYears = years;
      }
    }
  }
  return maxYears;
}

async function resolveIndexDefs(ids: string[]): Promise<IndexDef[]> {
  if (ids.length === 0) return [];
  const savedSpecs = await indexStore.getAll();
  const result: IndexDef[] = [];
  const seen = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    const saved = savedSpecs.find((s) => s.id === id);
    if (!saved) continue;
    const def = storedToRateIndexDef(saved);
    if (def) result.push(def);
  }
  return result;
}

function periodToYears(p: Period): number {
  if (p.unit === 'Days') return p.n / 365.0;
  if (p.unit === 'Weeks') return (p.n * 7) / 365.0;
  if (p.unit === 'Months') return p.n / 12.0;
  if (p.unit === 'Years') return p.n;
  return NaN;
}

/**
 * Build the SwaptionVolSpec wire payload for HW calibration via the shared
 * `buildSwaptionVolWirePayload` helper, plus the period-typed
 * expiries/tenors that the calibration block of the request needs.
 *
 * Pre-Step-2 this function carried its own AtmMatrix-or-Constant routing
 * logic (silently flattening anything else to constant_vol = 0.01); routing
 * a SabrParams or SmileCube surface through it would have broken HW
 * calibration silently. Step 2 dedups the wire-payload build by delegating
 * to the shared helper while keeping this file's specific responsibility:
 * trimming the calibration grid down to the largest subgrid that fits
 * inside the curve horizon (helpers near the corner of the matrix would
 * otherwise walk past the curve's max time during HW calibration).
 *
 * The helper then sees a "trimmed surface" (a copy with subset axes and
 * grid) and emits whatever envelope `surface.base.shape` calls for —
 * AtmMatrix2D today, future shapes by inheritance.
 */
function buildVolSurfacePayload(
  surface: VolSurfaceSpec,
  swapIndexId: string,
  asOfDate: string,
  maxCurveYears: number,
  resolveQuoteValue: (qid: string) => number | null,
): {
  volSurfacePayload: ReturnType<typeof buildSwaptionVolWirePayload>;
  calibrationExpiries: Period[];
  calibrationTenors: Period[];
} {
  const shape = surface.base?.shape || 'Constant';
  // Constant surfaces have no calibration grid — calibration would need
  // helpers anyway, so let the caller's grid-empty guard fire uniformly.
  if (shape === 'Constant') {
    return {
      volSurfacePayload: buildSwaptionVolWirePayload(surface, swapIndexId, asOfDate, resolveQuoteValue),
      calibrationExpiries: [],
      calibrationTenors: [],
    };
  }

  const { expiries, tenors } = getSurfaceAxes(surface);
  const expiryYears = expiries.map(periodToYears);
  const tenorYears = tenors.map(periodToYears);

  // 0.35Y safety margin keeps generated helper dates inside the curve
  // domain after the calendar roll (mirrors the pre-Step-2 cap).
  const horizonCap = Number.isFinite(maxCurveYears) && maxCurveYears > 0
    ? Math.max(0.25, maxCurveYears - 0.35)
    : Number.POSITIVE_INFINITY;

  let trimmedExpiryIdx: number[] = expiryYears.map((_, i) => i);
  let trimmedTenorIdx: number[] = tenorYears.map((_, i) => i);

  if (Number.isFinite(horizonCap)) {
    let best: { eIdx: number[]; tIdx: number[]; score: number; total: number } | null = null;
    for (let ei = 0; ei < expiryYears.length; ei += 1) {
      for (let ti = 0; ti < tenorYears.length; ti += 1) {
        const eMax = expiryYears[ei];
        const tMax = tenorYears[ti];
        if (!Number.isFinite(eMax) || !Number.isFinite(tMax)) continue;
        if (eMax + tMax > horizonCap) continue;
        const eIdx: number[] = [];
        for (let i = 0; i < expiryYears.length; i += 1) {
          if (expiryYears[i] <= eMax + 1e-9) eIdx.push(i);
        }
        const tIdx: number[] = [];
        for (let i = 0; i < tenorYears.length; i += 1) {
          if (tenorYears[i] <= tMax + 1e-9) tIdx.push(i);
        }
        const score = eIdx.length * tIdx.length;
        const total = eMax + tMax;
        if (!best || score > best.score || (score === best.score && total > best.total)) {
          best = { eIdx, tIdx, score, total };
        }
      }
    }
    if (!best) {
      // No subgrid fits — caller surfaces the "exceeds curve horizon" error.
      return {
        volSurfacePayload: buildSwaptionVolWirePayload(surface, swapIndexId, asOfDate, resolveQuoteValue),
        calibrationExpiries: [],
        calibrationTenors: [],
      };
    }
    trimmedExpiryIdx = best.eIdx;
    trimmedTenorIdx = best.tIdx;
  }

  const trimmedExpiries = trimmedExpiryIdx.map((i) => expiries[i]);
  const trimmedTenors = trimmedTenorIdx.map((i) => tenors[i]);

  function trimGrid<T>(grid: T[][] | undefined): T[][] | undefined {
    if (!grid || grid.length === 0) return grid;
    return trimmedExpiryIdx.map((r) =>
      trimmedTenorIdx.map((c) => (grid[r] ? grid[r][c] : (undefined as unknown as T)))
    );
  }

  // SabrCalibrate's market-vol cube is shaped (nE × nT × nS); trim along
  // both expiry and tenor dimensions so the wire helper's checkCubeShape
  // sees a self-consistent shape. Strike axis is left untouched (it's not
  // in the calibration-horizon constraint).
  function trimCube<T>(cube: T[][][] | undefined): T[][][] | undefined {
    if (!cube || cube.length === 0) return cube;
    return trimmedExpiryIdx.map((r) =>
      trimmedTenorIdx.map((c) => (cube[r]?.[c] ? cube[r][c] : ([] as unknown as T[])))
    );
  }

  // Build a surface copy with subset axes/grid so the helper sees a
  // self-consistent shape. The legacy `expiries`/`tenors: number[]` fields
  // are also trimmed for any downstream code that still reads them; the
  // helper itself prefers `axes_expiries`/`axes_tenors` and won't see the
  // legacy mismatch.
  const trimmedSurface: VolSurfaceSpec = {
    ...surface,
    axes_expiries: trimmedExpiries,
    axes_tenors: trimmedTenors,
    expiries: trimmedExpiryIdx.map((i) => expiryYears[i]),
    tenors: trimmedTenorIdx.map((i) => tenorYears[i]),
    grid: trimGrid(surface.grid as number[][] | undefined) as number[][] | undefined,
    sabr_alpha: trimGrid(surface.sabr_alpha),
    sabr_beta: trimGrid(surface.sabr_beta),
    sabr_rho: trimGrid(surface.sabr_rho),
    sabr_nu: trimGrid(surface.sabr_nu),
    sabr_market_vols: trimCube(surface.sabr_market_vols),
  };

  return {
    volSurfacePayload: buildSwaptionVolWirePayload(trimmedSurface, swapIndexId, asOfDate, resolveQuoteValue),
    calibrationExpiries: trimmedExpiries,
    calibrationTenors: trimmedTenors,
  };
}

export default function SwaptionModels() {
  const { asOfDate: globalAsOf } = useAsOfDate();

  const [asOfDate, setAsOfDate] = useState(globalAsOf);
  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [forwardCurveId, setForwardCurveId] = useState('');
  const [forwardCurve, setForwardCurve] = useState<Curve | null>(null);
  const [useSameCurve, setUseSameCurve] = useState(true);
  const [indexRef, setIndexRef] = useState<IndexRef>({ id: 'EURIBOR_6M' });
  const [swaptionVolId, setSwaptionVolId] = useState('');
  const [volSurfaces, setVolSurfaces] = useState<VolSurfaceSpec[]>([]);

  const [modelId, setModelId] = useState('hw_bermudan_1');
  const [aInit, setAInit] = useState(0.03);
  const [sigmaInit, setSigmaInit] = useState(0.01);
  const [calibrateA, setCalibrateA] = useState(true);
  const [calibrateSigma, setCalibrateSigma] = useState(true);
  const [maxIterations, setMaxIterations] = useState(1000);
  const [functionEvaluations, setFunctionEvaluations] = useState(999);
  const [endCriteriaEps, setEndCriteriaEps] = useState(1e-8);

  const [models, setModels] = useState<SwaptionModelRecord[]>([]);
  const [lastSuccessfulRequest, setLastSuccessfulRequest] = useState<any | null>(null);
  const [calibrating, setCalibrating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setAsOfDate(globalAsOf);
  }, [globalAsOf]);

  useEffect(() => {
    const surfaces = getVolSurfaces();
    setVolSurfaces(surfaces);
    if (surfaces.length > 0 && !swaptionVolId) setSwaptionVolId(surfaces[0].id);
    setModels(getSwaptionModels());
  }, []);

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  function handleDownloadRequestJson() {
    if (!lastSuccessfulRequest) return;
    const blob = new Blob([JSON.stringify(lastSuccessfulRequest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `swaption-model-calibration-request-${asOfDate}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  async function handleCalibrate() {
    setError(null);
    setSuccess(null);
    if (!discountCurve) {
      setError('Please select a discount curve');
      return;
    }
    const actualForward = useSameCurve ? discountCurve : forwardCurve;
    if (!actualForward) {
      setError('Please select a forwarding curve');
      return;
    }
    const volSurface = volSurfaces.find((s) => s.id === swaptionVolId);
    if (!volSurface) {
      setError('Please select a swaption volatility surface');
      return;
    }
    const swapIndexId = ((volSurface as any).swap_index_id as string | undefined)?.trim() || `${indexRef.id}_SWAP`;
    if (maxIterations <= 1) {
      setError('Max iterations must be greater than 1');
      return;
    }
    // Backend currently maps these two limits inversely, so enforce
    // max_iterations < function_evaluations to satisfy QuantLib constraints.
    const requestedMaxIterations = Math.max(2, maxIterations);
    const requestedFunctionEvaluations = Math.max(3, functionEvaluations);
    const safeFunctionEvaluations = Math.max(
      requestedFunctionEvaluations,
      requestedMaxIterations + 1
    );
    const safeMaxIterations = Math.min(
      requestedMaxIterations,
      safeFunctionEvaluations - 1
    );

    const curves: any[] = [
      {
        id: 'discount',
        day_counter: discountCurve.day_counter,
        interpolator: discountCurve.interpolator,
        bootstrap_trait: discountCurve.bootstrap_trait,
        reference_date: discountCurve.reference_date || asOfDate,
        points: discountCurve.points,
      },
    ];
    if (!useSameCurve && actualForward && actualForward.id !== discountCurve.id) {
      curves.push({
        id: 'forward',
        day_counter: actualForward.day_counter,
        interpolator: actualForward.interpolator,
        bootstrap_trait: actualForward.bootstrap_trait,
        reference_date: actualForward.reference_date || asOfDate,
        points: actualForward.points,
      });
    }
    const maxCurveYears = estimateCurveMaxYears(curves);
    // Mirror VolWorkbench.runSample: the wire helper resolves quote-cell
    // references via this callback. Quote cells aren't typical for HW
    // calibration grids today (AtmMatrix legacy data is plain numbers),
    // but threading the resolver keeps the helper API uniform and makes
    // future quote-bound calibration grids work out of the box.
    const flatById = new Map(getLegacyFlatQuotes().map((q) => [q.id, q]));
    const resolveQuoteValueById = (qid?: string): number | null => {
      if (!qid) return null;
      const q = flatById.get(qid);
      if (q && Number.isFinite(q.value)) return q.value;
      return null;
    };
    let volSurfacePayload: ReturnType<typeof buildSwaptionVolWirePayload>;
    let calibrationExpiries: Period[];
    let calibrationTenors: Period[];
    try {
      const built = buildVolSurfacePayload(
        volSurface,
        swapIndexId,
        asOfDate,
        maxCurveYears,
        resolveQuoteValueById,
      );
      volSurfacePayload = built.volSurfacePayload;
      calibrationExpiries = built.calibrationExpiries;
      calibrationTenors = built.calibrationTenors;
    } catch (err) {
      if (err instanceof VolSurfacePayloadError) {
        setError(err.message);
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to build vol surface payload');
      return;
    }
    if (calibrationExpiries.length === 0 || calibrationTenors.length === 0) {
      setError('Selected swaption vol surface grid exceeds curve horizon; choose longer curves or shorter expiries/tenors');
      return;
    }

    const curveIndexIds = collectIndexIdsFromCurves(curves);
    const allIndexIds = Array.from(new Set([indexRef.id, ...curveIndexIds]));
    const resolvedIndices = await resolveIndexDefs(allIndexIds);
    const unresolvedIds = allIndexIds.filter((idx) => !resolvedIndices.find((d) => d.id === idx));
    if (unresolvedIds.length > 0) {
      setError(`Selected curve references unknown indices: ${unresolvedIds.join(', ')}`);
      return;
    }
    const selectedIndexDef = resolvedIndices.find((d) => d.id === indexRef.id);
    const quoteIds = collectQuoteIdsFromCurves(curves);
    const savedQuotes = getLegacyFlatQuotes();
    const resolvedQuotesRaw = quoteIds.map((id) => savedQuotes.find((q) => q.id === id)).filter(Boolean) as any[];
    const missingQuoteIds = quoteIds.filter((id) => !resolvedQuotesRaw.find((q) => q.id === id));
    if (missingQuoteIds.length > 0) {
      setError(`Selected curve references unknown quotes: ${missingQuoteIds.join(', ')}`);
      return;
    }
    const resolvedQuotes = resolvedQuotesRaw.map((q) => ({
      id: q.id,
      kind: q.kind,
      value: q.value,
      quote_type: q.quote_type,
    }));

    setCalibrating(true);
    try {
      const request = {
        pricing: {
          as_of_date: asOfDate,
          indices: resolvedIndices.map(normalizeIndexDefForApi),
          swap_indices: [
            {
              id: swapIndexId,
              kind: 'IborSwapIndex',
              spot_days: 2,
              calendar: 'TARGET',
              business_day_convention: 'ModifiedFollowing',
              float_index_id: indexRef.id,
              fixed_leg: {
                fixed_frequency: 'Annual',
                fixed_day_counter: 'Thirty360',
                fixed_calendar: 'TARGET',
                fixed_bdc: 'ModifiedFollowing',
                fixed_term_bdc: 'ModifiedFollowing',
                fixed_date_rule: 'Forward',
                fixed_eom: false,
              },
              float_leg: {
                float_tenor: selectedIndexDef?.tenor || { n: 6, unit: 'Months' },
                float_calendar: 'TARGET',
                float_bdc: 'ModifiedFollowing',
                float_term_bdc: 'ModifiedFollowing',
                float_date_rule: 'Forward',
                float_eom: false,
              },
            },
          ],
          quotes: resolvedQuotes,
          curves: curves.map(normalizeCurveForApi),
          vol_surfaces: [volSurfacePayload],
          models: [
            {
              id: modelId,
              payload_type: 'SwaptionModelSpec',
              payload: {
                model_type: 'HullWhiteLattice',
              },
            },
          ],
        },
        model_id: modelId,
        calibration: {
          swaption_vol_id: volSurface.id,
          discount_curve_id: 'discount',
          forwarding_curve_id: useSameCurve ? 'discount' : 'forward',
          swap_index_id: swapIndexId,
          expiries: calibrationExpiries,
          tenors: calibrationTenors,
          calibrate_a: calibrateA,
          calibrate_sigma: calibrateSigma,
          a_init: aInit,
          sigma_init: sigmaInit,
          max_iterations: safeMaxIterations,
          function_evaluations: safeFunctionEvaluations,
          end_criteria_eps: endCriteriaEps,
        },
      };

      const response = await orchestratorPost<CalibrateSwaptionModelResult>(
        '/v1/calibrate-swaption-model',
        request,
      );
      if (!response.ok || !response.data) {
        throw new Error(response.ok ? 'Calibration failed' : response.envelope.error);
      }
      const calibrated = response.data;
      setLastSuccessfulRequest(request);
      await saveSwaptionModel({
        id: calibrated.model_id || modelId,
        kind: 'HullWhiteLattice',
        hw_a: calibrated.hw_a ?? aInit,
        hw_sigma: calibrated.hw_sigma ?? sigmaInit,
        rmse: calibrated.rmse,
        num_helpers: calibrated.num_helpers,
        grid_rows: calibrated.grid_rows,
        grid_cols: calibrated.grid_cols,
        grid_points: calibrated.grid_points,
        as_of_date: asOfDate,
        vol_surface_id: volSurface.id,
        discount_curve_id: 'discount',
        forwarding_curve_id: useSameCurve ? 'discount' : 'forward',
        swap_index_id: swapIndexId,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      });
      setModels(getSwaptionModels());
      setSuccess(
        `Calibrated ${calibrated.model_id} (a=${(calibrated.hw_a ?? aInit).toFixed(6)}, sigma=${(calibrated.hw_sigma ?? sigmaInit).toFixed(6)})`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Calibration failed');
    } finally {
      setCalibrating(false);
    }
  }

  async function handleDeleteModel(id: string) {
    if (!confirm(`Delete model "${id}"?`)) return;
    try {
      await deleteSwaptionModel(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete model');
    }
    setModels(getSwaptionModels());
  }

  function handleLoadModel(record: SwaptionModelRecord) {
    setModelId(record.id);
    setAInit(record.hw_a);
    setSigmaInit(record.hw_sigma);
    if (record.as_of_date) setAsOfDate(record.as_of_date);
    if (record.vol_surface_id) setSwaptionVolId(record.vol_surface_id);
  }

  async function handleDuplicateModel(record: SwaptionModelRecord) {
    const now = new Date().toISOString();
    try {
      await saveSwaptionModel({
        ...record,
        id: `${record.id}_copy_${Date.now()}`,
        createdAt: now,
        updatedAt: now,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to duplicate model');
    }
    setModels(getSwaptionModels());
  }

  function handleExportAll() {
    if (models.length === 0) return;
    const blob = new Blob([JSON.stringify(models, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `swaption-models-${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function handleImport(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const next: SwaptionModelRecord[] = Array.isArray(parsed) ? parsed : [parsed];
      replaceSwaptionModels(next);
      setModels(getSwaptionModels());
    } catch {
      setError('Invalid model JSON');
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-[#0a0a0a]">Models</h1>
            <p className="text-[#737373] mt-1">Calibrate and store Hull-White models for Bermudan swaption pricing.</p>
          </div>
          <div className="flex gap-2">
            <input ref={fileInputRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            <button onClick={() => fileInputRef.current?.click()} className={listStyles.secondaryButton}>
              <ImportIcon />
              Import
            </button>
            {models.length > 0 && (
              <button onClick={handleExportAll} className={listStyles.secondaryButton}>
                <ExportIcon />
                Export All
              </button>
            )}
            <button
              onClick={() => {
                setModelId(`hw_model_${Date.now()}`);
                setAInit(0.03);
                setSigmaInit(0.01);
              }}
              className={listStyles.primaryNewButton}
            >
              <NewIcon />
              New Model
            </button>
          </div>
        </div>

        {error && <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600">{error}</div>}
        {success && <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg text-sm text-green-700">{success}</div>}

        <div className="grid lg:grid-cols-2 gap-6">
          <section className="bg-white border border-[#e5e5e5] rounded-xl p-5">
            <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Swaption HW Calibration</h2>
            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <label className={labelClass}>As Of Date</label>
                <input type="date" value={asOfDate} onChange={(e) => setAsOfDate(e.target.value)} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>Model ID</label>
                <input type="text" value={modelId} onChange={(e) => setModelId(e.target.value)} className={inputClass} />
              </div>
              <CurveSetSelector
                label="Discounting Curve"
                curveRole="discount"
                curveSetId={curveSetId}
                curveId={discountCurveId}
                onChangeCurveSet={(id) => setCurveSetId(id)}
                onChangeCurve={(id, curve) => {
                  setDiscountCurveId(id);
                  setDiscountCurve(curve);
                }}
              />
              <div>
                <label className="flex items-center gap-2 text-xs text-[#737373] mb-1.5 font-medium">
                  Forwarding Curve
                  <label className="flex items-center gap-1 text-[#525252] font-normal">
                    <input
                      type="checkbox"
                      checked={useSameCurve}
                      onChange={(e) => setUseSameCurve(e.target.checked)}
                      className="rounded border-[#d4d4d4] w-3 h-3"
                    />
                    <span className="text-xs">Single-curve mode</span>
                  </label>
                </label>
                {useSameCurve ? (
                  <input type="text" value={discountCurve?.name || 'Select discount curve first'} disabled className={`${inputClass} bg-[#f5f5f5] text-[#737373]`} />
                ) : (
                  <CurveSetSelector
                    label=""
                    curveRole="forward"
                    curveSetId={curveSetId}
                    curveId={forwardCurveId}
                    onChangeCurveSet={(id) => setCurveSetId(id)}
                    onChangeCurve={(id, curve) => {
                      setForwardCurveId(id);
                      setForwardCurve(curve);
                    }}
                  />
                )}
              </div>
              <div>
                <label className={labelClass}>Index</label>
                <IndexPicker label="" value={indexRef} onChange={(ref) => setIndexRef(ref)} filter="Ibor" />
              </div>
              <div>
                <label className={labelClass}>Swaption Vol Surface</label>
                <select value={swaptionVolId} onChange={(e) => setSwaptionVolId(e.target.value)} className={inputClass}>
                  <option value="">Select...</option>
                  {volSurfaces.map((surface) => (
                    <option key={surface.id} value={surface.id}>
                      {surface.id}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="grid sm:grid-cols-2 gap-4 mt-4">
              <div>
                <label className={labelClass}>a init</label>
                <input type="number" step="0.0001" value={aInit} onChange={(e) => setAInit(parseFloat(e.target.value) || 0)} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>sigma init</label>
                <input type="number" step="0.0001" value={sigmaInit} onChange={(e) => setSigmaInit(parseFloat(e.target.value) || 0)} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>Max Iterations</label>
                <input type="number" value={maxIterations} onChange={(e) => setMaxIterations(parseInt(e.target.value, 10) || 0)} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>Function Evaluations</label>
                <input type="number" value={functionEvaluations} onChange={(e) => setFunctionEvaluations(parseInt(e.target.value, 10) || 0)} className={inputClass} />
              </div>
              <div>
                <label className={labelClass}>End Criteria Eps</label>
                <input type="number" step="0.00000001" value={endCriteriaEps} onChange={(e) => setEndCriteriaEps(parseFloat(e.target.value) || 0)} className={inputClass} />
              </div>
              <div className="flex items-end gap-4 pb-2">
                <label className="flex items-center gap-2 text-xs text-[#737373]">
                  <input type="checkbox" checked={calibrateA} onChange={(e) => setCalibrateA(e.target.checked)} className="rounded border-[#d4d4d4]" />
                  Calibrate a
                </label>
                <label className="flex items-center gap-2 text-xs text-[#737373]">
                  <input type="checkbox" checked={calibrateSigma} onChange={(e) => setCalibrateSigma(e.target.checked)} className="rounded border-[#d4d4d4]" />
                  Calibrate sigma
                </label>
              </div>
            </div>
            <div className="mt-5 grid grid-cols-1 sm:grid-cols-2 gap-3">
              <button
                onClick={handleCalibrate}
                disabled={calibrating}
                className="py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-60"
              >
                {calibrating ? 'Calibrating...' : 'Calibrate Hull-White Model'}
              </button>
              <button
                onClick={handleDownloadRequestJson}
                disabled={!lastSuccessfulRequest}
                title="Download last successful calibration request JSON"
                className="py-3 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-xl hover:bg-[#f5f5f5] disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Request JSON
              </button>
            </div>
          </section>

          <section className="bg-white border border-[#e5e5e5] rounded-xl p-5">
            <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Saved Models</h2>
            {models.length === 0 ? (
              <p className="text-sm text-[#737373]">No calibrated models saved yet.</p>
            ) : (
              <div className="space-y-2">
                {models.map((m) => (
                  <div key={m.id} className={listStyles.listCard} onClick={() => handleLoadModel(m)}>
                    <div className="flex items-start justify-between">
                      <div>
                        <p className="text-sm font-medium text-[#0a0a0a]">{m.id}</p>
                        <p className="text-xs text-[#737373]">
                          a={m.hw_a.toFixed(6)} · sigma={m.hw_sigma.toFixed(6)} · rmse={m.rmse?.toFixed(6) ?? 'n/a'}
                        </p>
                        <p className="text-xs text-[#a3a3a3] mt-1">
                          {m.as_of_date || 'n/a'} · vol={m.vol_surface_id || 'n/a'} · helpers={m.num_helpers ?? 'n/a'}
                        </p>
                      </div>
                      <div className={listStyles.hoverActions}>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDuplicateModel(m); }}
                          className={listStyles.duplicateButton}
                          title="Duplicate"
                        >
                          <DuplicateIcon />
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDeleteModel(m.id); }}
                          className={listStyles.deleteButton}
                          title="Delete"
                        >
                          <TrashButton />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>

      </main>
    </div>
  );
}
