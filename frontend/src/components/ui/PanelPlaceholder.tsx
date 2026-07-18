import { ReactNode } from 'react';
import { EntityIcon, renderEntityIcon } from './entityUi';

interface PanelPlaceholderProps {
  title: string;
  description: string;
  icon?: EntityIcon;
  actionLabel?: string;
  onAction?: () => void;
  children?: ReactNode;
  compact?: boolean;
}

export default function PanelPlaceholder({
  title,
  description,
  icon = 'placeholder',
  actionLabel,
  onAction,
  children,
  compact = false,
}: PanelPlaceholderProps) {
  return (
    <div className={`bg-white border border-[#e5e5e5] rounded-xl text-center ${compact ? 'p-4' : 'p-6'}`}>
      <div className={`mx-auto text-[#d4d4d4] ${compact ? 'mb-3' : 'mb-4'}`}>
        {renderEntityIcon(icon, compact ? 'w-10 h-10' : 'w-12 h-12')}
      </div>
      <h3 className={`${compact ? 'text-base' : 'text-lg'} font-medium text-[#0a0a0a] mb-2`}>{title}</h3>
      <p className={`text-[#737373] ${actionLabel || children ? 'mb-4' : ''}`}>{description}</p>
      {actionLabel && onAction ? (
        <button
          type="button"
          onClick={onAction}
          className="px-4 py-2 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] transition-colors"
        >
          {actionLabel}
        </button>
      ) : null}
      {children}
    </div>
  );
}
