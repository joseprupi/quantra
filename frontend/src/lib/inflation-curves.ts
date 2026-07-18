import {
  DEFAULT_INFLATION_CURVE,
  type Curve,
  type InflationCurveConfig,
  type InflationCurveMeasure,
  type InflationPointWrapper,
  type LegacyInflationSwapPillar,
} from './types';

import { getLegacyFlatQuotes, getQuoteBook, getResolutionMode, resolveQuoteValue } from './storage/quoteBook';

export function cloneDefaultInflationCurve(currency: string): InflationCurveConfig {
  return syncInflationConfig(JSON.parse(JSON.stringify(DEFAULT_INFLATION_CURVE)) as InflationCurveConfig, currency);
}

export function getInflationMeasures(kind: InflationCurveConfig['kind']): InflationCurveMeasure[] {
  return kind === 'YoYInflation' ? ['YoYRate'] : ['ZeroRate'];
}

function getPointType(kind: InflationCurveConfig['kind']): InflationPointWrapper['point_type'] {
  return kind === 'YoYInflation'
    ? 'YearOnYearInflationSwapHelper'
    : 'ZeroCouponInflationSwapHelper';
}

function legacyPillarToPoint(
  pillar: LegacyInflationSwapPillar,
  config: InflationCurveConfig,
): InflationPointWrapper {
  return {
    point_type: getPointType(config.kind),
    point: {
      tenor: pillar.maturity,
      quote_value: pillar.quote_value,
      quote_id: pillar.quote_id,
      swap_observation_lag: config.index.observation_lag,
      calendar: config.calendar,
      payment_convention: config.business_day_convention,
      day_counter: config.day_counter,
      observation_interpolation: 'AsIndex',
      nominal_curve_id: config.kind === 'YoYInflation' ? '' : undefined,
    },
  };
}

export function syncInflationConfig(config: InflationCurveConfig, currency: string): InflationCurveConfig {
  const kind = config.kind || 'ZeroInflation';
  const base = { ...config } as InflationCurveConfig;
  const migratedPoints = Array.isArray(base.points) && base.points.length > 0
    ? base.points
    : (base.pillars || []).map((pillar) => legacyPillarToPoint(pillar, base));

  return {
    ...base,
    index: {
      ...base.index,
      currency,
      kind,
    },
    points: migratedPoints.map((wrapper) => ({
      point_type: getPointType(kind),
      point: {
        ...wrapper.point,
        swap_observation_lag: wrapper.point.swap_observation_lag || base.index.observation_lag,
        calendar: wrapper.point.calendar || base.calendar,
        payment_convention: wrapper.point.payment_convention || base.business_day_convention,
        day_counter: wrapper.point.day_counter || base.day_counter,
        observation_interpolation: wrapper.point.observation_interpolation || 'AsIndex',
        ...(kind === 'YoYInflation'
          ? { nominal_curve_id: wrapper.point.nominal_curve_id || base.index.underlying_zero_index_id || '' }
          : {}),
      },
    })),
    pillars: undefined,
    payload_type: undefined,
  };
}

export function resolveInflationPoints(points: InflationPointWrapper[], asOfDate: string): InflationPointWrapper[] {
  const flatQuotes = getLegacyFlatQuotes();
  const bookEntries = getQuoteBook();
  const mode = getResolutionMode();

  return points.map((wrapper) => {
    const point = wrapper.point;
    if (!point.quote_id) return wrapper;

    const bookEntry = bookEntries.find((entry) => entry.id === point.quote_id);
    let value: number | null = null;

    if (bookEntry) {
      value = resolveQuoteValue(bookEntry.series, asOfDate, bookEntry.resolution_mode || mode);
    }

    if (value === null) {
      const flatQuote = flatQuotes.find((quote) => quote.id === point.quote_id);
      if (flatQuote && Number.isFinite(flatQuote.value)) {
        value = flatQuote.value;
      }
    }

    if (value === null) {
      throw new Error(`Unknown quote id: ${point.quote_id}`);
    }

    return {
      point_type: wrapper.point_type,
      point: {
        ...point,
        quote_value: value,
        quote_id: undefined,
      },
    };
  });
}

