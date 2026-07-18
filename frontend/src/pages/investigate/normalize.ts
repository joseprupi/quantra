// Pure, defensive extraction helpers for the Investigate pipeline inspector.
//
// The orchestrator's trace stages carry product-specific payloads (swap_ir /
// swaption / bonds / cds / equity / inflation). These helpers normalize them
// into product-agnostic view models, degrading gracefully (returning empty
// sections, never throwing) when a field is absent — older, partial and error
// traces must always render.

// Wire enum maps (FlatBuffers quantra.enums; numeric on the wire)

/** Short display forms for tenor time units. */
const WIRE_TIME_UNIT: Record<number, string> = {
  0: 'D',
  1: 'h',
  2: 'µs',
  3: 'ms',
  4: 'min',
  5: 'M',
  6: 's',
  7: 'W',
  8: 'Y',
};

/** Internal (orchestrator) payloads spell the unit out as a string. */
const NAMED_TIME_UNIT: Record<string, string> = {
  Days: 'D',
  Weeks: 'W',
  Months: 'M',
  Years: 'Y',
};

/** quantra/enums/Frequency.py. */
const WIRE_FREQUENCY: Record<number, string> = {
  0: 'Annual',
  1: 'Bimonthly',
  2: 'Biweekly',
  3: 'Daily',
  4: 'EveryFourthMonth',
  5: 'EveryFourthWeek',
  6: 'Monthly',
  7: 'NoFrequency',
  8: 'Once',
  9: 'OtherFrequency',
  10: 'Quarterly',
  11: 'Semiannual',
  12: 'Weekly',
};

/** quantra/enums/DayCounter.py. */
const WIRE_DAY_COUNTER: Record<number, string> = {
  0: 'Actual360',
  1: 'Actual365Fixed',
  2: 'Actual365NoLeap',
  3: 'ActualActual',
  4: 'ActualActualISMA',
  5: 'ActualActualBond',
  6: 'ActualActualISDA',
  7: 'ActualActualHistorical',
  8: 'ActualActual365',
  9: 'ActualActualAFB',
  10: 'ActualActualEuro',
  11: 'Business252',
  12: 'One',
  13: 'Simple',
  14: 'Thirty360',
};

/** SwapType on the wire is numeric (0 = Payer, 1 = Receiver). */
const WIRE_SWAP_TYPE: Record<number, string> = { 0: 'Payer', 1: 'Receiver' };

/** The orchestrator's default curve-bootstrap catalog index id. */
const DEFAULT_CATALOG_INDEX_ID = 'forwarding_index';

// Small generic value helpers

export function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isScalar(v: unknown): v is string | number | boolean {
  return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';
}

