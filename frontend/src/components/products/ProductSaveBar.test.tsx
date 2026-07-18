import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import ProductSaveBar from './ProductSaveBar';

afterEach(() => cleanup());

describe('ProductSaveBar — reason for change', () => {
  it('renders no reason input when the page does not wire it (legacy shape)', () => {
    render(<ProductSaveBar name="sw" onNameChange={() => {}} onSave={() => {}} />);
    expect(screen.queryByLabelText('Reason for change')).not.toBeInTheDocument();
  });

  it('renders the optional reason input and forwards edits', async () => {
    const onReasonChange = vi.fn();
    render(
      <ProductSaveBar
        name="sw"
        onNameChange={() => {}}
        onSave={() => {}}
        reason=""
        onReasonChange={onReasonChange}
      />,
    );
    const input = screen.getByLabelText('Reason for change');
    expect(input).toHaveAttribute('placeholder', 'Reason for change (optional)');
    await userEvent.type(input, 'n');
    expect(onReasonChange).toHaveBeenCalledWith('n');
  });

  it('shows the controlled reason value and still saves on click', async () => {
    const onSave = vi.fn();
    render(
      <ProductSaveBar
        name="sw"
        onNameChange={() => {}}
        onSave={onSave}
        reason="notional corrected"
        onReasonChange={() => {}}
      />,
    );
    expect(screen.getByLabelText('Reason for change')).toHaveValue('notional corrected');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalled();
  });
});
