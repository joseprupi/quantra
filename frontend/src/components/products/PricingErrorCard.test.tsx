import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import PricingErrorCard from './PricingErrorCard';
import type { PricingErrorInfo } from '../../lib/quantra-types';

afterEach(() => cleanup());

const baseInfo: PricingErrorInfo = {
  category: 'server',
  message: 'engine exploded',
  codeName: 'engine_unavailable',
  httpStatus: 502,
  raw: { error: 'engine exploded', code: 'engine_unavailable' },
};

describe('PricingErrorCard — View trace deep link', () => {
  it('builds the correct /investigate?request_id link from the card request id', () => {
    render(
      <PricingErrorCard
        error="engine exploded"
        info={{ ...baseInfo, requestId: 'abc 123/def' }}
      />,
    );

    const link = screen.getByRole('link', { name: 'View trace' });
    // request_id is URL-encoded into the query string.
    expect(link).toHaveAttribute('href', '/investigate?request_id=abc%20123%2Fdef');
  });

  it('does not render a View trace link when there is no request id', () => {
    render(<PricingErrorCard error="engine exploded" info={baseInfo} />);
    expect(screen.queryByRole('link', { name: 'View trace' })).not.toBeInTheDocument();
  });
});
