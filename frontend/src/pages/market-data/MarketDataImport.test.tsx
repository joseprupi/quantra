// MarketDataImport: manual + CSV import into the shared md.*
// catalog. These tests pin the three contract points the screen owes:
//   (1) manual entry builds the JSON batch the orchestrator client expects,
//   (2) the per-row import report renders (imported/skipped + errors[] table),
//       including partial success, and
//   (3) a hard error envelope renders as an error banner keyed on `code`.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

const { importJsonMock, importCsvMock, listSeriesMock } = vi.hoisted(() => ({
  importJsonMock: vi.fn(),
  importCsvMock: vi.fn(),
  listSeriesMock: vi.fn(),
}));

vi.mock('../../lib/api/orchestrator', () => ({
  importMarketDataJson: importJsonMock,
  importMarketDataCsv: importCsvMock,
  listSeries: listSeriesMock,
}));

function seriesRow(canonical_id: string) {
  return {
    canonical_id,
    asset_class: 'RATES',
    family: 'IRS',
    instrument: 'IRS',
    currency: 'USD',
    tenor: '5Y',
    field: 'RATE',
    frequency: 'daily',
    units: 'decimal_rate',
    description: null,
    created_at: '2025-01-01T00:00:00+00:00',
    updated_at: '2025-01-01T00:00:00+00:00',
  };
}
vi.mock('../../components/Header', () => ({ default: () => null }));
vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: () => ({ asOfDate: '2025-01-15', setAsOfDate: vi.fn() }),
}));

import MarketDataImport from './MarketDataImport';

function renderPage() {
  return render(
    <MemoryRouter>
      <MarketDataImport />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  importJsonMock.mockReset();
  importCsvMock.mockReset();
  listSeriesMock.mockReset();
  // Default: two existing series populate the canonical_id dropdown.
  listSeriesMock.mockResolvedValue({
    ok: true,
    data: { series: [seriesRow('USD.MYTEST.5Y'), seriesRow('EUR.IRS.10Y')], count: 2 },
    duration_ms: 1,
  });
});
afterEach(cleanup);

describe('MarketDataImport — manual entry', () => {
  it('builds the JSON batch from the editable rows (id/date/value + optional source)', async () => {
    const user = userEvent.setup();
    importJsonMock.mockResolvedValue({
      ok: true,
      data: { imported: 1, skipped: 0, errors: [], source: 'manual' },
      duration_ms: 1,
    });
    renderPage();

    // The canonical_id dropdown is populated from listSeries() — pick a series.
    await screen.findByRole('option', { name: 'USD.MYTEST.5Y' });
    await user.selectOptions(screen.getByLabelText('canonical_id row 1'), 'USD.MYTEST.5Y');
    // as_of defaults to the app As-Of (2025-01-15) via the mocked hook.
    await user.type(screen.getByLabelText('value row 1'), '0.032');
    await user.type(screen.getByLabelText('source row 1'), 'desk');

    await user.click(screen.getByRole('button', { name: /import rows/i }));

    await waitFor(() => expect(importJsonMock).toHaveBeenCalledTimes(1));
    expect(importJsonMock).toHaveBeenCalledWith([
      { canonical_id: 'USD.MYTEST.5Y', as_of: '2025-01-15', value: 0.032, source: 'desk' },
    ]);
  });

  it('renders the per-row report incl. imported/skipped counts and the errors[] table', async () => {
    const user = userEvent.setup();
    importJsonMock.mockResolvedValue({
      ok: true,
      data: {
        imported: 1,
        skipped: 1,
        errors: [{ row: 2, reason: 'invalid canonical_id: \'bad\'' }],
        source: 'manual',
      },
      duration_ms: 1,
    });
    renderPage();

    await screen.findByRole('option', { name: 'USD.MYTEST.5Y' });
    await user.selectOptions(screen.getByLabelText('canonical_id row 1'), 'USD.MYTEST.5Y');
    await user.type(screen.getByLabelText('value row 1'), '0.032');
    await user.click(screen.getByRole('button', { name: /import rows/i }));

    const report = await screen.findByText(/Partial import/i);
    const scope = report.closest('div')!.parentElement!;
    // Counts render: "Imported 1" and "Skipped 1".
    expect(within(scope).getByText(/Imported/)).toBeInTheDocument();
    expect(within(scope).getByText(/Skipped/)).toBeInTheDocument();
    expect(within(scope).getAllByText('1').length).toBeGreaterThanOrEqual(2);
    // The per-row error surfaces in the report table.
    expect(screen.getByText(/invalid canonical_id/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View in Quote Book/i })).toHaveAttribute('href', '/quote-book');
  });

  it('surfaces a hard error envelope as an error banner keyed on code', async () => {
    const user = userEvent.setup();
    importJsonMock.mockResolvedValue({
      ok: false,
      envelope: {
        error: 'Import batch is empty; supply at least one quote.',
        code: 'market_data_import_invalid_request',
        request_id: 'rid-abc',
      },
      httpStatus: 400,
      duration_ms: 1,
    });
    renderPage();

    await screen.findByRole('option', { name: 'USD.MYTEST.5Y' });
    await user.selectOptions(screen.getByLabelText('canonical_id row 1'), 'USD.MYTEST.5Y');
    await user.type(screen.getByLabelText('value row 1'), '0.032');
    await user.click(screen.getByRole('button', { name: /import rows/i }));

    expect(await screen.findByText(/Import failed/i)).toBeInTheDocument();
    expect(screen.getByText('market_data_import_invalid_request')).toBeInTheDocument();
  });

  it('blocks submit until at least one row passes client validation', async () => {
    renderPage();
    // Empty first row → Import button disabled, no network call.
    expect(screen.getByRole('button', { name: /import rows/i })).toBeDisabled();
    expect(importJsonMock).not.toHaveBeenCalled();
  });

  it('populates the canonical_id dropdown from the existing series (listSeries)', async () => {
    renderPage();
    // Both existing series show up as selectable options; the free-text field
    // is gone (import rejects unknown series).
    expect(await screen.findByRole('option', { name: 'USD.MYTEST.5Y' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'EUR.IRS.10Y' })).toBeInTheDocument();
    expect(listSeriesMock).toHaveBeenCalled();
    const select = screen.getByLabelText('canonical_id row 1');
    expect(select.tagName).toBe('SELECT');
  });
});

describe('MarketDataImport — CSV upload', () => {
  it('posts the picked file via the multipart client and renders its report', async () => {
    const user = userEvent.setup();
    importCsvMock.mockResolvedValue({
      ok: true,
      data: {
        imported: 2,
        skipped: 1,
        errors: [{ row: 3, reason: 'value must be numeric' }],
        source: 'csv',
      },
      duration_ms: 1,
    });
    renderPage();

    await user.click(screen.getByRole('button', { name: /csv upload/i }));
    const file = new File(
      ['canonical_id,as_of,value\nUSD.MYTEST.5Y,2025-01-15,0.032\nUSD.BAD.5Y,2025-01-15,oops\n'],
      'quotes.csv',
      { type: 'text/csv' },
    );
    await user.upload(screen.getByLabelText('CSV file'), file);
    await user.click(screen.getByRole('button', { name: /upload csv/i }));

    await waitFor(() => expect(importCsvMock).toHaveBeenCalledTimes(1));
    expect(importCsvMock.mock.calls[0][0]).toBe(file);
    expect(await screen.findByText(/Partial import/i)).toBeInTheDocument();
    expect(screen.getByText(/value must be numeric/i)).toBeInTheDocument();
  });
});
