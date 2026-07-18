import { ReactNode } from 'react';
import { Link } from 'react-router-dom';

interface BackLinkProps {
  label: string;
  to?: string;
  onClick?: () => void;
  className?: string;
  icon?: ReactNode;
}

const defaultClassName = 'flex items-center gap-1 text-sm text-[#737373] hover:text-[#0a0a0a] transition-colors';

function ChevronLeft() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
    </svg>
  );
}

export default function BackLink({ label, to, onClick, className, icon }: BackLinkProps) {
  const content = (
    <>
      {icon ?? <ChevronLeft />}
      {label}
    </>
  );

  if (to) {
    return (
      <Link to={to} className={className ?? defaultClassName}>
        {content}
      </Link>
    );
  }

  return (
    <button type="button" onClick={onClick} className={className ?? defaultClassName}>
      {content}
    </button>
  );
}
