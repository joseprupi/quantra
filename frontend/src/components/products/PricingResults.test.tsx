import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import PricingResults from './PricingResults';

afterEach(() => cleanup());

// A minimal successful result so the success card (not the loading/empty/error
// branch) renders.
const okResult = { npv: 1234.56 };

describe('PricingResults — success-card trace doorway', () => {
  it('renders the request id + an enabled "View trace" link when requestId is passed', () => {
    render(
      <PricingResults
        loading={false}
        error={null}
        result={okResult}
        durationMs={12}
        requestId="rid-abc/123"
      />,
    );

    // The id is visible on the card without clicking anything (copyable handle).
    expect(screen.getByText('rid-abc/123')).toBeInTheDocument();

    // The deep link is live and URL-encodes the id into the query string.
    const link = screen.getByRole('link', { name: 'View trace' });
    expect(link).toHaveAttribute('href', '/investigate?request_id=rid-abc%2F123');
  });

  it('hides the id and the View trace link when no requestId is passed', () => {
    render(
      <PricingResults
        loading={false}
        error={null}
        result={okResult}
        durationMs={12}
      />,
    );

    expect(screen.queryByRole('link', { name: 'View trace' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('pricing-request-id')).not.toBeInTheDocument();
  });
});
