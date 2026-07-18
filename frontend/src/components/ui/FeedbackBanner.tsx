interface FeedbackBannerProps {
  tone: 'error' | 'success' | 'info' | 'warning';
  message: string;
  onDismiss?: () => void;
  className?: string;
}

const toneClasses = {
  error: 'bg-red-50 border-red-200 text-red-600',
  success: 'bg-green-50 border-green-200 text-green-600',
  info: 'bg-blue-50 border-blue-200 text-blue-700',
  warning: 'bg-amber-50 border-amber-200 text-amber-800',
} as const;

export default function FeedbackBanner({ tone, message, onDismiss, className }: FeedbackBannerProps) {
  return (
    <div className={`mb-6 p-4 border rounded-lg ${toneClasses[tone]} ${className ?? ''}`.trim()}>
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm">{message}</p>
        {onDismiss ? (
          <button type="button" onClick={onDismiss} className="opacity-70 hover:opacity-100 transition-opacity">
            ✕
          </button>
        ) : null}
      </div>
    </div>
  );
}
