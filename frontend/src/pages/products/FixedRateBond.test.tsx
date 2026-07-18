import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceFixedBondMock,
  loginMock,
  useAuthMock,
  capturedCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceFixedBondMock: vi.fn(),
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
  priceFixedBond: priceFixedBondMock,
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

vi.mock('../../components/ui/BackLink', () => ({ default: () => null }));

vi.mock('../../lib/storage/indices', () => ({
  indexStore: { getAll: indexStoreGetAllMock },
  storedToRateIndexDef: vi.fn(),
}));

vi.mock('../../lib/storage/bonds', () => ({
  fixedBondStore: { getById: vi.fn().mockResolvedValue(null), save: vi.fn() },
  generateId: vi.fn().mockReturnValue('test-id'),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
}));

vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: vi.fn().mockReturnValue({ asOfDate: '2026-01-15', setAsOfDate: vi.fn() }),
}));

// Import component after mocks

import FixedRateBondPricer from './FixedRateBond';

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
  render(<FixedRateBondPricer />);
  await act(async () => {
    capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
  });
  await act(async () => {});
}

// Test suite

describe('FixedRateBond component — direct backend cutover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', makeLocalStorageMock());

    useParamsMock.mockReturnValue({ id: 'new' });
    useNavigateMock.mockReturnValue(vi.fn());
    indexStoreGetAllMock.mockResolvedValue([]);

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

    priceFixedBondMock.mockResolvedValue({
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
  it('C1: Price button calls priceFixedBond; Pricing Results rendered', async () => {
    await renderAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Bond' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceFixedBondMock).toHaveBeenCalledOnce());
    expect(priceFixedBondMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    await waitFor(() => expect(screen.getByText('Pricing Results')).toBeInTheDocument());
  });

  // C2: error response — error UI rendered; no Pricing Results panel
  it('C2: bond_fixed_not_found error — error UI rendered; no Pricing Results panel', async () => {
    priceFixedBondMock.mockResolvedValue({
      success: false,
      error: 'not found',
      errorInfo: {
        category: 'not_found',
        message: 'not found',
        httpStatus: 404,
        raw: { error: 'not found', code: 'bond_fixed_not_found' },
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

    expect(priceFixedBondMock).not.toHaveBeenCalled();
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });
});
