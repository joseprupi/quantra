import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceEquityOptionMock,
  loginMock,
  useAuthMock,
  capturedDiscountCurveCallback,
  capturedDividendCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedDiscountCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  const capturedDividendCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceEquityOptionMock: vi.fn(),
    loginMock: vi.fn<[], Promise<unknown>>(),
    useAuthMock: vi.fn(),
    capturedDiscountCurveCallback,
    capturedDividendCurveCallback,
    indexStoreGetAllMock: vi.fn(),
    useParamsMock: vi.fn(),
    useNavigateMock: vi.fn(),
  };
});

// Module mocks (hoisted before imports)

vi.mock('../../lib/api/equityOptionPricingService', () => ({
  priceEquityOption: priceEquityOptionMock,
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
  default: ({
    onChangeCurve,
    curveRole,
  }: {
    onChangeCurve?: (id: string, curve: unknown) => void;
    curveRole?: string;
  }) => {
    if (curveRole === 'discount') capturedDiscountCurveCallback.fn = onChangeCurve;
    if (curveRole === 'forward') capturedDividendCurveCallback.fn = onChangeCurve;
    return null;
  },
}));

vi.mock('../../components/ui/BackLink', () => ({ default: () => null }));

vi.mock('../../lib/storage/indices', () => ({
  indexStore: { getAll: indexStoreGetAllMock },
  storedToRateIndexDef: vi.fn().mockReturnValue(null),
  collectIndexRefIds: vi.fn().mockReturnValue([]),
}));

vi.mock('../../lib/storage/equityOptions', () => ({
  getEquityOptionEntryById: vi.fn().mockResolvedValue(null),
  saveEquityOption: vi.fn().mockResolvedValue('test-id'),
  deriveEquityOptionName: vi.fn().mockReturnValue('Mock Option'),
  EquityOptionRequest: {},
}));

vi.mock('../../lib/storage/volSurfaces', () => ({
  getVolSurfaces: vi.fn().mockReturnValue([
    { id: 'vol-1', payload_type: 'BlackVolSpec', base: { shape: 'Constant', constant_vol: 0.2 } },
  ]),
}));

vi.mock('../../lib/api-normalizers', () => ({
  normalizeCurveForApi: vi.fn().mockImplementation((c: unknown) => c),
  normalizeIndexDefForApi: vi.fn().mockImplementation((d: unknown) => d),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
}));

vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: vi.fn().mockReturnValue({ asOfDate: '2026-05-23', setAsOfDate: vi.fn() }),
}));

// Import component after mocks

import EquityOptions from './EquityOptions';

// Helpers

const mockDiscountCurve = {
  id: 'mock-discount-curve',
  name: 'Mock Discount Curve',
  currency: 'USD',
  role: 'discount' as const,
  day_counter: 'Actual365Fixed' as const,
  interpolator: 'LogLinear' as const,
  bootstrap_trait: 'Discount' as const,
  reference_date: '2026-05-23',
  points: [],
  createdAt: '',
  updatedAt: '',
};

const mockDividendCurve = {
  id: 'mock-dividend-curve',
  name: 'Mock Dividend Curve',
  currency: 'USD',
  role: 'forward' as const,
  day_counter: 'Actual365Fixed' as const,
  interpolator: 'LogLinear' as const,
  bootstrap_trait: 'Discount' as const,
  reference_date: '2026-05-23',
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

async function renderEquityOptionsAndSetup() {
  render(<EquityOptions />);
  // Flush vol surface auto-select useEffect
  await act(async () => {});
  // Inject discount curve via CurveSetSelector callback (curveRole='discount')
  await act(async () => {
    capturedDiscountCurveCallback.fn?.('mock-discount-curve', mockDiscountCurve);
  });
  // Inject dividend curve via CurveSetSelector callback (curveRole='forward')
  await act(async () => {
    capturedDividendCurveCallback.fn?.('mock-dividend-curve', mockDividendCurve);
  });
  await act(async () => {});
}

// Test suite

describe('EquityOptions component — direct backend cutover', () => {
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

    // Default success return (the { options: [...] } shape)
    priceEquityOptionMock.mockResolvedValue({
      success: true,
      data: { options: [{ npv: 12.50, delta: 0.55, implied_vol: 0.22 }] },
      duration_ms: 22,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // C1: success → adapter called; "Results" heading rendered
  it('C1: Price button calls priceEquityOption; Results rendered', async () => {
    await renderEquityOptionsAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceEquityOptionMock).toHaveBeenCalledOnce());
    expect(priceEquityOptionMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    // Adapter returns the { options: [...] } shape; component renders "Results" heading
    await waitFor(() => expect(screen.getByText('Results')).toBeInTheDocument());
  });

  // C2: equity_option_spot_resolution_failed 422 → error div visible; "Results" absent
  it('C2: equity_option_spot_resolution_failed 422 — error div visible; Results absent', async () => {
    priceEquityOptionMock.mockResolvedValue({
      success: false,
      error: 'Spot quote not resolvable.',
      duration_ms: 5,
    });

    await renderEquityOptionsAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(screen.getByText('Spot quote not resolvable.')).toBeInTheDocument());
    expect(screen.queryByText('Results')).not.toBeInTheDocument();
  });

  // C3: unauthenticated → login called; priceEquityOption not called; "Results" absent
  it('C3: Unauthenticated — login called; priceEquityOption not called; Results absent', async () => {
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

    render(<EquityOptions />);
    await act(async () => {});
    await act(async () => {
      capturedDiscountCurveCallback.fn?.('mock-discount-curve', mockDiscountCurve);
    });
    await act(async () => {
      capturedDividendCurveCallback.fn?.('mock-dividend-curve', mockDividendCurve);
    });
    await act(async () => {});

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(loginMock).toHaveBeenCalledOnce());

    expect(priceEquityOptionMock).not.toHaveBeenCalled();
    expect(screen.queryByText('Results')).not.toBeInTheDocument();
  });
});
