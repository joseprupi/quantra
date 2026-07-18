import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import { CURRENCIES, TIME_UNITS, TimeUnit } from '../../lib/types';
import {
  CreditCurvePoint,
  CreditCurveSource,
  CreditCurveSpec,
  exportCreditCurves,
  getCreditCurveById,
  getCreditCurves,
  saveCreditCurve,
} from '../../lib/storage/creditCurves';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import { entityUi, getBackLabel } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

const SOURCE_OPTIONS: Array<{ value: CreditCurveSource; label: string; description: string }> = [
  { value: 'flat', label: 'Flat hazard rate', description: 'Use one constant hazard rate across maturities.' },
  { value: 'manual', label: 'Manual spread points', description: 'Enter explicit par spreads for each tenor.' },
  { value: 'quote_book', label: 'Quote Book spread points', description: 'Reference credit quotes by `quote_id` and resolve them from the Quote Book.' },
];

function defaultPoints(): CreditCurvePoint[] {
  return [
    { tenor_number: 1, tenor_time_unit: 'Years', spread: 0.0080, quote_id: 'EUR_CDS_1Y' },
    { tenor_number: 3, tenor_time_unit: 'Years', spread: 0.0100, quote_id: 'EUR_CDS_3Y' },
    { tenor_number: 5, tenor_time_unit: 'Years', spread: 0.0120, quote_id: 'EUR_CDS_5Y' },
  ];
}

function defaultId() {
  return `credit_curve_${Date.now()}`;
}

function sourceCardClass(isSelected: boolean) {
  return isSelected
    ? 'border-[#8a6a2f] bg-amber-50/50 shadow-sm'
    : 'border-[#e5e5e5] bg-white hover:border-[#d4d4d4]';
}

