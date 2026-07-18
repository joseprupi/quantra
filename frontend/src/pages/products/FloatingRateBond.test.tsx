import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceFloatingBondMock,
  loginMock,
  useAuthMock,
  capturedCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceFloatingBondMock: vi.fn(),
    loginMock: vi.fn<[], Promise<unknown>>(),
    useAuthMock: vi.fn(),
    capturedCurveCallback,
    indexStoreGetAllMock: vi.fn(),
    useParamsMock: vi.fn(),
    useNavigateMock: vi.fn(),
  };
});

// Module mocks (hoisted before imports)

vi.mock('../../lib/api/bondPricingService', () => ({
  priceFloatingBond: priceFloatingBondMock,
}));

vi.mock('../../hooks/useAuth', () => ({
  useAuth: useAuthMock,
}));

vi.mock('react-router-dom', () => ({
  useParams: useParamsMock,
  useNavigate: useNavigateMock,
}));

vi.mock('../../lib/firebase', () => ({
  auth: { currentUser: null },
  onAuthStateChanged: vi.fn().mockReturnValue(vi.fn()),
  signInWithGoogle: vi.fn(),
  signInWithEmail: vi.fn(),
  logOut: vi.fn(),
  createOrUpdateUserProfile: vi.fn(),
  db: {},
}));

vi.mock('../../components/Header', () => ({ default: () => null }));

vi.mock('../../components/products/CurveSetSelector', () => ({
  default: ({ onChangeCurve }: { onChangeCurve?: (id: string, curve: unknown) => void }) => {
    capturedCurveCallback.fn = onChangeCurve;
    return null;
  },
}));

vi.mock('../../components/curves/IndexPicker', () => ({ default: () => null }));
vi.mock('../../components/ui/BackLink', () => ({ default: () => null }));

vi.mock('../../lib/storage/indices', () => ({
  indexStore: { getAll: indexStoreGetAllMock },
  storedToRateIndexDef: (saved: Record<string, unknown>) => ({
    id: saved['id'],
    name: saved['family'] || saved['id'],
    index_type: 'Ibor',
    currency: 'EUR',
    tenor: { n: (saved['tenor_number'] as number) || 6, unit: saved['tenor_time_unit'] || 'Months' },
    tenor_number: (saved['tenor_number'] as number) || 6,
    tenor_time_unit: saved['tenor_time_unit'] || 'Months',
    fixing_days: (saved['fixing_days'] as number) || 2,
    calendar: saved['calendar'] || 'TARGET',
    day_counter: saved['day_counter'] || 'Actual360',
    business_day_convention: saved['business_day_convention'] || 'ModifiedFollowing',
  }),
}));

vi.mock('../../lib/storage/bonds', () => ({
  floatingBondStore: { getById: vi.fn().mockResolvedValue(null), save: vi.fn() },
  generateId: vi.fn().mockReturnValue('test-id'),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
}));

vi.mock('../../lib/storage/quoteBook', () => ({
  getQuoteBook: vi.fn().mockReturnValue([]),
  getResolutionMode: vi.fn().mockReturnValue('last'),
  resolveQuoteValue: vi.fn().mockReturnValue(null),
  saveQuoteBook: vi.fn(),
}));

vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: vi.fn().mockReturnValue({ asOfDate: '2026-01-15', setAsOfDate: vi.fn() }),
}));

// Import component after mocks

import FloatingRateBondPricer from './FloatingRateBond';

// Helpers

const mockDiscountCurve = {
  id: 'mock-curve',
  name: 'Mock Curve',
  currency: 'EUR',
  role: 'discount' as const,
  day_counter: 'Actual365Fixed' as const,
  interpolator: 'LogLinear' as const,
  bootstrap_trait: 'Discount' as const,
  reference_date: '2026-01-15',
  points: [],
  createdAt: '',
  updatedAt: '',
};

const mockIborSpec = {
  id: 'EURIBOR_6M',
  type: 'IBOR',
  family: 'Euribor',
  tenor_number: 6,
  tenor_time_unit: 'Months',
  fixing_days: 2,
  calendar: 'TARGET',
  day_counter: 'Actual360',
  business_day_convention: 'ModifiedFollowing',
};

function makeLocalStorageMock() {
  let store: Record<string, string> = {};
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, val: string) => { store[key] = val; },
    removeItem: (key: string) => { delete store[key]; },
    clear: () => { store = {}; },
  };
}

async function renderAndSetup() {
  render(<FloatingRateBondPricer />);
  await act(async () => {
    capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
  });
  await act(async () => {});
}

// Test suite

describe('FloatingRateBond component — direct backend cutover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', makeLocalStorageMock());

    useParamsMock.mockReturnValue({ id: 'new' });
    useNavigateMock.mockReturnValue(vi.fn());
    indexStoreGetAllMock.mockResolvedValue([mockIborSpec]);

    useAuthMock.mockReturnValue({
      user: { uid: 'test-user' },
      login: loginMock,
      loading: false,
      error: null,
      isAuthenticated: true,
      loginWithPassword: vi.fn(),
      logout: vi.fn(),
      clearError: vi.fn(),
    });

    priceFloatingBondMock.mockResolvedValue({
      success: true,
      data: { bonds: [{ npv: 100.0 }] },
      duration_ms: 10,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // C1: success → adapter called; "Pricing Results" panel rendered
  it('C1: Price button calls priceFloatingBond; Pricing Results rendered', async () => {
    await renderAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Bond' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceFloatingBondMock).toHaveBeenCalledOnce());
    expect(priceFloatingBondMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    await waitFor(() => expect(screen.getByText('Pricing Results')).toBeInTheDocument());
  });

  // C2: error response — error UI rendered; no Pricing Results panel
  it('C2: bond_floating_not_found error — error UI rendered; no Pricing Results panel', async () => {
    priceFloatingBondMock.mockResolvedValue({
      success: false,
      error: 'not found',
      errorInfo: {
        category: 'not_found',
        message: 'not found',
        httpStatus: 404,
        raw: { error: 'not found', code: 'bond_floating_not_found' },
      },
      duration_ms: 5,
    });

    await renderAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Bond' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Branch on code, not prose — PricingErrorCard renders title from category
    await waitFor(() => expect(screen.getByText('Not Found')).toBeInTheDocument());
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });

  // C3: unauthenticated — login() fires before pricing; no Pricing Results
  it('C3: Unauthenticated — login called; no Pricing Results rendered', async () => {
    loginMock.mockResolvedValue(null);

    useAuthMock.mockReturnValue({
      user: null,
      login: loginMock,
      loading: false,
      error: null,
      isAuthenticated: false,
      loginWithPassword: vi.fn(),
      logout: vi.fn(),
      clearError: vi.fn(),
    });

    await renderAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Bond' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(loginMock).toHaveBeenCalledOnce());

    expect(priceFloatingBondMock).not.toHaveBeenCalled();
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });
});
