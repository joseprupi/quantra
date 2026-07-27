import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import Header from '../../components/Header';
import CurveSetSelector from '../../components/products/CurveSetSelector';
import PricingTraceLink from '../../components/products/PricingTraceLink';
import { collectIndexRefIds, Curve, IndexDef } from '../../lib/types';
import { priceEquityOption } from '../../lib/api/equityOptionPricingService';
import { curveBodyForApi, normalizeCurveForApi, normalizeIndexDefForApi } from '../../lib/api-normalizers';
import { useAsOfDate } from '../../hooks/useAsOfDate';
import { getVolSurfaces, VolSurfaceSpec } from '../../lib/storage/volSurfaces';
import { useAuth } from '../../hooks/useAuth';
import { EquityOptionRequest, deriveEquityOptionName, getEquityOptionEntryById, saveEquityOption } from '../../lib/storage/equityOptions';
import { buildSourceRefs } from '../../lib/storage/sourceRefs';
import {
  persistEquityOptionGraph,
  buildEquityOptionPriceArm,
  asEquityOptionAppGraph,
  type EquityOptionAppGraph,
  type CurveGraphInput,
} from '../../lib/api/equityOptionSaveGraph';
import type { components } from '../../lib/api/_generated/orchestrator';
import { buildPricingQuoteSnapshotWithBackend } from '../../lib/marketDataBackend';
import { indexStore, storedToRateIndexDef } from '../../lib/storage/indices';
import BackLink from '../../components/ui/BackLink';
import FeedbackBanner from '../../components/ui/FeedbackBanner';
import PageHeader from '../../components/ui/PageHeader';
import PanelPlaceholder from '../../components/ui/PanelPlaceholder';
import ProductSaveBar from '../../components/products/ProductSaveBar';
import HistoryPanel from '../../components/products/HistoryPanel';
import { entityUi, getBackLabel, getInstructionText } from '../../components/ui/entityUi';
import { formStyles } from '../../components/ui/formStyles';

type OptionStructure = 'Vanilla' | 'Barrier' | 'Digital' | 'Asian' | 'Lookback' | 'Basket';

interface TradeState {
  tradeId: string;
  baseStructure: OptionStructure;
  optionType: 'Call' | 'Put';
  exerciseType: 'European';
  expiryDate: string;
  strike: number;
  quantity: number;
  multiplier: number;
  settlementType: 'Physical' | 'Cash';
  underlyingId: string;
  spot: number;
  useSpotQuote: boolean;
  spotQuoteId: string;
  currency: string;
  features: {
    barrierFeature?: any;
    digitalFeature?: any;
    asianFeature?: any;
    lookbackFeature?: any;
    basketFeature?: any;
  };
  pricerOverride?: {
    engineType: 'Auto' | 'AnalyticBSM' | 'FD' | 'Binomial' | 'MC-LSM';
    timeSteps?: number;
    gridPoints?: number;
    samples?: number;
    seed?: number;
  };
}

