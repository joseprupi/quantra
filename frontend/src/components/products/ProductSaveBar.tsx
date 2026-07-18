import { formStyles } from '../ui/formStyles';

interface ProductSaveBarProps {
  name: string;
  onNameChange: (value: string) => void;
  onSave: () => void;
  placeholder?: string;
  saving?: boolean;
  saveLabel?: string;
  /**
   * Optional audit reason ("Reason for change"). When BOTH `reason` and
   * `onReasonChange` are wired the bar renders an unobtrusive extra input;
   * a non-empty value rides the save as the `X-Change-Reason` header and the
   * page clears it after a successful save. Pages that never pass these props
   * render exactly as before.
   */
  reason?: string;
  onReasonChange?: (value: string) => void;
}

// Name input + Save button rendered in a product detail page's PageHeader
// actions slot. Single source of truth for the "rename + save" UX across
// IR Swap, CDS, Inflation Swap, Swaption, Equity Option and Bond pages.
export default function ProductSaveBar({
  name,
  onNameChange,
  onSave,
  placeholder = 'Name this product…',
  saving = false,
  saveLabel = 'Save',
  reason,
  onReasonChange,
}: ProductSaveBarProps) {
  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        value={name}
        onChange={e => onNameChange(e.target.value)}
        placeholder={placeholder}
        className={`${formStyles.input} min-w-[220px]`}
        aria-label="Product name"
      />
      {onReasonChange && (
        <input
          type="text"
          value={reason ?? ''}
          onChange={e => onReasonChange(e.target.value)}
          placeholder="Reason for change (optional)"
          className={`${formStyles.input} min-w-[180px]`}
          aria-label="Reason for change"
        />
      )}
      <button
        type="button"
        onClick={onSave}
        disabled={saving}
        className={`${formStyles.primaryButton} disabled:opacity-50`}
      >
        {saving ? 'Saving…' : saveLabel}
      </button>
    </div>
  );
}
