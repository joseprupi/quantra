import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const { getTraceMock } = vi.hoisted(() => ({ getTraceMock: vi.fn() }));

vi.mock('../lib/api/orchestrator', () => ({
  getTrace: getTraceMock,
}));

vi.mock('../components/Header', () => ({ default: () => null }));

import Investigate from './Investigate';

// Helpers

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Investigate />
    </MemoryRouter>,
  );
}

// A realistic successful swap_ir trace, mirroring the live payload shapes:
// the engine_request stage carries BOTH engine_wire.decoded (numeric FlatBuffers
// enums) and assembled_request (string enums, orchestrator internal superset).
const SUCCESS_TRACE = {
  request_id: 'req-ok-1',
  stages: [
    {
      ts: '2026-07-02T10:00:00.000Z',
      stage: 'input',
      level: 'info',
      duration_ms: null,
      summary: 'Priced swaps_ir: 10,000,000 Payer @ 2.5%.',
      payload: {
        product: 'swaps_ir',
        as_of: '2026-02-09',
        swap_id: null,
        notional: 10000000,
        swap_type: 'Payer',
        fixed_rate: 0.025,
        inline_swap: true,
        snapshot_id: null,
        effective_date: '2026-02-11',
        termination_date: '2031-02-11',
      },
    },
    {
      ts: '2026-07-02T10:00:00.050Z',
      stage: 'load_entities',
      level: 'info',
      duration_ms: 5,
      summary: 'Loaded 1 curve (discount), index Euribor from inputs.',
      payload: { index: 'Euribor', curve_names: ['discount'], curve_set_id: null, snapshot: null },
    },
    {
      ts: '2026-07-02T10:00:00.060Z',
      stage: 'md_resolve',
      level: 'info',
      duration_ms: 3,
      summary: 'Resolved 1 of 2 quotes.',
      payload: {
        requested_canonical_ids: ['EUR.IRS.5Y', 'USD.IRS.1Y'],
        resolved: [
          { canonical_id: 'EUR.IRS.5Y', value: 0.0265, source: 'synthetic', from_snapshot: false, as_of: '2026-02-09' },
        ],
        misses: ['USD.IRS.1Y'],
        live_count: 1,
        snapshot_count: 0,
      },
    },
    {
      ts: '2026-07-02T10:00:00.100Z',
      stage: 'engine_request',
      level: 'info',
      duration_ms: null,
      summary: 'Sent PriceVanillaSwap to the engine (1640 bytes).',
      payload: {
        engine_wire: {
          rpc: 'PriceVanillaSwap',
          sent: true,
          request_bytes_len: 1640,
          decoded: {
            include_flows: true,
            swaps: [
              {
                vanilla_swap: {
                  swap_type: 0,
                  fixed_leg: {
                    rate: 0.025,
                    notional: 10000000,
                    day_counter: 14,
                    schedule: { frequency: 0, effective_date: '2026-02-11', termination_date: '2031-02-11' },
                  },
                  floating_leg: {
                    index: { id: 'Euribor3M' },
                    notional: 10000000,
                    day_counter: 0,
                    schedule: { frequency: 10, effective_date: '2026-02-11', termination_date: '2031-02-11' },
                  },
                },
                forwarding_curve: 'discount',
                discounting_curve: 'discount',
              },
            ],
            pricing: {
              as_of_date: '2026-02-09',
              rates: {
                curves: [{ id: 'discount', points: [] }],
                indices: [
                  { id: 'forwarding_index', name: 'Euribor', tenor: { n: 6, unit: 5 }, day_counter: 0 },
                  { id: 'Euribor3M', name: 'Euribor', tenor: { n: 3, unit: 5 }, day_counter: 0 },
                ],
              },
            },
          },
        },
        assembled_request: {
          as_of: '2026-02-09',
          snapshot_id: null,
          trade: {
            swap: {
              notional: 10000000,
              fixed_rate: 0.025,
              swap_type: 'Payer',
              effective_date: '2026-02-11',
              termination_date: '2031-02-11',
              floating_leg: { schedule: { frequency: 'Quarterly' } },
              pricing: {
                indices: [
                  { id: 'EURIBOR_3M', name: 'Euribor', tenor: { n: 3, unit: 'Months' }, day_counter: 'Actual360' },
                ],
              },
            },
          },
          curves: [{ id: null, name: 'discount', points: [] }],
        },
      },
    },
    {
      ts: '2026-07-02T10:00:00.300Z',
      stage: 'engine_response',
      level: 'info',
      duration_ms: 171,
      summary: 'Engine returned NPV -54,053.80.',
      payload: {
        npv: -54053.8,
        leg_npvs: [
          { role: 'fixed', npv: -1164383.05 },
          { role: 'floating', npv: 1110329.25 },
        ],
        extras: { fair_rate: 0.0238 },
        fixed_leg_flows: [
          { payment_date: '2027-02-11', amount: 250000, discount: 0.975, present_value: 243780.7 },
        ],
        floating_leg_flows: [
          { payment_date: '2026-08-11', amount: 133236.11, fixing_date: '2026-02-09', present_value: 131463.16 },
        ],
      },
    },
    {
      ts: '2026-07-02T10:00:00.320Z',
      stage: 'history_write',
      level: 'warn',
      duration_ms: null,
      summary: 'Audit-log write skipped — the pricing_history row was not persisted.',
      payload: { outcome: 'success_row', recorded: false, pricing_history_id: null },
    },
  ],
};

