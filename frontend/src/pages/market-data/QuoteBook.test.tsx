// Quote Book — ONE unified master-detail table. These tests pin the unified
// contract:
//   (1) rows come from the orchestrator catalog (listSeries) enriched with each
//       series' LATEST value + source from the MD read path,
//   (2) "+ New series" creates a row (orchestrator createSeries),
//   (3) clicking a row expands IN PLACE to show its value history + an inline
//       add-value that reuses the import endpoint (importMarketDataJson),
//   (4) per-row edit PATCHes only changed fields,
//   (5) delete opens a confirm dialog that WARNS the values go too, then deletes.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  listSeriesMock,
  createSeriesMock,
  updateSeriesMock,
  deleteSeriesMock,
  importMarketDataJsonMock,
  resolveCatalogValuesAtMock,
  getSeriesPointsMock,
} = vi.hoisted(() => ({
  listSeriesMock: vi.fn(),
  createSeriesMock: vi.fn(),
  updateSeriesMock: vi.fn(),
  deleteSeriesMock: vi.fn(),
  importMarketDataJsonMock: vi.fn(),
  resolveCatalogValuesAtMock: vi.fn(),
  getSeriesPointsMock: vi.fn(),
}));

vi.mock('../../lib/api/orchestrator', () => ({
  listSeries: listSeriesMock,
  createSeries: createSeriesMock,
  updateSeries: updateSeriesMock,
  deleteSeries: deleteSeriesMock,
  importMarketDataJson: importMarketDataJsonMock,
}));

vi.mock('../../lib/marketDataBackend', () => ({
  getMdBackendSettings: () => ({ enabled: true, baseUrl: 'http://localhost:8082' }),
  resolveCatalogValuesAt: resolveCatalogValuesAtMock,
  getSeriesPoints: getSeriesPointsMock,
}));

vi.mock('../../components/Header', () => ({ default: () => null }));

import QuoteBook from './QuoteBook';

// Fixtures

function seriesRow(over: Partial<Record<string, unknown>> = {}) {
  return {
    canonical_id: 'USD.IRS.5Y',
    asset_class: 'IRS',
    family: 'IRS',
    instrument: 'IRS',
    currency: 'USD',
    tenor: '5Y',
    field: 'RATE',
    frequency: 'daily',
    units: 'decimal_rate',
    description: 'Synthetic USD IRS 5Y',
    created_at: '2025-01-01T00:00:00+00:00',
    updated_at: '2025-01-01T00:00:00+00:00',
    ...over,
  };
}

function okList(series: unknown[]) {
  return { ok: true, data: { series, count: series.length }, duration_ms: 1 };
}

