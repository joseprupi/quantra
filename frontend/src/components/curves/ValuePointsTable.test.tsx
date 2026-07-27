import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

afterEach(cleanup);
import { ValueCurvePoint } from '../../lib/types';
import { pinnedDfPoint, validateValuePoints } from '../../lib/valueCurves';

vi.mock('../../lib/marketDataBackend', () => ({
  getMdBackendSettings: vi.fn().mockReturnValue({ enabled: true, baseUrl: 'http://md' }),
  listCatalogSeries: vi
    .fn()
    .mockResolvedValue([{ canonical_id: 'GBP.RATES.BOE.OIS.5Y.PAR' }]),
  resolveCatalogValuesAt: vi.fn().mockResolvedValue([
    { canonical_id: 'GBP.RATES.BOE.OIS.5Y.PAR', value: 0.041, resolved_as_of: '2026-01-15T00:00:00' },
  ]),
}));

vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: vi.fn().mockReturnValue({ asOfDate: '2026-01-15', setAsOfDate: vi.fn() }),
}));

import ValuePointsTable, { ValuePointsTableProps } from './ValuePointsTable';

const REF = '2026-01-15';

function zeroRows(): ValueCurvePoint[] {
  return [
    { point_type: 'ZeroRatePoint', point: { tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 } },
    { point_type: 'ZeroRatePoint', point: { tenor_number: 1, tenor_time_unit: 'Years', zero_rate: 0.022 } },
  ];
}

function renderTable(overrides: Partial<ValuePointsTableProps> = {}) {
  const props: ValuePointsTableProps = {
    quantity: 'zero',
    rows: [],
    onRowsChange: vi.fn(),
    anchorValue: undefined,
    onAnchorValueChange: vi.fn(),
    referenceDate: REF,
    rowErrors: new Map(),
    ...overrides,
  };
  render(<ValuePointsTable {...props} />);
  return props;
}