export default function CreditCurveEditor() {
  const ui = entityUi.creditCurve;
  const { id: routeId } = useParams();
  const navigate = useNavigate();
  const isNew = !routeId;

  const [originalCurve, setOriginalCurve] = useState<CreditCurveSpec | null>(null);
  const [id, setId] = useState(defaultId);
  const [name, setName] = useState('EUR Corporate Senior');
  const [referenceEntity, setReferenceEntity] = useState('EUR_CORP');
  const [currency, setCurrency] = useState('EUR');
  const [seniority, setSeniority] = useState('SeniorUnsecured');
  const [source, setSource] = useState<CreditCurveSource>('quote_book');
  const [recoveryRate, setRecoveryRate] = useState(0.4);
  const [flatHazardRate, setFlatHazardRate] = useState(0.02);
  const [points, setPoints] = useState<CreditCurvePoint[]>(defaultPoints);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;
  const secondaryButtonClass = formStyles.secondaryButton;

  useEffect(() => {
    if (!routeId) {
      setOriginalCurve(null);
      setId(defaultId());
      setName('EUR Corporate Senior');
      setReferenceEntity('EUR_CORP');
      setCurrency('EUR');
      setSeniority('SeniorUnsecured');
      setSource('quote_book');
      setRecoveryRate(0.4);
      setFlatHazardRate(0.02);
      setPoints(defaultPoints());
      setError(null);
      return;
    }

    const curve = getCreditCurveById(routeId);
    if (!curve) {
      setOriginalCurve(null);
      setError('Credit curve not found');
      return;
    }

    setOriginalCurve(curve);
    setId(curve.id);
    setName(curve.name || '');
    setReferenceEntity(curve.reference_entity || '');
    setCurrency(curve.currency || 'EUR');
    setSeniority(curve.seniority || '');
    setSource(curve.source);
    setRecoveryRate(curve.recovery_rate ?? 0.4);
    setFlatHazardRate(curve.flat_hazard_rate ?? 0.02);
    setPoints(curve.points?.length ? curve.points : defaultPoints());
    setError(null);
  }, [routeId]);

  const activeSource = useMemo(
    () => SOURCE_OPTIONS.find((option) => option.value === source),
    [source],
  );

  const pointCount = source === 'flat' ? 0 : points.length;
  const usesQuoteBook = source === 'quote_book';

  const addPoint = () => {
    setPoints((prev) => [...prev, {
      tenor_number: 7,
      tenor_time_unit: 'Years',
      spread: 0.0135,
      quote_id: usesQuoteBook ? 'EUR_CDS_7Y' : undefined,
    }]);
  };

  const removePoint = (index: number) => {
    setPoints((prev) => prev.filter((_, idx) => idx !== index));
  };

  const patchPoint = (index: number, patch: Partial<CreditCurvePoint>) => {
    setPoints((prev) => prev.map((point, idx) => (idx === index ? { ...point, ...patch } : point)));
  };

  const handleExport = () => {
    const curveId = id.trim();
    if (!curveId) return;

    exportCreditCurves([{
      id: curveId,
      name: name.trim() || undefined,
      reference_entity: referenceEntity.trim() || undefined,
      currency: currency.trim() || undefined,
      seniority: seniority.trim() || undefined,
      source,
      recovery_rate: recoveryRate,
      ...(source === 'flat' ? { flat_hazard_rate: flatHazardRate } : { points }),
      createdAt: originalCurve?.createdAt,
      updatedAt: originalCurve?.updatedAt,
    }]);
  };

  const handleSave = async () => {
    const trimmedId = id.trim();
    if (!trimmedId) {
      setError('ID is required');
      return;
    }

    if (!originalCurve && getCreditCurves().some((curve) => curve.id === trimmedId)) {
      setError(`A credit curve with ID "${trimmedId}" already exists`);
      return;
    }

    const cleanedPoints = points
      .map((point) => ({
        tenor_number: Number(point.tenor_number) || 0,
        tenor_time_unit: point.tenor_time_unit,
        ...(source === 'quote_book'
          ? { quote_id: point.quote_id?.trim() || undefined }
          : { spread: Number(point.spread) || 0 }),
      }))
      .filter((point) => point.tenor_number > 0);

    if (source !== 'flat' && cleanedPoints.length === 0) {
      setError('Add at least one valid spread point');
      return;
    }

    setSaving(true);
    setError(null);

    try {
      const nextCurve: CreditCurveSpec = {
        id: trimmedId,
        name: name.trim() || undefined,
        reference_entity: referenceEntity.trim() || undefined,
        currency: currency.trim() || undefined,
        seniority: seniority.trim() || undefined,
        source,
        recovery_rate: recoveryRate,
        ...(source === 'flat' ? { flat_hazard_rate: flatHazardRate } : { points: cleanedPoints }),
        createdAt: originalCurve?.createdAt,
        updatedAt: originalCurve?.updatedAt,
      };

      await saveCreditCurve(nextCurve);
      setOriginalCurve((prev) => prev ? { ...prev, ...nextCurve } : nextCurve);
      setSuccess('Credit curve saved');
      setTimeout(() => setSuccess(null), 3000);
      navigate(`/credit-curves/${trimmedId}`, { replace: true });
    } catch {
      setError('Failed to save credit curve');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : `Edit ${ui.singular}`}
          subtitle="Define reusable CDS hazard or spread term structures"
          backLink={<BackLink onClick={() => navigate('/credit-curves')} label={getBackLabel(ui.plural)} />}
          actions={
            <>
              <button onClick={handleExport} className={secondaryButtonClass}>
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

        {error && <FeedbackBanner tone="error" message={error} onDismiss={() => setError(null)} />}

        {success && <FeedbackBanner tone="success" message={success} />}

        <div className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Curve Settings</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>ID *</label>
                  <input
                    value={id}
                    onChange={(e) => setId(e.target.value.replace(/\s+/g, '_'))}
                    className={inputClass}
                    disabled={!isNew}
                  />
                </div>
                <div>
                  <label className={labelClass}>Currency</label>
                  <select value={currency} onChange={(e) => setCurrency(e.target.value)} className={inputClass}>
                    {CURRENCIES.map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </select>
                </div>
                <div className="sm:col-span-2">
                  <label className={labelClass}>Name</label>
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g., EUR Corporate Senior"
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Reference Entity</label>
                  <input
                    value={referenceEntity}
                    onChange={(e) => setReferenceEntity(e.target.value)}
                    placeholder="e.g., EUR_CORP"
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className={labelClass}>Seniority</label>
                  <input
                    value={seniority}
                    onChange={(e) => setSeniority(e.target.value)}
                    placeholder="e.g., SeniorUnsecured"
                    className={inputClass}
                  />
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Source Definition</h2>
              <div className="grid md:grid-cols-3 gap-3 mb-4">
                {SOURCE_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setSource(option.value)}
                    className={`text-left border rounded-xl p-4 transition-colors ${sourceCardClass(source === option.value)}`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-2">
                      <span className="text-sm font-medium text-[#0a0a0a]">{option.label}</span>
                      {source === option.value && (
                        <span className="text-[10px] font-semibold uppercase tracking-wide text-[#8a6a2f]">Selected</span>
                      )}
                    </div>
                    <p className="text-xs text-[#737373]">{option.description}</p>
                  </button>
                ))}
              </div>

              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className={labelClass}>Recovery Rate</label>
                  <input
                    type="number"
                    step="0.01"
                    value={recoveryRate}
                    onChange={(e) => setRecoveryRate(parseFloat(e.target.value) || 0)}
                    className={inputClass}
                  />
                </div>
                {source === 'flat' && (
                  <div>
                    <label className={labelClass}>Flat Hazard Rate</label>
                    <input
                      type="number"
                      step="0.0001"
                      value={flatHazardRate}
                      onChange={(e) => setFlatHazardRate(parseFloat(e.target.value) || 0)}
                      className={inputClass}
                    />
                  </div>
                )}
              </div>
            </div>

            {source !== 'flat' && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <h2 className="text-sm font-semibold text-[#0a0a0a]">Spread Points</h2>
                    <p className="text-xs text-[#737373] mt-1">
                      {usesQuoteBook
                        ? 'Each point references a Quote Book `quote_id` resolved at pricing time.'
                        : 'Enter par spreads directly in basis points.'}
                    </p>
                  </div>
                  <button onClick={addPoint} className={secondaryButtonClass}>
                    Add Point
                  </button>
                </div>

                <div className="space-y-3">
                  {points.map((point, index) => (
                    <div key={index} className="grid lg:grid-cols-12 gap-3 items-end border border-[#f0f0f0] rounded-xl p-4">
                      <div className="lg:col-span-2">
                        <label className={labelClass}>Tenor #</label>
                        <input
                          type="number"
                          value={point.tenor_number}
                          onChange={(e) => patchPoint(index, { tenor_number: parseInt(e.target.value, 10) || 0 })}
                          className={inputClass}
                        />
                      </div>
                      <div className="lg:col-span-2">
                        <label className={labelClass}>Unit</label>
                        <select
                          value={point.tenor_time_unit}
                          onChange={(e) => patchPoint(index, { tenor_time_unit: e.target.value as TimeUnit })}
                          className={inputClass}
                        >
                          {TIME_UNITS.map((unit) => (
                            <option key={unit} value={unit}>{unit}</option>
                          ))}
                        </select>
                      </div>
                      {usesQuoteBook ? (
                        <div className="lg:col-span-6">
                          <label className={labelClass}>Quote ID</label>
                          <input
                            value={point.quote_id || ''}
                            onChange={(e) => patchPoint(index, { quote_id: e.target.value })}
                            placeholder="EUR_CDS_5Y"
                            className={inputClass}
                          />
                        </div>
                      ) : (
                        <div className="lg:col-span-6">
                          <label className={labelClass}>Spread (bps)</label>
                          <input
                            type="number"
                            step="0.1"
                            value={(point.spread || 0) * 10000}
                            onChange={(e) => patchPoint(index, { spread: (parseFloat(e.target.value) || 0) / 10000 })}
                            className={inputClass}
                          />
                        </div>
                      )}
                      <div className="lg:col-span-2">
                        <button
                          onClick={() => removePoint(index)}
                          className="w-full px-3 py-2 text-sm font-medium text-red-600 bg-red-50 border border-red-100 rounded-lg hover:bg-red-100 transition-colors"
                        >
                          Remove
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="space-y-6">
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Summary</h2>
              <div className="space-y-3 text-sm">
                <div>
                  <p className="text-xs font-medium text-[#737373] mb-1">Curve ID</p>
                  <p className="font-mono text-[#0a0a0a] break-all">{id || '—'}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-[#737373] mb-1">Display Name</p>
                  <p className="text-[#0a0a0a]">{name || '—'}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-[#737373] mb-1">Selected Source</p>
                  <p className="text-[#0a0a0a]">{activeSource?.label || '—'}</p>
                </div>
                <div className="grid grid-cols-2 gap-3 pt-2">
                  <div className="rounded-lg bg-[#fafafa] border border-[#f0f0f0] p-3">
                    <p className="text-[11px] uppercase tracking-wide text-[#737373] mb-1">Recovery</p>
                    <p className="text-base font-semibold text-[#0a0a0a]">{(recoveryRate * 100).toFixed(2)}%</p>
                  </div>
                  <div className="rounded-lg bg-[#fafafa] border border-[#f0f0f0] p-3">
                    <p className="text-[11px] uppercase tracking-wide text-[#737373] mb-1">
                      {source === 'flat' ? 'Hazard' : 'Points'}
                    </p>
                    <p className="text-base font-semibold text-[#0a0a0a]">
                      {source === 'flat' ? flatHazardRate.toFixed(6) : pointCount}
                    </p>
                  </div>
                </div>
              </div>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-3">Usage Notes</h2>
              <div className="space-y-2 text-xs text-[#737373]">
                <p>`manual` stores spreads directly on the curve.</p>
                <p>`quote_book` stores `quote_id`s so CDS pricing resolves live quotes for the selected as-of date.</p>
                <p>`flat` is best for simple hazard-rate scenarios or quick tests.</p>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