function resolvedLatest(id: string, value: number, source = 'FRED') {
  return {
    canonical_id: id,
    found: true,
    is_exact: true,
    resolved_as_of: '2026-01-15T00:00:00+00:00',
    value,
    source,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QuoteBook />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listSeriesMock.mockReset();
  createSeriesMock.mockReset();
  updateSeriesMock.mockReset();
  deleteSeriesMock.mockReset();
  importMarketDataJsonMock.mockReset();
  resolveCatalogValuesAtMock.mockReset();
  getSeriesPointsMock.mockReset();

  listSeriesMock.mockResolvedValue(okList([seriesRow()]));
  resolveCatalogValuesAtMock.mockResolvedValue([resolvedLatest('USD.IRS.5Y', 0.039028)]);
  getSeriesPointsMock.mockResolvedValue([
    { canonical_id: 'USD.IRS.5Y', as_of: '2025-01-15T00:00:00+00:00', value: 0.039028, source: 'FRED' },
  ]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('QuoteBook (unified master-detail table)', () => {
  it('renders each series with its latest value + source in ONE table', async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText('USD.IRS.5Y')).toBeInTheDocument());
    // Latest value from the MD read path (rate → percent).
    await waitFor(() => expect(screen.getByText('3.9028%')).toBeInTheDocument());
    // Provenance chip shows the friendly provider label (FRED = real public data).
    expect(screen.getAllByText('FRED').length).toBeGreaterThan(0);

    expect(listSeriesMock).toHaveBeenCalled();
    expect(resolveCatalogValuesAtMock).toHaveBeenCalledWith(
      'http://localhost:8082',
      ['USD.IRS.5Y'],
      '2999-12-31',
    );
  });

  it('+ New series creates a row (orchestrator createSeries)', async () => {
    const user = userEvent.setup();
    createSeriesMock.mockResolvedValue({ ok: true, data: seriesRow({ canonical_id: 'USD.MYDESK.7Y' }), duration_ms: 1 });
    // After create, the reloaded list includes the new row.
    listSeriesMock
      .mockResolvedValueOnce(okList([seriesRow()]))
      .mockResolvedValue(okList([seriesRow(), seriesRow({ canonical_id: 'USD.MYDESK.7Y' })]));

    renderPage();
    await screen.findByText('USD.IRS.5Y');

    await user.click(screen.getByRole('button', { name: /new series/i }));
    await user.type(screen.getByLabelText('canonical_id'), 'USD.MYDESK.7Y');
    await user.type(screen.getByLabelText('asset_class'), 'IRS');
    await user.type(screen.getByLabelText('currency'), 'USD');
    await user.click(screen.getByRole('button', { name: /create series/i }));

    await waitFor(() => expect(createSeriesMock).toHaveBeenCalledWith({
      canonical_id: 'USD.MYDESK.7Y',
      asset_class: 'IRS',
      currency: 'USD',
    }));
    expect(await screen.findByText('USD.MYDESK.7Y')).toBeInTheDocument();
  });

  it('clicking a row expands it to show value history + an inline add-value that reuses the import endpoint', async () => {
    const user = userEvent.setup();
    importMarketDataJsonMock.mockResolvedValue({
      ok: true,
      data: { imported: 1, skipped: 0, errors: [], source: 'manual' },
      duration_ms: 1,
    });
    renderPage();
    await screen.findByText('USD.IRS.5Y');

    // Expand in place.
    await user.click(screen.getByText('USD.IRS.5Y'));
    await waitFor(() => expect(getSeriesPointsMock).toHaveBeenCalled());
    expect(await screen.findByText(/Value history/i)).toBeInTheDocument();

    // Inline add-value → import endpoint with a single manual quote.
    await user.type(screen.getByLabelText('add value date'), '2025-01-16');
    await user.type(screen.getByLabelText('add value amount'), '0.0375');
    await user.click(screen.getByRole('button', { name: /add value/i }));

    await waitFor(() => expect(importMarketDataJsonMock).toHaveBeenCalledWith([
      { canonical_id: 'USD.IRS.5Y', as_of: '2025-01-16', value: 0.0375 },
    ]));
    expect(await screen.findByText(/Added value for 2025-01-16/i)).toBeInTheDocument();
  });

  it('per-row edit PATCHes only the changed fields', async () => {
    const user = userEvent.setup();
    updateSeriesMock.mockResolvedValue({ ok: true, data: seriesRow({ description: 'new desc' }), duration_ms: 1 });
    renderPage();
    await screen.findByText('USD.IRS.5Y');

    await user.click(screen.getByRole('button', { name: /edit USD.IRS.5Y/i }));
    const desc = screen.getByLabelText('description');
    await user.clear(desc);
    await user.type(desc, 'new desc');
    await user.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(updateSeriesMock).toHaveBeenCalledWith('USD.IRS.5Y', { description: 'new desc' }));
  });

  it('delete opens a confirm dialog that warns values go too, then deletes', async () => {
    const user = userEvent.setup();
    deleteSeriesMock.mockResolvedValue({
      ok: true,
      data: { canonical_id: 'USD.IRS.5Y', deleted: true, deleted_quote_points: 42 },
      duration_ms: 1,
    });
    listSeriesMock.mockResolvedValueOnce(okList([seriesRow()])).mockResolvedValue(okList([]));
    renderPage();
    await screen.findByText('USD.IRS.5Y');

    await user.click(screen.getByRole('button', { name: /delete USD.IRS.5Y/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete series/i });
    expect(within(dialog).getByText(/quote values/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /confirm delete/i }));
    await waitFor(() => expect(deleteSeriesMock).toHaveBeenCalledWith('USD.IRS.5Y'));
    expect(await screen.findByText(/42 quote values/i)).toBeInTheDocument();
  });
});
