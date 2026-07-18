// IR Vol Sampler — trace doorway.
//
// The Sampler must offer the same "View trace" deep link into /investigate
// that the product pricing pages carry, so a user can inspect the pipeline
// logs for a sample exactly as they can for a swap price. These tests pin the
// two contract points:
//   (1) after a sample SUCCEEDS, the Sampler renders a "View trace" link whose
//       href URL-encodes the request id it sent, and
//   (2) after a sample FAILS, the same doorway still renders (that's precisely
//       when the logs matter) — the request id is captured on the error path.
//
// The link component (PricingTraceLink) is dev-gated; import.meta.env.DEV is
// true under vitest, mirroring src/components/products/PricingResults.test.tsx.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

const { orchestratorPostMock } = vi.hoisted(() => ({
  orchestratorPostMock: vi.fn(),
}));

// Heavy / environment-bound deps stubbed so the page mounts headless.
vi.mock('../../components/Header', () => ({ default: () => null }));
vi.mock('react-plotly.js', () => ({ default: () => null }));
vi.mock('../../hooks/useAsOfDate', () => ({
  useAsOfDate: () => ({ asOfDate: '2025-01-15', setAsOfDate: vi.fn() }),
}));

// The single stored surface the workbench lists. An Optionlet surface keeps
// buildRequest on its simplest path (no swaption wire helper, no curve-horizon
// guard) so the test exercises the trace wiring, not the payload assembly.
const OPTIONLET_SURFACE = {
  id: 'opt_demo',
  payload_type: 'OptionletVolSpec',
  base: { shape: 'Constant', constant_vol: 0.2, volatility_type: 'Normal' },
  strikes: [],
  createdAt: '2025-01-01T00:00:00.000Z',
  updatedAt: '2025-01-01T00:00:00.000Z',
};

vi.mock('../../lib/storage/volSurfaces', () => ({
  getVolSurfaces: () => [OPTIONLET_SURFACE],
  saveVolSurface: vi.fn(() => Promise.resolve()),
  deleteVolSurface: vi.fn(() => Promise.resolve()),
  exportVolSurfaces: vi.fn(() => '[]'),
  importVolSurfaces: vi.fn(() => []),
}));

// One yield curve set with points → buildRequest's "at least one yield curve"
// guard passes.
vi.mock('../../lib/storage/curveSets', () => ({
  getSavedCurveSets: () => [{ id: 'cs1', name: 'USD', refs: [] }],
  resolveCurveSetCurves: () => [
    {
      id: 'usd_ois',
      role: 'discount',
      day_counter: 'Actual365Fixed',
      interpolator: 'Linear',
      bootstrap_trait: 'Discount',
      reference_date: '2025-01-15',
      points: [{ point: { rate: 0.02, tenor: { n: 1, unit: 'Years' } } }],
    },
  ],
}));

vi.mock('../../lib/storage/indices', () => ({
  indexStore: { getAll: () => Promise.resolve([]) },
  storedToRateIndexDef: () => null,
}));

vi.mock('../../lib/storage/quoteBook', () => ({
  getQuoteBook: () => [],
  getLegacyFlatQuotes: () => [],
  getResolutionMode: () => 'previous',
  resolveQuoteValue: () => null,
}));

// Passthrough normalizers so the mocked curve/index shapes reach the request
// untouched.
vi.mock('../../lib/api-normalizers', () => ({
  normalizeCurveForApi: (c: unknown) => c,
  normalizeIndexDefForApi: (i: unknown) => i,
}));

vi.mock('../../lib/api/orchestrator', () => ({
  orchestratorPost: orchestratorPostMock,
}));

import VolWorkbench from './VolWorkbench';

function renderPage() {
  return render(
    <MemoryRouter>
      <VolWorkbench />
    </MemoryRouter>,
  );
}

// Open the single listed surface → the Sampler/Editor view (showSurfaceList
// false) where the trace doorway lives; the auto-sample effect then fires a
// runSample against the mocked orchestrator.
async function openSurfaceAndSample(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText('opt_demo'));
}

beforeEach(() => {
  orchestratorPostMock.mockReset();
});
afterEach(cleanup);

describe('VolWorkbench — Sampler trace doorway', () => {
  it('renders a "View trace" link to /investigate with the sample request id on SUCCESS', async () => {
    const user = userEvent.setup();
    orchestratorPostMock.mockResolvedValue({
      ok: true,
      data: { queries: [] },
      duration_ms: 7,
      requestId: 'rid-vol/ok-1',
    });

    renderPage();
    await openSurfaceAndSample(user);

    await waitFor(
      () => {
        const links = screen.getAllByRole('link', { name: 'View trace' });
        expect(links.length).toBeGreaterThan(0);
        expect(links[0]).toHaveAttribute(
          'href',
          '/investigate?request_id=rid-vol%2Fok-1',
        );
      },
      { timeout: 4000 },
    );
  });

  it('still renders the "View trace" doorway with the request id on a FAILED sample', async () => {
    const user = userEvent.setup();
    orchestratorPostMock.mockResolvedValue({
      ok: false,
      envelope: { error: 'Sampling failed on the wire', code: 'engine_error', request_id: 'rid-vol/err-1' },
      duration_ms: 7,
      requestId: 'rid-vol/err-1',
    });

    renderPage();
    await openSurfaceAndSample(user);

    // The error banner surfaces...
    await screen.findByText(/Sampling failed on the wire/i, undefined, { timeout: 4000 });
    // ...and the trace doorway points at this request's logs.
    await waitFor(() => {
      const links = screen.getAllByRole('link', { name: 'View trace' });
      expect(links.length).toBeGreaterThan(0);
      links.forEach(link =>
        expect(link).toHaveAttribute(
          'href',
          '/investigate?request_id=rid-vol%2Ferr-1',
        ),
      );
    });
  });
});