// A failed trace: pre-send curve-resolution failure (real live shape).
const ERROR_TRACE = {
  request_id: 'req-err-1',
  stages: [
    {
      ts: '2026-07-02T11:00:00.000Z',
      stage: 'input',
      level: 'info',
      duration_ms: null,
      summary: 'Priced swaps_ir.',
      payload: { product: 'swaps_ir', notional: 10000000, inline_swap: true },
    },
    {
      ts: '2026-07-02T11:00:00.050Z',
      stage: 'engine_request',
      level: 'info',
      duration_ms: null,
      summary: 'Assembled the engine request (not sent — pre-send failure).',
      payload: {
        engine_wire: { sent: false },
        assembled_request: { as_of: '2026-02-09', trade: { swap: { notional: 10000000 } } },
      },
    },
    {
      ts: '2026-07-02T11:00:00.060Z',
      stage: 'error',
      level: 'error',
      duration_ms: null,
      summary: 'Failed: swap_ir_curve_resolution_failed.',
      payload: {
        error: {
          code: 'swap_ir_curve_resolution_failed',
          error: "Curve helper references index id 'EURIBOR_6M' but no IndexDef could be registered for it.",
          details: [{ unregistered_index_id: 'EURIBOR_6M' }],
          status_code: 422,
        },
      },
    },
  ],
};

// A CDS trace: the engine_request payload IS the assembled request (no
// engine_wire wrapper — wire capture is not recorded for this product).
const CDS_TRACE = {
  request_id: 'req-cds-1',
  stages: [
    {
      ts: '2026-07-02T12:00:00.000Z',
      stage: 'input',
      level: 'info',
      duration_ms: null,
      summary: 'Priced cds: inline trade.',
      payload: { product: 'cds', as_of: '2025-01-15', cds_id: null, inline_cds: true },
    },
    {
      ts: '2026-07-02T12:00:00.050Z',
      stage: 'engine_request',
      level: 'info',
      duration_ms: null,
      summary: 'Assembled the engine request.',
      payload: {
        as_of: '2025-01-15',
        trade: {
          cds: {
            notional: 10000000,
            running_coupon: 0.01,
            schedule: { effective_date: '2025-01-15', termination_date: '2030-01-15' },
          },
        },
        discount_curve: { name: 'USD-OIS', points: [] },
        credit_curve: { name: 'ACME-SR', recovery_rate: 0.3 },
      },
    },
    {
      ts: '2026-07-02T12:00:00.100Z',
      stage: 'engine_response',
      level: 'info',
      duration_ms: 37,
      summary: 'Engine returned NPV 178,322.29.',
      payload: { npv: 178322.29, fair_spread: 0.0139, extras: { default_leg_npv: 639411.83 } },
    },
  ],
};

