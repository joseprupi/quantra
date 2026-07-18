
export type EntityIcon =
  | 'rates'
  | 'document'
  | 'credit'
  | 'curve'
  | 'index'
  | 'surface'
  | 'timeSeries'
  | 'placeholder';

export interface EntityUiConfig {
  singular: string;
  plural: string;
  icon: EntityIcon;
  emptyDescription: string;
  createVerb?: 'Create' | 'Add';
}

export const entityUi = {
  swap: {
    singular: 'Swap',
    plural: 'Swaps',
    icon: 'rates',
    emptyDescription: 'Create your first swap to get started.',
  },
  inflationSwap: {
    singular: 'Inflation Swap',
    plural: 'Inflation Swaps',
    icon: 'rates',
    emptyDescription: 'Create your first inflation swap to get started.',
  },
  cds: {
    singular: 'CDS',
    plural: 'CDS',
    icon: 'credit',
    emptyDescription: 'Create your first CDS request to get started.',
  },
  swaption: {
    singular: 'Swaption',
    plural: 'Swaptions',
    icon: 'document',
    emptyDescription: 'Create your first swaption to get started.',
  },
  equityOption: {
    singular: 'Equity Option',
    plural: 'Equity Options',
    icon: 'document',
    emptyDescription: 'Create your first equity option to get started.',
  },
  fixedRateBond: {
    singular: 'Bond',
    plural: 'Fixed Rate Bonds',
    icon: 'document',
    emptyDescription: 'Create your first bond to get started.',
  },
  floatingRateBond: {
    singular: 'Bond',
    plural: 'Floating Rate Bonds',
    icon: 'document',
    emptyDescription: 'Create your first bond to get started.',
  },
  curveSet: {
    singular: 'Curve Set',
    plural: 'Curve Sets',
    icon: 'document',
    emptyDescription: 'Create your first curve set to define a reusable pricing context.',
  },
  yieldCurve: {
    singular: 'Yield Curve',
    plural: 'Yield Curves',
    icon: 'curve',
    emptyDescription: 'Create your first yield curve to get started.',
  },
  inflationCurve: {
    singular: 'Inflation Curve',
    plural: 'Inflation Curves',
    icon: 'curve',
    emptyDescription: 'Create your first inflation curve to get started.',
  },
  creditCurve: {
    singular: 'Credit Curve',
    plural: 'Credit Curves',
    icon: 'credit',
    emptyDescription: 'Create your first reusable credit curve to get started.',
  },
  index: {
    singular: 'Index',
    plural: 'Indices',
    icon: 'index',
    emptyDescription: 'Create your first index to get started.',
  },
  quote: {
    singular: 'Quote',
    plural: 'Quotes',
    icon: 'rates',
    emptyDescription: 'Add market quotes to use in curve construction.',
    createVerb: 'Add',
  },
  timeSeries: {
    singular: 'Series',
    plural: 'Series',
    icon: 'timeSeries',
    emptyDescription: 'Adjust the filters or select a series with enough history.',
  },
  surface: {
    singular: 'Surface',
    plural: 'Surfaces',
    icon: 'surface',
    emptyDescription: 'Create or import a surface to get started.',
  },
} satisfies Record<string, EntityUiConfig>;

export type EntityUiKey = keyof typeof entityUi;

export function getNewLabel(config: EntityUiConfig) {
  return `New ${config.singular}`;
}

export function getCreateLabel(config: EntityUiConfig) {
  return `${config.createVerb ?? 'Create'} ${config.singular}`;
}

export function getEmptyTitle(config: EntityUiConfig) {
  return `No ${config.plural.toLowerCase()} yet`;
}

export function getBackLabel(pageName: string) {
  return `Back to ${pageName}`;
}

export function getInstructionText(subject: string, actionLabel: string) {
  return `Configure ${subject} and click ${actionLabel}.`;
}

export function renderEntityIcon(icon: EntityIcon, className = 'w-12 h-12') {
  const common = {
    className,
    fill: 'none',
    viewBox: '0 0 24 24',
    stroke: 'currentColor',
    'aria-hidden': true,
  } as const;

  if (icon === 'rates') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 19.5h16M6.75 16.5l3.25-4.5 3.25 2.5 4-6" />
        <circle cx="6.75" cy="16.5" r="1" fill="currentColor" stroke="none" />
        <circle cx="10" cy="12" r="1" fill="currentColor" stroke="none" />
        <circle cx="13.25" cy="14.5" r="1" fill="currentColor" stroke="none" />
        <circle cx="17.25" cy="8.5" r="1" fill="currentColor" stroke="none" />
      </svg>
    );
  }

  if (icon === 'credit') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M6 4.75h8.25L18 8.5v10.75a1 1 0 01-1 1H7a1 1 0 01-1-1V4.75z" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M14 4.75V8.5h4" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8.25 12h7.5M8.25 15.5h4.5" />
      </svg>
    );
  }

  if (icon === 'curve') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4.5 18.5h15" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M6 15c2.25 0 2.25-6 4.5-6s2.25 8 4.5 8 2.25-5 3-7" />
      </svg>
    );
  }

  if (icon === 'index') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5.5 6.5h13M5.5 12h13M5.5 17.5h8" />
        <circle cx="17.5" cy="17.5" r="1.75" strokeWidth={1.5} />
      </svg>
    );
  }

  if (icon === 'surface') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4.5 9.5l7.5-4 7.5 4-7.5 4-7.5-4z" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4.5 14.5l7.5 4 7.5-4" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4.5 9.5v5M19.5 9.5v5" />
      </svg>
    );
  }

  if (icon === 'timeSeries') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4.5 18.5h15" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M6.5 14.5l3-2.5 2.5 1 3-4.5 2.5 1.5" />
      </svg>
    );
  }

  if (icon === 'placeholder') {
    return (
      <svg {...common}>
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M6.75 6.75h10.5v10.5H6.75z" />
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.5 12h5" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M6 4.75h8.25L18 8.5v10.75a1 1 0 01-1 1H7a1 1 0 01-1-1V4.75z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M14 4.75V8.5h4" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8.25 12h7.5M8.25 15.5h4.5" />
    </svg>
  );
}