export interface BuildInflationCurveSpecOptions {
  /**
   * When false the helper `quote_id`s pass through
   * UNRESOLVED so the orchestrator's server-side md_resolution substitutes the
   * value (same as product pricing / the yield arm). When true (default,
   * preserved for the still-legacy `:8081` CurveSet + saved-swap fat-shape
   * callers) `resolveInflationPoints` substitutes the value client-side.
   */
  resolveQuotes?: boolean;
}

export function buildInflationCurveSpec(
  curve: Curve,
  asOfDate: string,
  options: BuildInflationCurveSpecOptions = {},
) {
  const config = curve.inflation_curve;
  if (!config) {
    throw new Error('Inflation curve settings are missing.');
  }
  const resolveQuotes = options.resolveQuotes !== false;

  const normalized = syncInflationConfig(config, curve.currency);
  const emittedPoints = resolveQuotes
    ? resolveInflationPoints(normalized.points, asOfDate)
    : normalized.points;
  return {
    id: curve.id,
    reference_date: curve.reference_date || asOfDate,
    calendar: normalized.calendar,
    business_day_convention: normalized.business_day_convention,
    day_counter: normalized.day_counter,
    interpolator: normalized.interpolator,
    bootstrap_accuracy: normalized.bootstrap_accuracy,
    kind: normalized.kind,
    index_id: normalized.index.id,
    ...(curve.discount_curve_id ? { discount_curve_id: curve.discount_curve_id } : {}),
    allow_extrapolation: normalized.allow_extrapolation,
    points: emittedPoints.map((wrapper) => ({
      point_type: wrapper.point_type,
      point: {
        ...wrapper.point,
        ...(wrapper.point.tenor ? { tenor: wrapper.point.tenor } : {}),
        ...(normalized.kind === 'YoYInflation' && !wrapper.point.nominal_curve_id && curve.discount_curve_id
          ? { nominal_curve_id: curve.discount_curve_id }
          : {}),
      },
    })),
  };
}

/**
 * The `/v1/curve-preview` inflation arm consumes the index body verbatim (
 * no MD lookup) and the engine bootstrap FAILS without at least one base CPI
 * fixing to anchor I(0). The portal's index UI does not (yet) capture fixings,
 * so when a spec carries none we synthesise a short flat monthly series ending
 * at the reference month — a positive base level cancels out of a ZCIIS fair
 * rate, so the bootstrapped zero rates still equal the swap quotes. A spec that
 * already carries fixings is returned untouched.
 */
export function ensureInflationBaseFixings(
  spec: { fixings?: Array<{ date: string; value: number }> },
  referenceDate: string,
): typeof spec & { fixings: Array<{ date: string; value: number }> } {
  if (Array.isArray(spec.fixings) && spec.fixings.length > 0) {
    return spec as typeof spec & { fixings: Array<{ date: string; value: number }> };
  }
  return { ...spec, fixings: synthesiseBaseFixings(referenceDate) };
}

function synthesiseBaseFixings(referenceDate: string): Array<{ date: string; value: number }> {
  const ref = new Date(referenceDate);
  const year = ref.getUTCFullYear();
  const month = ref.getUTCMonth(); // 0-based
  const fixings: Array<{ date: string; value: number }> = [];
  // Four first-of-month fixings ending at the reference month cover an
  // observation lag of up to ~3M with interpolation. Flat 100.0 base level.
  for (let offset = 3; offset >= 0; offset -= 1) {
    const d = new Date(Date.UTC(year, month - offset, 1));
    fixings.push({ date: d.toISOString().split('T')[0], value: 100.0 });
  }
  return fixings;
}

export function buildInflationIndexSpec(curve: Curve) {
  if (!curve.inflation_curve) {
    throw new Error('Inflation curve settings are missing.');
  }
  const normalized = syncInflationConfig(curve.inflation_curve, curve.currency);
  return normalized.index;
}

