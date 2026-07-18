import { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  backLink?: ReactNode;
  actions?: ReactNode;
}

export default function PageHeader({ title, subtitle, backLink, actions }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-4 mb-6">
      <div className="min-w-0">
        {backLink ? <div className="mb-2">{backLink}</div> : null}
        <h1 className="text-2xl font-semibold text-[#0a0a0a]">{title}</h1>
        {subtitle ? <p className="text-[#737373] mt-1">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex gap-2 items-end flex-wrap justify-end">{actions}</div> : null}
    </div>
  );
}
