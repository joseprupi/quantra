import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { useState } from 'react';

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

/** Stateful wrapper: rows actually update between events (the component is
 * controlled, so multi-step interactions need a live parent). */
function renderStateful(initialRows: ValueCurvePoint[], quantity: 'zero' | 'df' | 'fwd' = 'zero') {
  const latest = { rows: initialRows };
  function Harness() {
    const [rows, setRows] = useState(initialRows);
    latest.rows = rows;
    return (
      <ValuePointsTable
        quantity={quantity}
        rows={rows}
        onRowsChange={setRows}
        anchorValue={0.019}
        onAnchorValueChange={vi.fn()}
        referenceDate={REF}
        rowErrors={new Map()}
      />
    );
  }
  render(<Harness />);
  return latest;
}

describe('ValuePointsTable', () => {
  it('renders the pinned anchor + rows with percent values and supports add/delete', () => {
    const { onRowsChange } = renderTable({ rows: zeroRows(), anchorValue: 0.019 });

    // Pinned reference-date header row: not part of the editable row list.
    const anchor = screen.getByTestId('value-anchor-row');
    expect(anchor).toHaveTextContent(`Start · ${REF}`);
    expect(screen.getByLabelText('Start value at the reference date')).toHaveValue(1.9);

    expect(screen.getAllByTestId('value-point-row')).toHaveLength(2);
    // Percent entry: 0.02 renders as "2".
    expect(screen.getByLabelText('Value 1')).toHaveValue(2);
    // Stored tenor rows load in Tenor mode with their n/unit.
    expect(screen.getByLabelText('Tenor number 1')).toHaveValue(6);
    expect(screen.getByLabelText('Tenor unit 1')).toHaveValue('Months');
    // Resolved dates render as grey hints on tenor rows.
    expect(screen.getAllByTestId('resolved-date')[0]).toHaveTextContent('→ 2026-07-15');
    expect(screen.getAllByTestId('resolved-date')[1]).toHaveTextContent('→ 2027-01-15');

    fireEvent.click(screen.getByText('+ Add row'));
    expect(onRowsChange).toHaveBeenCalledTimes(1);
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[0][0]).toHaveLength(3);

    fireEvent.click(screen.getByLabelText('Delete row 1'));
    expect((onRowsChange as ReturnType<typeof vi.fn>).mock.calls[1][0]).toHaveLength(1);
  });

  it('tenor number edits commit on blur and auto-sort rows by resolved date', () => {
    const state = renderStateful(zeroRows());

    // Retype the 1Y row's number as 3 (unit stays Months after switching):
    // typing updates the row, the SORT happens on blur, never mid-keystroke.
    const number2 = screen.getByLabelText('Tenor number 2');
    fireEvent.change(number2, { target: { value: '3' } });
    // Still in entry order (3Y sorts after 6M anyway; change unit next).
    fireEvent.blur(number2);
    expect(state.rows[1].point).toMatchObject({ tenor_number: 3, tenor_time_unit: 'Years' });

    // A unit change is a completed choice: commits AND sorts immediately
    // (3 Years -> 3 Months sorts before 6 Months).
    fireEvent.change(screen.getByLabelText('Tenor unit 2'), { target: { value: 'Months' } });
    expect(state.rows[0].point).toMatchObject({ tenor_number: 3, tenor_time_unit: 'Months' });
    expect(state.rows[1].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months' });
  });

  it('Date mode: converts a tenor to its resolved date, accepts a date, no hint shown', () => {
    const state = renderStateful([
      { point_type: 'ZeroRatePoint', point: { tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 } },
      { point_type: 'ZeroRatePoint', point: { tenor_number: 1, tenor_time_unit: 'Years', zero_rate: 0.022 } },
    ]);
    const row1 = screen.getAllByTestId('value-point-row')[0];

    // Toggle row 1 to Date mode: the committed 6M tenor becomes its resolved date.
    fireEvent.click(row1.querySelector('button[aria-pressed="false"]')!);
    expect(state.rows[0].point).toMatchObject({ date: '2026-07-15' });
    expect(state.rows[0].point.tenor_number).toBeUndefined();

    // Date rows show a date input and NO resolved-date hint (redundant).
    const dateInput = screen.getByLabelText('Maturity date 1');
    expect(dateInput).toHaveValue('2026-07-15');
    expect(screen.getAllByTestId('resolved-date')).toHaveLength(1); // only the 1Y tenor row

    // Editing the date and committing re-sorts (2033 sorts after 1Y).
    fireEvent.change(dateInput, { target: { value: '2033-01-15' } });
    fireEvent.blur(screen.getByLabelText('Maturity date 1'));
    expect(state.rows[1].point).toMatchObject({ date: '2033-01-15' });
    expect(state.rows[0].point).toMatchObject({ tenor_number: 1, tenor_time_unit: 'Years' });
  });

  it('new rows default to Tenor mode and can be committed via number + unit', () => {
    const state = renderStateful([
      { point_type: 'ZeroRatePoint', point: { tenor_number: 5, tenor_time_unit: 'Years', zero_rate: 0.03 } },
    ]);
    fireEvent.click(screen.getByText('+ Add row'));
    // The new empty row shows the Tenor controls (default mode).
    expect(screen.getByLabelText('Tenor number 2')).toHaveValue(null);
    const tenorBtn = screen.getAllByTestId('value-point-row')[1].querySelector('button[aria-pressed="true"]')!;
    expect(tenorBtn).toHaveTextContent('Tenor');

    // Pick the unit first (no maturity yet: no sort), then type the number.
    fireEvent.change(screen.getByLabelText('Tenor unit 2'), { target: { value: 'Months' } });
    fireEvent.change(screen.getByLabelText('Tenor number 2'), { target: { value: '6' } });
    fireEvent.keyDown(screen.getByLabelText('Tenor number 2'), { key: 'Enter' });
    // 6M auto-sorts before 5Y.
    expect(state.rows[0].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months' });
    expect(state.rows[1].point).toMatchObject({ tenor_number: 5, tenor_time_unit: 'Years' });
  });

  it('applies a pasted two-column block and sets each row mode from the parsed line', () => {
    const state = renderStateful([]);
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: '6M\t2.0\n2033-01-15 3.4' },
    });
    fireEvent.click(screen.getByText('Apply'));
    expect(state.rows).toHaveLength(2);
    expect(state.rows[0].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 });
    expect(state.rows[1].point).toMatchObject({ date: '2033-01-15' });
    // Tenor line -> Tenor mode controls; date line -> Date mode control.
    expect(screen.getByLabelText('Tenor number 1')).toHaveValue(6);
    expect(screen.getByLabelText('Maturity date 2')).toHaveValue('2033-01-15');
    expect(screen.getAllByTestId('resolved-date')).toHaveLength(1);
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
    expect(anchor).toHaveTextContent('Always 1.0');
    expect(screen.queryByLabelText('Start value at the reference date')).toBeNull();
    expect(screen.getByTestId('value-point-row-error')).toHaveTextContent('(0, 1]');
    // The pinned row has no delete button; the single editable row is row 1.
    expect(screen.getByLabelText('Delete row 1')).toBeInTheDocument();
    expect(screen.queryByLabelText('Delete row 2')).toBeNull();
  });

  it('zero / fwd: pinned row value is editable and anchor errors render under it', () => {
    const { onAnchorValueChange } = renderTable({
      rows: zeroRows(),
      anchorValue: undefined,
      rowErrors: new Map([[0, 'Start value required.']]),
    });
    const anchorInput = screen.getByLabelText('Start value at the reference date');
    expect(anchorInput).toHaveAttribute('placeholder', 'start value');
    expect(screen.getAllByTestId('value-point-row-error')[0]).toHaveTextContent(
      'Start value required.',
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
