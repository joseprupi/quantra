import { PropsWithChildren } from 'react';

export const listStyles = {
  secondaryButton: 'px-4 py-2 text-sm font-medium text-[#525252] bg-white border border-[#d4d4d4] rounded-lg hover:bg-[#f5f5f5] transition-colors flex items-center gap-2',
  primaryNewButton: 'px-4 py-2 text-sm font-medium text-white bg-[#8a6a2f] rounded-lg hover:bg-[#7a5c28] transition-colors flex items-center gap-2',
  listCard: 'bg-white border border-[#e5e5e5] rounded-xl p-5 hover:border-[#d4d4d4] transition-colors group cursor-pointer',
  hoverActions: 'flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity',
  duplicateButton: 'p-2 text-[#737373] hover:text-[#0a0a0a] hover:bg-[#f5f5f5] rounded-lg transition-colors',
  deleteButton: 'p-2 text-[#737373] hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors',
};

export function ImportIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
    </svg>
  );
}

export function ExportIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
  );
}

export function NewIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
    </svg>
  );
}

export function DuplicateIcon() {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
    </svg>
  );
}

export function TrashButton({ children }: PropsWithChildren) {
  return (
    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
      {children}
    </svg>
  );
}
