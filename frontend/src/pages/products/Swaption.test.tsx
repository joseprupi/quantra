import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceSwaptionMock,
  loginMock,
  useAuthMock,
  capturedCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceSwaptionMock: vi.fn(),
    loginMock: vi.fn<[], Promise<unknown>>(),
    useAuthMock: vi.fn(),
    capturedCurveCallback,
    indexStoreGetAllMock: vi.fn(),
    useParamsMock: vi.fn(),
    useNavigateMock: vi.fn(),
  };
});

// Module mocks (hoisted before imports)

vi.mock('../../lib/api/swaptionPricingService', () => ({
  priceSwaption: priceSwaptionMock,
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
    tenor: { n: (saved['tenor_number'] as number) || 3, unit: saved['tenor_time_unit'] || 'Months' },
    tenor_number: (saved['tenor_number'] as number) || 3,
    tenor_time_unit: saved['tenor_time_unit'] || 'Months',
    fixing_days: (saved['fixing_days'] as number) || 2,
    calendar: saved['calendar'] || 'TARGET',
    day_counter: saved['day_counter'] || 'Actual360',
    business_day_convention: saved['business_day_convention'] || 'ModifiedFollowing',
  }),
}));

vi.mock('../../lib/storage/swaptions', () => ({
  getSwaptionEntryById: vi.fn().mockResolvedValue(null),
  saveSwaption: vi.fn().mockResolvedValue('test-id'),
  deriveSwaptionName: vi.fn().mockReturnValue('Mock Swaption'),
  getSwaptionEntries: vi.fn().mockResolvedValue([]),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
  getMdBackendSettings: vi.fn().mockReturnValue({ enabled: false, baseUrl: '' }),
}));

vi.mock('../../lib/storage/volSurfaces', () => ({
  getVolSurfaces: vi.fn().mockReturnValue([]),
  getVolSurfaceResolutionMode: vi.fn().mockReturnValue('last'),
  resolveVolSurfaceSnapshot: vi.fn().mockReturnValue(null),
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

vi.mock('../../lib/storage/volSurfacePayload', () => ({
  buildSwaptionVolWirePayload: vi.fn().mockReturnValue({
    id: 'vol-payload',
    payload_type: 'SwaptionVolConstantSpec',
    base: { shape: 'Constant', constant_vol: 0.005 },
  }),
  VolSurfacePayloadError: class extends Error {},
}));

vi.mock('../../lib/storage/swaptionModels', () => ({
  getSwaptionModels: vi.fn().mockReturnValue([]),
  getSwaptionModelById: vi.fn().mockReturnValue(null),
}));

// Import component after mocks

import Swaption from './Swaption';

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

// Matches the Swaption component's default indexRef.id ('EURIBOR_6M')
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

async function renderSwaptionAndSetup() {
  render(<Swaption />);
  // Trigger CurveSetSelector callback to set discountCurve state
  await act(async () => {
    capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
  });
  // Flush any pending promises (indexStore.getAll, resolveIndexDefs)
  await act(async () => {});
}

// Test suite

describe('Swaption component — direct backend cutover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', makeLocalStorageMock());

    useParamsMock.mockReturnValue({ id: 'new' });
    useNavigateMock.mockReturnValue(vi.fn());
    indexStoreGetAllMock.mockResolvedValue([mockIborSpec]);

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

    // Default success return from adapter (the { swaptions: [...] } shape)
    priceSwaptionMock.mockResolvedValue({
      success: true,
      data: { swaptions: [{ npv: 500.0 }] },
      duration_ms: 10,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // C1: success → adapter called; "Pricing Results" panel rendered
  it('C1: Price button calls priceSwaption; Pricing Results rendered', async () => {
    await renderSwaptionAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Swaption' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceSwaptionMock).toHaveBeenCalledOnce());
    expect(priceSwaptionMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    // Adapter returns the { swaptions: [...] } shape; component renders result panel
    await waitFor(() => expect(screen.getByText('Pricing Results')).toBeInTheDocument());
  });

  // C2: error response — error UI rendered; no Pricing Results panel
  it('C2: swaption_not_found error — error UI rendered; no Pricing Results panel', async () => {
    priceSwaptionMock.mockResolvedValue({
      success: false,
      error: 'not found',
      errorInfo: {
        category: 'not_found',
        message: 'not found',
        httpStatus: 404,
        raw: { error: 'not found', code: 'swaption_not_found' },
      },
      duration_ms: 5,
    });

    await renderSwaptionAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Swaption' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Error card title for not_found category (branch on code, not prose)
    await waitFor(() => expect(screen.getByText('Not Found')).toBeInTheDocument());
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });

  // C3: unauthenticated — login() fires before pricing; no Pricing Results
  it('C3: Unauthenticated — login called; no Pricing Results rendered', async () => {
    loginMock.mockResolvedValue(null); // login fails

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

    await renderSwaptionAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price Swaption' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Auth guard fires: login() called before pricing
    await waitFor(() => expect(loginMock).toHaveBeenCalledOnce());

    // priceSwaption never reached after failed login
    expect(priceSwaptionMock).not.toHaveBeenCalled();
    expect(screen.queryByText('Pricing Results')).not.toBeInTheDocument();
  });
});
