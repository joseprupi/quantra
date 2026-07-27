import { TimeUnit } from './types';

const DEFAULT_TIME_UNIT: TimeUnit = 'Days';

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function normalizeBlackIborCouponPricer(pricer: unknown): unknown {
  if (!isRecord(pricer)) return pricer;

  const out: Record<string, unknown> = { ...pricer };
  if (
    out.optionlet_volatility === undefined &&
    out.optionlet_volatility_structure !== undefined
  ) {
    out.optionlet_volatility = out.optionlet_volatility_structure;
  }

  delete out.optionlet_volatility_structure_type;
  delete out.optionlet_volatility_structure;
  return out;
}

function normalizeLegacySchemaRenames(payload: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...payload };

  if (out.forwarding_curve === undefined && out.forecasting_curve !== undefined) {
    out.forwarding_curve = out.forecasting_curve;
  }
  delete out.forecasting_curve;

  if (
    out.black_ibor_coupon_pricer === undefined &&
    (out.pricer_type === 'BlackIborCouponPricer' || isRecord(out.pricer))
  ) {
    out.black_ibor_coupon_pricer = normalizeBlackIborCouponPricer(out.pricer);
  }
  delete out.pricer_type;
  delete out.pricer;

  if (
    out.optionlet_volatility === undefined &&
    out.optionlet_volatility_structure !== undefined
  ) {
    out.optionlet_volatility = out.optionlet_volatility_structure;
  }
  delete out.optionlet_volatility_structure_type;
  delete out.optionlet_volatility_structure;

  const hadLegacyUnderlyingSwap = out.underlying === undefined && out.underlying_swap !== undefined;
  if (hadLegacyUnderlyingSwap) {
    out.underlying = out.underlying_swap;
  }
  delete out.underlying_swap;

  const underlying = isRecord(out.underlying) ? out.underlying : {};
  const looksLikeSwaption =
    hadLegacyUnderlyingSwap ||
    out.exercise_date !== undefined ||
    out.exercise_dates !== undefined ||
    underlying.fixed_leg !== undefined ||
    underlying.floating_leg !== undefined ||
    underlying.overnight_leg !== undefined;

  if (out.underlying_type === 'IborSwap' || (looksLikeSwaption && out.underlying !== undefined && out.underlying_type === undefined)) {
    out.underlying_type = 'VanillaSwap';
  }

  const looksLikeInflationSwap =
    out.swap_type !== undefined &&
    (
      out.inflation_index_id !== undefined ||
      out.observation_lag !== undefined ||
      out.fixed_schedule !== undefined ||
      out.start_date !== undefined
    );

  if (looksLikeInflationSwap && out.notional === undefined && out.nominal !== undefined) {
    out.notional = out.nominal;
  }
  if (looksLikeInflationSwap) {
    delete out.nominal;
  }

  return out;
}

function mergeDomainFields(
  current: unknown,
  legacy: Record<string, unknown>,
): Record<string, unknown> | undefined {
  const currentObject = isRecord(current) ? { ...current } : {};
  const merged: Record<string, unknown> = { ...currentObject };

  for (const [key, value] of Object.entries(legacy)) {
    if (merged[key] === undefined && value !== undefined) {
      merged[key] = value;
    }
  }

  return Object.keys(merged).length > 0 ? merged : undefined;
}

function buildPeriod(tenor: any, n: any, unit: any, defaultN = 0): { n: number; unit: TimeUnit } {
  if (tenor && typeof tenor === 'object') {
    const tn = Number((tenor as any).n ?? defaultN);
    const tu = ((tenor as any).unit || DEFAULT_TIME_UNIT) as TimeUnit;
    return { n: Number.isFinite(tn) ? tn : defaultN, unit: tu };
  }
  const parsedN = Number(n ?? defaultN);
  const parsedUnit = (unit || DEFAULT_TIME_UNIT) as TimeUnit;
  return { n: Number.isFinite(parsedN) ? parsedN : defaultN, unit: parsedUnit };
}

export function normalizeIndexDefForApi(def: any): any {
  if (!def || typeof def !== 'object') return def;
  const out = { ...def };
  const period = buildPeriod(out.tenor, out.tenor_number, out.tenor_time_unit, 0);
  out.tenor = period;
  delete out.tenor_number;
  delete out.tenor_time_unit;
  return out;
}

export function normalizeCurvePointForApi(pointWrapper: any): any {
  if (!pointWrapper || typeof pointWrapper !== 'object') return pointWrapper;
  const out = { ...pointWrapper };
  const p = { ...(out.point || {}) };

  const hasLegacyTenor =
    Object.prototype.hasOwnProperty.call(p, 'tenor_number') ||
    Object.prototype.hasOwnProperty.call(p, 'tenor_time_unit');
  if (hasLegacyTenor) {
    p.tenor = buildPeriod(p.tenor, p.tenor_number, p.tenor_time_unit, 0);
    delete p.tenor_number;
    delete p.tenor_time_unit;
  }

  out.point = p;
  return out;
}