export function formatNumber(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

/**
 * Precision-aware scalar formatter: integers and large magnitudes get
 * thousands separators; small fractions (rates, spreads, factors) keep up to
 * 6 significant digits so 0.025 never rounds to "0.03".
 */
export function formatValue(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  if (Number.isInteger(n)) return n.toLocaleString('en-US');
  if (Math.abs(n) < 1) return String(Number(n.toPrecision(6)));
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

function formatRate(r: number): string {
  if (!Number.isFinite(r)) return String(r);
  if (Math.abs(r) < 1) return `${(r * 100).toLocaleString('en-US', { maximumFractionDigits: 4 })}%`;
  return formatNumber(r);
}

function formatTenor(tenor: unknown): string | null {
  if (!isObj(tenor) || typeof tenor.n !== 'number') return null;
  const unit = tenor.unit;
  if (typeof unit === 'number') {
    const u = WIRE_TIME_UNIT[unit];
    return u ? `${tenor.n}${u}` : `${tenor.n} (unit ${unit})`;
  }
  if (typeof unit === 'string') {
    const u = NAMED_TIME_UNIT[unit];
    return u ? `${tenor.n}${u}` : `${tenor.n} ${unit}`;
  }
  return String(tenor.n);
}

function formatDayCount(dc: unknown): string | null {
  if (typeof dc === 'number') return WIRE_DAY_COUNTER[dc] ?? `day_counter ${dc}`;
  if (typeof dc === 'string') return dc;
  return null;
}

function formatFrequency(f: unknown): string | null {
  if (typeof f === 'number') return WIRE_FREQUENCY[f] ?? `frequency ${f}`;
  if (typeof f === 'string') return f;
  return null;
}

function formatSwapType(t: unknown): string | null {
  if (typeof t === 'number') return WIRE_SWAP_TYPE[t] ?? String(t);
  if (typeof t === 'string') return t;
  return null;
}

/**
 * Depth-bounded search: the first value found under any of `keys`, walking
 * objects and arrays breadth-first so shallower (more canonical) fields win.
 */
function findByKey(root: unknown, keys: string[], maxDepth = 8): unknown {
  const queue: Array<{ node: unknown; depth: number }> = [{ node: root, depth: 0 }];
  while (queue.length > 0) {
    const { node, depth } = queue.shift() as { node: unknown; depth: number };
    if (depth > maxDepth) continue;
    if (Array.isArray(node)) {
      for (const item of node) queue.push({ node: item, depth: depth + 1 });
      continue;
    }
    if (!isObj(node)) continue;
    for (const key of keys) {
      if (key in node && node[key] !== null && node[key] !== undefined) return node[key];
    }
    for (const v of Object.values(node)) queue.push({ node: v, depth: depth + 1 });
  }
  return undefined;
}

/** Collect every object found under `key`, depth-bounded. */
function collectByKey(root: unknown, key: string, maxDepth = 8): unknown[] {
  const out: unknown[] = [];
  const queue: Array<{ node: unknown; depth: number }> = [{ node: root, depth: 0 }];
  while (queue.length > 0) {
    const { node, depth } = queue.shift() as { node: unknown; depth: number };
    if (depth > maxDepth) continue;
    if (Array.isArray(node)) {
      for (const item of node) queue.push({ node: item, depth: depth + 1 });
      continue;
    }
    if (!isObj(node)) continue;
    if (key in node && node[key] !== null && node[key] !== undefined) out.push(node[key]);
    for (const [k, v] of Object.entries(node)) {
      if (k !== key) queue.push({ node: v, depth: depth + 1 });
    }
  }
  return out;
}

// Engine-request provenance (wire ⟷ internal)

export interface EngineRequestViews {
  /** Decoded from the exact FlatBuffers bytes sent to the engine, when captured. */
  wire: Record<string, unknown> | null;
  /** The orchestrator's internal pre-encoding inputs (superset, not transmitted). */
  internal: Record<string, unknown> | null;
  rpc: string | null;
  sent: boolean | null;
  bytesLen: number | null;
}

/**
 * Split an `engine_request` stage payload into its wire / internal views.
 * swap_ir traces carry `{engine_wire: {decoded, …}, assembled_request}`;
 * other products' payloads ARE the assembled request with no wrapper.
 */
export function splitEngineRequestViews(payload: unknown): EngineRequestViews {
  const none: EngineRequestViews = { wire: null, internal: null, rpc: null, sent: null, bytesLen: null };
  if (!isObj(payload)) return none;
  const hasWrapper = 'engine_wire' in payload || 'assembled_request' in payload;
  if (!hasWrapper) {
    return { ...none, internal: payload };
  }
  const ew = isObj(payload.engine_wire) ? payload.engine_wire : null;
  const decoded = ew && isObj(ew.decoded) ? ew.decoded : null;
  const internal = isObj(payload.assembled_request) ? payload.assembled_request : null;
  return {
    wire: decoded,
    internal,
    rpc: ew && typeof ew.rpc === 'string' ? ew.rpc : null,
    sent: ew && typeof ew.sent === 'boolean' ? ew.sent : null,
    bytesLen: ew && typeof ew.request_bytes_len === 'number' ? ew.request_bytes_len : null,
  };
}

// Engine-request structured summary

export interface KeyTerm {
  label: string;
  value: string;
}

export interface CurveRow {
  id: string;
  role: string | null;
}

export interface IndexRow {
  id: string | null;
  name: string;
  tenor: string | null;
  dayCount: string | null;
  /** Leg labels whose floating_leg.index.id resolves to this entry. */
  boundTo: string[];
  isDefaultCatalog: boolean;
}

export interface ScheduleRow {
  label: string;
  frequency: string;
}

export interface NormalizedEngineRequest {
  trade: KeyTerm[];
  curves: CurveRow[];
  indices: IndexRow[];
  schedules: ScheduleRow[];
}

function tradeRoot(req: Record<string, unknown>): unknown {
  // Wire shape roots the trade under a product array (swaps / bonds / …);
  // internal shape roots it under `trade`. Fall back to the whole object,
  // minus the market-data sections that would pollute a `rate` search.
  for (const key of ['swaps', 'bonds', 'swaptions', 'options', 'trade']) {
    const v = req[key];
    if (Array.isArray(v) && v.length > 0) return v;
    if (isObj(v) && key === 'trade') return v;
  }
  const clone: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(req)) {
    if (k !== 'pricing' && k !== 'curves' && !k.endsWith('_curve')) clone[k] = v;
  }
  return clone;
}

function extractTradeTerms(req: Record<string, unknown>): KeyTerm[] {
  const root = tradeRoot(req);
  const terms: KeyTerm[] = [];
  const notional = findByKey(root, ['notional']);
  if (typeof notional === 'number') terms.push({ label: 'Notional', value: formatNumber(notional) });
  const rate = findByKey(root, ['fixed_rate', 'running_coupon', 'strike', 'rate']);
  if (typeof rate === 'number') terms.push({ label: 'Rate', value: formatRate(rate) });
  const type = formatSwapType(findByKey(root, ['swap_type', 'option_type', 'side']));
  if (type != null) terms.push({ label: 'Type', value: type });
  const effective = findByKey(root, ['effective_date', 'start_date', 'issue_date']);
  if (typeof effective === 'string') terms.push({ label: 'Effective', value: effective });
  const termination = findByKey(root, ['termination_date', 'maturity_date', 'exercise_date', 'end_date']);
  if (typeof termination === 'string') terms.push({ label: 'Termination', value: termination });
  return terms;
}

function extractCurves(req: Record<string, unknown>): CurveRow[] {
  const rows: CurveRow[] = [];
  const seen = new Set<string>();
  const push = (id: unknown, role: string | null) => {
    if (typeof id !== 'string' || id.length === 0) return;
    const existing = rows.find(r => r.id === id);
    if (existing) {
      if (role && existing.role && !existing.role.includes(role)) existing.role = `${existing.role} + ${role}`;
      else if (role && !existing.role) existing.role = role;
      return;
    }
    seen.add(id);
    rows.push({ id, role });
  };

  // Role hints from trade legs (wire shape): forwarding_curve / discounting_curve.
  const roleByName = new Map<string, string[]>();
  const addRole = (name: unknown, role: string) => {
    if (typeof name !== 'string') return;
    const cur = roleByName.get(name) ?? [];
    if (!cur.includes(role)) cur.push(role);
    roleByName.set(name, cur);
  };
  for (const v of collectByKey(req, 'forwarding_curve', 4)) addRole(v, 'forwarding');
  for (const v of collectByKey(req, 'discounting_curve', 4)) addRole(v, 'discounting');

  // Curve arrays: wire pricing.rates.curves / internal top-level curves.
  for (const arr of collectByKey(req, 'curves', 4)) {
    if (!Array.isArray(arr)) continue;
    for (const c of arr) {
      if (!isObj(c)) continue;
      const id = (typeof c.id === 'string' && c.id) || (typeof c.name === 'string' && c.name) || null;
      const roles = id ? roleByName.get(id) : undefined;
      push(id, roles ? roles.join(' + ') : null);
    }
  }
  // Singular role-named curves: discount_curve / credit_curve / … objects.
  for (const [k, v] of Object.entries(req)) {
    if (!k.endsWith('_curve') || !isObj(v)) continue;
    const id = (typeof v.id === 'string' && v.id) || (typeof v.name === 'string' && v.name) || null;
    push(id, k.slice(0, -'_curve'.length));
  }
  return rows;
}

interface LegBinding {
  label: string;
  indexId: string;
}

function extractLegBindings(req: Record<string, unknown>): LegBinding[] {
  const legs = collectByKey(req, 'floating_leg', 6).filter(isObj);
  const bindings: LegBinding[] = [];
  legs.forEach((leg, i) => {
    const idx = leg.index;
    if (isObj(idx) && typeof idx.id === 'string') {
      bindings.push({
        label: legs.length === 1 ? 'floating leg' : `floating leg ${i + 1}`,
        indexId: idx.id,
      });
    }
  });
  return bindings;
}

function extractIndices(req: Record<string, unknown>): IndexRow[] {
  const bindings = extractLegBindings(req);
  const rows: IndexRow[] = [];
  const seen = new Set<string>();
  for (const arr of collectByKey(req, 'indices', 6)) {
    if (!Array.isArray(arr)) continue;
    for (const raw of arr) {
      if (!isObj(raw)) continue;
      const id = typeof raw.id === 'string' ? raw.id : null;
      const name = (typeof raw.name === 'string' && raw.name) || id || '(unnamed)';
      const dedupeKey = id ?? `name:${name}`;
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
      rows.push({
        id,
        name,
        tenor: formatTenor(raw.tenor),
        dayCount: formatDayCount(raw.day_counter),
        boundTo: bindings.filter(b => b.indexId === id).map(b => b.label),
        isDefaultCatalog: id === DEFAULT_CATALOG_INDEX_ID,
      });
    }
  }
  // A leg bound to an index id missing from the catalog is worth surfacing.
  for (const b of bindings) {
    if (!rows.some(r => r.id === b.indexId)) {
      rows.push({
        id: b.indexId,
        name: `${b.indexId} (not found in indices)`,
        tenor: null,
        dayCount: null,
        boundTo: [b.label],
        isDefaultCatalog: false,
      });
    }
  }
  return rows;
}

function extractSchedules(req: Record<string, unknown>): ScheduleRow[] {
  const rows: ScheduleRow[] = [];
  const legKeys: Array<[string, string]> = [
    ['fixed_leg', 'Fixed leg'],
    ['floating_leg', 'Floating leg'],
    ['cms_leg', 'CMS leg'],
  ];
  for (const [key, label] of legKeys) {
    const legs = collectByKey(req, key, 6).filter(isObj);
    legs.forEach((leg, i) => {
      const sched = leg.schedule;
      const freq = isObj(sched) ? formatFrequency(sched.frequency) : null;
      if (freq != null) {
        rows.push({ label: legs.length === 1 ? label : `${label} ${i + 1}`, frequency: freq });
      }
    });
  }
  return rows;
}

export function normalizeEngineRequest(req: unknown): NormalizedEngineRequest {
  if (!isObj(req)) return { trade: [], curves: [], indices: [], schedules: [] };
  try {
    return {
      trade: extractTradeTerms(req),
      curves: extractCurves(req),
      indices: extractIndices(req),
      schedules: extractSchedules(req),
    };
  } catch {
    return { trade: [], curves: [], indices: [], schedules: [] };
  }
}

// Market-data stage

export interface ResolvedQuoteRow {
  canonicalId: string;
  value: number | null;
  source: string | null;
  fromSnapshot: boolean | null;
  asOf: string | null;
}

export interface MdSummary {
  resolved: ResolvedQuoteRow[];
  misses: string[];
  liveCount: number | null;
  snapshotCount: number | null;
}

export function normalizeMdResolve(payload: unknown): MdSummary {
  const empty: MdSummary = { resolved: [], misses: [], liveCount: null, snapshotCount: null };
  if (!isObj(payload)) return empty;
  const resolved: ResolvedQuoteRow[] = Array.isArray(payload.resolved)
    ? payload.resolved.filter(isObj).map(q => ({
        canonicalId: typeof q.canonical_id === 'string' ? q.canonical_id : '(unknown)',
        value: typeof q.value === 'number' ? q.value : null,
        source: typeof q.source === 'string' ? q.source : null,
        fromSnapshot: typeof q.from_snapshot === 'boolean' ? q.from_snapshot : null,
        asOf: typeof q.as_of === 'string' ? q.as_of : null,
      }))
    : [];
  const misses = Array.isArray(payload.misses) ? payload.misses.filter((m): m is string => typeof m === 'string') : [];
  return {
    resolved,
    misses,
    liveCount: typeof payload.live_count === 'number' ? payload.live_count : null,
    snapshotCount: typeof payload.snapshot_count === 'number' ? payload.snapshot_count : null,
  };
}

// Engine-response stage

export interface LegNpv {
  role: string;
  npv: number;
}

export interface FlowsSection {
  label: string;
  rows: Array<Record<string, unknown>>;
}

export interface EngineResponseSummary {
  npv: number | null;
  legNpvs: LegNpv[];
  /** Flat numeric metrics (fair_rate, fair_spread, extras.*, …). */
  metrics: KeyTerm[];
  flows: FlowsSection[];
  error: { code: string | null; message: string | null; details: unknown } | null;
}

function titleize(key: string): string {
  return key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}

export function normalizeEngineResponse(payload: unknown): EngineResponseSummary {
  const out: EngineResponseSummary = { npv: null, legNpvs: [], metrics: [], flows: [], error: null };
  if (!isObj(payload)) return out;

  if (isObj(payload.error)) {
    const e = payload.error;
    out.error = {
      code: typeof e.code === 'string' ? e.code : null,
      message: typeof e.error === 'string' ? e.error : typeof e.message === 'string' ? e.message : null,
      details: e.details ?? null,
    };
  }

  if (typeof payload.npv === 'number') out.npv = payload.npv;

  if (Array.isArray(payload.leg_npvs)) {
    for (const leg of payload.leg_npvs) {
      if (isObj(leg) && typeof leg.npv === 'number') {
        out.legNpvs.push({ role: typeof leg.role === 'string' ? leg.role : 'leg', npv: leg.npv });
      }
    }
  }

  const pushMetric = (key: string, v: unknown) => {
    if (typeof v === 'number') out.metrics.push({ label: titleize(key), value: formatValue(v) });
  };
  for (const [k, v] of Object.entries(payload)) {
    if (k === 'npv' || k === 'leg_npvs' || k === 'extras' || k.endsWith('_flows')) continue;
    pushMetric(k, v);
  }
  if (isObj(payload.extras)) {
    for (const [k, v] of Object.entries(payload.extras)) pushMetric(k, v);
  }

  for (const [k, v] of Object.entries(payload)) {
    if (!k.endsWith('_flows') || !Array.isArray(v)) continue;
    const rows = v.filter(isObj);
    if (rows.length > 0) out.flows.push({ label: titleize(k.replace(/_flows$/, '')), rows });
  }
  return out;
}

/** Preferred flow-table column order; unknown columns append alphabetically. */
const FLOW_COLUMN_ORDER = [
  'payment_date',
  'accrual_start_date',
  'accrual_end_date',
  'fixing_date',
  'rate',
  'index_fixing',
  'spread',
  'amount',
  'discount',
  'present_value',
  'accrual_year_fraction',
];

export function flowColumns(rows: Array<Record<string, unknown>>): string[] {
  const present = new Set<string>();
  for (const row of rows) {
    for (const [k, v] of Object.entries(row)) {
      if (isScalar(v)) present.add(k);
    }
  }
  const ordered = FLOW_COLUMN_ORDER.filter(c => present.has(c));
  const rest = [...present].filter(c => !FLOW_COLUMN_ORDER.includes(c) && !c.startsWith('has_')).sort();
  return [...ordered, ...rest];
}

export function formatFlowCell(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return formatNumber(v);
    if (Math.abs(v) < 1) return v.toFixed(6);
    return formatNumber(v);
  }
  return String(v);
}

