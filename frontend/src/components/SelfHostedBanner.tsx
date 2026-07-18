import { useEffect, useState } from 'react';
import { isDevAuthBypass } from '../lib/devAuth';
import { fetchLatestRealDate } from '../lib/latestRealDate';

/**
 * Honesty banner copy.
 *
 * The platform now serves ONLY real, daily public market data (Bank of England,
 * US Treasury, FRED, ECB) — there is no synthetic data. The banner is still
 * provenance-aware so it never overclaims on a fresh install:
 *   - real public data ingested (latest real date present) ⇒ the "live" copy;
 *   - none yet (fresh bundle, endpoint empty/unreachable) ⇒ an honest "no data
 *     ingested yet" copy — never claims prices exist before they do.
 */
export const SELF_HOSTED_BANNER_NO_DATA =
  'Self-hosted · public market data — none ingested yet';
export const SELF_HOSTED_BANNER_LIVE =
  'Live public market data · Bank of England, US Treasury, FRED, ECB · updated daily';

/**
 * A slim, always-visible bar shown whenever the app runs in self-hosted /
 * dev-auth-bypass mode. It reuses the single dev-bypass signal
 * (`isDevAuthBypass`), so it is present in the self-hosted image (runtime
 * `config.js` opts in) and in local dev with the bypass on, and renders
 * nothing in the public hosted build.
 *
 * Fixed to the bottom of the viewport so it is unmissable without colliding
 * with the fixed top `Header` or its `pt-24` content offset — i.e. it does not
 * disturb existing page layout.
 */
export default function SelfHostedBanner() {
  // Start on the conservative "no data ingested yet" copy; upgrade to "live"
  // only once we confirm real public data has been ingested. Fetch failure ⇒
  // stays honest.
  const [live, setLive] = useState(false);

  useEffect(() => {
    if (!isDevAuthBypass()) return;
    let cancelled = false;
    void fetchLatestRealDate().then((date) => {
      if (!cancelled && date) setLive(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!isDevAuthBypass()) return null;

  const text = live ? SELF_HOSTED_BANNER_LIVE : SELF_HOSTED_BANNER_NO_DATA;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="self-hosted-banner"
      className="fixed bottom-0 left-0 right-0 z-[100] flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium text-[#7a5b12] bg-[#fdf3d6] border-t border-[#e8d9a6] shadow-[0_-1px_2px_rgba(0,0,0,0.04)]"
    >
      <span
        aria-hidden="true"
        className={live ? 'text-[#1f9d55]' : 'text-[#b7911f]'}
      >
        &#9679;
      </span>
      <span>{text}</span>
    </div>
  );
}
