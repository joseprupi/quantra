import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { getVersion, type VersionInfo } from '../lib/api/orchestrator';

/**
 * About Quantra — a small info panel opened from the header ⓘ button. Shows the
 * three versions that make up a running stack so anyone looking at the screen
 * knows exactly what is deployed:
 *   - Web client (this portal): a build-time constant (__APP_VERSION__).
 *   - Backend (orchestrator): version + build SHA from GET /v1/version.
 *   - Pricing engine: version from GET /v1/version.
 *
 * The web-client row always renders (it needs no network). The backend/engine
 * rows show a subtle loading state and, on fetch error, a graceful
 * "Unavailable" — never blank, never a crash, never blocking the panel.
 */

type BackendState =
  | { status: 'loading' }
  | { status: 'ok'; info: VersionInfo }
  | { status: 'error' };

function Row({
  label,
  primary,
  secondary,
}: {
  label: string;
  primary: React.ReactNode;
  secondary?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-2.5 border-b border-[#f0f0f0] last:border-b-0">
      <span className="text-sm text-[#525252]">{label}</span>
      <span className="text-right">
        <span className="text-sm font-mono text-[#0a0a0a]">{primary}</span>
        {secondary != null && (
          <span className="block text-[11px] font-mono text-[#a3a3a3]">{secondary}</span>
        )}
      </span>
    </div>
  );
}

export default function AboutPanel({ onClose }: { onClose: () => void }) {
  const [backend, setBackend] = useState<BackendState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;
    getVersion().then((res) => {
      if (cancelled) return;
      setBackend(res.ok ? { status: 'ok', info: res.data } : { status: 'error' });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const loading = backend.status === 'loading';
  const unavailable = backend.status === 'error';
  const info = backend.status === 'ok' ? backend.info : null;

  const backendVersion = loading ? (
    <span className="text-[#a3a3a3]">Loading…</span>
  ) : unavailable ? (
    <span className="text-[#a3a3a3] font-sans italic">Unavailable</span>
  ) : (
    info!.orchestrator.version
  );

  const backendSha =
    info != null ? `build ${info.orchestrator.build_sha}` : undefined;

  const engineVersion = loading ? (
    <span className="text-[#a3a3a3]">Loading…</span>
  ) : unavailable ? (
    <span className="text-[#a3a3a3] font-sans italic">Unavailable</span>
  ) : (
    info!.engine.version
  );

  // Render through a portal to document.body so the overlay escapes the header's
  // containing block: the <header> ancestor sets `backdrop-blur-xl` (a
  // backdrop-filter), which makes it the containing block for `position: fixed`
  // descendants — trapping the overlay inside the header instead of covering the
  // viewport. Portaling to <body> restores true viewport-fixed positioning. The
  // z-[60] sits above the z-50 header.
  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="About Quantra"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-[#e5e5e5] flex items-center justify-between">
          <h3 className="text-base font-semibold text-[#0a0a0a]">About Quantra</h3>
          <button
            onClick={onClose}
            className="text-[#a3a3a3] hover:text-[#0a0a0a] text-xl leading-none"
            aria-label="Close about panel"
          >
            ×
          </button>
        </div>
        <div className="px-5 py-3">
          <Row label="Web client" primary={__APP_VERSION__} />
          <Row label="Backend" primary={backendVersion} secondary={backendSha} />
          <Row label="Pricing engine" primary={engineVersion} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
