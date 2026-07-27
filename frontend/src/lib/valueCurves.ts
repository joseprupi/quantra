// Value-based curve construction ("Interpolate given values", engine 0.5.0
// Interpolated* families): quantity model, pillar parsing, paste-table
// parsing, and client-side validation mirroring the backend's 422 rules so
// most errors never round-trip.
//
// Storage/wire shape per point (see lib/types.ts ValueCurvePoint): the pillar
// is EITHER an explicit ISO `date` OR a flat tenor (`tenor_number` +
// `tenor_time_unit`, nested to `tenor: {n, unit}` by the shared
// `normalizeCurvePointForApi`); the value is EITHER inline (decimal) OR an MD
// `quote_id` resolved server-side.
import {
  BootstrapTrait,
  Compounding,
  Frequency,
  TimeUnit,
  ValueCurvePoint,
  ValuePointType,
} from './types';

export type ValueQuantity = 'zero' | 'df' | 'fwd';

export interface ValueQuantitySpec {
  quantity: ValueQuantity;
  label: string;
  trait: BootstrapTrait;
  pointType: ValuePointType;
  /** JSON key carrying the inline value on the point. */
  valueKey: 'zero_rate' | 'discount_factor' | 'forward_rate';
  /** Values are entered in percent (zeros / forwards) vs raw decimal (DFs). */
  percent: boolean;
}

export const VALUE_QUANTITIES: ValueQuantitySpec[] = [
  {
    quantity: 'zero',
    label: 'Zero rates',
    trait: 'InterpolatedZero',
    pointType: 'ZeroRatePoint',
    valueKey: 'zero_rate',
    percent: true,
  },
  {
    quantity: 'df',
    label: 'Discount factors',
    trait: 'InterpolatedDiscount',
    pointType: 'DiscountFactorPoint',
    valueKey: 'discount_factor',
    percent: false,
  },
  {
    quantity: 'fwd',
    label: 'Forward rates',
    trait: 'InterpolatedFwd',
    pointType: 'ForwardRatePoint',
    valueKey: 'forward_rate',
    percent: true,
  },
];

export function quantitySpec(quantity: ValueQuantity): ValueQuantitySpec {
  return VALUE_QUANTITIES.find(q => q.quantity === quantity)!;
}

export function quantityForTrait(trait: string | undefined): ValueQuantitySpec | undefined {
  return VALUE_QUANTITIES.find(q => q.trait === trait);
}

// ---------------------------------------------------------------------------
// Pillars
// ---------------------------------------------------------------------------

export type ParsedPillar =
  | { kind: 'tenor'; n: number; unit: TimeUnit }
  | { kind: 'date'; iso: string };

const TENOR_UNITS: Record<string, TimeUnit> = {
  d: 'Days',
  w: 'Weeks',
  m: 'Months',
  y: 'Years',
};

