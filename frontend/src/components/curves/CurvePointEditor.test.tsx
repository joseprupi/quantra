// CurvePointEditor — engine-0.6 OIS overnight params (payment_lag,
// averaging_method, lookback_days, lockout_days, apply_observation_shift;
// DatedOIS fixed_leg_frequency). The controls must render on both OIS
// branches and emit ints as NUMBERS (never strings) on save.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import CurvePointEditor from './CurvePointEditor';

afterEach(cleanup);
import { OISHelper, DatedOISHelper } from '../../lib/types';

// IndexPicker pulls the saved-index store (backend) — inert stub for these tests.
vi.mock('./IndexPicker', () => ({
  default: () => <div data-testid="index-picker-stub" />,
}));

// The MD quote catalog is irrelevant here; fail-quiet like an unreachable server.
vi.mock('../../lib/marketDataBackend', () => ({
  getMdBackendSettings: () => ({ baseUrl: 'http://localhost:0', enabled: false }),
  listCatalogSeries: async () => {
    throw new Error('no MD in tests');
  },
  resolveCatalogValuesAt: async () => {
    throw new Error('no MD in tests');
  },
}));

const OIS_POINT: OISHelper = {
  point_type: 'OISHelper',
  point: {
    rate: 0.0533,
    tenor_number: 5,
    tenor_time_unit: 'Years',
    overnight_index: { id: 'SOFR' },
    settlement_days: 2,
    calendar: 'UnitedStatesGovernmentBond',
    fixed_leg_frequency: 'Annual',
    fixed_leg_convention: 'ModifiedFollowing',
    fixed_leg_day_counter: 'Actual360',
    payment_lag: 0,
    averaging_method: 'Compound',
    lookback_days: 0,
    lockout_days: 0,
    apply_observation_shift: false,
  },
};

const DATED_POINT: DatedOISHelper = {
  point_type: 'DatedOISHelper',
  point: {
    rate: 0.031,
    start_date: '2025-01-15',
    end_date: '2026-01-15',
    overnight_index: { id: 'ESTR' },
    settlement_days: 2,
    calendar: 'TARGET',
    fixed_leg_frequency: 'Annual',
    fixed_leg_convention: 'ModifiedFollowing',
    fixed_leg_day_counter: 'Actual360',
    payment_lag: 0,
    averaging_method: 'Compound',
    lookback_days: 0,
    lockout_days: 0,
    apply_observation_shift: false,
  },
};

describe('CurvePointEditor — OIS overnight params (engine 0.6)', () => {
  it('renders the five overnight controls on the OISHelper branch', () => {
    render(<CurvePointEditor point={OIS_POINT} onSave={() => {}} onCancel={() => {}} />);
    expect(screen.getByLabelText('Payment lag (bd)')).toBeInTheDocument();
    expect(screen.getByLabelText('Averaging')).toBeInTheDocument();
    expect(screen.getByLabelText('Lookback (d)')).toBeInTheDocument();
    expect(screen.getByLabelText('Lockout (d)')).toBeInTheDocument();
    expect(screen.getByLabelText('Obs. shift')).toBeInTheDocument();
  });

  it('Averaging is a strict select with only Compound and Simple', () => {
    render(<CurvePointEditor point={OIS_POINT} onSave={() => {}} onCancel={() => {}} />);
    const select = screen.getByLabelText('Averaging') as HTMLSelectElement;
    expect(select.tagName).toBe('SELECT');
    expect(Array.from(select.options).map(o => o.value)).toEqual(['Compound', 'Simple']);
  });

  it('emits payment_lag/lookback/lockout as NUMBERS and the checkbox as boolean on save', () => {
    const onSave = vi.fn();
    render(<CurvePointEditor point={OIS_POINT} onSave={onSave} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText('Payment lag (bd)'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Averaging'), { target: { value: 'Simple' } });
    fireEvent.change(screen.getByLabelText('Lookback (d)'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('Lockout (d)'), { target: { value: '3' } });
    fireEvent.click(screen.getByLabelText('Obs. shift'));
    fireEvent.click(screen.getByText('Update'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const saved = onSave.mock.calls[0][0] as OISHelper;
    expect(saved.point.payment_lag).toBe(2);
    expect(saved.point.averaging_method).toBe('Simple');
    expect(saved.point.lookback_days).toBe(5);
    expect(saved.point.lockout_days).toBe(3);
    expect(saved.point.apply_observation_shift).toBe(true);
    expect(typeof saved.point.payment_lag).toBe('number');
    expect(typeof saved.point.lookback_days).toBe('number');
    expect(typeof saved.point.lockout_days).toBe('number');
  });

  it('clamps a negative payment lag to 0 (backend rejects negatives with a 422)', () => {
    const onSave = vi.fn();
    render(<CurvePointEditor point={OIS_POINT} onSave={onSave} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Payment lag (bd)'), { target: { value: '-3' } });
    fireEvent.click(screen.getByText('Update'));
    expect((onSave.mock.calls[0][0] as OISHelper).point.payment_lag).toBe(0);
  });

  it('renders the overnight controls plus Fixed leg freq on the DatedOISHelper branch', () => {
    const onSave = vi.fn();
    render(<CurvePointEditor point={DATED_POINT} onSave={onSave} onCancel={() => {}} />);
    expect(screen.getByLabelText('Payment lag (bd)')).toBeInTheDocument();
    expect(screen.getByLabelText('Averaging')).toBeInTheDocument();
    expect(screen.getByLabelText('Lookback (d)')).toBeInTheDocument();
    expect(screen.getByLabelText('Lockout (d)')).toBeInTheDocument();
    expect(screen.getByLabelText('Obs. shift')).toBeInTheDocument();

    const freq = screen.getByLabelText('Fixed leg freq') as HTMLSelectElement;
    expect(freq.tagName).toBe('SELECT');
    fireEvent.change(freq, { target: { value: 'Quarterly' } });
    fireEvent.change(screen.getByLabelText('Payment lag (bd)'), { target: { value: '1' } });
    fireEvent.click(screen.getByText('Update'));

    const saved = onSave.mock.calls[0][0] as DatedOISHelper;
    expect(saved.point.fixed_leg_frequency).toBe('Quarterly');
    expect(saved.point.payment_lag).toBe(1);
    expect(typeof saved.point.payment_lag).toBe('number');
  });

  it('a fresh OIS point starts from the legacy defaults (0 / Compound / 0 / 0 / false)', () => {
    const onSave = vi.fn();
    render(<CurvePointEditor onSave={onSave} onCancel={() => {}} />);
    fireEvent.click(screen.getByText('OIS'));
    fireEvent.click(screen.getByText('Add Instrument', { selector: 'button.flex-1' }));
    const saved = onSave.mock.calls[0][0] as OISHelper;
    expect(saved.point.payment_lag).toBe(0);
    expect(saved.point.averaging_method).toBe('Compound');
    expect(saved.point.lookback_days).toBe(0);
    expect(saved.point.lockout_days).toBe(0);
    expect(saved.point.apply_observation_shift).toBe(false);
  });
});
