import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks: the dev-bypass signal + the real-data probe.
const { isDevAuthBypassMock, fetchLatestRealDateMock } = vi.hoisted(() => ({
  isDevAuthBypassMock: vi.fn(),
  fetchLatestRealDateMock: vi.fn(),
}));
vi.mock('../lib/devAuth', () => ({ isDevAuthBypass: isDevAuthBypassMock }));
vi.mock('../lib/latestRealDate', () => ({ fetchLatestRealDate: fetchLatestRealDateMock }));

import SelfHostedBanner, {
  SELF_HOSTED_BANNER_NO_DATA,
  SELF_HOSTED_BANNER_LIVE,
} from './SelfHostedBanner';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SelfHostedBanner (self-hosted honesty banner — provenance-aware)', () => {
  it('shows the honest no-data-yet copy when no real data is present', async () => {
    isDevAuthBypassMock.mockReturnValue(true);
    fetchLatestRealDateMock.mockResolvedValue(null);
    render(<SelfHostedBanner />);
    const banner = screen.getByTestId('self-hosted-banner');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(SELF_HOSTED_BANNER_NO_DATA);
    // The empty-state copy must NOT claim synthetic data or real prices exist.
    expect(SELF_HOSTED_BANNER_NO_DATA).toBe(
      'Self-hosted · public market data — none ingested yet',
    );
    expect(SELF_HOSTED_BANNER_NO_DATA).not.toMatch(/synthetic data/i);
  });

  it('upgrades to the real public-data copy once data is ingested (no "synthetic")', async () => {
    isDevAuthBypassMock.mockReturnValue(true);
    fetchLatestRealDateMock.mockResolvedValue('2026-07-14');
    render(<SelfHostedBanner />);
    await waitFor(() =>
      expect(screen.getByTestId('self-hosted-banner')).toHaveTextContent(
        SELF_HOSTED_BANNER_LIVE,
      ),
    );
    expect(SELF_HOSTED_BANNER_LIVE).toBe(
      'Live public market data · Bank of England, US Treasury, FRED, ECB · updated daily',
    );
    // No longer claims "other markets synthetic".
    expect(SELF_HOSTED_BANNER_LIVE).not.toMatch(/synthetic/i);
  });

  it('renders nothing when the dev-auth bypass is off (hosted build)', () => {
    isDevAuthBypassMock.mockReturnValue(false);
    fetchLatestRealDateMock.mockResolvedValue(null);
    const { container } = render(<SelfHostedBanner />);
    expect(screen.queryByTestId('self-hosted-banner')).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });
});