describe('ValuePointsTable', () => {
  it('renders the pinned anchor + rows with percent values and supports add/delete', () => {
    const { onRowsChange } = renderTable({ rows: zeroRows(), anchorValue: 0.019 });

    // Pinned reference-date header row: not part of the editable row list.
    const anchor = screen.getByTestId('value-anchor-row');
    expect(anchor).toHaveTextContent(`Reference date · ${REF}`);
    expect(screen.getByLabelText('Reference date value')).toHaveValue(1.9);

    expect(screen.getAllByTestId('value-point-row')).toHaveLength(2);
    // Percent entry: 0.02 renders as "2".
    expect(screen.getByLabelText('Value 1')).toHaveValue(2);
    // Resolved dates render next to the maturities.
    expect(screen.getAllByTestId('resolved-date')[0]).toHaveTextContent('→ 2026-07-15');
    expect(screen.getAllByTestId('resolved-date')[1]).toHaveTextContent('→ 2027-01-15');

    fireEvent.click(screen.getByText('+ Add row'));
    expect(onRowsChange).toHaveBeenCalledTimes(1);
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0]).toHaveLength(3);

    fireEvent.click(screen.getByLabelText('Delete row 1'));
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[1][0]).toHaveLength(1);
  });

  it('parses the maturity on blur (tenor or date) and auto-sorts rows by resolved date', () => {
    const { onRowsChange } = renderTable({ rows: zeroRows(), anchorValue: 0.019 });
    const calls = (onRowsChange as ReturnType<typeof vi.fn>).mock.calls;

    // Retype the 1Y row as 3M: on blur the rows re-sort (3M before 6M).
    const maturity2 = screen.getByLabelText('Maturity 2');
    fireEvent.change(maturity2, { target: { value: '3m' } });
    expect(onRowsChange).not.toHaveBeenCalled(); // never mid-keystroke
    fireEvent.blur(maturity2, { target: { value: '3m' } });
    const sorted = calls[0][0] as ValueCurvePoint[];
    expect(sorted[0].point).toMatchObject({ tenor_number: 3, tenor_time_unit: 'Months' });
    expect(sorted[1].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months' });
  });

  it('accepts an ISO date as a maturity and echoes it as the resolved date', () => {
    const { onRowsChange } = renderTable({
      rows: [{ point_type: 'ZeroRatePoint', point: { tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 } }],
      anchorValue: 0.019,
    });
    const maturity = screen.getByLabelText('Maturity 1');
    fireEvent.change(maturity, { target: { value: '2033-01-15' } });
    fireEvent.blur(maturity, { target: { value: '2033-01-15' } });
    const next = (onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0] as ValueCurvePoint[];
    expect(next[0].point).toMatchObject({ date: '2033-01-15' });
    expect(next[0].point.tenor_number).toBeUndefined();
  });

  it('shows a per-row error for an unparsable maturity and keeps the text', () => {
    renderTable({ rows: zeroRows(), anchorValue: 0.019 });
    const maturity1 = screen.getByLabelText('Maturity 1');
    fireEvent.change(maturity1, { target: { value: 'banana' } });
    fireEvent.blur(maturity1, { target: { value: 'banana' } });
    expect(screen.getByLabelText('Maturity 1')).toHaveValue('banana');
    expect(screen.getByTestId('value-point-row-error')).toHaveTextContent('Unrecognized maturity "banana"');
  });

  it('applies a pasted two-column block as the new row list (no anchor row created)', () => {
    const { onRowsChange, onAnchorValueChange } = renderTable();
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: '6M\t2.0\n10Y 3.4' },
    });
    fireEvent.click(screen.getByText('Apply'));
    const parsed = (onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0] as ValueCurvePoint[];
    expect(parsed).toHaveLength(2);
    expect(parsed[0].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 });
    expect(onAnchorValueChange).not.toHaveBeenCalled();
  });

  it('a pasted reference-date line feeds the pinned row instead of creating a row', () => {
    const { onRowsChange, onAnchorValueChange } = renderTable();
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: `${REF} 1.8\n10Y 3.4` },
    });
    fireEvent.click(screen.getByText('Apply'));
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0]).toHaveLength(1);
    expect(onAnchorValueChange).toHaveBeenCalledTimes(1);
    expect(
      (onAnchorValueChange as ReturnType<typeof vi.fn>).mock.calls[0][0] as number,
    ).toBeCloseTo(0.018, 12);
  });

  it('DF: a pasted reference-date line is ignored with a note (pinned row fixed at 1.0)', () => {
    const { onRowsChange, onAnchorValueChange } = renderTable({ quantity: 'df' });
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: `${REF} 1.0\n5Y 0.85` },
    });
    fireEvent.click(screen.getByText('Apply'));
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0]).toHaveLength(1);
    expect(onAnchorValueChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('paste-note')).toHaveTextContent('ignored');
  });

  it('reports unparsable paste lines without dropping good rows', () => {
    const { onRowsChange } = renderTable();
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: '6M 2.0\ngarbage-line' },
    });
    fireEvent.click(screen.getByText('Apply'));
    expect(screen.getByText(/Line 2/)).toBeInTheDocument();
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0]).toHaveLength(1);
  });

  it('DF: pinned row is fixed at 1.0000 (not editable) and per-row errors land on rows', () => {
    const rows = [
      { point_type: 'DiscountFactorPoint', point: { tenor_number: 5, tenor_time_unit: 'Years', discount_factor: 1.2 } } as ValueCurvePoint,
    ];
    const validation = validateValuePoints([pinnedDfPoint(REF), ...rows], 'df', REF);
    renderTable({ quantity: 'df', rows, rowErrors: validation.rowErrors });
    const anchor = screen.getByTestId('value-anchor-row');
    expect(anchor).toHaveTextContent('1.0000');
    expect(anchor).toHaveTextContent('Fixed: a discount curve starts at 1.0 on its reference date.');
    expect(screen.queryByLabelText('Reference date value')).toBeNull();
    expect(screen.getByTestId('value-point-row-error')).toHaveTextContent('(0, 1]');
    // The pinned row has no delete button; the single editable row is row 1.
    expect(screen.getByLabelText('Delete row 1')).toBeInTheDocument();
    expect(screen.queryByLabelText('Delete row 2')).toBeNull();
  });

  it('zero / fwd: pinned row value is editable and anchor errors render under it', () => {
    const { onAnchorValueChange } = renderTable({
      rows: zeroRows(),
      anchorValue: undefined,
      rowErrors: new Map([[0, 'Enter the value at the reference date (the curve short end).']]),
    });
    const anchorInput = screen.getByLabelText('Reference date value');
    expect(anchorInput).toHaveAttribute('placeholder', 'short end');
    expect(screen.getAllByTestId('value-point-row-error')[0]).toHaveTextContent(
      'value at the reference date',
    );
    fireEvent.change(anchorInput, { target: { value: '1.9' } });
    expect(onAnchorValueChange).toHaveBeenCalledWith(0.019);
  });

  it('offers the MD-catalog quote picker with As-Of-resolved values', async () => {
    const rows: ValueCurvePoint[] = [
      { point_type: 'ZeroRatePoint', point: { tenor_number: 5, tenor_time_unit: 'Years', quote_id: '' } },
    ];
    const { onRowsChange } = renderTable({ rows, anchorValue: 0.019 });
    expect(screen.getByLabelText('Source 1')).toHaveValue('quote');
    await waitFor(() => {
      expect(screen.getByText(/GBP\.RATES\.BOE\.OIS\.5Y\.PAR — 4\.100% @2026-01-15/)).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Quote 1'), {
      target: { value: 'GBP.RATES.BOE.OIS.5Y.PAR' },
    });
    const updated = (onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0] as ValueCurvePoint[];
    expect(updated[0].point.quote_id).toBe('GBP.RATES.BOE.OIS.5Y.PAR');
  });
});
