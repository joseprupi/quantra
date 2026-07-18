// Curve Builder page - create, edit, and bootstrap yield curves
import { useState, useEffect } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveChart from '../../components/curves/CurveChart';
import CurvePointEditor from '../../components/curves/CurvePointEditor';
import CurvePointsList from '../../components/curves/CurvePointsList';
import InflationCurveFields from '../../components/curves/InflationCurveFields';
import InflationCurveChart from '../../components/curves/InflationCurveChart';
import InflationPointEditor from '../../components/curves/InflationPointEditor';
import InflationPointsList from '../../components/curves/InflationPointsList';
import BackLink from '../../components/ui/BackLink';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getBackLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';
import {
  Curve,
  CurvePoint,
  CurveRole,
  IndexDef,
  DAY_COUNTERS,
  INTERPOLATORS,
  BOOTSTRAP_TRAITS,
  CURRENCIES,
  CALENDARS,
  BUSINESS_DAY_CONVENTIONS,
  TIME_UNITS,
  CURVE_ROLES,
  collectIndexRefIds,
} from '../../lib/types';
import {
  saveCurve,
  getCurveById,
  exportCurve,
  generateId,
  getSavedCurves,
} from '../../lib/storage/curves';
import { BootstrapCurveResult } from '../../lib/quantra-types';
import { orchestratorPost } from '../../lib/api/orchestrator';
import HistoryPanel from '../../components/products/HistoryPanel';
import { normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { buildInflationCurveSpec, buildInflationIndexSpec, cloneDefaultInflationCurve, ensureInflationBaseFixings, getInflationMeasures } from '../../lib/inflation-curves';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';

type GridType = 'tenor' | 'range';

interface TenorItem {
  n: number;
  unit: string;
}

function curveListPathForRole(role: CurveRole) {
  return role === 'inflation' ? '/inflation-curves' : '/yield-curves';
}

async function resolveIndexDefsFromCurve(points: CurvePoint[]): Promise<IndexDef[]> {
  const ids = collectIndexRefIds(points || []);
  if (ids.length === 0) return [];

  const savedSpecs = await indexStore.getAll();
  const result: IndexDef[] = [];
  const seen = new Set<string>();

  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);

    const saved = savedSpecs.find((spec) => spec.id === id);
    if (!saved) continue;
    const def = storedToRateIndexDef(saved);
    if (def) result.push(def);
  }

  return result;
}

