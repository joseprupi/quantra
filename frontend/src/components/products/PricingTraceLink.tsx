// The trace doorway shown on a SUCCESSFUL pricing card.
//
// On a success the portal already holds the `X-Request-ID` it sent for the call
// (threaded up from the orchestrator client). This renders that id (small,
// copyable) plus a "View trace" deep link into /investigate, so a price that
// "works" — even one that returns no cashflows — is still one click from its
// in-app trace, exactly like the error card already is.
//
// Dev-gated exactly like the error card's deep link and the original dormant
// success-card link: nothing renders in a prod build (`import.meta.env.DEV ===
// false`), so production behaviour is unchanged.
import { isDevTooling } from '../../lib/devAuth';

interface PricingTraceLinkProps {
  /** Correlation id for this pricing call; the doorway is dormant without it. */
  requestId?: string;
}

export default function PricingTraceLink({ requestId }: PricingTraceLinkProps) {
  if (!requestId || !isDevTooling()) return null;

  const href = `/investigate?request_id=${encodeURIComponent(requestId)}`;
  const handleCopy = () => {
    void navigator.clipboard?.writeText(requestId);
  };

  return (
    <span className="flex items-center gap-1.5">
      <span
        className="text-[10px] font-mono text-[#a3a3a3] break-all select-all"
        title="Request ID — correlation handle for this pricing call"
        data-testid="pricing-request-id"
      >
        {requestId}
      </span>
      <button
        type="button"
        onClick={handleCopy}
        className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-white border border-[#e5e5e5] text-[#525252] hover:bg-[#f5f5f5]"
      >
        Copy
      </button>
      <a
        href={href}
        className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-white border border-[#e5e5e5] text-[#525252] hover:bg-[#f5f5f5]"
      >
        View trace
      </a>
    </span>
  );
}
