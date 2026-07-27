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

import ValuePointsTable from './ValuePointsTable';

const REF = '2026-01-15';

function zeroPoints(): ValueCurvePoint[] {
  return [
    { point_type: 'ZeroRatePoint', point: { tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 } },
    { point_type: 'ZeroRatePoint', point: { tenor_number: 1, tenor_time_unit: 'Years', zero_rate: 0.022 } },
  ];
}

describe('ValuePointsTable', () => {
  it('renders rows with percent values and supports add/delete', () => {
    const onChange = vi.fn();
    render(
      <ValuePointsTable
        quantity="zero"
        points={zeroPoints()}
        onChange={onChange}
        referenceDate={REF}
        pillarStyle="tenor"
        rowErrors={new Map()}
      />,
    );
    expect(screen.getAllByTestId('value-point-row')).toHaveLength(2);
    // Percent entry: 0.02 renders as "2".
    expect(screen.getByLabelText('Value 1')).toHaveValue(2);

    fireEvent.click(screen.getByText('+ Add row'));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toHaveLength(3);

    fireEvent.click(screen.getByLabelText('Delete row 1'));
    expect(onChange.mock.calls[1][0]).toHaveLength(1);
  });

  it('applies a pasted two-column block, replacing the table', () => {
    const onChange = vi.fn();
    render(
      <ValuePointsTable
        quantity="zero"
        points={[]}
        onChange={onChange}
        referenceDate={REF}
        pillarStyle="tenor"
        rowErrors={new Map()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: '6M\t2.0\n10Y 3.4' },
    });
    fireEvent.click(screen.getByText('Apply'));
    const parsed = onChange.mock.calls[0][0] as ValueCurvePoint[];
    // 2 pasted rows + the auto-prepended reference-date anchor.
    expect(parsed).toHaveLength(3);
    expect(parsed[0].point).toMatchObject({ date: REF, zero_rate: 0.02 });
    expect(parsed[1].point).toMatchObject({ tenor_number: 6, tenor_time_unit: 'Months', zero_rate: 0.02 });
  });

  it('reports unparsable paste lines without dropping good rows', () => {
    const onChange = vi.fn();
    render(
      <ValuePointsTable
        quantity="zero"
        points={[]}
        onChange={onChange}
        referenceDate={REF}
        pillarStyle="tenor"
        rowErrors={new Map()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Paste table…' }));
    fireEvent.change(screen.getByTestId('paste-table-input'), {
      target: { value: '6M 2.0\ngarbage-line' },
    });
    fireEvent.click(screen.getByText('Apply'));
    expect(screen.getByText(/Line 2/)).toBeInTheDocument();
    expect(onChange.mock.calls[0][0]).toHaveLength(2); // good row + its reference anchor
  });

  it('pins the DF reference row (read-only) and shows per-row errors', () => {
    const points = [
      pinnedDfPoint(REF),
      { point_type: 'DiscountFactorPoint', point: { tenor_number: 5, tenor_time_unit: 'Years', discount_factor: 1.2 } } as ValueCurvePoint,
    ];
    const validation = validateValuePoints(points, 'df', REF);
    render(
      <ValuePointsTable
        quantity="df"
        points={points}
        onChange={vi.fn()}
        referenceDate={REF}
        pillarStyle="tenor"
        rowErrors={validation.rowErrors}
      />,
    );
    expect(screen.getByText('(reference)')).toBeInTheDocument();
    expect(screen.getByTestId('value-point-row-error')).toHaveTextContent('(0, 1]');
    // The pinned row has no delete button.
    expect(screen.queryByLabelText('Delete row 1')).toBeNull();
    expect(screen.getByLabelText('Delete row 2')).toBeInTheDocument();
  });

  it('offers the MD-catalog quote picker with As-Of-resolved values', async () => {
    const onChange = vi.fn();
    const points: ValueCurvePoint[] = [
      { point_type: 'ZeroRatePoint', point: { tenor_number: 5, tenor_time_unit: 'Years', quote_id: '' } },
    ];
    render(
      <ValuePointsTable
        quantity="zero"
        points={points}
        onChange={onChange}
        referenceDate={REF}
        pillarStyle="tenor"
        rowErrors={new Map()}
      />,
    );
    expect(screen.getByLabelText('Source 1')).toHaveValue('quote');
    await waitFor(() => {
      expect(screen.getByText(/GBP\.RATES\.BOE\.OIS\.5Y\.PAR — 4\.100% @2026-01-15/)).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Quote 1'), {
      target: { value: 'GBP.RATES.BOE.OIS.5Y.PAR' },
    });
    const updated = onChange.mock.calls[0][0] as ValueCurvePoint[];
    expect(updated[0].point.quote_id).toBe('GBP.RATES.BOE.OIS.5Y.PAR');
  });
});
