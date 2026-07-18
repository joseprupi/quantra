// Shared error card used by every pricing product page. Renders a structured
// PricingErrorInfo when available (with code_name, HTTP status, raw payload),
// and gracefully falls back to a plain string for callers that haven't been
// migrated yet (or for purely client-side validation errors).
import { useState } from 'react';
import type { PricingErrorInfo } from '../../lib/quantra-types';
import { isDevTooling } from '../../lib/devAuth';

/** Dev-only deep link into the pricing-trace investigation page. */
function investigateHref(requestId: string): string {
  return `/investigate?request_id=${encodeURIComponent(requestId)}`;
}

interface PricingErrorCardProps {
  error: string;
  info?: PricingErrorInfo;
  durationMs?: number;
  title?: string;
  className?: string;
}

type Palette = {
  border: string;
  iconBg: string;
  iconColor: string;
  titleColor: string;
  bodyColor: string;
  metaColor: string;
};

const PALETTES: Record<NonNullable<PricingErrorInfo['category']> | 'default', Palette> = {
  validation: {
    border: 'border-amber-200',
    iconBg: 'bg-amber-50',
    iconColor: 'text-amber-500',
    titleColor: 'text-amber-700',
    bodyColor: 'text-amber-800',
    metaColor: 'text-amber-600',
  },
  not_found: {
    border: 'border-amber-200',
    iconBg: 'bg-amber-50',
    iconColor: 'text-amber-500',
    titleColor: 'text-amber-700',
    bodyColor: 'text-amber-800',
    metaColor: 'text-amber-600',
  },
  unavailable: {
    border: 'border-orange-200',
    iconBg: 'bg-orange-50',
    iconColor: 'text-orange-500',
    titleColor: 'text-orange-700',
    bodyColor: 'text-orange-800',
    metaColor: 'text-orange-600',
  },
  auth: {
    border: 'border-blue-200',
    iconBg: 'bg-blue-50',
    iconColor: 'text-blue-500',
    titleColor: 'text-blue-700',
    bodyColor: 'text-blue-800',
    metaColor: 'text-blue-600',
  },
  network: {
    border: 'border-slate-200',
    iconBg: 'bg-slate-50',
    iconColor: 'text-slate-500',
    titleColor: 'text-slate-700',
    bodyColor: 'text-slate-800',
    metaColor: 'text-slate-500',
  },
  server: {
    border: 'border-red-200',
    iconBg: 'bg-red-50',
    iconColor: 'text-red-500',
    titleColor: 'text-red-600',
    bodyColor: 'text-red-500',
    metaColor: 'text-red-500',
  },
  client: {
    border: 'border-red-200',
    iconBg: 'bg-red-50',
    iconColor: 'text-red-500',
    titleColor: 'text-red-600',
    bodyColor: 'text-red-500',
    metaColor: 'text-red-500',
  },
  default: {
    border: 'border-red-200',
    iconBg: 'bg-red-50',
    iconColor: 'text-red-500',
    titleColor: 'text-red-600',
    bodyColor: 'text-red-500',
    metaColor: 'text-red-500',
  },
};

const CATEGORY_TITLES: Record<NonNullable<PricingErrorInfo['category']>, string> = {
  validation: 'Invalid Request',
  not_found: 'Not Found',
  unavailable: 'Backend Unavailable',
  auth: 'Authentication Required',
  network: 'Network Error',
  server: 'Pricing Error',
  client: 'Pricing Error',
};

function pickTitle(info: PricingErrorInfo | undefined, override: string | undefined): string {
  if (override) return override;
  if (info) return CATEGORY_TITLES[info.category] ?? 'Pricing Error';
  return 'Pricing Error';
}

function pickPalette(info: PricingErrorInfo | undefined): Palette {
  if (!info) return PALETTES.default;
  return PALETTES[info.category] ?? PALETTES.default;
}

function formatRaw(raw: unknown): string {
  if (raw === undefined || raw === null) return '';
  if (typeof raw === 'string') return raw;
  try {
    return JSON.stringify(raw, null, 2);
  } catch {
    return String(raw);
  }
}

export default function PricingErrorCard({ error, info, durationMs, title, className }: PricingErrorCardProps) {
  const [showDetails, setShowDetails] = useState(false);
  const palette = pickPalette(info);
  const resolvedTitle = pickTitle(info, title);
  const message = info?.message ?? error;

  const metaParts: string[] = [];
  if (info?.codeName) metaParts.push(info.codeName);
  if (info && info.httpStatus > 0) metaParts.push(`HTTP ${info.httpStatus}`);
  if (typeof durationMs === 'number' && Number.isFinite(durationMs)) {
    metaParts.push(`${durationMs.toFixed(0)}ms`);
  }
  const rawText = info?.raw !== undefined ? formatRaw(info.raw) : '';
  // Actionable "what to do next" + the correlation id (support
  // handle, greppable across the stack), both derived from the envelope code.
  const suggestion = info?.suggestion;
  const requestId = info?.requestId;

  const handleCopy = () => {
    if (!rawText) return;
    void navigator.clipboard?.writeText(rawText);
  };

  const handleCopyId = () => {
    if (!requestId) return;
    void navigator.clipboard?.writeText(requestId);
  };

  return (
    <div className={`bg-white border ${palette.border} rounded-xl p-6 ${className ?? ''}`}>
      <div className="flex items-start gap-3">
        <div className={`flex-shrink-0 w-8 h-8 rounded-full ${palette.iconBg} flex items-center justify-center`}>
          <svg className={`w-5 h-5 ${palette.iconColor}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <p className={`text-sm font-semibold ${palette.titleColor}`}>{resolvedTitle}</p>
          <p className={`text-sm mt-1 break-words whitespace-pre-wrap ${palette.bodyColor}`}>{message}</p>

          {suggestion && (
            <p className={`text-sm mt-2 break-words ${palette.bodyColor}`}>
              <span className="font-medium">Suggested fix: </span>
              {suggestion}
            </p>
          )}

          {metaParts.length > 0 && (
            <p className={`mt-2 text-xs font-mono ${palette.metaColor}`}>{metaParts.join(' · ')}</p>
          )}

          {requestId && (
            <p className={`mt-1 text-xs font-mono ${palette.metaColor} flex items-center gap-2`}>
              <span className="break-all">
                <span className="opacity-70">Request ID:</span> {requestId}
              </span>
              <button
                type="button"
                onClick={handleCopyId}
                className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-white border border-[#e5e5e5] text-[#525252] hover:bg-[#f5f5f5]"
              >
                Copy
              </button>
              {isDevTooling() && (
                <a
                  href={investigateHref(requestId)}
                  className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-white border border-[#e5e5e5] text-[#525252] hover:bg-[#f5f5f5]"
                >
                  View trace
                </a>
              )}
            </p>
          )}

          {rawText && (
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setShowDetails(v => !v)}
                className={`text-xs font-medium underline-offset-2 hover:underline ${palette.metaColor}`}
              >
                {showDetails ? 'Hide details' : 'Show details'}
              </button>
              {showDetails && (
                <div className="mt-2 relative">
                  <pre className="text-xs bg-[#fafafa] border border-[#e5e5e5] rounded-md p-3 overflow-auto max-h-48 font-mono text-[#404040] whitespace-pre-wrap break-words">
{rawText}
                  </pre>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="absolute top-2 right-2 text-[10px] font-medium px-2 py-1 rounded bg-white border border-[#e5e5e5] text-[#525252] hover:bg-[#f5f5f5]"
                  >
                    Copy
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
