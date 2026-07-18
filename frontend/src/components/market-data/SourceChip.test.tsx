import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SourceChip from './SourceChip';

afterEach(cleanup);

describe('SourceChip — provider attribution', () => {
  it('renders a real public feed (FRED) as real public data, not Imported', () => {
    render(<SourceChip source="FRED" />);
    const chip = screen.getByText('FRED');
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveAttribute(
      'title',
      'Real public market data · FRED · updated daily',
    );
  });

  it('renders ECB as a real public feed', () => {
    render(<SourceChip source="ecb" />);
    const chip = screen.getByText('ECB');
    expect(chip.getAttribute('title')).toContain('Real public market data');
  });

  it('renders a BoE feed with the institution label', () => {
    render(<SourceChip source="boe_ois" />);
    expect(screen.getByText('Bank of England')).toBeInTheDocument();
  });

  it('renders user-supplied data as Imported with a distinct label', () => {
    render(<SourceChip source="manual" />);
    const chip = screen.getByText('Imported');
    expect(chip).toBeInTheDocument();
    expect(chip.getAttribute('title')).toContain('Imported by you');
  });

  it('renders legacy synthetic rows distinctly, without the official-feed styling', () => {
    render(<SourceChip source="synthetic" />);
    const chip = screen.getByText('Synthetic (legacy)');
    expect(chip).toBeInTheDocument();
    expect(chip.getAttribute('title')).toContain('not real market prices');
    expect(chip.getAttribute('title')).not.toContain('Real public market data');
    expect(chip.className).not.toContain('bg-[#e6f6ec]');
  });

  it('renders a dash for a missing source', () => {
    render(<SourceChip source={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
