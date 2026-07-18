import { ReactNode } from 'react';
import { EntityIcon, renderEntityIcon } from './entityUi';

interface CollectionEmptyStateProps {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  icon: EntityIcon;
  actionClassName?: string;
  children?: ReactNode;
}

export default function CollectionEmptyState({
  title,
  description,
  actionLabel,
  onAction,
  icon,
  actionClassName,
  children,
}: CollectionEmptyStateProps) {
  return (
    <div className="bg-white border border-[#e5e5e5] rounded-xl p-12 text-center">
      <div className="mx-auto text-[#d4d4d4] mb-4">{renderEntityIcon(icon)}</div>
      <h3 className="text-lg font-medium text-[#0a0a0a] mb-2">{title}</h3>
      <p className="text-[#737373] mb-6">{description}</p>
      {actionLabel && onAction ? (
        <button
          type="button"
          onClick={onAction}
          className={actionClassName ?? 'px-6 py-2.5 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] transition-colors'}
        >
          {actionLabel}
        </button>
      ) : null}
      {children}
    </div>
  );
}
