// Time Series Lab — pins the LIVE data path (post market-data unification):
//   (1) the series universe comes from the orchestrator catalog (listSeries),
//       NOT the retired localStorage['quantra_quote_book'];
//   (2) latest value + provenance per series comes from the MD read path
//       (resolveCatalogValuesAt) and drives PROVIDER attribution —
//       FRED/ECB/BoE/Treasury are real public feeds, manual/csv → "Imported";
//   (3) value history for selected series comes from the MD read path
//       (getSeriesPoints) and is charted;
//   (4) an empty catalog and a catalog-load error show real states, not blank.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

const {
  listSeriesMock,
  resolveCatalogValuesAtMock,
  getSeriesPointsMock,
} = vi.hoisted(() => ({
  listSeriesMock: vi.fn(),
  resolveCatalogValuesAtMock: vi.fn(),
  getSeriesPointsMock: vi.fn(),
}));

vi.mock('../../lib/api/orchestrator', () => ({
  listSeries: listSeriesMock,
}));

vi.mock('../../lib/marketDataBackend', () => ({
  getMdBackendSettings: () => ({ enabled: true, baseUrl: 'http://localhost:8082' }),
  resolveCatalogValuesAt: resolveCatalogValuesAtMock,
  getSeriesPoints: getSeriesPointsMock,
}));

vi.mock('../../components/Header', () => ({ default: () => null }));

// Stub ECharts: render each plotted series' `name` so tests can observe that
// real points made it into the chart.
vi.mock('echarts-for-react', () => ({
  default: ({ option }: { option: { series: Array<{ name: string; data: unknown[] }> } }) => (
    <div data-testid="echart">
      {(option.series || []).map((s) => (
        <div key={s.name} data-testid="chart-series">{`${s.name}::${s.data.length}`}</div>
      ))}
    </div>
  ),
}));

import TimeSeriesLab from './TimeSeriesLab';

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

function resolved(id: string, source: string | null) {
  return {
    canonical_id: id,
    found: source !== null,
    is_exact: true,
    resolved_as_of: '2026-01-15T00:00:00+00:00',
    value: 0.039,
    source,
  };
}

function history(id: string, source: string) {
  // 5 daily points → chartable (>= 2 after downsampling).
  return Array.from({ length: 5 }, (_, i) => ({
    canonical_id: id,
    as_of: `2026-01-0${i + 1}T00:00:00+00:00`,
    value: 0.03 + i * 0.001,
    source,
  }));
}

function renderPage() {
  return render(
    <MemoryRouter>
      <TimeSeriesLab />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listSeriesMock.mockReset();
  resolveCatalogValuesAtMock.mockReset();
  getSeriesPointsMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('TimeSeriesLab (live data path + provider attribution)', () => {
  it('loads the series universe from the orchestrator catalog, not localStorage', async () => {
    listSeriesMock.mockResolvedValue(okList([seriesRow()]));
    resolveCatalogValuesAtMock.mockResolvedValue([resolved('USD.IRS.5Y', 'FRED')]);
    getSeriesPointsMock.mockResolvedValue(history('USD.IRS.5Y', 'FRED'));

    renderPage();

    await waitFor(() => expect(listSeriesMock).toHaveBeenCalled());
    expect(await screen.findByText('USD.IRS.5Y')).toBeInTheDocument();
    // Latest value + provenance resolved from the MD read path with the sentinel.
    expect(resolveCatalogValuesAtMock).toHaveBeenCalledWith(
      'http://localhost:8082',
      ['USD.IRS.5Y'],
      '2999-12-31',
    );
  });

  it('filters by PROVIDER: FRED (real public data) vs Imported (manual)', async () => {
    listSeriesMock.mockResolvedValue(
      okList([
        seriesRow({ canonical_id: 'USD.IRS.5Y', description: 'FRED swap rate' }),
        seriesRow({ canonical_id: 'USD.MYDESK.7Y', description: 'My manual desk' }),
      ]),
    );
    resolveCatalogValuesAtMock.mockResolvedValue([
      resolved('USD.IRS.5Y', 'FRED'),
      resolved('USD.MYDESK.7Y', 'manual'),
    ]);
    getSeriesPointsMock.mockImplementation((_b: string, id: string) =>
      Promise.resolve(history(id, id === 'USD.MYDESK.7Y' ? 'manual' : 'FRED')),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText('USD.IRS.5Y');
    await screen.findByText('USD.MYDESK.7Y');

    // Filter to "Imported" → only the user-supplied (manual) series survives.
    await user.selectOptions(screen.getByDisplayValue('All providers'), 'Imported');
    await waitFor(() => expect(screen.queryByText('USD.IRS.5Y')).not.toBeInTheDocument());
    expect(screen.getByText('USD.MYDESK.7Y')).toBeInTheDocument();

    // Filter to "FRED" → only the real FRED series survives (NOT Imported).
    await user.selectOptions(screen.getByDisplayValue('Imported'), 'FRED');
    await waitFor(() => expect(screen.queryByText('USD.MYDESK.7Y')).not.toBeInTheDocument());
    expect(screen.getByText('USD.IRS.5Y')).toBeInTheDocument();
  });

  it('charts real value history fetched from the MD read path', async () => {
    listSeriesMock.mockResolvedValue(okList([seriesRow()]));
    resolveCatalogValuesAtMock.mockResolvedValue([resolved('USD.IRS.5Y', 'FRED')]);
    getSeriesPointsMock.mockResolvedValue(history('USD.IRS.5Y', 'FRED'));

    renderPage();

    await waitFor(() =>
      expect(getSeriesPointsMock).toHaveBeenCalledWith(
        'http://localhost:8082',
        'USD.IRS.5Y',
        '1900-01-01',
        '2999-12-31',
        expect.any(Number),
      ),
    );
    // The stubbed chart renders one series tagged with its provider "(FRED)".
    const plotted = await screen.findByTestId('chart-series');
    expect(plotted.textContent).toContain('(FRED)');
    expect(plotted.textContent).not.toMatch(/::(0|1)$/); // >= 2 points charted
  });

  it('shows a real empty state (not a blank page) when the catalog is empty', async () => {
    listSeriesMock.mockResolvedValue(okList([]));
    resolveCatalogValuesAtMock.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText(/No series yet/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Quote Book/i })).toBeInTheDocument();
    expect(getSeriesPointsMock).not.toHaveBeenCalled();
  });

  it('surfaces a catalog-load error envelope', async () => {
    listSeriesMock.mockResolvedValue({
      ok: false,
      envelope: { error: 'boom', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 1,
    });

    renderPage();

    expect(await screen.findByText(/Could not reach the orchestrator/i)).toBeInTheDocument();
  });
});