describe('Investigate page (pipeline inspector redesign)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  // Header bar

  it('renders the header bar: request_id, product, OK status, headline NPV', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('trace-header')).toBeInTheDocument());
    const header = screen.getByTestId('trace-header');
    expect(within(header).getByTestId('trace-request-id')).toHaveTextContent('req-ok-1');
    expect(within(header).getByTestId('trace-product')).toHaveTextContent('swaps_ir');
    expect(within(header).getByTestId('trace-status')).toHaveTextContent('OK');
    expect(within(header).getByTestId('trace-headline')).toHaveTextContent('NPV -54,053.8');
    expect(within(header).getByTestId('copy-request-id')).toBeInTheDocument();
  });

  it('renders the header bar in ERROR state with the machine code as headline', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: ERROR_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-err-1');

    await waitFor(() => expect(screen.getByTestId('trace-header')).toBeInTheDocument());
    const header = screen.getByTestId('trace-header');
    expect(within(header).getByTestId('trace-status')).toHaveTextContent('ERROR');
    expect(within(header).getByTestId('trace-headline')).toHaveTextContent('swap_ir_curve_resolution_failed');
  });

  it('copies the request_id via the clipboard API', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('copy-request-id')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('copy-request-id'));
    expect(writeText).toHaveBeenCalledWith('req-ok-1');
  });

  // Pipeline cards

  it('renders one card per stage, in order, with status dots and durations', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getAllByTestId('stage-card')).toHaveLength(6));
    const titles = screen.getAllByTestId('stage-title').map(el => el.textContent);
    expect(titles).toEqual([
      'Request received',
      'Entities loaded',
      'Market data',
      'Engine request',
      'Engine response',
      'History write',
    ]);
    expect(screen.getAllByTestId('stage-dot')).toHaveLength(6);
    expect(screen.getByText('171 ms')).toBeInTheDocument();
  });

  it('shows request mode (inline) and the market-data resolved-quotes table with misses', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('request-mode')).toHaveTextContent('inline'));

    const table = screen.getByTestId('resolved-quotes-table');
    expect(within(table).getByText('EUR.IRS.5Y')).toBeInTheDocument();
    expect(within(table).getByText('0.0265')).toBeInTheDocument();
    expect(screen.getByTestId('md-misses')).toHaveTextContent('USD.IRS.1Y');
  });

  // Engine request: structured summary + wire ⟷ internal toggle

  it('defaults to the wire view: trade, curves, indices table (with day count) and schedules', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('view-toggle-wire')).toBeInTheDocument());
    expect(screen.getByTestId('view-toggle-wire')).toHaveAttribute('aria-pressed', 'true');

    // Trade key terms (wire: numeric swap_type 0 → Payer).
    const trade = screen.getByTestId('engine-request-trade');
    expect(trade).toHaveTextContent('10,000,000');
    expect(trade).toHaveTextContent('2.5%');
    expect(trade).toHaveTextContent('Payer');
    expect(trade).toHaveTextContent('2026-02-11');
    expect(trade).toHaveTextContent('2031-02-11');

    // Curves with roles derived from the trade legs.
    const curves = screen.getByTestId('engine-request-curves');
    expect(curves).toHaveTextContent('discount');
    expect(curves).toHaveTextContent('forwarding');
    expect(curves).toHaveTextContent('discounting');

    // Indices TABLE: both catalog entries; the leg's real index (3M) is marked
    // as bound; the default catalog entry gets a subtle badge (no prose lecture).
    const rows = screen.getAllByTestId('index-row');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('6M');
    expect(within(rows[0]).getByTestId('default-catalog-badge')).toHaveTextContent('default catalog');
    expect(rows[1]).toHaveTextContent('3M');
    expect(rows[1]).toHaveTextContent('Actual360'); // wire numeric day_counter 0 mapped
    expect(within(rows[1]).getByTestId('index-bound-leg')).toHaveTextContent('floating leg');

    // Schedules: wire numeric frequencies mapped (0 = Annual, 10 = Quarterly).
    const schedules = screen.getByTestId('engine-request-schedules');
    expect(schedules).toHaveTextContent('Fixed leg');
    expect(schedules).toHaveTextContent('Annual');
    expect(schedules).toHaveTextContent('Floating leg');
    expect(schedules).toHaveTextContent('Quarterly');
  });

  it('the toggle switches summary + raw JSON to the orchestrator-internal object', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('view-toggle-internal')).toBeInTheDocument());

    // One-line tooltip on the ⓘ affordance.
    expect(screen.getByTestId('view-toggle-tooltip')).toHaveAttribute(
      'title',
      expect.stringMatching(/Wire = exact FlatBuffers sent to the engine.*not transmitted/),
    );

    fireEvent.click(screen.getByTestId('view-toggle-internal'));
    expect(screen.getByTestId('view-toggle-internal')).toHaveAttribute('aria-pressed', 'true');

    // Internal indices id (EURIBOR_3M) replaces the wire ids.
    const rows = screen.getAllByTestId('index-row');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('EURIBOR_3M');

    // Raw JSON now reads the assembled_request.
    const engineCard = screen.getAllByTestId('stage-card')[3];
    fireEvent.click(within(engineCard).getByTestId('raw-json-toggle'));
    const raw = within(engineCard).getByTestId('raw-json');
    expect(raw).toHaveTextContent('EURIBOR_3M');
    expect(raw).toHaveTextContent('snapshot_id');
  });

  // Engine response

  it('shows NPV, per-leg NPVs, fair rate and a collapsed flows table that expands', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getByTestId('engine-response-npv')).toHaveTextContent('-54,053.8'));
    expect(screen.getByTestId('leg-npvs')).toHaveTextContent('fixed');
    expect(screen.getByTestId('leg-npvs')).toHaveTextContent('floating');
    expect(screen.getByTestId('engine-response-metrics')).toHaveTextContent('Fair rate');

    // Flows are collapsed by default.
    expect(screen.queryByTestId('flows-table')).not.toBeInTheDocument();
    const toggles = screen.getAllByTestId('flows-toggle');
    expect(toggles).toHaveLength(2); // fixed + floating
    fireEvent.click(toggles[1]);
    const table = screen.getByTestId('flows-table');
    expect(table).toHaveTextContent('2026-08-11');
    expect(table).toHaveTextContent('133,236.11');
  });

  // Error rendering

  it('renders the real error message + code prominently on a failed trace', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: ERROR_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-err-1');

    await waitFor(() => expect(screen.getByTestId('error-block')).toBeInTheDocument());
    const block = screen.getByTestId('error-block');
    expect(block).toHaveTextContent('swap_ir_curve_resolution_failed');
    expect(block).toHaveTextContent(/no IndexDef could be registered/);
    expect(block).toHaveTextContent('unregistered_index_id');
  });

  it('a pre-send failure renders the engine-request card without a wire view, no crash', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: ERROR_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-err-1');

    await waitFor(() => expect(screen.getByTestId('view-toggle-internal')).toBeInTheDocument());
    // engine_wire has no decoded object → wire disabled, internal selected.
    expect(screen.getByTestId('view-toggle-wire')).toBeDisabled();
    expect(screen.getByTestId('view-toggle-internal')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('engine-request-trade')).toHaveTextContent('10,000,000');
  });

  // Product-agnostic rendering

  it('renders a CDS trace generically: assembled request only, curves with roles, no indices', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: CDS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-cds-1');

    await waitFor(() => expect(screen.getByTestId('trace-header')).toBeInTheDocument());
    expect(screen.getByTestId('trace-product')).toHaveTextContent('cds');
    expect(screen.getByTestId('trace-headline')).toHaveTextContent('NPV 178,322.29');

    // No wrapper → the payload IS the internal request; wire is unavailable.
    expect(screen.getByTestId('view-toggle-wire')).toBeDisabled();
    expect(screen.getByTestId('view-toggle-internal')).toHaveAttribute('aria-pressed', 'true');

    // Trade terms from trade.cds; curves from *_curve keys with derived roles.
    const trade = screen.getByTestId('engine-request-trade');
    expect(trade).toHaveTextContent('10,000,000');
    expect(trade).toHaveTextContent('1%'); // running_coupon 0.01
    expect(trade).toHaveTextContent('2030-01-15');
    const curves = screen.getByTestId('engine-request-curves');
    expect(curves).toHaveTextContent('USD-OIS');
    expect(curves).toHaveTextContent('discount');
    expect(curves).toHaveTextContent('ACME-SR');
    expect(curves).toHaveTextContent('credit');

    // No index catalog on a CDS → the indices table is simply absent.
    expect(screen.queryByTestId('engine-request-indices')).not.toBeInTheDocument();
  });

  // Removed band-aids

  it('the bug-specific band-aids are gone: no standalone callout, no snapshot_id prose', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getAllByTestId('stage-card')).toHaveLength(6));
    expect(screen.queryByText(/what the engine actually prices/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/carries TWO objects/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/STRIPPED during/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/does NOT mean null is sent/i)).not.toBeInTheDocument();
  });

  // Raw JSON + copy per stage

  it('raw JSON is collapsed by default, expands per stage, and has a copy button', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    renderAt('/investigate?request_id=req-ok-1');

    await waitFor(() => expect(screen.getAllByTestId('stage-card')).toHaveLength(6));
    expect(screen.queryByTestId('raw-json')).not.toBeInTheDocument();

    const inputCard = screen.getAllByTestId('stage-card')[0];
    fireEvent.click(within(inputCard).getByTestId('raw-json-toggle'));
    expect(within(inputCard).getByTestId('raw-json')).toHaveTextContent('"product": "swaps_ir"');

    fireEvent.click(within(inputCard).getByTestId('copy-raw-json'));
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('"product": "swaps_ir"'));
  });

  // Lookup flow (unchanged behavior)

  it('shows a clear not-found message on trace_not_found (404)', async () => {
    getTraceMock.mockResolvedValue({
      ok: false,
      envelope: { error: "No pricing trace found for request_id 'nope'.", code: 'trace_not_found', request_id: 'x' },
      httpStatus: 404,
      duration_ms: 2,
    });

    renderAt('/investigate?request_id=nope');

    await waitFor(() => expect(screen.getByText('No trace for this id')).toBeInTheDocument());
    expect(screen.getByText(/may belong to/i)).toBeInTheDocument();
    expect(screen.queryByTestId('stage-card')).not.toBeInTheDocument();
  });

  it('shows an idle prompt with no request_id and looks one up on submit', async () => {
    getTraceMock.mockResolvedValue({ ok: true, data: SUCCESS_TRACE, duration_ms: 3 });

    renderAt('/investigate');

    expect(screen.getByText(/Enter a request id above/i)).toBeInTheDocument();
    expect(getTraceMock).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('request_id'), { target: { value: 'req-ok-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Investigate' }));

    await waitFor(() => expect(getTraceMock).toHaveBeenCalledWith('req-ok-1'));
    await waitFor(() => expect(screen.getAllByTestId('stage-card')).toHaveLength(6));
  });

  it('renders an older/partial trace (unknown stage, no payload) without crashing', async () => {
    getTraceMock.mockResolvedValue({
      ok: true,
      data: {
        request_id: 'req-old-1',
        stages: [{ ts: 'not-a-date', stage: 'mystery_stage', level: 'debug', duration_ms: null }],
      },
      duration_ms: 3,
    });

    renderAt('/investigate?request_id=req-old-1');

    await waitFor(() => expect(screen.getAllByTestId('stage-card')).toHaveLength(1));
    // Unknown stages fall back to the raw stage key as the title.
    expect(screen.getByTestId('stage-title')).toHaveTextContent('mystery_stage');
    expect(screen.getByTestId('trace-status')).toHaveTextContent('OK');
  });
});