export function normalizeCurveForApi(curve: any): any {
  if (!curve || typeof curve !== 'object') return curve;
  return {
    ...curve,
    points: Array.isArray(curve.points) ? curve.points.map(normalizeCurvePointForApi) : curve.points,
  };
}

/**
 * The `body` of an inline wire CurveRef (thin-A product pricing). The
 * backend's curve translator reads `body.interpolator` and
 * `body.bootstrap_trait` — NOT top-level fields — and silently defaults to
 * LogLinear / Discount when absent. Dropping them was harmless for the
 * seeded Discount/LogLinear curves but wrong for ZeroRate/FwdRate helper
 * curves and fatal for Interpolated* value curves (typed 422
 * `point_family_mismatch`). Every product page's `toThinACurve` builds its
 * `body` here so the construction fields always ride along.
 */
export function curveBodyForApi(curve: any, role?: string): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (role) body.role = role;
  if (curve && typeof curve === 'object') {
    if (curve.interpolator) body.interpolator = curve.interpolator;
    if (curve.bootstrap_trait) body.bootstrap_trait = curve.bootstrap_trait;
  }
  return body;
}

export function normalizeCdsQuoteForApi(quote: any): any {
  if (!quote || typeof quote !== 'object') return quote;
  const out = { ...quote };
  out.tenor = buildPeriod(out.tenor, out.tenor_number, out.tenor_time_unit, 0);
  delete out.tenor_number;
  delete out.tenor_time_unit;
  return out;
}

export function normalizePricingForApi(pricing: any): any {
  if (!isRecord(pricing)) return pricing;

  const out: Record<string, unknown> = { ...pricing };
  const ratesRaw = mergeDomainFields(out.rates, {
    indices: out.indices,
    swap_indices: out.swap_indices,
    curves: out.curves,
    coupon_pricers: out.coupon_pricers,
  });
  const creditRaw = mergeDomainFields(out.credit, {
    credit_curves: out.credit_curves,
  });
  const volatility = mergeDomainFields(out.volatility, {
    vol_surfaces: out.vol_surfaces,
    models: out.models,
  });
  const equity = mergeDomainFields(out.equity, {
    equity_underlyings: out.equity_underlyings,
  });
  const inflation = mergeDomainFields(out.inflation, {
    inflation_indices: out.inflation_indices,
    inflation_curves: out.inflation_curves,
  });
  const options = mergeDomainFields(out.options, {
    bond_pricing_details: out.bond_pricing_details,
    bond_pricing_flows: out.bond_pricing_flows,
    swaption_pricing_details: out.swaption_pricing_details,
    swaption_pricing_rebump: out.swaption_pricing_rebump,
  });
  const rates = ratesRaw
    ? {
        ...ratesRaw,
        indices: Array.isArray(ratesRaw.indices) ? ratesRaw.indices.map(normalizeIndexDefForApi) : ratesRaw.indices,
        curves: Array.isArray(ratesRaw.curves) ? ratesRaw.curves.map(normalizeCurveForApi) : ratesRaw.curves,
      }
    : undefined;
  const credit = creditRaw
    ? {
        ...creditRaw,
        credit_curves: Array.isArray(creditRaw.credit_curves)
          ? creditRaw.credit_curves.map((curve) => {
              if (!isRecord(curve) || !Array.isArray(curve.quotes)) return curve;
              return {
                ...curve,
                quotes: curve.quotes.map(normalizeCdsQuoteForApi),
              };
            })
          : creditRaw.credit_curves,
      }
    : undefined;

  if (rates) out.rates = rates;
  if (credit) out.credit = credit;
  if (volatility) out.volatility = volatility;
  if (equity) out.equity = equity;
  if (inflation) out.inflation = inflation;
  if (options) out.options = options;

  delete out.indices;
  delete out.swap_indices;
  delete out.curves;
  delete out.coupon_pricers;
  delete out.credit_curves;
  delete out.vol_surfaces;
  delete out.models;
  delete out.equity_underlyings;
  delete out.inflation_indices;
  delete out.inflation_curves;
  delete out.bond_pricing_details;
  delete out.bond_pricing_flows;
  delete out.swaption_pricing_details;
  delete out.swaption_pricing_rebump;

  return out;
}

export function normalizeQuantraRequestForApi(payload: any): any {
  if (Array.isArray(payload)) {
    return payload.map(normalizeQuantraRequestForApi);
  }
  if (!isRecord(payload)) return payload;

  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    const normalizedValue = normalizeQuantraRequestForApi(value);
    out[key] = key === 'pricing' ? normalizePricingForApi(normalizedValue) : normalizedValue;
  }
  return normalizeLegacySchemaRenames(out);
}