/** Parse a maturity token: a tenor like `6M` / `10Y` / `2w` or an ISO date. */
export function parsePillarToken(raw: string): ParsedPillar | null {
  const text = raw.trim();
  if (!text) return null;
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (dateMatch) {
    const d = new Date(`${text}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return null;
    return { kind: 'date', iso: text };
  }
  const tenorMatch = /^(\d+)\s*([dwmy])$/i.exec(text);
  if (tenorMatch) {
    const n = parseInt(tenorMatch[1], 10);
    if (!Number.isFinite(n) || n <= 0) return null;
    return { kind: 'tenor', n, unit: TENOR_UNITS[tenorMatch[2].toLowerCase()] };
  }
  return null;
}

/** Mirror the backend's unadjusted tenor anchoring: reference date + tenor,
 * no calendar adjustment — exactly what goes on the wire today. */
export function pillarDate(point: ValueCurvePoint, referenceDate: string): Date | null {
  const p = point.point;
  if (p.date) {
    const d = new Date(`${p.date.slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  if (p.tenor_number === undefined || p.tenor_number === null) return null;
  const ref = new Date(`${referenceDate}T00:00:00Z`);
  if (Number.isNaN(ref.getTime())) return null;
  const n = p.tenor_number;
  switch (p.tenor_time_unit) {
    case 'Days':
      return new Date(ref.getTime() + n * 86_400_000);
    case 'Weeks':
      return new Date(ref.getTime() + n * 7 * 86_400_000);
    case 'Months':
    case 'Years': {
      const months = p.tenor_time_unit === 'Years' ? n * 12 : n;
      const total = ref.getUTCMonth() + months;
      const year = ref.getUTCFullYear() + Math.floor(total / 12);
      const month = ((total % 12) + 12) % 12;
      const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      return new Date(Date.UTC(year, month, Math.min(ref.getUTCDate(), daysInMonth)));
    }
    default:
      return null;
  }
}

export function pillarLabel(point: ValueCurvePoint): string {
  const p = point.point;
  if (p.date) return p.date;
  if (p.tenor_number !== undefined && p.tenor_time_unit) {
    return `${p.tenor_number}${p.tenor_time_unit[0]}`;
  }
  return '';
}

/** Resolved maturity date (ISO) of a point, or null when the maturity is unset. */
export function resolvedPillarIso(point: ValueCurvePoint, referenceDate: string): string | null {
  const d = pillarDate(point, referenceDate);
  return d ? d.toISOString().slice(0, 10) : null;
}

// ---------------------------------------------------------------------------
// Points
// ---------------------------------------------------------------------------

export function makeValuePoint(
  quantity: ValueQuantity,
  pillar: ParsedPillar,
  opts: { value?: number; quoteId?: string } = {},
): ValueCurvePoint {
  const spec = quantitySpec(quantity);
  const inner: Record<string, unknown> = {};
  if (pillar.kind === 'date') {
    inner.date = pillar.iso;
  } else {
    inner.tenor_number = pillar.n;
    inner.tenor_time_unit = pillar.unit;
  }
  if (opts.quoteId) inner.quote_id = opts.quoteId;
  if (opts.value !== undefined) inner[spec.valueKey] = opts.value;
  return { point_type: spec.pointType, point: inner } as ValueCurvePoint;
}

/** The mandatory first DF pillar: exactly 1.0 AT the curve reference date. */
export function pinnedDfPoint(referenceDate: string): ValueCurvePoint {
  return {
    point_type: 'DiscountFactorPoint',
    point: { date: referenceDate, discount_factor: 1.0 },
  };
}

/**
 * The pinned reference-date anchor as it goes ON THE WIRE (always the first
 * point of the curve): DF = the mandatory 1.0; zero / fwd = the entered
 * short-end value at the reference date (omitted while still empty).
 */
export function anchorPoint(
  quantity: ValueQuantity,
  referenceDate: string,
  anchorValue?: number,
): ValueCurvePoint {
  if (quantity === 'df') return pinnedDfPoint(referenceDate);
  return makeValuePoint(quantity, { kind: 'date', iso: referenceDate }, { value: anchorValue });
}

/**
 * Split STORED points into the pinned anchor + the editable rows: the first
 * point renders as the pinned reference-date row (its inline value feeds the
 * pinned input for zero / fwd) and the rest are editable rows. A first point
 * that is NOT at the reference date (or is quote-referenced) cannot be the
 * anchor — it stays an editable row and validation guides the user.
 */
export function splitStoredValuePoints(
  points: ValueCurvePoint[],
  referenceDate: string,
): { anchorValue: number | undefined; rows: ValueCurvePoint[] } {
  const first = points[0];
  if (
    first &&
    !first.point.quote_id &&
    resolvedPillarIso(first, referenceDate) === referenceDate
  ) {
    return { anchorValue: pointValue(first), rows: points.slice(1) };
  }
  return { anchorValue: undefined, rows: points };
}

/** Inline value of a point (decimal / raw), regardless of family. */
export function pointValue(point: ValueCurvePoint): number | undefined {
  const p = point.point as Record<string, unknown>;
  for (const key of ['zero_rate', 'discount_factor', 'forward_rate']) {
    const v = p[key];
    if (typeof v === 'number') return v;
  }
  return undefined;
}

/** Sort points by pillar date (unknown pillars keep their relative order at the end). */
export function sortValuePoints(
  points: ValueCurvePoint[],
  referenceDate: string,
): ValueCurvePoint[] {
  return points
    .map((point, i) => ({ point, i, d: pillarDate(point, referenceDate) }))
    .sort((a, b) => {
      if (a.d === null && b.d === null) return a.i - b.i;
      if (a.d === null) return 1;
      if (b.d === null) return -1;
      const diff = a.d.getTime() - b.d.getTime();
      return diff !== 0 ? diff : a.i - b.i;
    })
    .map(entry => entry.point);
}

/** Re-target existing rows onto another quantity, keeping maturities + quote
 * refs. Inline values survive between the two percent families (zero <-> fwd)
 * and are dropped when the unit changes (to / from raw discount factors).
 * Operates on EDITABLE rows only — the pinned anchor is assembled separately. */
export function convertValuePoints(
  points: ValueCurvePoint[],
  from: ValueQuantity,
  to: ValueQuantity,
): ValueCurvePoint[] {
  if (from === to) return points;
  const fromSpec = quantitySpec(from);
  const toSpec = quantitySpec(to);
  const keepValues = fromSpec.percent === toSpec.percent;
  return points.map(pt => {
    const { date, tenor_number, tenor_time_unit, quote_id } = pt.point;
    const inner: Record<string, unknown> = {};
    if (date) inner.date = date;
    if (tenor_number !== undefined) {
      inner.tenor_number = tenor_number;
      inner.tenor_time_unit = tenor_time_unit;
    }
    if (quote_id) {
      inner.quote_id = quote_id;
    } else if (keepValues) {
      const v = pointValue(pt);
      if (v !== undefined) inner[toSpec.valueKey] = v;
    }
    return { point_type: toSpec.pointType, point: inner } as ValueCurvePoint;
  });
}

// ---------------------------------------------------------------------------
// Paste-table parsing
// ---------------------------------------------------------------------------

export interface PasteResult {
  /** Editable rows, sorted by resolved date. NEVER contains the anchor. */
  rows: ValueCurvePoint[];
  /** Value from a pasted reference-date line (zero / fwd): feeds the pinned row. */
  anchorValue?: number;
  /** Subtle non-blocking note (e.g. a DF reference-date line was ignored). */
  anchorNote?: string;
  errors: string[];
}

/**
 * Parse a pasted two-column block: one `maturity value` pair per line, tab /
 * comma / whitespace separated. Maturities are tenors (`6M`, `10Y`) or ISO
 * dates; values follow the quantity's entry unit (percent for zeros /
 * forwards, raw decimal for DFs). Paste never creates or duplicates the
 * pinned reference-date anchor: a line AT the reference date feeds the
 * pinned row's value (zero / fwd) or is ignored with a note (DF, fixed 1.0).
 */
export function parsePastedTable(
  text: string,
  quantity: ValueQuantity,
  referenceDate: string,
): PasteResult {
  const spec = quantitySpec(quantity);
  const points: ValueCurvePoint[] = [];
  const errors: string[] = [];
  const lines = text.split(/\r?\n/);
  lines.forEach((line, lineNo) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const cols = trimmed.split(/[\t,;]+|\s+/).filter(Boolean);
    if (cols.length < 2) {
      errors.push(`Line ${lineNo + 1}: expected "maturity value", got "${trimmed}"`);
      return;
    }
    const pillar = parsePillarToken(cols[0]);
    if (!pillar) {
      errors.push(`Line ${lineNo + 1}: unrecognized maturity "${cols[0]}" (use 6M / 10Y or YYYY-MM-DD)`);
      return;
    }
    const rawValue = Number(cols[1].replace('%', ''));
    if (!Number.isFinite(rawValue)) {
      errors.push(`Line ${lineNo + 1}: unrecognized value "${cols[1]}"`);
      return;
    }
    const value = spec.percent ? rawValue / 100 : rawValue;
    points.push(makeValuePoint(quantity, pillar, { value }));
  });
  let rows = sortValuePoints(points, referenceDate);
  let anchorValue: number | undefined;
  let anchorNote: string | undefined;
  const first = rows[0];
  if (first && resolvedPillarIso(first, referenceDate) === referenceDate) {
    rows = rows.slice(1);
    if (quantity === 'df') {
      anchorNote = `The ${referenceDate} line was ignored: the pinned reference-date row fixes the discount factor at 1.0.`;
    } else {
      anchorValue = pointValue(first);
    }
  }
  return { rows, anchorValue, anchorNote, errors };
}

// ---------------------------------------------------------------------------
// Client-side validation (mirrors the backend's typed 422 rules)
// ---------------------------------------------------------------------------

export interface ValueCurveValidation {
  /** index -> human message; index -1 = curve-level. */
  rowErrors: Map<number, string>;
  ok: boolean;
}

export function validateValuePoints(
  points: ValueCurvePoint[],
  quantity: ValueQuantity,
  referenceDate: string,
): ValueCurveValidation {
  const rowErrors = new Map<number, string>();
  if (points.length === 0) {
    rowErrors.set(-1, 'Add at least one point.');
    return { rowErrors, ok: false };
  }

  const dates: (Date | null)[] = [];
  points.forEach((pt, i) => {
    const p = pt.point as Record<string, unknown>;
    const d = pillarDate(pt, referenceDate);
    dates.push(d);
    if (d === null) {
      rowErrors.set(i, 'Maturity required: a tenor like 6M / 10Y or a date like 2033-01-15.');
      return;
    }
    const hasQuote = !!p.quote_id;
    const value = pointValue(pt);
    if (hasQuote && value !== undefined) {
      rowErrors.set(i, 'Give an inline value OR a quote reference, not both.');
      return;
    }
    if (!hasQuote) {
      if (value === undefined || !Number.isFinite(value)) {
        rowErrors.set(
          i,
          i === 0 && quantity !== 'df'
            ? 'Enter the value at the reference date (the curve short end).'
            : 'Value required: enter a number or pick a quote.',
        );
        return;
      }
      if (quantity === 'df') {
        if (value <= 0 || value > 1) {
          rowErrors.set(i, 'Discount factors must be in (0, 1].');
          return;
        }
        if (i === 0 && value !== 1.0) {
          rowErrors.set(i, 'The first discount factor must be exactly 1.0 at the reference date.');
          return;
        }
      }
    }
    if (quantity === 'df' && i === 0 && pt.point.date !== referenceDate) {
      rowErrors.set(i, `The first discount factor must sit AT the reference date (${referenceDate}).`);
    }
    // The interpolated curve is ANCHORED at its first point (QuantLib
    // semantics: dates[0] is the curve reference date). The UI assembles the
    // pinned reference-date anchor as the first point, so this only fires as
    // a safety net on malformed stored data.
    if (
      quantity !== 'df' &&
      i === 0 &&
      d !== null &&
      d.toISOString().slice(0, 10) !== referenceDate &&
      !rowErrors.has(0)
    ) {
      rowErrors.set(
        0,
        `The first point must sit at the reference date (${referenceDate}): the interpolated curve is anchored at its first point.`,
      );
    }
  });

  // Rows are auto-sorted by the UI, so out-of-order dates are a pre-submit
  // safety net; duplicates are the real user-facing case.
  for (let i = 1; i < points.length; i++) {
    const prev = dates[i - 1];
    const cur = dates[i];
    if (prev && cur && !rowErrors.has(i)) {
      if (cur.getTime() === prev.getTime()) {
        rowErrors.set(
          i,
          `Duplicate maturity: this row resolves to the same date as "${pillarLabel(points[i - 1])}".`,
        );
      } else if (cur.getTime() < prev.getTime()) {
        rowErrors.set(i, 'Points must be in increasing date order.');
      }
    }
  }

  return { rowErrors, ok: rowErrors.size === 0 };
}

// ---------------------------------------------------------------------------
// Server 422 -> per-row mapping (best effort)
// ---------------------------------------------------------------------------

/**
 * Map a curve-preview 422 onto table rows. The route flattens
 * `CurveTranslationError` into prose (`"Curve translation failed: <kind>
 * point <N> of curve <id> ..."`) and unresolved quotes into `"Unresolved
 * quote id(s): a, b"`; structured `details` (rule + point_index) are used
 * when present.
 */
export function mapServerErrorToRows(
  error: string,
  details: Array<Record<string, unknown>> | null | undefined,
  points: ValueCurvePoint[],
): Map<number, string> {
  const rowErrors = new Map<number, string>();
  for (const detail of details ?? []) {
    const index = detail.point_index;
    if (typeof index === 'number' && index >= 0 && index < points.length) {
      rowErrors.set(index, error);
    }
  }
  if (rowErrors.size === 0) {
    const pointMatch = /point (\d+)/i.exec(error);
    if (pointMatch) {
      const index = parseInt(pointMatch[1], 10);
      if (index >= 0 && index < points.length) rowErrors.set(index, error);
    }
  }
  // Quote-resolution failures name the offending ids in prose (two variants
  // exist: "Unresolved quote id(s): a, b" and "Could not resolve N quote
  // id(s) at DATE: a, b") — map any row whose quote id the message names.
  if (/quote id/i.test(error)) {
    points.forEach((pt, i) => {
      const quoteId = pt.point.quote_id;
      if (quoteId && error.includes(quoteId)) {
        rowErrors.set(i, `Quote "${quoteId}" could not be resolved at the pricing As-Of.`);
      }
    });
  }
  return rowErrors;
}

// ---------------------------------------------------------------------------
// Zero-point conventions (one set for the whole curve)
// ---------------------------------------------------------------------------

/** Stamp the curve-wide compounding/frequency onto every zero point. */
export function stampZeroConventions(
  points: ValueCurvePoint[],
  compounding: Compounding,
  frequency: Frequency,
): ValueCurvePoint[] {
  return points.map(pt =>
    pt.point_type === 'ZeroRatePoint'
      ? { ...pt, point: { ...pt.point, compounding, frequency } }
      : pt,
  );
}