export default function EquityOptions() {
  const { id } = useParams();
  const navigate = useNavigate();
  const ui = entityUi.equityOption;
  const isNew = !id || id === 'new';
  const { user, login } = useAuth();
  const { asOfDate: globalAsOf } = useAsOfDate();

  const today = new Date().toISOString().split('T')[0];
  const defaultExpiry = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

  const [asOfDate, setAsOfDate] = useState(globalAsOf || today);
  const [settlementDate, setSettlementDate] = useState(today);
  const [attachQuotes, setAttachQuotes] = useState(true);

  const [curveSetId, setCurveSetId] = useState('');
  const [discountCurveId, setDiscountCurveId] = useState('');
  const [discountCurve, setDiscountCurve] = useState<Curve | null>(null);
  const [dividendCurveId, setDividendCurveId] = useState('');
  const [dividendCurve, setDividendCurve] = useState<Curve | null>(null);

  const [volSurfaces, setVolSurfaces] = useState<VolSurfaceSpec[]>([]);
  const [volId, setVolId] = useState('');

  const [trade, setTrade] = useState<TradeState>({
    tradeId: '',
    baseStructure: 'Vanilla',
    optionType: 'Call',
    exerciseType: 'European',
    expiryDate: defaultExpiry,
    strike: 100,
    quantity: 1,
    multiplier: 1,
    settlementType: 'Cash',
    underlyingId: 'AAPL',
    spot: 100,
    useSpotQuote: false,
    spotQuoteId: '',
    currency: 'USD',
    features: {},
    pricerOverride: { engineType: 'Auto' },
  });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});
  const [result, setResult] = useState<any | null>(null);
  const [durationMs, setDurationMs] = useState<number | undefined>();
  const [requestId, setRequestId] = useState<string | undefined>();
  const [optionId, setOptionId] = useState(id && id !== 'new' ? id : '');
  const [optionName, setOptionName] = useState('');
  // The persisted server-side UUID graph (curves + vol_surface + equity_option).
  // Present ⇒ price by-reference; absent ⇒ inline. Loaded from
  // the saved entry's ``appGraph``.
  const [appGraph, setAppGraph] = useState<EquityOptionAppGraph | null>(null);
  const [saveStatus, setSaveStatus] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  // Optional audit reason for the next save; rides every write as X-Change-Reason.
  const [changeReason, setChangeReason] = useState('');

  const inputClass = formStyles.input;
  const labelClass = formStyles.label;

  useEffect(() => {
    setAsOfDate(globalAsOf || today);
  }, [globalAsOf, today]);

  useEffect(() => {
    const surfaces = getVolSurfaces().filter((v: any) => v.payload_type === 'BlackVolSpec');
    setVolSurfaces(surfaces);
    if (surfaces.length > 0 && !volId) setVolId(surfaces[0].id);
  }, [volId]);

  useEffect(() => {
    if (!isNew && id) {
      void getEquityOptionEntryById(id).then((entry) => {
        if (!entry) return;
        const saved = entry.request;
        setOptionId(entry.id);
        setOptionName(entry.name || '');
        setAppGraph(asEquityOptionAppGraph(entry.appGraph));
        const req: any = saved;
        const pricing = req?.pricing || {};
        const row = req?.options?.[0] || {};
        if (pricing.as_of_date) setAsOfDate(pricing.as_of_date);
        if (pricing.settlement_date) setSettlementDate(pricing.settlement_date);
        if (Array.isArray(pricing.curves)) {
          const disc = pricing.curves.find((c: any) => c.id === 'discount');
          const div = pricing.curves.find((c: any) => c.id === 'dividend');
          if (disc) {
            setDiscountCurveId('discount');
            setDiscountCurve({
              id: 'discount',
              name: 'discount',
              currency: 'USD',
              role: 'discount',
              day_counter: disc.day_counter,
              interpolator: disc.interpolator,
              bootstrap_trait: disc.bootstrap_trait,
              reference_date: disc.reference_date || asOfDate,
              points: disc.points || [],
              createdAt: '',
              updatedAt: '',
            } as Curve);
          }
          if (div) {
            setDividendCurveId('dividend');
            setDividendCurve({
              id: 'dividend',
              name: 'dividend',
              currency: 'USD',
              role: 'forward',
              day_counter: div.day_counter,
              interpolator: div.interpolator,
              bootstrap_trait: div.bootstrap_trait,
              reference_date: div.reference_date || asOfDate,
              points: div.points || [],
              createdAt: '',
              updatedAt: '',
            } as Curve);
          }
        }
        if (Array.isArray(pricing.vol_surfaces) && pricing.vol_surfaces[0]?.id) setVolId(pricing.vol_surfaces[0].id);
        setTrade((prev) => ({
          ...prev,
          tradeId: row.trade_id || row.name || prev.tradeId,
          optionType: row.option_type || prev.optionType,
          exerciseType: 'European',
          expiryDate: row.expiry_date || prev.expiryDate,
          strike: row.strike ?? prev.strike,
          quantity: row.quantity ?? prev.quantity,
          multiplier: row.multiplier ?? prev.multiplier,
          settlementType: row.settlement_type || prev.settlementType,
          underlyingId: row?.underlying?.underlying_id || row.underlying_id || prev.underlyingId,
          spot: row?.underlying?.spot ?? prev.spot,
          useSpotQuote: !!row?.underlying?.spot_quote_id,
          spotQuoteId: row?.underlying?.spot_quote_id || '',
          currency: row?.underlying?.currency || prev.currency,
        }));
      });
    }
  }, [id, isNew, asOfDate]);

  const validateRequestInputs = (): Record<string, string> => {
    const errs: Record<string, string> = {};
    if (!discountCurve) errs.discountCurve = 'Discount curve is required.';
    if (!dividendCurve) errs.dividendCurve = 'Dividend / repo curve is required.';
    if (!volId) errs.volId = 'Vol surface is required.';
    if (!trade.underlyingId.trim()) errs.underlyingId = 'Underlying id is required.';
    if (trade.useSpotQuote) {
      if (!trade.spotQuoteId.trim()) errs.spotQuoteId = 'Spot quote id is required.';
    } else if (!(trade.spot > 0)) {
      errs.spot = 'Spot must be positive.';
    }
    if (!trade.expiryDate) errs.expiryDate = 'Expiry is required.';
    if (!(trade.strike > 0)) errs.strike = 'Strike must be > 0.';
    if (trade.quantity === 0) errs.quantity = 'Quantity cannot be 0.';
    return errs;
  };

  const selectedVol = useMemo(() => volSurfaces.find(v => v.id === volId) || null, [volSurfaces, volId]);

  // Map a portal-shaped curve (id, day_counter, points, interpolator,
  // bootstrap_trait, reference_date) onto the orchestrator's inline
  // CurveRef (which forbids extra fields). Role tagged via
  // ``body.role`` so the backend picks discount vs dividend. Points
  // pass through normalizeCurvePointForApi so the legacy
  // ``tenor_number``/``tenor_time_unit`` shape becomes
  // ``tenor: {n, unit}``; ``quote_id`` is forwarded unresolved
  // (the orchestrator's market-data resolution substitutes the value).
  const toThinACurve = (curve: Curve, role: 'discount' | 'dividend'): any => {
    const normalized = normalizeCurveForApi({
      id: curve.id,
      day_counter: curve.day_counter,
      reference_date: curve.reference_date || asOfDate,
      points: curve.points,
    });
    const out: Record<string, any> = {
      name: curve.id || curve.name || role,
      points: normalized.points,
    };
    if (curve.currency) out.currency = curve.currency;
    if (curve.day_counter) out.day_counter = curve.day_counter;
    if (curve.reference_date || asOfDate) out.reference_date = curve.reference_date || asOfDate;
    out.body = curveBodyForApi(curve, role);
    return out;
  };

  // Build the top-level inline ``vol_surface`` (VolSurfaceRef).
  // VolSurfaceRef forbids extra fields and requires ``kind`` +
  // ``payload`` in inline mode; the backend enforces
  // ``kind = 'BlackVolSpec'``. The orchestrator reads
  // ``payload.base.*`` directly; richer payload shapes
  // (SurfaceFromPrices, term_vols, surface_vols) are forwarded
  // verbatim but are base-value-only in production today.
  const toThinAVolSurface = (
    vol: VolSurfaceSpec,
  ): { name: string; kind: string; payload: Record<string, any> } => {
    const base = vol.base || {};
    const baseOut: Record<string, any> = {
      reference_date: base.reference_date || asOfDate,
      calendar: base.calendar || 'NullCalendar',
      business_day_convention: base.business_day_convention || 'Following',
      day_counter: base.day_counter || 'Actual365Fixed',
      shape: base.shape || 'Constant',
    };
    const isSurfaceFromPrices = (base as any)?.shape === 'SurfaceFromPrices';
    if (!isSurfaceFromPrices) {
      baseOut.constant_vol = base.constant_vol ?? 0.2;
    }
    const payload: Record<string, any> = { base: baseOut };
    if (isSurfaceFromPrices) {
      // Base-value-only: forward the surface arrays verbatim so the
      // orchestrator can route the BlackVolSpec payload
      // unchanged; richer-shape handling remains the engine's job.
      if (vol.surface_prices) payload.surface_prices = vol.surface_prices;
      if (vol.price_expiries) payload.price_expiries = vol.price_expiries;
      if (vol.price_strikes) payload.price_strikes = vol.price_strikes;
      if (vol.price_option_type) payload.price_option_type = vol.price_option_type;
      if ((vol as any).spot !== undefined) payload.spot = (vol as any).spot;
      if ((vol as any).spot_quote_id) payload.spot_quote_id = (vol as any).spot_quote_id;
      if ((vol as any).discount_curve_id) payload.discount_curve_id = (vol as any).discount_curve_id;
      if ((vol as any).use_flat_discount_rate !== undefined) payload.use_flat_discount_rate = (vol as any).use_flat_discount_rate;
      if ((vol as any).flat_discount_rate !== undefined) payload.flat_discount_rate = (vol as any).flat_discount_rate;
      if ((vol as any).dividend_curve_id) payload.dividend_curve_id = (vol as any).dividend_curve_id;
      if ((vol as any).use_flat_dividend_rate !== undefined) payload.use_flat_dividend_rate = (vol as any).use_flat_dividend_rate;
      if ((vol as any).flat_dividend_rate !== undefined) payload.flat_dividend_rate = (vol as any).flat_dividend_rate;
      if ((vol as any).surface_interpolator) payload.surface_interpolator = (vol as any).surface_interpolator;
    }
    return {
      name: vol.id || 'inline_vol',
      kind: 'BlackVolSpec',
      payload,
    };
  };

  // Build the top-level inline ``spot`` (SpotQuoteRef). Two branches:
  // an inline ``value`` short-circuits market-data resolution;
  // ``canonical_id`` triggers a market-data lookup. When
  // both are set, the orchestrator uses ``value`` immediately and
  // derives the per-trade ``underlyingId`` from ``canonical_id``.
  // The portal posts both whenever it can so the underlier id is
  // stable across runs.
  const toThinASpot = (): { canonical_id?: string; value?: number } => {
    const out: { canonical_id?: string; value?: number } = {};
    if (trade.useSpotQuote) {
      if (trade.spotQuoteId.trim()) out.canonical_id = trade.spotQuoteId.trim();
    } else {
      out.value = trade.spot;
      if (trade.underlyingId?.trim()) out.canonical_id = trade.underlyingId.trim();
    }
    return out;
  };

  // Inline POST body for ``POST /v1/price/equity-option``. Flat
  // trade body (the backend reads ``option_type`` / ``strike`` /
  // ``quantity`` / ``expiry_date`` / ``settlement`` / ``trade_id``
  // from the equity_option root) + role-tagged top-level curves +
  // top-level vol_surface (BlackVolSpec) + top-level spot. The
  // EquityOptionPriceRequest validator requires all three top-level
  // refs when ``equity_option`` is set inline. The default equity
  // model (analytic Black-Scholes, ``DEFAULT_EQUITY_MODEL_ID``)
  // is selected by the orchestrator — no model field needed.
  const buildThinARequest = async (): Promise<Record<string, any>> => {
    if (!discountCurve || !selectedVol) throw new Error('Missing required market inputs');
    const trade_body: Record<string, any> = {
      option_type: trade.optionType,
      strike: trade.strike,
      quantity: trade.quantity,
      expiry_date: trade.expiryDate,
      settlement: trade.settlementType,
    };
    if (trade.tradeId?.trim()) trade_body.trade_id = trade.tradeId.trim();
    const curves: any[] = [toThinACurve(discountCurve, 'discount')];
    if (dividendCurve) curves.push(toThinACurve(dividendCurve, 'dividend'));
    return {
      equity_option: trade_body,
      curves,
      vol_surface: toThinAVolSurface(selectedVol),
      spot: toThinASpot(),
      as_of: asOfDate,
    };
  };

  const buildRequest = async (preferBackendQuotes: boolean): Promise<EquityOptionRequest> => {
    if (!discountCurve || !dividendCurve || !selectedVol) throw new Error('Missing required market inputs');

    const curves = [
      {
        id: 'discount',
        day_counter: discountCurve.day_counter,
        interpolator: discountCurve.interpolator,
        bootstrap_trait: discountCurve.bootstrap_trait,
        reference_date: discountCurve.reference_date || asOfDate,
        points: discountCurve.points,
      },
      {
        id: 'dividend',
        day_counter: dividendCurve.day_counter,
        interpolator: dividendCurve.interpolator,
        bootstrap_trait: dividendCurve.bootstrap_trait,
        reference_date: dividendCurve.reference_date || asOfDate,
        points: dividendCurve.points,
      },
    ];

    const pricingQuotes = attachQuotes
      ? (
        await buildPricingQuoteSnapshotWithBackend(asOfDate, {
          quoteType: 'Curve',
          preferBackend: preferBackendQuotes,
        })
      ).quotes
      : [];

    const indexIds = collectIndexRefIds(curves.flatMap(c => c.points || []));
    const uniqueIndexIds = Array.from(new Set(indexIds.filter(Boolean)));
    const storedIndices = await indexStore.getAll();
    const resolvedIndices: IndexDef[] = storedIndices
      .filter(i => uniqueIndexIds.includes(i.id))
      .map(i => storedToRateIndexDef(i))
      .filter((index): index is IndexDef => !!index);

    // quote_ids reach the orchestrator UNRESOLVED (server-side market-data
    // resolution pulls the value); inline points pass through.
    const resolvedCurvesForApi = curves.map(normalizeCurveForApi);

    return {
      pricing: {
        as_of_date: asOfDate,
        settlement_date: settlementDate,
        curves: resolvedCurvesForApi,
        indices: resolvedIndices.map(normalizeIndexDefForApi),
        vol_surfaces: [{
          id: selectedVol.id,
          payload_type: 'BlackVolSpec',
          payload: (selectedVol.base as any)?.shape === 'SurfaceFromPrices'
            ? {
              base: {
                reference_date: selectedVol.base?.reference_date || asOfDate,
                calendar: selectedVol.base?.calendar || 'NullCalendar',
                business_day_convention: selectedVol.base?.business_day_convention || 'Following',
                day_counter: selectedVol.base?.day_counter || 'Actual365Fixed',
                shape: 'SurfaceFromPrices',
              },
              surface_prices: selectedVol.surface_prices,
              price_expiries: selectedVol.price_expiries,
              price_strikes: selectedVol.price_strikes,
              price_option_type: selectedVol.price_option_type || 'Call',
              ...(selectedVol.spot !== undefined ? { spot: selectedVol.spot } : {}),
              ...(selectedVol.spot_quote_id ? { spot_quote_id: selectedVol.spot_quote_id } : {}),
              ...(selectedVol.discount_curve_id ? { discount_curve_id: selectedVol.discount_curve_id } : {}),
              ...(selectedVol.use_flat_discount_rate !== undefined ? { use_flat_discount_rate: selectedVol.use_flat_discount_rate } : {}),
              ...(selectedVol.flat_discount_rate !== undefined ? { flat_discount_rate: selectedVol.flat_discount_rate } : {}),
              ...(selectedVol.dividend_curve_id ? { dividend_curve_id: selectedVol.dividend_curve_id } : {}),
              ...(selectedVol.use_flat_dividend_rate !== undefined ? { use_flat_dividend_rate: selectedVol.use_flat_dividend_rate } : {}),
              ...(selectedVol.flat_dividend_rate !== undefined ? { flat_dividend_rate: selectedVol.flat_dividend_rate } : {}),
              ...(selectedVol.surface_interpolator ? { surface_interpolator: selectedVol.surface_interpolator } : {}),
            }
            : {
              base: {
                reference_date: selectedVol.base?.reference_date || asOfDate,
                calendar: selectedVol.base?.calendar || 'NullCalendar',
                business_day_convention: selectedVol.base?.business_day_convention || 'Following',
                day_counter: selectedVol.base?.day_counter || 'Actual365Fixed',
                shape: 'Constant',
                constant_vol: selectedVol.base?.constant_vol ?? 0.2,
              },
            },
        }],
        ...(attachQuotes ? { quotes: pricingQuotes } : {}),
      },
      options: [{
        trade_id: trade.tradeId || undefined,
        option_type: trade.optionType,
        exercise_type: trade.exerciseType,
        expiry_date: trade.expiryDate,
        strike: trade.strike,
        quantity: trade.quantity,
        multiplier: trade.multiplier,
        settlement_type: trade.settlementType,
        underlying: {
          underlying_id: trade.underlyingId,
          ...(trade.useSpotQuote ? { spot_quote_id: trade.spotQuoteId } : { spot: trade.spot }),
          currency: trade.currency,
          dividend_yield_curve_id: 'dividend',
          discrete_dividends: [],
        },
        discount_curve_id: 'discount',
        volatility: selectedVol.id,
        base_structure: trade.baseStructure,
        features: trade.features,
        pricer_override: trade.pricerOverride?.engineType === 'Auto'
          ? undefined
          : {
            engine_type: trade.pricerOverride?.engineType,
            time_steps: trade.pricerOverride?.timeSteps,
            grid_points: trade.pricerOverride?.gridPoints,
            samples: trade.pricerOverride?.samples,
            seed: trade.pricerOverride?.seed,
          },
      }],
    };
  };

  const handleSave = async () => {
    setSaveStatus(null);
    const errs = validateRequestInputs();
    setValidationErrors(errs);
    if (Object.keys(errs).length > 0) {
      const issues = Object.values(errs).filter(Boolean);
      setSaveStatus({
        tone: 'error',
        message: issues.length > 0
          ? `Cannot save: ${issues.join(' ')}`
          : 'Cannot save while the form has validation errors.',
      });
      return;
    }
    setSaving(true);
    try {
      const request = await buildRequest(false);
      const trimmedName = optionName.trim();
      const priorGraph = appGraph;
      // Link the discount/dividend curves + vol-surface by
      // store id so the lazy migration can reload the quote-id-bearing entities
      // and rebuild the vol payload (the stripped request lost the curve quote-ids).
      const sourceRefs = buildSourceRefs({
        curves: [
          { role: 'discount', curveId: discountCurveId },
          { role: 'dividend', curveId: dividendCurveId },
        ],
        volSurfaceId: volId || undefined,
      });

      // Persist the entity graph server-side leaf→root (curves → vol_surface →
      // equity_option) and stamp the UUIDs back. The persisted entity bodies are
      // derived from the SAME inline wire payload the inline arm posts
      // (``buildThinARequest``), so the by-reference price equals the inline price
      // for the same inputs. The spot rides inline in pricing.spot (no
      // separate underlier entity). The localStorage save still runs so no
      // user data is lost (dual-read, additive).
      let thinARequest: Record<string, any>;
      try {
        thinARequest = await buildThinARequest();
      } catch (err) {
        await saveEquityOption(request, {
          id: isNew ? undefined : (optionId || undefined),
          name: trimmedName || undefined,
          appId: priorGraph?.equityOptionId,
          appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          sourceRefs,
        });
        setSaveStatus({
          tone: 'error',
          message: `Saved locally, but could not build the market payload to reference server-side: ${err instanceof Error ? err.message : 'unknown error'}`,
        });
        setSaving(false);
        return;
      }

      const graphCurves: CurveGraphInput[] = (thinARequest.curves as any[]).map((c) => ({
        key: (c.body?.role as string) || 'discount',
        body: c as components['schemas']['CurveCreate'],
      }));

      let nextGraph: EquityOptionAppGraph | null = priorGraph;
      let serverNote = '';
      const graphResult = await persistEquityOptionGraph(
        {
          name: trimmedName || deriveEquityOptionName(request),
          curves: graphCurves,
          volSurface: thinARequest.vol_surface as components['schemas']['VolSurfaceCreate'],
          spot: thinARequest.spot as { canonical_id?: string; value?: number },
          equityOptionRequest: thinARequest.equity_option as Record<string, unknown>,
          changeReason: changeReason.trim() || undefined,
        },
        priorGraph,
      );
      if (!graphResult.ok) {
        // Branch on the structured envelope code. The localStorage save
        // below still runs so no user data is lost (dual-read).
        const stage = `${graphResult.stage.entity}${graphResult.stage.key ? ` (${graphResult.stage.key})` : ''}`;
        await saveEquityOption(request, {
          id: isNew ? undefined : (optionId || undefined),
          name: trimmedName || undefined,
          appId: priorGraph?.equityOptionId,
          appGraph: (priorGraph ?? undefined) as Record<string, unknown> | undefined,
          sourceRefs,
        });
        setSaveStatus({
          tone: 'error',
          message: `Saved locally, but server persist failed at ${stage} [${graphResult.envelope.code}]: ${graphResult.envelope.error}`,
        });
        setSaving(false);
        return;
      }
      nextGraph = graphResult.graph;
      serverNote = priorGraph ? ' Updated server copy.' : ' Referenced server-side for by-reference pricing.';

      const savedId = await saveEquityOption(request, {
        id: isNew ? undefined : (optionId || undefined),
        name: trimmedName || undefined,
        appId: nextGraph?.equityOptionId,
        appGraph: (nextGraph ?? undefined) as unknown as Record<string, unknown> | undefined,
        sourceRefs,
      });
      setOptionId(savedId);
      setAppGraph(nextGraph);
      const displayName = trimmedName || deriveEquityOptionName(request);
      setSaveStatus({ tone: 'success', message: `Saved "${displayName}".${serverNote}` });
      setChangeReason('');
      if (isNew || id !== savedId) {
        navigate(`/products/equity-options/${savedId}`, { replace: true });
      }
    } catch (e) {
      setSaveStatus({ tone: 'error', message: e instanceof Error ? e.message : 'Failed to save' });
    } finally {
      setSaving(false);
    }
  };

  const handlePrice = async () => {
    if (!user) {
      const signedIn = await login();
      if (!signedIn) {
        setError('Sign in is required to run pricing calls.');
        return;
      }
    }
    const errs = validateRequestInputs();
    setValidationErrors(errs);
    if (Object.keys(errs).length > 0) return;

    setLoading(true);
    setError(null);
    setResult(null);
    try {
      // Price-arm selection. A saved equity option
      // (appGraph present) prices by-reference: a minimal
      // {equity_option_id, as_of} body, and the orchestrator loads the trade +
      // curves + vol_surface + spot server-side — so we skip the local
      // curve/vol/spot build entirely. An unsaved option prices inline,
      // unchanged: flat-trade-body + top-level curves + top-level
      // vol_surface + top-level spot envelope.
      let request: unknown;
      if (appGraph?.equityOptionId) {
        request = buildEquityOptionPriceArm({ appGraph, inlineRequest: undefined, asOf: asOfDate });
      } else {
        request = await buildThinARequest();
      }
      const response = await priceEquityOption(request, asOfDate);
      setDurationMs(response.duration_ms);
      setRequestId(response.requestId);
      if (!response.success || !response.data) throw new Error(response.error || 'Pricing failed');
      const first = response.data.options?.[0];
      if (!first) throw new Error('No pricing result returned');
      setResult(first);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Pricing failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#fafafa]">
      <Header />
      <main className="max-w-6xl mx-auto px-4 sm:px-6 pt-24 pb-12">
        <PageHeader
          title={isNew ? `New ${ui.singular}` : (optionName.trim() || ui.singular)}
          subtitle="Price and manage equity option trades."
          backLink={<BackLink onClick={() => navigate('/products/equity-options')} label={getBackLabel(ui.plural)} />}
          actions={
            <ProductSaveBar
              name={optionName}
              onNameChange={setOptionName}
              onSave={handleSave}
              saving={saving}
              placeholder="Option name…"
              reason={changeReason}
              onReasonChange={setChangeReason}
            />
          }
        />
        {saveStatus && (
          <FeedbackBanner
            tone={saveStatus.tone}
            message={saveStatus.message}
            onDismiss={() => setSaveStatus(null)}
          />
        )}

        <div className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Pricing Context</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div><label className={labelClass}>As Of Date</label><input type="date" value={asOfDate} onChange={e => setAsOfDate(e.target.value)} className={inputClass} /></div>
                <div><label className={labelClass}>Settlement Date</label><input type="date" value={settlementDate} onChange={e => setSettlementDate(e.target.value)} className={inputClass} /></div>
                <CurveSetSelector label="Discount Curve" curveRole="discount" curveSetId={curveSetId} curveId={discountCurveId} onChangeCurveSet={setCurveSetId} onChangeCurve={(cid, c) => { setDiscountCurveId(cid); setDiscountCurve(c); }} />
                <CurveSetSelector label="Dividend / Repo Curve" curveRole="forward" curveSetId={curveSetId} curveId={dividendCurveId} onChangeCurveSet={setCurveSetId} onChangeCurve={(cid, c) => { setDividendCurveId(cid); setDividendCurve(c); }} />
                <div>
                  <label className={labelClass}>Vol Surface</label>
                  <select className={inputClass} value={volId} onChange={e => setVolId(e.target.value)}>
                    <option value="">Select...</option>
                    {volSurfaces.map(v => <option key={v.id} value={v.id}>{v.id}</option>)}
                  </select>
                </div>
              </div>
              {validationErrors.discountCurve && <p className="text-xs text-red-600 mt-1">{validationErrors.discountCurve}</p>}
              {validationErrors.dividendCurve && <p className="text-xs text-red-600 mt-1">{validationErrors.dividendCurve}</p>}
              {validationErrors.volId && <p className="text-xs text-red-600 mt-1">{validationErrors.volId}</p>}
              <label className="mt-3 inline-flex items-center gap-2 text-xs text-[#525252]">
                <input type="checkbox" checked={attachQuotes} onChange={e => setAttachQuotes(e.target.checked)} />
                Attach quote snapshot
              </label>
            </div>

            <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
              <h2 className="text-sm font-semibold text-[#0a0a0a] mb-4">Underlying & Trade</h2>
              <div className="grid sm:grid-cols-2 gap-4">
                <div><label className={labelClass}>Trade Id / Name</label><input className={inputClass} value={trade.tradeId} onChange={e => setTrade(p => ({ ...p, tradeId: e.target.value }))} /></div>
                <div><label className={labelClass}>Underlying Id</label><input className={inputClass} value={trade.underlyingId} onChange={e => setTrade(p => ({ ...p, underlyingId: e.target.value }))} /></div>
                <div><label className={labelClass}>Option Type</label><select className={inputClass} value={trade.optionType} onChange={e => setTrade(p => ({ ...p, optionType: e.target.value as 'Call' | 'Put' }))}><option value="Call">Call</option><option value="Put">Put</option></select></div>
                <div><label className={labelClass}>Settlement</label><select className={inputClass} value={trade.settlementType} onChange={e => setTrade(p => ({ ...p, settlementType: e.target.value as 'Physical' | 'Cash' }))}><option value="Cash">Cash</option><option value="Physical">Physical</option></select></div>
                <div><label className={labelClass}>Expiry Date</label><input type="date" className={inputClass} value={trade.expiryDate} onChange={e => setTrade(p => ({ ...p, expiryDate: e.target.value }))} /></div>
                <div><label className={labelClass}>Strike</label><input type="number" className={inputClass} value={trade.strike} onChange={e => setTrade(p => ({ ...p, strike: parseFloat(e.target.value) || 0 }))} /></div>
                <div><label className={labelClass}>Quantity</label><input type="number" className={inputClass} value={trade.quantity} onChange={e => setTrade(p => ({ ...p, quantity: parseFloat(e.target.value) || 0 }))} /></div>
                <div><label className={labelClass}>Multiplier</label><input type="number" className={inputClass} value={trade.multiplier} onChange={e => setTrade(p => ({ ...p, multiplier: parseFloat(e.target.value) || 0 }))} /></div>
                <div><label className={labelClass}>Currency</label><input className={inputClass} value={trade.currency} onChange={e => setTrade(p => ({ ...p, currency: e.target.value }))} /></div>
                <div>
                  <label className={labelClass}>Spot</label>
                  {trade.useSpotQuote ? (
                    <input className={inputClass} value={trade.spotQuoteId} onChange={e => setTrade(p => ({ ...p, spotQuoteId: e.target.value }))} placeholder="SPOT_QUOTE_ID" />
                  ) : (
                    <input type="number" className={inputClass} value={trade.spot} onChange={e => setTrade(p => ({ ...p, spot: parseFloat(e.target.value) || 0 }))} />
                  )}
                </div>
              </div>
              <label className="mt-3 inline-flex items-center gap-2 text-xs text-[#525252]">
                <input type="checkbox" checked={trade.useSpotQuote} onChange={e => setTrade(p => ({ ...p, useSpotQuote: e.target.checked }))} />
                Use spot quote id
              </label>
              <div className="mt-2 space-y-1">
                {validationErrors.underlyingId && <p className="text-xs text-red-600">{validationErrors.underlyingId}</p>}
                {validationErrors.spot && <p className="text-xs text-red-600">{validationErrors.spot}</p>}
                {validationErrors.spotQuoteId && <p className="text-xs text-red-600">{validationErrors.spotQuoteId}</p>}
                {validationErrors.expiryDate && <p className="text-xs text-red-600">{validationErrors.expiryDate}</p>}
                {validationErrors.strike && <p className="text-xs text-red-600">{validationErrors.strike}</p>}
                {validationErrors.quantity && <p className="text-xs text-red-600">{validationErrors.quantity}</p>}
              </div>
            </div>

            <button
              onClick={() => void handlePrice()}
              disabled={loading || Object.keys(validateRequestInputs()).length > 0}
              className="w-full py-3 text-sm font-medium text-white bg-[#0a0a0a] rounded-xl hover:bg-[#262626] disabled:opacity-50"
            >
              {loading ? 'Pricing...' : 'Price'}
            </button>
          </div>

          <div className="space-y-4">
            {error && <div className="bg-white border border-red-200 rounded-xl p-4 text-sm text-red-700">{error}</div>}
            {!error && !result && (
              <PanelPlaceholder
                compact
                title="No pricing results yet"
                description={getInstructionText('trade parameters', 'Price')}
                icon={ui.icon}
              />
            )}
            {result && (
              <div className="bg-white border border-[#e5e5e5] rounded-xl p-5">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-semibold text-[#0a0a0a]">Results</h3>
                  <div className="flex items-center gap-3">
                    <PricingTraceLink requestId={requestId} />
                    {durationMs !== undefined && <span className="text-xs text-[#a3a3a3]">{durationMs.toFixed(0)}ms</span>}
                  </div>
                </div>
                <div className="bg-[#f5f5f5] rounded-lg p-3 mb-3">
                  <p className="text-xs text-[#737373] mb-1">NPV</p>
                  <p className="text-xl font-semibold text-[#0a0a0a] font-mono">{result.npv?.toLocaleString?.() ?? result.npv ?? '—'}</p>
                </div>
                <div className="grid grid-cols-2 gap-2 text-sm text-[#525252]">
                  <div>Delta: <span className="font-mono">{result.delta ?? '—'}</span></div>
                  <div>Gamma: <span className="font-mono">{result.gamma ?? '—'}</span></div>
                  <div>Vega: <span className="font-mono">{result.vega ?? '—'}</span></div>
                  <div>Theta: <span className="font-mono">{result.theta ?? '—'}</span></div>
                  <div>Rho: <span className="font-mono">{result.rho ?? '—'}</span></div>
                  <div>Implied Vol: <span className="font-mono">{result.implied_vol ?? '—'}</span></div>
                </div>
                {result.engine_used && <p className="text-xs text-[#737373] mt-3">Engine: <span className="font-mono">{result.engine_used}</span></p>}
              </div>
            )}
          </div>
        </div>

        {appGraph?.equityOptionId && (
          <div className="mt-6">
            <HistoryPanel
              entityPath="/v1/equity-options"
              entityId={appGraph.equityOptionId}
              refreshKey={saveStatus}
            />
          </div>
        )}
      </main>
    </div>
  );
}