// Trace-level header derivation

export interface TraceStageLike {
  ts: string;
  stage: string;
  level: string;
  duration_ms?: number | null;
  summary?: string | null;
  payload?: unknown;
}

export interface TraceHeader {
  product: string | null;
  timestamp: string | null;
  isError: boolean;
  totalDurationMs: number | null;
  /** NPV on success; null when unavailable. */
  npv: number | null;
  /** Stable machine error code on failure; null on success. */
  errorCode: string | null;
  errorMessage: string | null;
}

export function deriveTraceHeader(stages: TraceStageLike[]): TraceHeader {
  const header: TraceHeader = {
    product: null,
    timestamp: null,
    isError: false,
    totalDurationMs: null,
    npv: null,
    errorCode: null,
    errorMessage: null,
  };
  if (stages.length === 0) return header;

  const input = stages.find(s => s.stage === 'input');
  if (input && isObj(input.payload) && typeof input.payload.product === 'string') {
    header.product = input.payload.product;
  }
  header.timestamp = stages[0].ts ?? null;

  const first = Date.parse(stages[0].ts ?? '');
  const last = Date.parse(stages[stages.length - 1].ts ?? '');
  if (Number.isFinite(first) && Number.isFinite(last) && last >= first) {
    header.totalDurationMs = last - first;
  } else {
    const sum = stages.reduce((acc, s) => acc + (typeof s.duration_ms === 'number' ? s.duration_ms : 0), 0);
    header.totalDurationMs = sum > 0 ? sum : null;
  }

  const errorStage = stages.find(s => s.stage === 'error') ?? stages.find(s => s.level === 'error');
  if (errorStage) {
    header.isError = true;
    const p = errorStage.payload;
    const e = isObj(p) && isObj(p.error) ? p.error : isObj(p) ? p : null;
    if (e) {
      header.errorCode = typeof e.code === 'string' ? e.code : null;
      header.errorMessage =
        typeof e.error === 'string' ? e.error : typeof e.message === 'string' ? e.message : null;
    }
  }

  const resp = stages.find(s => s.stage === 'engine_response');
  if (resp && isObj(resp.payload) && typeof resp.payload.npv === 'number') {
    header.npv = resp.payload.npv;
  }
  return header;
}

/** Request mode shown on the "Request received" card. */
export function deriveRequestMode(payload: unknown): string | null {
  if (!isObj(payload)) return null;
  const inline = Object.entries(payload).some(([k, v]) => k.startsWith('inline_') && v === true);
  if (inline) return 'inline';
  const byRef = Object.entries(payload).some(
    ([k, v]) => k.endsWith('_id') && k !== 'snapshot_id' && v !== null && v !== undefined,
  );
  if (byRef) return 'by-reference';
  return null;
}

export function prettyJson(payload: unknown): string {
  if (payload === undefined) return '(no payload)';
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}
