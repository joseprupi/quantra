import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Hoisted mock: control the GET /v1/version result per test.
const { getVersionMock } = vi.hoisted(() => ({ getVersionMock: vi.fn() }));
vi.mock('../lib/api/orchestrator', () => ({ getVersion: getVersionMock }));

import AboutPanel from './AboutPanel';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AboutPanel (About Quantra version info)', () => {
  it('renders the three labelled rows and populates backend/engine from /v1/version', async () => {
    getVersionMock.mockResolvedValue({
      ok: true,
      data: {
        orchestrator: { version: '0.1.0', build_sha: 'abc1234' },
        engine: { version: '0.2.1', source: 'hardcoded' },
      },
      duration_ms: 1,
    });

    render(<AboutPanel onClose={() => {}} />);

    // Title + the three rows are always present.
    expect(screen.getByRole('dialog', { name: 'About Quantra' })).toBeInTheDocument();
    expect(screen.getByText('Web client')).toBeInTheDocument();
    expect(screen.getByText('Backend')).toBeInTheDocument();
    expect(screen.getByText('Pricing engine')).toBeInTheDocument();

    // Web-client version comes from the build-time constant (__APP_VERSION__),
    // which Vite `define` injects as the package.json version ("0.4.1").
    expect(screen.getByText('0.4.1')).toBeInTheDocument();

    // Backend + engine populate from the fetched contract.
    await waitFor(() => {
      expect(screen.getByText('0.1.0')).toBeInTheDocument();
    });
    expect(screen.getByText('build abc1234')).toBeInTheDocument();
    expect(screen.getByText('0.2.1')).toBeInTheDocument();

    // The implementation-detail `source` field is never user-facing text.
    expect(screen.queryByText(/hardcoded/i)).not.toBeInTheDocument();
  });

  it('portals the overlay to document.body (escapes the blurred header containing block)', () => {
    getVersionMock.mockReturnValue(new Promise(() => {}));

    render(
      <header>
        <AboutPanel onClose={() => {}} />
      </header>,
    );

    const dialog = screen.getByRole('dialog', { name: 'About Quantra' });
    // The overlay must be a DIRECT child of <body> (portaled), not nested under
    // the <header> where a backdrop-filter would trap position:fixed.
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog.closest('header')).toBeNull();
    // And it carries the above-header z-index.
    expect(dialog).toHaveClass('z-[60]');
  });

  it('closes on overlay click and on the × button', () => {
    getVersionMock.mockReturnValue(new Promise(() => {}));

    const onClose = vi.fn();
    render(<AboutPanel onClose={onClose} />);

    // Click the overlay backdrop → closes.
    fireEvent.click(screen.getByRole('dialog', { name: 'About Quantra' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    // Click the × button → closes.
    fireEvent.click(screen.getByRole('button', { name: 'Close about panel' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('shows a loading indicator for backend/engine before the fetch resolves', () => {
    // Never-resolving promise → stuck in the loading state.
    getVersionMock.mockReturnValue(new Promise(() => {}));

    render(<AboutPanel onClose={() => {}} />);

    // Web-client row still renders immediately (no network needed).
    expect(screen.getByText('Web client')).toBeInTheDocument();
    expect(screen.getByText('0.4.1')).toBeInTheDocument();
    // Backend + engine show the subtle loading indicator.
    expect(screen.getAllByText('Loading…').length).toBeGreaterThanOrEqual(2);
  });

  it('still renders the panel with a graceful "Unavailable" when the fetch fails', async () => {
    getVersionMock.mockResolvedValue({
      ok: false,
      envelope: { error: 'boom', code: 'network_error' },
      httpStatus: 0,
      duration_ms: 1,
    });

    render(<AboutPanel onClose={() => {}} />);

    // Panel + web-client row survive the failure.
    expect(screen.getByRole('dialog', { name: 'About Quantra' })).toBeInTheDocument();
    expect(screen.getByText('0.4.1')).toBeInTheDocument();

    // Backend + engine degrade to "Unavailable", never blank/crash.
    await waitFor(() => {
      expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(2);
    });
  });
});
