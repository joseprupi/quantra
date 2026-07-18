import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveChart from '../../components/curves/CurveChart';
import InflationCurveChart from '../../components/curves/InflationCurveChart';
import BackLink from '../../components/ui/BackLink';
import { entityUi, getBackLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';
import {
  BUSINESS_DAY_CONVENTIONS,
  CALENDARS,
  CURRENCIES,
  CURVE_ROLES,
  Curve,
  CurveRole,
  CurveSet,
  CurveSetCurveRef,
  IndexDef,
  TIME_UNITS,
  TimeUnit,
  collectIndexRefIds,
} from '../../lib/types';
import { getCurveSetById, saveCurveSet } from '../../lib/storage/curveSets';
import { getCurveById, getSavedCurves } from '../../lib/storage/curves';
import { BootstrapCurveResult } from '../../lib/quantra-types';
import { orchestratorPost } from '../../lib/api/orchestrator';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { getInflationMeasures, buildInflationCurveSpec, buildInflationIndexSpec } from '../../lib/inflation-curves';
import { indexStore, storedToRateIndexDef, type StoredIndexSpec } from '../../lib/storage/indices';

import { getLegacyFlatQuotes, getQuoteBook, getResolutionMode, resolveQuoteValue } from '../../lib/storage/quoteBook';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { CreditCurveSpec, getCreditCurves } from '../../lib/storage/creditCurves';

type DiagTab = 'table' | 'plot' | 'json';
type GridType = 'tenor' | 'range';

interface TenorItem {
  n: number;
  unit: TimeUnit;
}

interface ResolvedRef extends CurveSetCurveRef {
  curve: Curve | null;
}

function generateCurveSetRefId(): string {
  return `csref_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function roleBadgeClass(role: CurveRole) {
  if (role === 'discount') return 'bg-emerald-50 text-emerald-700';
  if (role === 'forward') return 'bg-blue-50 text-blue-700';
  if (role === 'inflation') return 'bg-rose-50 text-rose-700';
  return 'bg-purple-50 text-purple-700';
}

function curvePath(curve: Curve) {
  return curve.role === 'inflation' ? `/inflation-curves/${curve.id}` : `/yield-curves/${curve.id}`;
}

async function resolveIndicesFromCurves(curves: Curve[]): Promise<IndexDef[]> {
  const allPoints = curves.flatMap((curve) => curve.points || []);
  const ids = collectIndexRefIds(allPoints);
  if (ids.length === 0) return [];

  const savedSpecs = await indexStore.getAll();
  const result: IndexDef[] = [];
  const seen = new Set<string>();

  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    const saved = savedSpecs.find((spec: StoredIndexSpec) => spec.id === id);
    if (!saved) continue;
    const def = storedToRateIndexDef(saved);
    if (def) result.push(def);
  }

  return result;
}

function resolveQuoteIds(points: any[], asOfDate: string): any[] {
  const flatQuotes = getLegacyFlatQuotes();
  const bookEntries = getQuoteBook();
  const mode = getResolutionMode();
  const unresolved: string[] = [];

  const resolved = points.map((wrapper) => {
    const point = { ...wrapper.point };
    if (!point.quote_id) return { ...wrapper, point };

    const quoteId = point.quote_id;
    let value: number | null = null;

    const bookEntry = bookEntries.find((entry: any) => entry.id === quoteId);
    if (bookEntry) {
      value = resolveQuoteValue(bookEntry.series, asOfDate, mode);
    }

    if (value === null) {
      const flatQuote = flatQuotes.find((quote) => quote.id === quoteId);
      if (flatQuote) value = flatQuote.value;
    }

    if (value === null) {
      unresolved.push(quoteId);
      return { ...wrapper, point };
    }

    const cleaned = { ...point };
    delete cleaned.quote_id;
    if (wrapper.point_type === 'BondHelper' && cleaned.price === undefined) {
      cleaned.price = value;
    } else if (wrapper.point_type === 'FutureHelper' && cleaned.futures_price === undefined) {
      cleaned.futures_price = value;
    } else {
      cleaned.rate = value;
    }
    return { point_type: wrapper.point_type, point: cleaned };
  });

  if (unresolved.length > 0) {
    throw new Error(`Cannot resolve quote_ids: ${unresolved.join(', ')}`);
  }

  return resolved;
}

function isCurveEmpty(curve: Curve) {
  return curve.role === 'inflation'
    ? !(curve.inflation_curve?.points && curve.inflation_curve.points.length > 0)
    : (curve.points || []).length === 0;
}

export default function CurveSetReferenceEditor() {
  const ui = entityUi.curveSet;
  const { id } = useParams();
  const navigate = useNavigate();
  const { asOfDate: globalAsOf } = useAsOfDate();

  const [curveSet, setCurveSet] = useState<CurveSet | null>(null);
  const [availableCurves, setAvailableCurves] = useState<Curve[]>([]);
  const [availableCreditCurves, setAvailableCreditCurves] = useState<CreditCurveSpec[]>([]);
  const [selectedRefIdx, setSelectedRefIdx] = useState(0);

  const [bootstrapping, setBootstrapping] = useState(false);
  const [bootstrapResults, setBootstrapResults] = useState<BootstrapCurveResult[]>([]);
  const [lastRequest, setLastRequest] = useState<object | null>(null);

  const [diagTab, setDiagTab] = useState<DiagTab>('table');
  const [showMeasure, setShowMeasure] = useState<'ZERO' | 'FWD' | 'DF'>('ZERO');
  const [showInflationMeasure, setShowInflationMeasure] = useState<'ZeroRate' | 'YoYRate'>('ZeroRate');

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [gridType, setGridType] = useState<GridType>('tenor');
  const [tenorGrid, setTenorGrid] = useState<TenorItem[]>([
    { n: 1, unit: 'Months' }, { n: 3, unit: 'Months' }, { n: 6, unit: 'Months' },
    { n: 1, unit: 'Years' }, { n: 2, unit: 'Years' }, { n: 3, unit: 'Years' },
    { n: 5, unit: 'Years' }, { n: 7, unit: 'Years' }, { n: 10, unit: 'Years' },
    { n: 15, unit: 'Years' }, { n: 20, unit: 'Years' }, { n: 30, unit: 'Years' },
  ]);
  const [tenorGridCalendar, setTenorGridCalendar] = useState('TARGET');
  const [tenorGridBdc, setTenorGridBdc] = useState('ModifiedFollowing');
  const [rangeEndDate, setRangeEndDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() + 30);
    return d.toISOString().split('T')[0];
  });
  const [rangeStepN, setRangeStepN] = useState(1);
  const [rangeStepUnit, setRangeStepUnit] = useState('Months');
  const [rangeBusinessDaysOnly, setRangeBusinessDaysOnly] = useState(false);
  const [rangeCalendar, setRangeCalendar] = useState('TARGET');
  const [showGridOptions, setShowGridOptions] = useState(false);

  useEffect(() => {
    if (!id) return;
    const loaded = getCurveSetById(id);
    if (!loaded) {
      setError('Curve set not found');
      return;
    }
    setCurveSet(loaded);
    setSelectedRefIdx(0);
  }, [id]);

  useEffect(() => {
    const loadMarketObjects = () => {
      setAvailableCurves(getSavedCurves());
      setAvailableCreditCurves(getCreditCurves());
    };
    loadMarketObjects();
    window.addEventListener('focus', loadMarketObjects);
    return () => window.removeEventListener('focus', loadMarketObjects);
  }, []);

  const persist = useCallback((updated: CurveSet) => {
    setCurveSet(updated);
    // Optimistic UI; the backend PATCH runs behind it (last write wins).
    void saveCurveSet(updated).catch((err) => {
      setError(err instanceof Error ? err.message : 'Failed to save curve set');
    });
  }, []);

  useEffect(() => {
    if (!curveSet || !globalAsOf || curveSet.as_of_date === globalAsOf) return;
    persist({ ...curveSet, as_of_date: globalAsOf });
  }, [curveSet, globalAsOf, persist]);

  const availableCurveMap = useMemo(
    () => new Map(availableCurves.map((curve) => [curve.id, curve])),
    [availableCurves],
  );

  const resolvedRefs = useMemo<ResolvedRef[]>(
    () => (curveSet?.curve_refs || []).map((ref) => ({ ...ref, curve: availableCurveMap.get(ref.curve_id) || null })),
    [curveSet, availableCurveMap],
  );

  useEffect(() => {
    if (selectedRefIdx < resolvedRefs.length) return;
    setSelectedRefIdx(Math.max(0, resolvedRefs.length - 1));
  }, [resolvedRefs.length, selectedRefIdx]);

  const selectedRef = resolvedRefs[selectedRefIdx] || null;
  const selectedCurve = selectedRef?.curve || null;
  const selectedResult = selectedCurve ? bootstrapResults.find((result) => result.id === selectedCurve.id) || null : null;

  const inputClass = formStyles.input;
  const labelClass = formStyles.compactLabel;
  const buttonClass = 'px-3 py-1.5 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors';

  const updateMeta = (field: keyof CurveSet, value: string) => {
    if (!curveSet) return;
    persist({ ...curveSet, [field]: value });
  };

  const addRef = (role: CurveRole) => {
    if (!curveSet) return;
    const next = {
      ...curveSet,
      curve_refs: [
        ...(curveSet.curve_refs || []),
        { id: generateCurveSetRefId(), role, curve_id: '' },
      ],
    };
    persist(next);
    setSelectedRefIdx(next.curve_refs.length - 1);
  };

  const updateRef = (index: number, update: Partial<CurveSetCurveRef>) => {
    if (!curveSet) return;
    const nextRefs = (curveSet.curve_refs || []).map((ref, refIdx) => (
      refIdx === index ? { ...ref, ...update } : ref
    ));
    persist({ ...curveSet, curve_refs: nextRefs });
  };

  const toggleCreditCurve = (creditCurveId: string) => {
    if (!curveSet) return;
    const current = new Set(curveSet.credit_curve_ids || []);
    if (current.has(creditCurveId)) current.delete(creditCurveId);
    else current.add(creditCurveId);
    persist({ ...curveSet, credit_curve_ids: Array.from(current) });
  };

  const removeRef = (index: number) => {
    if (!curveSet) return;
    const nextRefs = (curveSet.curve_refs || []).filter((_, refIdx) => refIdx !== index);
    persist({ ...curveSet, curve_refs: nextRefs });
    setSelectedRefIdx((current) => Math.max(0, Math.min(current, nextRefs.length - 1)));
  };

  const addTenor = () => setTenorGrid((grid) => [...grid, { n: 1, unit: 'Years' }]);
  const removeTenor = (index: number) => setTenorGrid((grid) => grid.filter((_, gridIdx) => gridIdx !== index));
  const updateTenor = (index: number, field: keyof TenorItem, value: any) => {
    setTenorGrid((grid) => grid.map((item, itemIdx) => (itemIdx === index ? { ...item, [field]: value } : item)));
  };

  const handleBootstrap = async () => {
    if (!curveSet) return;

    const missingRefs = resolvedRefs.filter((ref) => ref.curve_id && !ref.curve);
    if (missingRefs.length > 0) {
      setError(`Missing referenced curves: ${missingRefs.map((ref) => ref.label || ref.curve_id).join(', ')}`);
      return;
    }

    const selectedCurves = resolvedRefs
      .map((ref) => ref.curve)
      .filter((curve): curve is Curve => curve !== null);

    if (selectedCurves.length === 0) {
      setError('Select at least one standalone curve');
      return;
    }

    const emptyCurves = selectedCurves.filter(isCurveEmpty);
    if (emptyCurves.length > 0) {
      setError(`Curves with no instruments/helpers: ${emptyCurves.map((curve) => curve.name).join(', ')}`);
      return;
    }

    const nominalCurveMap = new Map<string, Curve>();
    const addNominalCurve = (curve: Curve | null | undefined) => {
      if (!curve || curve.role === 'inflation') return;
      nominalCurveMap.set(curve.id, curve);
    };

    selectedCurves.forEach(addNominalCurve);
    for (const curve of selectedCurves) {
      if (!curve.discount_curve_id) continue;
      const dependency = availableCurveMap.get(curve.discount_curve_id) || getCurveById(curve.discount_curve_id);
      if (!dependency) {
        setError(`"${curve.name}" references a missing discount curve`);
        return;
      }
      addNominalCurve(dependency);
    }

    const nominalCurves = Array.from(nominalCurveMap.values()).sort((left, right) => {
      const priority = (curve: Curve) => (curve.role === 'discount' ? 0 : curve.role === 'forward' ? 1 : 2);
      return priority(left) - priority(right);
    });
    const inflationCurves = selectedCurves.filter((curve) => curve.role === 'inflation');

    setBootstrapping(true);
    setError(null);
    setSuccess(null);
    setBootstrapResults([]);

    try {
      const gridConfig = gridType === 'tenor'
        ? {
            grid_type: 'TenorGrid',
            grid: {
              tenors: tenorGrid.map((tenor) => ({ n: tenor.n, unit: tenor.unit })),
              calendar: tenorGridCalendar,
              business_day_convention: tenorGridBdc,
            },
          }
        : {
            grid_type: 'RangeGrid',
            grid: {
              start_date: curveSet.as_of_date,
              end_date: rangeEndDate,
              step_number: rangeStepN,
              step_time_unit: rangeStepUnit,
              business_days_only: rangeBusinessDaysOnly,
              calendar: rangeCalendar,
              business_day_convention: tenorGridBdc,
            },
          };

      const buildCurveQuery = (curveId: string) => ({
        curve_id: curveId,
        measures: ['DF', 'ZERO', 'FWD'],
        zero: { compounding: 'Continuous', frequency: 'Annual' },
        fwd: { forward_type: 'Instantaneous', compounding: 'Continuous' },
        grid: gridConfig,
      });

      const buildInflationQuery = (curve: Curve) => ({
        curve_id: curve.id,
        measures: getInflationMeasures(curve.inflation_curve?.kind || 'ZeroInflation'),
        grid: gridConfig,
      });

      const curvesPayload = nominalCurves.map((curve) => {
        const points = (curve.points || []).map((wrapper) => {
          const point = { ...wrapper.point } as any;
          delete point.sw_floating_leg_index;
          delete point.float_index_type;
          delete point.overnight_index_spec;

          const needsDiscountDep =
            curve.discount_curve_id &&
            (wrapper.point_type === 'SwapHelper' || wrapper.point_type === 'OISHelper' || wrapper.point_type === 'DatedOISHelper');
          if (needsDiscountDep) {
            point.deps = {
              ...(point.deps || {}),
              discount_curve: { id: curve.discount_curve_id },
            };
          }

          return { ...wrapper, point };
        });

        return {
          id: curve.id,
          day_counter: curve.day_counter,
          interpolator: curve.interpolator,
          bootstrap_trait: curve.bootstrap_trait,
          reference_date: curve.reference_date || curveSet.as_of_date,
          points: resolveQuoteIds(points, curveSet.as_of_date),
        };
      });

      const requestParts: Record<string, unknown> = {};
      const combinedResults: BootstrapCurveResult[] = [];
      let totalDuration = 0;
      const resolvedNominalIndices = nominalCurves.length > 0
        ? (await resolveIndicesFromCurves(nominalCurves)).map(normalizeIndexDefForApi)
        : [];

      if (nominalCurves.length > 0) {
        const nominalRequest = {
          pricing: {
            as_of_date: curveSet.as_of_date,
            indices: resolvedNominalIndices,
            curves: curvesPayload.map(normalizeCurveForApi),
          },
          queries: nominalCurves.map((curve) => buildCurveQuery(curve.id)),
        };
        requestParts.nominal = nominalRequest;

        const nominalResult = await orchestratorPost<{ results: BootstrapCurveResult[] }>(
          '/v1/curve-preview',
          nominalRequest,
        );
        totalDuration += nominalResult.duration_ms;
        if (!nominalResult.ok || !nominalResult.data?.results) {
          setError(nominalResult.ok ? 'Bootstrap failed' : nominalResult.envelope.error);
          setLastRequest(nominalRequest);
          return;
        }
        combinedResults.push(...nominalResult.data.results);
      }

      if (inflationCurves.length > 0) {
        const inflationRequest = {
          pricing: {
            as_of_date: curveSet.as_of_date,
            ...(curvesPayload.length > 0 ? { curves: curvesPayload.map(normalizeCurveForApi) } : {}),
            ...(resolvedNominalIndices.length > 0 ? { indices: resolvedNominalIndices } : {}),
            inflation_indices: inflationCurves.map(buildInflationIndexSpec),
            inflation_curves: inflationCurves.map((curve) => buildInflationCurveSpec(curve, curveSet.as_of_date)),
          },
          queries: inflationCurves.map(buildInflationQuery),
        };
        requestParts.inflation = inflationRequest;

        const inflationResult = await orchestratorPost<{ results: BootstrapCurveResult[] }>(
          '/v1/curve-preview',
          inflationRequest,
        );
        totalDuration += inflationResult.duration_ms;
        if (!inflationResult.ok || !inflationResult.data?.results) {
          setError(inflationResult.ok ? 'Bootstrap failed' : inflationResult.envelope.error);
          setLastRequest(inflationRequest);
          return;
        }
        combinedResults.push(...inflationResult.data.results);
      }

      setBootstrapResults(combinedResults);
      setLastRequest(requestParts.nominal && requestParts.inflation
        ? requestParts
        : (requestParts.nominal || requestParts.inflation || null));

      const failed = combinedResults.filter((result) => result.error?.error_message);
      if (failed.length > 0) {
        const firstFailure = failed[0]?.error?.error_message || 'Bootstrap failed';
        setError(
          failed.length === combinedResults.length
            ? firstFailure
            : `${failed.length} of ${combinedResults.length} curve(s) failed. First error: ${firstFailure}`,
        );
      } else {
        setSuccess(`Bootstrapped ${combinedResults.length} curve(s) in ${totalDuration.toFixed(0)}ms`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bootstrap failed');
    } finally {
      setBootstrapping(false);
    }
  };

  const handleDownloadRequest = () => {
    if (!lastRequest || !curveSet) return;
    const blob = new Blob([JSON.stringify(lastRequest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `curve-set-${curveSet.name.toLowerCase().replace(/\s+/g, '-')}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleDownloadBootstrap = () => {
    if (!curveSet || bootstrapResults.length === 0) return;
    const namedCurves = new Map(availableCurves.map((curve) => [curve.id, curve.name]));
    const exportData = bootstrapResults.map((result) => ({
      curve_id: result.id,
      curve_name: namedCurves.get(result.id) || result.id,
      reference_date: result.reference_date,
      grid_dates: result.grid_dates,
      pillar_dates: result.pillar_dates,
      series: result.series,
      error: result.error,
    }));
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${curveSet.name.toLowerCase().replace(/\s+/g, '-')}-bootstrap-${curveSet.as_of_date}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const renderTable = (result: BootstrapCurveResult) => {
    const measures = result.series.map((series) => series.measure || 'DF');
    return (
      <div className="overflow-x-auto border border-[#e5e5e5] rounded-lg">
        <table className="min-w-full text-xs">
          <thead className="bg-[#fafafa] border-b border-[#e5e5e5]">
            <tr>
              <th className="px-3 py-2 text-left text-[#737373] font-medium">Date</th>
              {measures.map((measure) => (
                <th key={measure} className="px-3 py-2 text-right text-[#737373] font-medium">{measure}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.grid_dates.map((date, index) => (
              <tr key={date} className="border-t border-[#f5f5f5]">
                <td className="px-3 py-2 text-[#525252]">{date}</td>
                {result.series.map((series) => (
                  <td key={`${date}-${series.measure || 'DF'}`} className="px-3 py-2 text-right text-[#0a0a0a]">
                    {(series.values[index] ?? 0).toFixed(8)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  const renderPlot = () => {
    if (!selectedCurve) return null;
    if (selectedCurve.role === 'inflation') {
      return (
        <InflationCurveChart
          points={selectedCurve.inflation_curve?.points || []}
          bootstrapResult={selectedResult}
          showMeasure={showInflationMeasure}
        />
      );
    }
    return (
      <CurveChart
        points={selectedCurve.points || []}
        bootstrapResult={selectedResult}
        showMeasure={showMeasure}
      />
    );
  };

  const renderJson = () => (
    <pre className="bg-[#fafafa] border border-[#e5e5e5] rounded-lg p-4 text-xs overflow-x-auto text-[#262626]">
      {JSON.stringify(lastRequest, null, 2)}
    </pre>
  );

  if (!curveSet) {
    return (
      <div className="min-h-screen bg-[#fafafa]">
        <Header />
        <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12 text-center">
          <p className="text-[#737373]">{error || 'Loading...'}</p>
        </main>
      </div>
    );
  }

  const selectedRoleCurves = availableCurves.filter((curve) => curve.role === (selectedRef?.role || 'discount'));
  const selectedCreditCurveIds = new Set(curveSet.credit_curve_ids || []);
  const selectedCreditCurves = availableCreditCurves.filter((curve) => selectedCreditCurveIds.has(curve.id));

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-7xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <div className="flex flex-col gap-4 mb-6 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <BackLink onClick={() => navigate('/curve-sets')} label={getBackLabel(ui.plural)} className="flex items-center gap-1 text-sm text-[#737373] hover:text-[#0a0a0a] mb-2 transition-colors" />
            <h1 className="text-2xl font-semibold text-[#0a0a0a]">{curveSet.name}</h1>
            <p className="text-[#737373] mt-1">Reference pricing environment composed from standalone curves</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {lastRequest && (
              <button onClick={handleDownloadRequest} className={buttonClass}>
                Download Request
              </button>
            )}
            {bootstrapResults.length > 0 && (
              <button onClick={handleDownloadBootstrap} className={buttonClass}>
                Download Results
              </button>
            )}
            <button
              onClick={handleBootstrap}
              disabled={bootstrapping}
              className="px-5 py-2 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#a67c3a] disabled:opacity-50 transition-colors flex items-center gap-2"
            >
              {bootstrapping ? (
                <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
              )}
              Bootstrap Environment
            </button>
          </div>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-center justify-between">
            <p className="text-sm text-red-600">{error}</p>
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600">✕</button>
          </div>
        )}
        {success && (
          <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg">
            <p className="text-sm text-green-600">{success}</p>
          </div>
        )}

        <div className="grid lg:grid-cols-4 gap-6">
          <div className="space-y-4">
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Environment</h2>
              <div className="space-y-3">
                <div>
                  <label className={labelClass}>Name</label>
                  <input value={curveSet.name} onChange={(e) => updateMeta('name', e.target.value)} className={inputClass} />
                </div>
                <div>
                  <label className={labelClass}>Description</label>
                  <textarea value={curveSet.description || ''} onChange={(e) => updateMeta('description', e.target.value)} className={`${inputClass} min-h-[90px]`} />
                </div>
                <div>
                  <label className={labelClass}>Currency</label>
                  <select value={curveSet.currency} onChange={(e) => updateMeta('currency', e.target.value)} className={inputClass}>
                    {CURRENCIES.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>As Of Date</label>
                  <input type="date" value={curveSet.as_of_date} onChange={(e) => updateMeta('as_of_date', e.target.value)} className={inputClass} />
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-[#0a0a0a]">Market References</h2>
                <span className="text-xs text-[#737373]">
                  {resolvedRefs.length + selectedCreditCurves.length} total
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5 mb-3">
                <button onClick={() => addRef('discount')} className="px-2 py-1 text-[10px] font-medium rounded bg-emerald-50 text-emerald-700 hover:bg-emerald-100 transition-colors">+ Discount</button>
                <button onClick={() => addRef('forward')} className="px-2 py-1 text-[10px] font-medium rounded bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors">+ Forward</button>
                <button onClick={() => addRef('basis')} className="px-2 py-1 text-[10px] font-medium rounded bg-purple-50 text-purple-700 hover:bg-purple-100 transition-colors">+ Basis</button>
                <button onClick={() => addRef('inflation')} className="px-2 py-1 text-[10px] font-medium rounded bg-rose-50 text-rose-700 hover:bg-rose-100 transition-colors">+ Inflation</button>
              </div>
              {resolvedRefs.length === 0 ? (
                <p className="text-xs text-[#a3a3a3]">No references yet. Add one of the standalone curves above.</p>
              ) : (
                <div className="space-y-1.5">
                  {resolvedRefs.map((ref, index) => {
                    const result = ref.curve ? bootstrapResults.find((entry) => entry.id === ref.curve!.id) : null;
                    return (
                      <div
                        key={ref.id}
                        onClick={() => setSelectedRefIdx(index)}
                        className={`flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors group ${
                          index === selectedRefIdx ? 'bg-[#0a0a0a] text-white' : 'hover:bg-[#f5f5f5]'
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className={`w-2 h-2 rounded-full ${!ref.curve && ref.curve_id ? 'bg-red-400' : result?.error ? 'bg-red-400' : result ? 'bg-green-400' : 'bg-[#a3a3a3]'}`} />
                            <span className="text-xs font-medium truncate">{ref.curve?.name || ref.label || ref.curve_id || 'Select a curve'}</span>
                          </div>
                          <p className={`text-[10px] mt-0.5 ${index === selectedRefIdx ? 'text-white/70' : 'text-[#a3a3a3]'}`}>
                            {CURVE_ROLES.find((role) => role.value === ref.role)?.label || ref.role}
                          </p>
                        </div>
                        <button
                          onClick={(e) => { e.stopPropagation(); removeRef(index); }}
                          className={`p-0.5 opacity-0 group-hover:opacity-100 transition-opacity ${index === selectedRefIdx ? 'text-white/60 hover:text-white' : 'text-[#a3a3a3] hover:text-red-500'}`}
                          title="Remove reference"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
              <div className="mt-4 pt-4 border-t border-[#f0f0f0]">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-[#737373]">Credit Curves</h3>
                  <span className="text-xs text-[#737373]">{selectedCreditCurves.length} selected</span>
                </div>
                {availableCreditCurves.length === 0 ? (
                  <div className="space-y-2">
                    <p className="text-xs text-[#a3a3a3]">No credit curves saved yet.</p>
                    <button onClick={() => navigate('/credit-curves')} className={buttonClass}>
                      Open Credit Curves
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {availableCreditCurves.map((creditCurve) => {
                      const checked = selectedCreditCurveIds.has(creditCurve.id);
                      return (
                        <label
                          key={creditCurve.id}
                          className={`flex items-start gap-3 px-3 py-2 rounded-lg border cursor-pointer transition-colors ${
                            checked
                              ? 'border-amber-300 bg-amber-50'
                              : 'border-[#e5e5e5] hover:bg-[#fafafa]'
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleCreditCurve(creditCurve.id)}
                            className="mt-0.5 rounded border-[#d4d4d4] text-[#8a6a2f] focus:ring-[#8a6a2f]"
                          />
                          <div className="min-w-0">
                            <div className="text-xs font-medium text-[#0a0a0a] truncate">
                              {creditCurve.name || creditCurve.id}
                            </div>
                            <div className="text-[10px] text-[#737373] mt-0.5">
                              {creditCurve.reference_entity || 'Credit curve'} • {creditCurve.source}
                            </div>
                          </div>
                        </label>
                      );
                    })}
                    <button onClick={() => navigate('/credit-curves')} className={buttonClass}>
                      Manage Credit Curves
                    </button>
                  </div>
                )}
              </div>
            </div>

            {bootstrapResults.length > 0 && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
                <h2 className="text-sm font-semibold text-[#0a0a0a] mb-2">Bootstrap Status</h2>
                <div className="space-y-1 text-xs">
                  {bootstrapResults.map((result) => (
                    <div key={result.id} className="flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${result.error ? 'bg-red-400' : 'bg-green-400'}`} />
                      <span className="truncate text-[#525252]">{availableCurveMap.get(result.id)?.name || result.id}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-4">
              <div className="flex items-center justify-between cursor-pointer" onClick={() => setShowGridOptions((open) => !open)}>
                <h2 className="text-sm font-semibold text-[#0a0a0a]">Grid Options</h2>
                <svg className={`w-4 h-4 text-[#737373] transition-transform ${showGridOptions ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </div>
              {showGridOptions && (
                <div className="mt-4 space-y-4">
                  <div>
                    <label className={labelClass}>Grid Type</label>
                    <div className="flex gap-2">
                      {([
                        { value: 'tenor' as GridType, label: 'Tenor Grid' },
                        { value: 'range' as GridType, label: 'Date Range' },
                      ]).map(({ value, label }) => (
                        <button
                          key={value}
                          onClick={() => setGridType(value)}
                          className={`flex-1 px-2 py-1.5 text-xs font-medium rounded-lg transition-colors ${gridType === value ? 'bg-[#0a0a0a] text-white' : 'bg-[#f5f5f5] text-[#525252] hover:bg-[#e5e5e5]'}`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {gridType === 'tenor' ? (
                    <div className="space-y-3 pt-3 border-t border-[#e5e5e5]">
                      <div>
                        <label className={labelClass}>Calendar</label>
                        <select value={tenorGridCalendar} onChange={(e) => setTenorGridCalendar(e.target.value)} className={`${inputClass} text-xs`}>
                          {CALENDARS.map((calendar) => <option key={calendar}>{calendar}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className={labelClass}>Convention</label>
                        <select value={tenorGridBdc} onChange={(e) => setTenorGridBdc(e.target.value)} className={`${inputClass} text-xs`}>
                          {BUSINESS_DAY_CONVENTIONS.map((convention) => <option key={convention}>{convention}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className={labelClass}>Tenors</label>
                        <div className="space-y-1 max-h-40 overflow-y-auto">
                          {tenorGrid.map((tenor, index) => (
                            <div key={`${tenor.n}-${tenor.unit}-${index}`} className="flex gap-1 items-center">
                              <input type="number" value={tenor.n} onChange={(e) => updateTenor(index, 'n', parseInt(e.target.value, 10) || 1)} className="w-14 px-2 py-1 text-xs border border-[#d4d4d4] rounded" />
                              <select value={tenor.unit} onChange={(e) => updateTenor(index, 'unit', e.target.value as TimeUnit)} className="flex-1 px-2 py-1 text-xs border border-[#d4d4d4] rounded">
                                {TIME_UNITS.map((unit) => <option key={unit}>{unit}</option>)}
                              </select>
                              <button onClick={() => removeTenor(index)} className="p-1 text-[#a3a3a3] hover:text-red-500">
                                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            </div>
                          ))}
                        </div>
                        <button onClick={addTenor} className="mt-2 text-xs text-[#8a6a2f] hover:underline">+ Add tenor</button>
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-3 pt-3 border-t border-[#e5e5e5]">
                      <div>
                        <label className={labelClass}>End Date</label>
                        <input type="date" value={rangeEndDate} onChange={(e) => setRangeEndDate(e.target.value)} className={`${inputClass} text-xs`} />
                      </div>
                      <div>
                        <label className={labelClass}>Step</label>
                        <div className="flex gap-1">
                          <input type="number" value={rangeStepN} onChange={(e) => setRangeStepN(parseInt(e.target.value, 10) || 1)} className="w-14 px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg" />
                          <select value={rangeStepUnit} onChange={(e) => setRangeStepUnit(e.target.value)} className="flex-1 px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg">
                            {TIME_UNITS.map((unit) => <option key={unit}>{unit}</option>)}
                          </select>
                        </div>
                      </div>
                      <div>
                        <label className={labelClass}>Calendar</label>
                        <select value={rangeCalendar} onChange={(e) => setRangeCalendar(e.target.value)} className={`${inputClass} text-xs`}>
                          {CALENDARS.map((calendar) => <option key={calendar}>{calendar}</option>)}
                        </select>
                      </div>
                      <label className="flex items-center gap-2 text-xs text-[#525252]">
                        <input type="checkbox" checked={rangeBusinessDaysOnly} onChange={(e) => setRangeBusinessDaysOnly(e.target.checked)} className="rounded border-[#d4d4d4]" />
                        Business days only
                      </label>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          <div className="lg:col-span-3 space-y-6">
            {selectedRef ? (
              <>
                <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-sm font-semibold text-[#0a0a0a]">Reference Slot</h2>
                    <span className={`px-2 py-0.5 text-xs font-medium rounded ${roleBadgeClass(selectedRef.role)}`}>
                      {CURVE_ROLES.find((role) => role.value === selectedRef.role)?.label || selectedRef.role}
                    </span>
                  </div>
                  <div className="grid md:grid-cols-2 gap-4">
                    <div>
                      <label className={labelClass}>Role</label>
                      <select
                        value={selectedRef.role}
                        onChange={(e) => {
                          const nextRole = e.target.value as CurveRole;
                          const keepCurve = selectedCurve && selectedCurve.role === nextRole ? selectedCurve.id : '';
                          updateRef(selectedRefIdx, { role: nextRole, curve_id: keepCurve });
                        }}
                        className={inputClass}
                      >
                        {CURVE_ROLES.map((role) => <option key={role.value} value={role.value}>{role.label}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className={labelClass}>Optional Label</label>
                      <input value={selectedRef.label || ''} onChange={(e) => updateRef(selectedRefIdx, { label: e.target.value })} className={inputClass} placeholder="Primary discount, 6M forwarding, ..." />
                    </div>
                    <div className="md:col-span-2">
                      <label className={labelClass}>Standalone Curve</label>
                      <select
                        value={selectedRef.curve_id}
                        onChange={(e) => updateRef(selectedRefIdx, { curve_id: e.target.value })}
                        className={inputClass}
                      >
                        <option value="">Select a standalone curve...</option>
                        {selectedRoleCurves.map((curve) => (
                          <option key={curve.id} value={curve.id}>{curve.name}</option>
                        ))}
                      </select>
                      <div className="flex flex-wrap gap-2 mt-2">
                        <button onClick={() => navigate(selectedRef.role === 'inflation' ? '/inflation-curves/new' : '/yield-curves/new')} className={buttonClass}>
                          Create Matching Curve
                        </button>
                        {selectedCurve && (
                          <button onClick={() => navigate(curvePath(selectedCurve))} className={buttonClass}>
                            Open Standalone Curve
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                {selectedCurve ? (
                  <>
                    <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                      <div className="flex items-center justify-between mb-4">
                        <div>
                          <h2 className="text-sm font-semibold text-[#0a0a0a]">{selectedCurve.name}</h2>
                          <p className="text-xs text-[#737373] mt-1">This curve is managed in its dedicated standalone app and referenced here by ID.</p>
                        </div>
                        <span className={`px-2 py-0.5 text-xs font-medium rounded ${roleBadgeClass(selectedCurve.role)}`}>
                          {selectedCurve.role}
                        </span>
                      </div>
                      <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-3 text-sm">
                        <div className="p-3 bg-[#fafafa] border border-[#f0f0f0] rounded-lg">
                          <p className="text-xs text-[#737373] mb-1">Reference Date</p>
                          <p className="font-medium text-[#0a0a0a]">{selectedCurve.reference_date || curveSet.as_of_date}</p>
                        </div>
                        <div className="p-3 bg-[#fafafa] border border-[#f0f0f0] rounded-lg">
                          <p className="text-xs text-[#737373] mb-1">{selectedCurve.role === 'inflation' ? 'Helpers' : 'Instruments'}</p>
                          <p className="font-medium text-[#0a0a0a]">
                            {selectedCurve.role === 'inflation' ? (selectedCurve.inflation_curve?.points.length || 0) : (selectedCurve.points.length || 0)}
                          </p>
                        </div>
                        <div className="p-3 bg-[#fafafa] border border-[#f0f0f0] rounded-lg">
                          <p className="text-xs text-[#737373] mb-1">Dependency</p>
                          <p className="font-medium text-[#0a0a0a]">{selectedCurve.discount_curve_id || 'Self-contained'}</p>
                        </div>
                        <div className="p-3 bg-[#fafafa] border border-[#f0f0f0] rounded-lg">
                          <p className="text-xs text-[#737373] mb-1">Interpolator</p>
                          <p className="font-medium text-[#0a0a0a]">{selectedCurve.role === 'inflation' ? (selectedCurve.inflation_curve?.interpolator || 'Linear') : selectedCurve.interpolator}</p>
                        </div>
                      </div>
                    {selectedCreditCurves.length > 0 && (
                      <div className="mt-4 pt-4 border-t border-[#f0f0f0]">
                        <p className="text-xs text-[#737373] mb-2">Attached Credit Curves</p>
                        <div className="flex flex-wrap gap-1.5">
                          {selectedCreditCurves.map((creditCurve) => (
                            <span key={creditCurve.id} className="px-2 py-0.5 text-[10px] font-medium rounded bg-amber-50 text-amber-700">
                              {creditCurve.name || creditCurve.id}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    </div>

                    <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                      {renderPlot()}
                    </div>

                    {(selectedResult || lastRequest) && (
                      <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                        <div className="flex items-center justify-between mb-4">
                          <div className="flex gap-1">
                            {(['table', 'plot', 'json'] as DiagTab[]).map((tab) => (
                              <button
                                key={tab}
                                onClick={() => setDiagTab(tab)}
                                className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${diagTab === tab ? 'bg-[#0a0a0a] text-white' : 'bg-[#f5f5f5] text-[#737373] hover:bg-[#e5e5e5]'}`}
                              >
                                {tab === 'table' ? 'Curve Table' : tab === 'plot' ? 'Plot' : 'API JSON'}
                              </button>
                            ))}
                          </div>
                          {diagTab === 'plot' && (
                            <div className="flex gap-1">
                              {selectedCurve.role === 'inflation'
                                ? (['ZeroRate', 'YoYRate'] as const).map((measure) => (
                                    <button
                                      key={measure}
                                      onClick={() => setShowInflationMeasure(measure)}
                                      className={`px-2 py-1 text-xs font-medium rounded ${showInflationMeasure === measure ? 'bg-[#8a6a2f] text-white' : 'bg-[#f5f5f5] text-[#737373] hover:bg-[#e5e5e5]'}`}
                                    >
                                      {measure === 'ZeroRate' ? 'Zero' : 'YoY'}
                                    </button>
                                  ))
                                : (['ZERO', 'FWD', 'DF'] as const).map((measure) => (
                                    <button
                                      key={measure}
                                      onClick={() => setShowMeasure(measure)}
                                      className={`px-2 py-1 text-xs font-medium rounded ${showMeasure === measure ? 'bg-[#8a6a2f] text-white' : 'bg-[#f5f5f5] text-[#737373] hover:bg-[#e5e5e5]'}`}
                                    >
                                      {measure}
                                    </button>
                                  ))}
                            </div>
                          )}
                        </div>

                        {selectedResult?.error ? (
                          <div className="p-4 bg-red-50 rounded-lg">
                            <p className="text-sm text-red-600">Error: {selectedResult.error.error_message}</p>
                          </div>
                        ) : selectedResult ? (
                          diagTab === 'table' ? renderTable(selectedResult) : diagTab === 'plot' ? renderPlot() : renderJson()
                        ) : diagTab === 'json' && lastRequest ? (
                          renderJson()
                        ) : (
                          <p className="text-sm text-[#737373]">Bootstrap this environment to inspect diagnostics.</p>
                        )}
                      </div>
                    )}
                  </>
                ) : (
                  <div className="bg-white border border-[#e5e5e5] rounded-xl p-8 text-center">
                    <h2 className="text-lg font-medium text-[#0a0a0a] mb-2">Select a standalone curve</h2>
                    <p className="text-[#737373] max-w-xl mx-auto">
                      This curve set no longer authors curves inline. Pick a saved standalone curve for this slot, or create one in the dedicated yield or inflation curve app.
                    </p>
                  </div>
                )}
              </>
            ) : (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-8 text-center">
                <h2 className="text-lg font-medium text-[#0a0a0a] mb-2">No reference selected</h2>
                <p className="text-[#737373]">Add a discount, forward, basis, or inflation reference from the left panel to compose this pricing environment.</p>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
