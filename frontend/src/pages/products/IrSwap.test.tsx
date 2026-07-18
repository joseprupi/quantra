import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const {
  priceIrSwapMock,
  loginMock,
  useAuthMock,
  capturedCurveCallback,
  indexStoreGetAllMock,
  useParamsMock,
  useNavigateMock,
} = vi.hoisted(() => {
  const capturedCurveCallback: { fn?: (id: string, curve: unknown) => void } = {};
  return {
    priceIrSwapMock: vi.fn(),
    loginMock: vi.fn<[], Promise<unknown>>(),
    useAuthMock: vi.fn(),
    capturedCurveCallback,
    indexStoreGetAllMock: vi.fn(),
    useParamsMock: vi.fn(),
    useNavigateMock: vi.fn(),
  };
});

// Module mocks (hoisted before imports)

vi.mock('../../lib/api/irSwapPricingService', () => ({
  priceIrSwap: priceIrSwapMock,
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

vi.mock('../../lib/storage/swaps', () => ({
  getIrSwapEntryById: vi.fn().mockResolvedValue(null),
  saveIrSwap: vi.fn().mockResolvedValue('test-id'),
  deriveIrSwapName: vi.fn().mockReturnValue('Mock Swap'),
  getIrSwaps: vi.fn().mockResolvedValue([]),
  replaceIrSwaps: vi.fn(),
  clearIrSwaps: vi.fn(),
}));

vi.mock('../../lib/marketDataBackend', () => ({
  buildPricingQuoteSnapshotWithBackend: vi.fn().mockResolvedValue({ quotes: [] }),
  getMdBackendSettings: vi.fn().mockReturnValue({ enabled: false, baseUrl: '' }),
}));

vi.mock('../../lib/storage/volSurfaces', () => ({
  getVolSurfaces: vi.fn().mockReturnValue([]),
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

import IrSwap from './IrSwap';

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
  id: 'ibor-1',
  type: 'IBOR',
  family: 'Euribor',
  tenor_number: 3,
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

async function renderIrSwapAndSetup() {
  render(<IrSwap />);
  // Trigger the mock CurveSetSelector callback to set discountCurve state
  await act(async () => {
    capturedCurveCallback.fn?.('mock-curve', mockDiscountCurve);
  });
  // Flush indexStore.getAll() promise so indexRef.id is auto-set
  await act(async () => {});
}

// Test suite: C1–C4 component tests

describe('IrSwap component — C1–C4 (adapter cutover)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', makeLocalStorageMock());

    // Re-apply return values after clearAllMocks
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

    // Default success return from adapter
    priceIrSwapMock.mockResolvedValue({
      success: true,
      data: { swaps: [{ npv: 1234.56 }] },
      duration_ms: 10,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // C1: prices via orchestrator → Swap Results rendered
  it('C1: Price button calls priceIrSwap; Swap Results rendered', async () => {
    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(priceIrSwapMock).toHaveBeenCalledOnce());
    expect(priceIrSwapMock).toHaveBeenCalledWith(
      expect.any(Object),
      expect.any(String),
    );

    // Adapter returns the { swaps: [...] } shape; component renders result panel
    await waitFor(() => expect(screen.getByText('Swap Results')).toBeInTheDocument());
  });

  // C2: error response — PricingErrorCard rendered; NPV panel absent
  it('C2: not_found error — PricingErrorCard rendered; no NPV panel', async () => {
    priceIrSwapMock.mockResolvedValue({
      success: false,
      error: 'not found',
      errorInfo: {
        category: 'not_found',
        message: 'not found',
        httpStatus: 404,
        raw: { error: 'not found', code: 'swap_ir_not_found' },
      },
      duration_ms: 5,
    });

    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Error card title for not_found category
    await waitFor(() => expect(screen.getByText('Not Found')).toBeInTheDocument());
    // NPV result panel not shown (branch on code, not prose)
    expect(screen.queryByText('Swap Results')).not.toBeInTheDocument();
  });

  // C5: a SUCCESSFUL price threads the call's request id onto the
  // Swap Results card — the id is rendered (copyable) and the dev-gated
  // "View trace" link points at /investigate?request_id=<that id>. This is the
  // doorway that lets a price that "works" still be investigated.
  it('C5: success surfaces the request id + an enabled View trace link', async () => {
    priceIrSwapMock.mockResolvedValue({
      success: true,
      data: { swaps: [{ npv: 1234.56 }] },
      duration_ms: 10,
      requestId: 'rid-swap-success-1',
    });

    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    await waitFor(() => expect(screen.getByText('Swap Results')).toBeInTheDocument());

    // The id is visible on the card without clicking anything.
    expect(screen.getByText('rid-swap-success-1')).toBeInTheDocument();

    // The dev-gated deep link is live and URL-encodes the id into the query.
    const link = screen.getByRole('link', { name: 'View trace' });
    expect(link).toHaveAttribute('href', '/investigate?request_id=rid-swap-success-1');
  });

  // C6: per-period cashflows render in the ORIGINAL tabbed "Flows" card for a
  // vanilla swap — fed by the new top-level fixed_leg_flows/floating_leg_flows.
  // The default Fixed tab shows fixed-leg rows; clicking Floating reveals the
  // floating-leg rows (with a fixing date). The CSV download is present, and
  // there is no separate standalone "Cashflows" section (flows shown once).
  it('C6: renders cashflows in the tabbed Flows card (fixed + floating)', async () => {
    priceIrSwapMock.mockResolvedValue({
      success: true,
      data: {
        swaps: [{
          npv: 1234.56,
          fixed_leg_flows: [{
            payment_date: '2027-02-13',
            accrual_start_date: '2026-02-11',
            accrual_end_date: '2027-02-11',
            amount: 250000,
            discount: 0.985123,
            present_value: 246250,
            rate: 0.025,
          }],
          floating_leg_flows: [{
            payment_date: '2026-08-13',
            accrual_start_date: '2026-02-11',
            accrual_end_date: '2026-08-11',
            fixing_date: '2026-02-09',
            index_fixing: 0.0231,
            amount: 115000,
            discount: 0.992001,
            present_value: 114080,
            rate: 0.0231,
          }],
        }],
      },
      duration_ms: 10,
    });

    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());
    fireEvent.click(priceButton);

    // The original tabbed card heading is "Flows" (not a separate "Cashflows").
    await waitFor(() => expect(screen.getByText('Flows')).toBeInTheDocument());
    expect(screen.queryByText('Cashflows')).not.toBeInTheDocument();

    // CSV download button is present for vanilla.
    expect(screen.getByRole('button', { name: 'Download CSV' })).toBeInTheDocument();

    // Default Fixed tab: fixed-leg row — payment date + amount + PV.
    expect(screen.getByText('2027-02-13')).toBeInTheDocument();
    expect(screen.getByText('250,000.00')).toBeInTheDocument();
    expect(screen.getByText('246,250.00')).toBeInTheDocument();

    // Switch to the Floating tab: floating-leg row — payment date, fixing date,
    // and PV. (Fixed leg carries no fixing date.)
    fireEvent.click(screen.getByRole('button', { name: 'Floating' }));
    await waitFor(() => expect(screen.getByText('2026-08-13')).toBeInTheDocument());
    expect(screen.getByText('2026-02-09')).toBeInTheDocument();
    expect(screen.getByText('114,080.00')).toBeInTheDocument();
  });

  // C7: empty flows render the empty state in the tabbed card without crashing,
  // and there is no duplicate standalone "Cashflows" section.
  it('C7: empty flows render the empty state without crashing', async () => {
    priceIrSwapMock.mockResolvedValue({
      success: true,
      data: { swaps: [{ npv: 1234.56, fixed_leg_flows: [], floating_leg_flows: [] }] },
      duration_ms: 10,
    });

    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());
    fireEvent.click(priceButton);

    await waitFor(() => expect(screen.getByText('Flows')).toBeInTheDocument());

    // Empty state for the active tab; no crash, no separate Cashflows section.
    expect(screen.getByText('No cashflows returned')).toBeInTheDocument();
    expect(screen.queryByText('Cashflows')).not.toBeInTheDocument();
    // Swap Results card still renders alongside the empty Flows card.
    expect(screen.getByText('Swap Results')).toBeInTheDocument();
  });

  // C4: Unauthenticated — login() fires before pricing; no NPV rendered
  it('C4: Unauthenticated — login called; no NPV panel rendered', async () => {
    loginMock.mockResolvedValue(null); // login fails → returns null

    useAuthMock.mockReturnValue({
      user: null, // not logged in
      login: loginMock,
      loading: false,
      error: null,
      isAuthenticated: false,
      loginWithPassword: vi.fn(),
      logout: vi.fn(),
      clearError: vi.fn(),
    });

    await renderIrSwapAndSetup();

    const priceButton = await screen.findByRole('button', { name: 'Price' });
    await waitFor(() => expect(priceButton).not.toBeDisabled());

    fireEvent.click(priceButton);

    // Auth guard fires: login() is called before pricing
    await waitFor(() => expect(loginMock).toHaveBeenCalledOnce());

    // priceIrSwap is never reached (early return after failed login)
    expect(priceIrSwapMock).not.toHaveBeenCalled();

    // No NPV result panel
    expect(screen.queryByText('Swap Results')).not.toBeInTheDocument();
  });
});
