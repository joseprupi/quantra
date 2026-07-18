// Proves the pricing-trace doorway follows the WIDENED isDevTooling() gate:
// it must render in a production build running under the self-hosted / demo
// dev-auth bypass (config.js `devAuthBypass: true`), even though
// `import.meta.env.DEV` is false there. Before the widening the link only ever
// showed under a `vite dev` server, so the self-hosted bundle and the public
// demo never surfaced the "View trace" affordance despite recording traces.
//
// We mock only the runtime-config bypass signal and stub DEV, so the REAL
// isDevTooling() gate is exercised end-to-end through the component.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const { isRuntimeDevAuthBypassMock } = vi.hoisted(() => ({
  isRuntimeDevAuthBypassMock: vi.fn<[], boolean>(),
}));

vi.mock('../../lib/runtimeConfig', () => ({
  isRuntimeDevAuthBypass: isRuntimeDevAuthBypassMock,
}));

import PricingTraceLink from './PricingTraceLink';

describe('PricingTraceLink — visible under self-hosted dev-bypass (prod build)', () => {
  beforeEach(() => {
    isRuntimeDevAuthBypassMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it('renders the "View trace" link in a production build when dev-bypass is ON', () => {
    // A shipped self-hosted / demo bundle: production build, dev-bypass injected.
    vi.stubEnv('DEV', false);
    isRuntimeDevAuthBypassMock.mockReturnValue(true);

    render(<PricingTraceLink requestId="rid-demo/1" />);

    const link = screen.getByRole('link', { name: 'View trace' });
    expect(link).toHaveAttribute('href', '/investigate?request_id=rid-demo%2F1');
    expect(screen.getByText('rid-demo/1')).toBeInTheDocument();
  });

  it('renders NOTHING in a hosted production build (DEV false, no dev-bypass)', () => {
    vi.stubEnv('DEV', false);
    isRuntimeDevAuthBypassMock.mockReturnValue(false);

    const { container } = render(<PricingTraceLink requestId="rid-demo/2" />);

    expect(screen.queryByRole('link', { name: 'View trace' })).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });
});
