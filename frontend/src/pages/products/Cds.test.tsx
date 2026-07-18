import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceCdsMock,
  loginMock,
  useAuthMock,
  capturedCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceCdsMock: vi.fn(),
    loginMock: vi.fn<[], Promise<unknown>>(),
    useAuthMock: vi.fn(),
    capturedCurveCallback,
    indexStoreGetAllMock: vi.fn(),
    useParamsMock: vi.fn(),
    useNavigateMock: vi.fn(),
  };
});

// Module mocks (hoisted before imports)

vi.mock('../../lib/api/cdsPricingService', () => ({
  priceCds: priceCdsMock,
}));

vi.mock('../../hooks/useAuth', () => ({
  useAuth: useAuthMock,
}));

vi.mock('react-router-dom', () => ({
  useParams: useParamsMock,
  useNavigate: useNavigateMock,
  Link: () => null,
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
  storedToRateIndexDef: (saved: Record<string, unknown>) => ({
    id: saved['id'],
    name: saved['family'] || saved['id'],
    index_type: 'Ibor',
    currency: 'EUR',
    tenor: { n: (saved['tenor_number'] as number) || 3, unit: saved['tenor_time_unit'] || 'Months' },
    tenor_number: (saved['tenor_number'] as number) || 3,
    tenor_time_unit: saved['tenor_time_unit'] || 'Months',
    fixing_days: (saved['fixing_days'] as number) || 2,
    calendar: saved['calendar'] || 'TARGET',
    day_counter: saved['day_counter'] || 'Actual360',
    business_day_convention: saved['business_day_convention'] || 'ModifiedFollowing',
  }),
}));

vi.mock('../../lib/storage/cds', () => ({
  getCdsEntryById: vi.fn().mockResolvedValue(null),
  saveCds: vi.fn().mockResolvedValue('test-id'),
  deriveCdsName: vi.fn().mockReturnValue('Mock CDS'),
}));

vi.mock('../../lib/storage/creditCurves', () => ({
  getCreditCurves: vi.fn().mockReturnValue([]),
  getCreditCurveById: vi.fn().mockReturnValue(null),
  resolveCreditCurve: vi.fn().mockReturnValue({
    recovery_rate: 0.4,
    flat_hazard_rate: 0.02,
  }),
}));

vi.mock('../../lib/api-normalizers', () => ({
  normalizeCdsQuoteForApi: vi.fn().mockImplementation((q: unknown) => q),
  normalizeCurveForApi: vi.fn().mockImplementation((c: unknown) => c),
  normalizeIndexDefForApi: vi.fn().mockImplementation((d: unknown) => d),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
  getMdBackendSettings: vi.fn().mockReturnValue({ enabled: false, baseUrl: '' }),
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

import Cds from './Cds';

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

async function renderCdsAndSetup() {
  render(<Cds />);
  // Inject discount curve via CurveSetSelector callback
  await act(async () => {
    capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
  });
  // Change Curve Source to "Inline override in this trade" to bypass saved-curve guard
  await act(async () => {
    const curveSourceSelect = screen.getByDisplayValue('Use saved credit curve');
    fireEvent.change(curveSourceSelect, { target: { value: 'inline' } });
  });
  // Change Curve Mode to "Flat hazard rate" for a clean inline credit curve
  await act(async () => {
    const curveModeSelect = screen.getByDisplayValue('Bootstrapped from spread quotes');
    fireEvent.change(curveModeSelect, { target: { value: 'flat' } });
  });
  // Flush any pending promises
  await act(async () => {});
}

// Test suite

describe('Cds component — direct backend cutover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', makeLocalStorageMock());

    useParamsMock.mockReturnValue({ id: 'new' });
    useNavigateMock.mockReturnValue(vi.fn());
    indexStoreGetAllMock.mockResolvedValue([]);

    // Default: logged-in user
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

    // Default success return (the { cds_list: [...] } shape)
    priceCdsMock.mockResolvedValue({
      success: true,
      data: { cds_list: [{ npv: -25000.0, fair_spread: 0.0105 }] },
      duration_ms: 15,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // C1: success → adapter called; "Pricing Results" panel rendered
  it('C1: Price button calls priceCds; Pricing Results rendered', async () => {
    await renderCdsAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price CDS' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceCdsMock).toHaveBeenCalledOnce());
    expect(priceCdsMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    // Adapter returns the { cds_list: [...] } shape; component renders result panel
    await waitFor(() => expect(screen.getByText('Pricing Results')).toBeInTheDocument());
  });

  // C2: cds_credit_curve_resolution_failed 422 → error card; no Pricing Results
  it('C2: cds_credit_curve_resolution_failed 422 — error card; no Pricing Results', async () => {
    priceCdsMock.mockResolvedValue({
      success: false,
      error: 'credit curve resolution failed',
      errorInfo: {
        category: 'validation',
        message: 'credit curve resolution failed',
        httpStatus: 422,
        raw: { error: 'credit curve resolution failed', code: 'cds_credit_curve_resolution_failed' },
      },
      duration_ms: 5,
    });

    await renderCdsAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price CDS' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Error card title for validation category (PricingErrorCard: 'Invalid Request')
    await waitFor(() => expect(screen.getByText('Invalid Request')).toBeInTheDocument());
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });

  // C3: unauthenticated → login() fires before pricing; no Pricing Results
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

    render(<Cds />);
    // Still need discountCurve so the button isn't disabled
    await act(async () => {
      capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
    });
    // Change to inline mode
    await act(async () => {
      const curveSourceSelect = screen.getByDisplayValue('Use saved credit curve');
      fireEvent.change(curveSourceSelect, { target: { value: 'inline' } });
    });
    await act(async () => {});

    const priceButton = await screen.findByRole('button', { name: 'Price CDS' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Auth guard fires: login() called before pricing
    await waitFor(() => expect(loginMock).toHaveBeenCalledOnce());

    // priceCds never reached after failed login
    expect(priceCdsMock).not.toHaveBeenCalled();
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });
});