export default function CurveBuilder() {
  const location = useLocation();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const isEditing = !!id;
  const pageMode = location.pathname.startsWith('/inflation-curves')
    ? 'inflation'
    : location.pathname.startsWith('/yield-curves')
      ? 'yield'
      : 'legacy';
  const roleLocked = pageMode !== 'legacy';
  const isInflationPage = pageMode === 'inflation';
  
  // Curve metadata
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [currency, setCurrency] = useState('EUR');
  const [role, setRole] = useState<CurveRole>(isInflationPage ? 'inflation' : 'discount');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [dayCounter, setDayCounter] = useState('Actual365Fixed');
  const [interpolator, setInterpolator] = useState('LogLinear');
  const [bootstrapTrait, setBootstrapTrait] = useState('Discount');
  const [inflationCurve, setInflationCurve] = useState(() => cloneDefaultInflationCurve('EUR'));
  const [referenceDate, setReferenceDate] = useState(
    new Date().toISOString().split('T')[0]
  );
  const [availableDiscountCurves, setAvailableDiscountCurves] = useState<Curve[]>([]);
  
  // Curve points
  const [points, setPoints] = useState<CurvePoint[]>([]);
  
  // Grid options
  const [gridType, setGridType] = useState<GridType>('tenor');
  const [tenorGrid, setTenorGrid] = useState<TenorItem[]>([
    { n: 1, unit: 'Months' },
    { n: 3, unit: 'Months' },
    { n: 6, unit: 'Months' },
    { n: 1, unit: 'Years' },
    { n: 2, unit: 'Years' },
    { n: 5, unit: 'Years' },
    { n: 10, unit: 'Years' },
    { n: 30, unit: 'Years' },
  ]);
  const [tenorGridCalendar, setTenorGridCalendar] = useState('TARGET');
  const [tenorGridBdc, setTenorGridBdc] = useState('ModifiedFollowing');
  
  // Range grid options
  const [rangeEndDate, setRangeEndDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() + 30);
    return d.toISOString().split('T')[0];
  });
  const [rangeStepN, setRangeStepN] = useState(1);
  const [rangeStepUnit, setRangeStepUnit] = useState('Months');
  const [rangeBusinessDaysOnly, setRangeBusinessDaysOnly] = useState(false);
  const [rangeCalendar, setRangeCalendar] = useState('TARGET');
  
  // Bootstrap results
  const [bootstrapResult, setBootstrapResult] = useState<BootstrapCurveResult | null>(null);
  const [bootstrapping, setBootstrapping] = useState(false);
  const [lastRequest, setLastRequest] = useState<object | null>(null);
  const [showMeasure, setShowMeasure] = useState<'ZERO' | 'FWD' | 'DF'>('ZERO');
  const [showInflationMeasure, setShowInflationMeasure] = useState<'ZeroRate' | 'YoYRate'>('ZeroRate');
  const [showGridOptions, setShowGridOptions] = useState(false);
  
  // UI state
  const [showPointEditor, setShowPointEditor] = useState(false);
  const [editingPointIndex, setEditingPointIndex] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isInflationCurve = role === 'inflation';
  const curveListPath = curveListPathForRole(role);
  const ui = isInflationCurve ? entityUi.inflationCurve : entityUi.yieldCurve;
  const visibleRoles = roleLocked
    ? CURVE_ROLES.filter((entry) => (pageMode === 'yield' ? entry.value !== 'inflation' : entry.value === 'inflation'))
    : CURVE_ROLES;
  
  // Load existing curve if editing
  useEffect(() => {
    if (id) {
      const curve = getCurveById(id);
      if (curve) {
        if (pageMode === 'inflation' && curve.role !== 'inflation') {
          navigate(`/yield-curves/${curve.id}`, { replace: true });
          return;
        }
        if (pageMode === 'yield' && curve.role === 'inflation') {
          navigate(`/inflation-curves/${curve.id}`, { replace: true });
          return;
        }
        setName(curve.name);
        setDescription(curve.description || '');
        setCurrency(curve.currency);
        setRole(curve.role || 'discount');
        setDiscountCurveId(curve.discount_curve_id || '');
        setDayCounter(curve.day_counter);
        setInterpolator(curve.interpolator);
        setBootstrapTrait(curve.bootstrap_trait);
        setInflationCurve(curve.inflation_curve || cloneDefaultInflationCurve(curve.currency));
        setReferenceDate(curve.reference_date);
        setPoints(curve.points);
      } else {
        setError('Curve not found');
      }
    }
  }, [id, navigate, pageMode]);

  useEffect(() => {
    if (!id && roleLocked) {
      setRole((prev) => {
        if (pageMode === 'inflation') return 'inflation';
        return prev === 'inflation' ? 'discount' : prev;
      });
    }
  }, [id, pageMode, roleLocked]);

  useEffect(() => {
    const saved = getSavedCurves().filter(c => c.role === 'discount' && c.id !== id);
    setAvailableDiscountCurves(saved);
  }, [id]);

  useEffect(() => {
    setInflationCurve(prev => ({
      ...prev,
      index: { ...prev.index, currency },
    }));
  }, [currency]);

  useEffect(() => {
    if (role === 'inflation') {
      setShowInflationMeasure(inflationCurve.kind === 'YoYInflation' ? 'YoYRate' : 'ZeroRate');
    }
  }, [role, inflationCurve.kind]);
  
  // Clear bootstrap result when points change
  useEffect(() => {
    setBootstrapResult(null);
  }, [points, inflationCurve, dayCounter, interpolator, bootstrapTrait, referenceDate, role]);

  useEffect(() => {
    setShowPointEditor(false);
    setEditingPointIndex(null);
  }, [role]);
  
  const handleAddPoint = (point: CurvePoint) => {
    if (editingPointIndex !== null) {
      const newPoints = [...points];
      newPoints[editingPointIndex] = point;
      setPoints(newPoints);
      setEditingPointIndex(null);
    } else {
      setPoints([...points, point]);
    }
    setShowPointEditor(false);
  };
  
  const handleEditPoint = (index: number) => {
    setEditingPointIndex(index);
    setShowPointEditor(true);
  };
  
  const handleDeletePoint = (index: number) => {
    if (confirm('Delete this point?')) {
      setPoints(points.filter((_, i) => i !== index));
    }
  };

  const handleAddInflationPoint = (point: typeof inflationCurve.points[number]) => {
    const nextPoints = [...inflationCurve.points];
    if (editingPointIndex !== null) {
      nextPoints[editingPointIndex] = point;
    } else {
      nextPoints.push(point);
    }
    setInflationCurve(prev => ({ ...prev, points: nextPoints }));
    setShowPointEditor(false);
    setEditingPointIndex(null);
  };

  const handleEditInflationPoint = (index: number) => {
    setEditingPointIndex(index);
    setShowPointEditor(true);
  };

  const handleDeleteInflationPoint = (index: number) => {
    if (confirm('Delete this helper?')) {
      setInflationCurve(prev => ({ ...prev, points: prev.points.filter((_, i) => i !== index) }));
    }
  };
  
  const buildQuery = () => {
    const baseQuery: any = {
      measures: ['DF', 'ZERO', 'FWD'],
      zero: {
        compounding: 'Continuous',
        frequency: 'Annual',
      },
      fwd: {
        forward_type: 'Instantaneous',
        compounding: 'Continuous',
      },
    };
    
    if (gridType === 'tenor') {
      baseQuery.grid = {
        grid_type: 'TenorGrid',
        grid: {
          tenors: tenorGrid,
          calendar: tenorGridCalendar,
          business_day_convention: tenorGridBdc,
        },
      };
    } else if (gridType === 'range') {
      baseQuery.grid = {
        grid_type: 'RangeGrid',
        grid: {
          end_date: rangeEndDate,
          step_number: rangeStepN,
          step_time_unit: rangeStepUnit,
          business_days_only: rangeBusinessDaysOnly,
          calendar: rangeCalendar,
        },
      };
    }
    
    return baseQuery;
  };

  const buildInflationQuery = (curveId: string) => {
    const measures = getInflationMeasures(inflationCurve.kind);
    if (gridType === 'tenor') {
      return {
        curve_id: curveId,
        measures,
        grid: {
          grid_type: 'TenorGrid',
          grid: {
            tenors: tenorGrid,
            calendar: tenorGridCalendar,
            business_day_convention: tenorGridBdc,
          },
        },
      };
    }

    return {
      curve_id: curveId,
      measures,
      grid: {
        grid_type: 'RangeGrid',
        grid: {
          start_date: referenceDate,
          end_date: rangeEndDate,
          step_number: rangeStepN,
          step_time_unit: rangeStepUnit,
          business_days_only: rangeBusinessDaysOnly,
          calendar: rangeCalendar,
          business_day_convention: tenorGridBdc,
        },
      },
    };
  };
  
  const handleBootstrap = async () => {
    if (role === 'inflation') {
      if (inflationCurve.points.length === 0) {
        setError('Add at least one inflation helper to bootstrap');
        return;
      }
    } else if (points.length === 0) {
      setError('Add at least one curve point to bootstrap');
      return;
    }
    
    setBootstrapping(true);
    setError(null);
    
    try {
      const curveId = name.toLowerCase().replace(/\s+/g, '_') || 'curve';

      if (role === 'inflation') {
        // Inflation arm: build against `/v1/curve-preview` exactly like
        // the nominal arm. Helper `quote_id`s stay UNRESOLVED —
        // the orchestrator's md_resolution substitutes the value server-side, so
        // we no longer call `resolveInflationPoints` (resolveQuotes: false).
        const inflationCurveInput: Curve = {
          id: curveId,
          name,
          description,
          currency,
          role,
          discount_curve_id: discountCurveId || undefined,
          day_counter: dayCounter as Curve['day_counter'],
          interpolator: interpolator as Curve['interpolator'],
          bootstrap_trait: bootstrapTrait as Curve['bootstrap_trait'],
          reference_date: referenceDate,
          points: [],
          inflation_curve: {
            ...inflationCurve,
            index: { ...inflationCurve.index, currency },
          },
          createdAt: '',
          updatedAt: '',
        };

        const inflationCurveForApi = buildInflationCurveSpec(
          inflationCurveInput,
          referenceDate,
          { resolveQuotes: false },
        );
        // The index body is consumed verbatim (no market data) and REQUIRES at least
        // one base CPI fixing or the engine bootstrap fails. The index UI does
        // not capture fixings yet, so synthesise a base series when absent.
        const inflationIndexForApi = ensureInflationBaseFixings(
          buildInflationIndexSpec(inflationCurveInput),
          referenceDate,
        );

        // The engine's inflation bootstrap discounts the helper legs on a
        // nominal term structure, so it requires at least one nominal curve in
        // `pricing.curves` (a bare inflation-only request fails with "curves is
        // required"). Resolve the selected discount curve from the store,
        // forward its points with quote_ids UNRESOLVED, and carry
        // any rate indices it references — mirroring the nominal arm + the
        // inflation-swap product.
        if (!discountCurveId) {
          setError('Select a nominal discount curve under Inflation Curve Settings — the inflation bootstrap discounts the helper legs on it.');
          setBootstrapping(false);
          return;
        }
        const nominalCurve = getCurveById(discountCurveId);
        if (!nominalCurve) {
          setError('The selected nominal discount curve is no longer available. Pick another under Inflation Curve Settings.');
          setBootstrapping(false);
          return;
        }
        const nominalForApi = normalizeCurveForApi({
          id: nominalCurve.id,
          day_counter: nominalCurve.day_counter,
          interpolator: nominalCurve.interpolator,
          bootstrap_trait: nominalCurve.bootstrap_trait,
          reference_date: nominalCurve.reference_date || referenceDate,
          points: nominalCurve.points,
        });
        const nominalIndices = await resolveIndexDefsFromCurve(nominalCurve.points);

        const request = {
          pricing: {
            as_of_date: referenceDate,
            ...(nominalIndices.length > 0 ? { indices: nominalIndices.map(normalizeIndexDefForApi) } : {}),
            curves: [nominalForApi],
            inflation_indices: [inflationIndexForApi],
            inflation_curves: [inflationCurveForApi],
          },
          queries: [buildInflationQuery(curveId)],
        };

        const result = await orchestratorPost<{ results: BootstrapCurveResult[] }>(
          '/v1/curve-preview',
          request,
        );

        if (result.ok && result.data?.results?.[0]) {
          const firstResult = result.data.results[0];
          setBootstrapResult(firstResult);
          setLastRequest(request);
          if (firstResult.error?.error_message) {
            setError(firstResult.error.error_message);
          }
        } else {
          setError(result.ok ? 'Bootstrap returned no results' : result.envelope.error);
          setLastRequest(null);
        }
        return;
      }

      const resolvedIndices = await resolveIndexDefsFromCurve(points);
      const referencedIds = collectIndexRefIds(points);
      if (referencedIds.length > 0 && resolvedIndices.length === 0) {
        setError('This curve references indices that are not available. Add them in Market Data -> Indices.');
        setBootstrapping(false);
        return;
      }

      // quote_ids stay UNRESOLVED — the orchestrator resolves
      // them via the market-data server, same as product pricing.
      const curveForApi = normalizeCurveForApi({
        id: curveId,
        day_counter: dayCounter,
        interpolator: interpolator,
        bootstrap_trait: bootstrapTrait,
        reference_date: referenceDate,
        points: points,
      } as any);
      
      const request = {
        pricing: {
          as_of_date: referenceDate,
          ...(resolvedIndices.length > 0 ? { indices: resolvedIndices.map(normalizeIndexDefForApi) } : {}),
          curves: [curveForApi],
        },
        queries: [{
          curve_id: curveForApi.id,
          ...buildQuery(),
        }],
      };
      
      const result = await orchestratorPost<{ results: BootstrapCurveResult[] }>(
        '/v1/curve-preview',
        request,
      );

      if (result.ok && result.data?.results?.[0]) {
        const firstResult = result.data.results[0];
        setBootstrapResult(firstResult);
        setLastRequest(request);
        if (firstResult.error?.error_message) {
          setError(firstResult.error.error_message);
        }
      } else {
        // Error envelope: quote misses / translation problems arrive as
        // actionable 422 details from the orchestrator.
        setError(result.ok ? 'Bootstrap returned no results' : result.envelope.error);
        setLastRequest(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bootstrap failed');
      setLastRequest(null);
    } finally {
      setBootstrapping(false);
    }
  };
  
  const handleDownloadRequest = () => {
    if (!lastRequest) return;
    const blob = new Blob([JSON.stringify(lastRequest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${name.toLowerCase().replace(/\s+/g, '-') || 'curve'}-request-${referenceDate}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };
  
  const handleDownloadBootstrap = () => {
    if (!bootstrapResult) return;
    
    // Build a nice downloadable format
    const downloadData = {
      curve_id: bootstrapResult.id,
      reference_date: bootstrapResult.reference_date,
      grid_type: gridType,
      grid_dates: bootstrapResult.grid_dates,
      pillar_dates: bootstrapResult.pillar_dates,
      series: bootstrapResult.series.map(s => ({
        measure: s.measure || 'DF',
        values: s.values,
      })),
      // Also include as a table format for easy CSV conversion
      table: bootstrapResult.grid_dates.map((date, i) => {
        const row: any = { date };
        bootstrapResult.series.forEach(s => {
          const measureName = s.measure || 'DF';
          row[measureName] = s.values[i];
        });
        return row;
      }),
    };
    
    const blob = new Blob([JSON.stringify(downloadData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${name.toLowerCase().replace(/\s+/g, '-') || 'curve'}-bootstrap-${referenceDate}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };
  
  const handleSave = async () => {
    if (!name.trim()) {
      setError('Please enter a curve name');
      return;
    }
    
    if (isInflationCurve ? inflationCurve.points.length === 0 : points.length === 0) {
      setError(isInflationCurve ? 'Please add at least one inflation helper' : 'Please add at least one curve point');
      return;
    }
    
    setSaving(true);
    setError(null);
    
    try {
      const curve: Curve = {
        id: id || generateId(),
        name: name.trim(),
        description: description.trim() || undefined,
        currency,
        role,
        discount_curve_id: discountCurveId || undefined,
        day_counter: dayCounter as Curve['day_counter'],
        interpolator: interpolator as Curve['interpolator'],
        bootstrap_trait: bootstrapTrait as Curve['bootstrap_trait'],
        reference_date: referenceDate,
        points: isInflationCurve ? [] : points,
        inflation_curve: isInflationCurve
          ? {
              ...inflationCurve,
              index: { ...inflationCurve.index, currency },
            }
          : undefined,
        createdAt: '',
        updatedAt: '',
      };
      
      await saveCurve(curve);
      navigate(curveListPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save curve');
    } finally {
      setSaving(false);
    }
  };
  
  const handleExport = () => {
    if (isInflationCurve ? inflationCurve.points.length === 0 : points.length === 0) {
      setError(isInflationCurve ? 'Add inflation helpers before exporting' : 'Add points before exporting');
      return;
    }
    
    const curve: Curve = {
      id: id || generateId(),
      name: name.trim() || 'Untitled Curve',
      description: description.trim() || undefined,
      currency,
      role,
      discount_curve_id: discountCurveId || undefined,
      day_counter: dayCounter as Curve['day_counter'],
      interpolator: interpolator as Curve['interpolator'],
      bootstrap_trait: bootstrapTrait as Curve['bootstrap_trait'],
      reference_date: referenceDate,
      points: isInflationCurve ? [] : points,
      inflation_curve: isInflationCurve
        ? {
            ...inflationCurve,
            index: { ...inflationCurve.index, currency },
          }
        : undefined,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    
    exportCurve(curve);
  };
  
  // Tenor grid management
  const addTenor = () => {
    setTenorGrid([...tenorGrid, { n: 1, unit: 'Years' }]);
  };
  
  const updateTenor = (index: number, field: 'n' | 'unit', value: number | string) => {
    const newGrid = [...tenorGrid];
    newGrid[index] = { ...newGrid[index], [field]: value };
    setTenorGrid(newGrid);
  };
  
  const removeTenor = (index: number) => {
    setTenorGrid(tenorGrid.filter((_, i) => i !== index));
  };
  
  const inputClass = formStyles.input;
  const labelClass = formStyles.label;
  
  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      
      <main className="max-w-7xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <PageHeader
            title={isEditing ? `Edit ${ui.singular}` : `New ${ui.singular}`}
            backLink={<BackLink onClick={() => navigate(curveListPath)} label={getBackLabel(ui.plural)} />}
            actions={
              <>
                <button
                  onClick={handleExport}
                  className={formStyles.secondaryButton}
                >
                  Export JSON
                </button>
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className={`${formStyles.primaryButton} disabled:opacity-50`}
                >
                  {saving ? 'Saving...' : 'Save'}
                </button>
              </>
            }
          />
        </div>
        
        {/* Error */}
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg flex items-center justify-between">
            <p className="text-sm text-red-600">{error}</p>
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600">✕</button>
          </div>
        )}
        
        <div className="grid lg:grid-cols-3 gap-6">
          {/* Left column - Metadata & Points */}
          <div className="lg:col-span-2 space-y-6">
            {/* Curve metadata */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Curve Settings</h2>
              
              <div className="grid sm:grid-cols-2 gap-4">
                <div className="sm:col-span-2">
                  <label className={labelClass}>Curve Name *</label>
                  <input
                    type="text"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder={isInflationCurve ? 'e.g., EUR HICP Inflation' : 'e.g., EUR Discount Curve'}
                    className={inputClass}
                  />
                </div>
                
                <div className="sm:col-span-2">
                  <label className={labelClass}>Description</label>
                  <input
                    type="text"
                    value={description}
                    onChange={e => setDescription(e.target.value)}
                    placeholder="Optional description"
                    className={inputClass}
                  />
                </div>
                
                <div>
                  <label className={labelClass}>Currency</label>
                  <select value={currency} onChange={e => setCurrency(e.target.value)} className={inputClass}>
                    {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                
                <div>
                  <label className={labelClass}>{roleLocked ? 'Curve Family' : 'Curve Role'}</label>
                  {pageMode === 'inflation' ? (
                    <>
                      <input value="Inflation" disabled className={`${inputClass} bg-[#f5f5f5] text-[#737373]`} />
                      <p className="text-[10px] text-[#a3a3a3] mt-1">
                        Dedicated inflation workspace.
                      </p>
                    </>
                  ) : (
                    <>
                      <select value={role} onChange={e => setRole(e.target.value as CurveRole)} className={inputClass}>
                        {visibleRoles.map(r => (
                          <option key={r.value} value={r.value}>{r.label}</option>
                        ))}
                      </select>
                      <p className="text-[10px] text-[#a3a3a3] mt-1">
                        {CURVE_ROLES.find(r => r.value === role)?.description}
                      </p>
                    </>
                  )}
                </div>
                
                <div>
                  <label className={labelClass}>Reference Date</label>
                  <input
                    type="date"
                    value={referenceDate}
                    onChange={e => setReferenceDate(e.target.value)}
                    className={inputClass}
                  />
                </div>
                
                {!isInflationCurve && (
                  <>
                    <div>
                      <label className={labelClass}>Day Counter</label>
                      <select value={dayCounter} onChange={e => setDayCounter(e.target.value)} className={inputClass}>
                        {DAY_COUNTERS.map(dc => <option key={dc} value={dc}>{dc}</option>)}
                      </select>
                    </div>
                    
                    <div>
                      <label className={labelClass}>Interpolation</label>
                      <select value={interpolator} onChange={e => setInterpolator(e.target.value)} className={inputClass}>
                        {INTERPOLATORS.map(i => <option key={i} value={i}>{i}</option>)}
                      </select>
                    </div>
                    
                    <div className="sm:col-span-2">
                      <label className={labelClass}>Bootstrap Trait</label>
                      <select value={bootstrapTrait} onChange={e => setBootstrapTrait(e.target.value)} className={inputClass}>
                        {BOOTSTRAP_TRAITS.map(bt => <option key={bt} value={bt}>{bt}</option>)}
                      </select>
                    </div>
                  </>
                )}
              </div>
            </div>
            
            {isInflationCurve ? (
              <>
                <InflationCurveFields
                  currency={currency}
                  config={inflationCurve}
                  discountCurveId={discountCurveId}
                  availableDiscountCurves={availableDiscountCurves}
                  onChange={setInflationCurve}
                  onChangeDiscountCurveId={(curveId) => setDiscountCurveId(curveId || '')}
                />
                <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-sm font-semibold text-[#0a0a0a]">
                      Inflation Helpers ({inflationCurve.points.length})
                    </h2>
                    <button
                      onClick={() => {
                        setEditingPointIndex(null);
                        setShowPointEditor(true);
                      }}
                      className="px-3 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#a67c3a] transition-colors flex items-center gap-1"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                      </svg>
                      Add Helper
                    </button>
                  </div>

                  {showPointEditor ? (
                    <InflationPointEditor
                      point={editingPointIndex !== null ? inflationCurve.points[editingPointIndex] : undefined}
                      kind={inflationCurve.kind}
                      defaultObservationLag={inflationCurve.index.observation_lag}
                      defaultCalendar={inflationCurve.calendar}
                      defaultPaymentConvention={inflationCurve.business_day_convention}
                      defaultDayCounter={inflationCurve.day_counter}
                      defaultDiscountCurveId={discountCurveId || undefined}
                      availableDiscountCurves={availableDiscountCurves}
                      onSave={handleAddInflationPoint}
                      onCancel={() => {
                        setShowPointEditor(false);
                        setEditingPointIndex(null);
                      }}
                    />
                  ) : (
                    <InflationPointsList
                      points={inflationCurve.points}
                      onEdit={handleEditInflationPoint}
                      onDelete={handleDeleteInflationPoint}
                    />
                  )}
                </div>
              </>
            ) : (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold text-[#0a0a0a]">
                    Curve Points ({points.length})
                  </h2>
                  <button
                    onClick={() => {
                      setEditingPointIndex(null);
                      setShowPointEditor(true);
                    }}
                    className="px-3 py-1.5 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#a67c3a] transition-colors flex items-center gap-1"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                    </svg>
                    Add Instrument
                  </button>
                </div>
                
                {showPointEditor ? (
                  <CurvePointEditor
                    point={editingPointIndex !== null ? points[editingPointIndex] : undefined}
                    onSave={handleAddPoint}
                    onCancel={() => {
                      setShowPointEditor(false);
                      setEditingPointIndex(null);
                    }}
                  />
                ) : (
                  <CurvePointsList
                    points={points}
                    onEdit={handleEditPoint}
                    onDelete={handleDeletePoint}
                  />
                )}
              </div>
            )}
          </div>
          
          {/* Right column - Visualization */}
          <div className="space-y-6">
            {/* Chart with measure selector */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="flex gap-1">
                  {isInflationCurve
                    ? (['ZeroRate', 'YoYRate'] as const).map(m => (
                        <button
                          key={m}
                          onClick={() => setShowInflationMeasure(m)}
                          className={`px-2 py-1 text-xs font-medium rounded ${
                            showInflationMeasure === m
                              ? 'bg-[#8a6a2f] text-white'
                              : 'bg-[#f5f5f5] text-[#737373] hover:bg-[#e5e5e5]'
                          }`}
                        >
                          {m === 'ZeroRate' ? 'Zero' : 'YoY'}
                        </button>
                      ))
                    : (['ZERO', 'FWD', 'DF'] as const).map(m => (
                        <button
                          key={m}
                          onClick={() => setShowMeasure(m)}
                          className={`px-2 py-1 text-xs font-medium rounded ${
                            showMeasure === m
                              ? 'bg-[#8a6a2f] text-white'
                              : 'bg-[#f5f5f5] text-[#737373] hover:bg-[#e5e5e5]'
                          }`}
                        >
                          {m === 'ZERO' ? 'Zero' : m === 'FWD' ? 'Forward' : 'DF'}
                        </button>
                      ))}
                </div>
                <div className="flex gap-1">
                  {lastRequest && (
                    <button
                      onClick={handleDownloadRequest}
                      className="px-2 py-1 text-xs font-medium text-[#8a6a2f] bg-[#f5f0e6] border border-[#e5d9c3] rounded hover:bg-[#efe5d3] transition-colors flex items-center gap-1"
                      title="Download API request JSON"
                    >
                      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                      </svg>
                      Request
                    </button>
                  )}
                  {bootstrapResult && (
                    <button
                      onClick={handleDownloadBootstrap}
                      className="px-2 py-1 text-xs font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded hover:bg-[#f5f5f5] transition-colors flex items-center gap-1"
                      title="Download bootstrapped curve"
                    >
                      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                      </svg>
                      Results
                    </button>
                  )}
                  <button
                    onClick={handleBootstrap}
                    disabled={bootstrapping || (isInflationCurve ? inflationCurve.points.length === 0 : points.length === 0)}
                    className="px-3 py-1 text-xs font-medium text-white bg-[#0a0a0a] rounded hover:bg-[#262626] disabled:opacity-50 transition-colors flex items-center gap-1"
                  >
                    {bootstrapping ? (
                      <>
                        <div className="w-3 h-3 border border-white border-t-transparent rounded-full animate-spin" />
                        Bootstrapping...
                      </>
                    ) : (
                      <>
                        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                        </svg>
                        Bootstrap
                      </>
                    )}
                  </button>
                </div>
              </div>
              {isInflationCurve ? (
                <InflationCurveChart
                  points={inflationCurve.points}
                  bootstrapResult={bootstrapResult}
                  height={320}
                  showMeasure={showInflationMeasure}
                />
              ) : (
                <CurveChart 
                  points={points} 
                  bootstrapResult={bootstrapResult}
                  height={320}
                  showMeasure={showMeasure}
                />
              )}
            </div>
            
            {/* Grid Options */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <div 
                className="flex items-center justify-between cursor-pointer"
                onClick={() => setShowGridOptions(!showGridOptions)}
              >
                <h3 className="text-sm font-semibold text-[#0a0a0a]">Grid Options</h3>
                <svg 
                  className={`w-4 h-4 text-[#737373] transition-transform ${showGridOptions ? 'rotate-180' : ''}`} 
                  fill="none" viewBox="0 0 24 24" stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </div>
              
              {showGridOptions && (
                <div className="mt-4 space-y-4">
                  {/* Grid type selector */}
                  <div>
                    <label className={labelClass}>Grid Type</label>
                    <div className="flex gap-2">
                      {([
                        { value: 'tenor', label: 'Tenor Grid' },
                        { value: 'range', label: 'Date Range' },
                      ] as { value: GridType; label: string }[]).map(({ value, label }) => (
                        <button
                          key={value}
                          onClick={() => setGridType(value)}
                          className={`flex-1 px-2 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                            gridType === value
                              ? 'bg-[#0a0a0a] text-white'
                              : 'bg-[#f5f5f5] text-[#525252] hover:bg-[#e5e5e5]'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <p className="text-xs text-[#a3a3a3] mt-1">
                      {gridType === 'tenor' && 'Returns values at specified tenors from reference date'}
                      {gridType === 'range' && 'Returns values at regular intervals between dates'}
                    </p>
                  </div>
                  
                  {/* Tenor Grid options */}
                  {gridType === 'tenor' && (
                    <div className="space-y-3 pt-3 border-t border-[#e5e5e5]">
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <label className={labelClass}>Calendar</label>
                          <select 
                            value={tenorGridCalendar} 
                            onChange={e => setTenorGridCalendar(e.target.value)} 
                            className={`${inputClass} text-xs`}
                          >
                            {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelClass}>Convention</label>
                          <select 
                            value={tenorGridBdc} 
                            onChange={e => setTenorGridBdc(e.target.value)} 
                            className={`${inputClass} text-xs`}
                          >
                            {BUSINESS_DAY_CONVENTIONS.map(b => <option key={b} value={b}>{b}</option>)}
                          </select>
                        </div>
                      </div>
                      
                      <div>
                        <label className={labelClass}>Tenors</label>
                        <div className="space-y-1 max-h-32 overflow-y-auto">
                          {tenorGrid.map((tenor, i) => (
                            <div key={i} className="flex gap-1 items-center">
                              <input
                                type="number"
                                value={tenor.n}
                                onChange={e => updateTenor(i, 'n', parseInt(e.target.value) || 1)}
                                className="w-16 px-2 py-1 text-xs border border-[#d4d4d4] rounded"
                              />
                              <select
                                value={tenor.unit}
                                onChange={e => updateTenor(i, 'unit', e.target.value)}
                                className="flex-1 px-2 py-1 text-xs border border-[#d4d4d4] rounded"
                              >
                                {TIME_UNITS.map(u => <option key={u} value={u}>{u}</option>)}
                              </select>
                              <button
                                onClick={() => removeTenor(i)}
                                className="p-1 text-[#a3a3a3] hover:text-red-500"
                              >
                                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            </div>
                          ))}
                        </div>
                        <button
                          onClick={addTenor}
                          className="mt-2 text-xs text-[#8a6a2f] hover:underline"
                        >
                          + Add tenor
                        </button>
                      </div>
                    </div>
                  )}
                  
                  {/* Range Grid options */}
                  {gridType === 'range' && (
                    <div className="space-y-3 pt-3 border-t border-[#e5e5e5]">
                      <div>
                        <label className={labelClass}>End Date</label>
                        <input
                          type="date"
                          value={rangeEndDate}
                          onChange={e => setRangeEndDate(e.target.value)}
                          className={`${inputClass} text-xs`}
                        />
                      </div>
                      
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <label className={labelClass}>Step</label>
                          <div className="flex gap-1">
                            <input
                              type="number"
                              value={rangeStepN}
                              onChange={e => setRangeStepN(parseInt(e.target.value) || 1)}
                              className="w-16 px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg"
                            />
                            <select
                              value={rangeStepUnit}
                              onChange={e => setRangeStepUnit(e.target.value)}
                              className="flex-1 px-2 py-1.5 text-xs border border-[#d4d4d4] rounded-lg"
                            >
                              {TIME_UNITS.map(u => <option key={u} value={u}>{u}</option>)}
                            </select>
                          </div>
                        </div>
                        <div>
                          <label className={labelClass}>Calendar</label>
                          <select 
                            value={rangeCalendar} 
                            onChange={e => setRangeCalendar(e.target.value)} 
                            className={`${inputClass} text-xs`}
                          >
                            {CALENDARS.map(c => <option key={c} value={c}>{c}</option>)}
                          </select>
                        </div>
                      </div>
                      
                      <label className="flex items-center gap-2 text-xs text-[#525252]">
                        <input
                          type="checkbox"
                          checked={rangeBusinessDaysOnly}
                          onChange={e => setRangeBusinessDaysOnly(e.target.checked)}
                          className="rounded border-[#d4d4d4]"
                        />
                        Business days only
                      </label>
                    </div>
                  )}
                </div>
              )}
            </div>
            
            {/* Quick stats */}
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h3 className="text-sm font-semibold text-[#0a0a0a] mb-3">Summary</h3>
              <div className="space-y-2 text-sm">
                {isInflationCurve ? (
                  <>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Total Helpers</span>
                      <span className="font-medium">{inflationCurve.points.length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Curve Kind</span>
                      <span className="font-medium">{inflationCurve.kind === 'YoYInflation' ? 'YoY' : 'Zero'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Index ID</span>
                      <span className="font-medium">{inflationCurve.index.id || '—'}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Discount Curve</span>
                      <span className="font-medium">{discountCurveId || 'None'}</span>
                    </div>
                    {inflationCurve.points.length > 0 && (() => {
                      const rates = inflationCurve.points
                        .map(point => point.point.quote_value)
                        .filter((value): value is number => value != null && !isNaN(value));
                      if (rates.length === 0) return null;
                      return (
                        <>
                          <hr className="border-[#e5e5e5] my-2" />
                          <div className="flex justify-between">
                            <span className="text-[#737373]">Lowest Quote</span>
                            <span className="font-mono">{(Math.min(...rates) * 100).toFixed(3)}%</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-[#737373]">Highest Quote</span>
                            <span className="font-mono">{(Math.max(...rates) * 100).toFixed(3)}%</span>
                          </div>
                        </>
                      );
                    })()}
                  </>
                ) : (
                  <>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Total Instruments</span>
                      <span className="font-medium">{points.length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Deposits</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'DepositHelper').length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Swaps</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'SwapHelper').length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">FRAs</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'FRAHelper').length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Futures</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'FutureHelper').length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Bonds</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'BondHelper').length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-[#737373]">OIS</span>
                      <span className="font-medium">{points.filter(p => p.point_type === 'OISHelper' || p.point_type === 'DatedOISHelper').length}</span>
                    </div>
                    {points.length > 0 && (() => {
                      const rates = points
                        .map(p => (p.point as any).rate)
                        .filter((r): r is number => r != null && !isNaN(r));
                      if (rates.length === 0) return null;
                      return (
                        <>
                          <hr className="border-[#e5e5e5] my-2" />
                          <div className="flex justify-between">
                            <span className="text-[#737373]">Short Rate</span>
                            <span className="font-mono">
                              {(Math.min(...rates) * 100).toFixed(3)}%
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-[#737373]">Long Rate</span>
                            <span className="font-mono">
                              {(Math.max(...rates) * 100).toFixed(3)}%
                            </span>
                          </div>
                        </>
                      );
                    })()}
                  </>
                )}
                {bootstrapResult && !bootstrapResult.error && (
                  <>
                    <hr className="border-[#e5e5e5] my-2" />
                    <div className="flex justify-between">
                      <span className="text-[#737373]">Grid Points</span>
                      <span className="font-medium">{bootstrapResult.grid_dates.length}</span>
                    </div>
                    {bootstrapResult.pillar_dates && (
                      <div className="flex justify-between">
                        <span className="text-[#737373]">Pillar Dates</span>
                        <span className="font-medium">{bootstrapResult.pillar_dates.length}</span>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Audit history for SAVED curves (the route id is the backend UUID). */}
        {id && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/curves"
              entityId={id}
              restoreKeys={[
                'name',
                'currency',
                'day_counter',
                'helper_kind',
                'reference_date',
                'points',
                'body',
              ]}
            />
          </div>
        )}
      </main>
    </div>
  );
}
