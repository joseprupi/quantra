// Display list of curve points with quote reference resolution
import { CurvePoint, QuoteSpec, getPointTenorLabel, getPointRate, getPointIndexRefId } from '../../lib/types';
import { useState, useEffect } from 'react';
import { getLegacyFlatQuotes } from '../../lib/storage/quoteBook';

interface Props {
  points: CurvePoint[];
  onEdit: (index: number) => void;
  onDelete: (index: number) => void;
}

const TYPE_COLORS: Record<string, string> = {
  DepositHelper: 'bg-[#8a6a2f]',
  SwapHelper: 'bg-blue-500',
  FRAHelper: 'bg-green-500',
  FutureHelper: 'bg-purple-500',
  BondHelper: 'bg-orange-500',
  OISHelper: 'bg-teal-500',
  DatedOISHelper: 'bg-cyan-500',
};

const TYPE_LABELS: Record<string, string> = {
  DepositHelper: 'DEP',
  SwapHelper: 'IRS',
  FRAHelper: 'FRA',
  FutureHelper: 'FUT',
  BondHelper: 'BOND',
  OISHelper: 'OIS',
  DatedOISHelper: 'DOIS',
};

export default function CurvePointsList({ points, onEdit, onDelete }: Props) {
  const [quotes, setQuotes] = useState<QuoteSpec[]>([]);

  useEffect(() => {
    setQuotes(getLegacyFlatQuotes());
  }, []);

  if (points.length === 0) {
    return (
      <div className="text-center py-8 text-[#a3a3a3]">
        <p className="text-sm">No instruments added yet</p>
        <p className="text-xs mt-1">Click "Add Point" to add curve instruments</p>
      </div>
    );
  }

  const formatRate = (point: CurvePoint): string => {
    const rate = getPointRate(point, quotes);
    if (rate === null) return '—';
    if (point.point_type === 'FutureHelper') return rate.toFixed(2);
    if (point.point_type === 'BondHelper') return rate.toFixed(2);
    return `${(rate * 100).toFixed(3)}%`;
  };

  const isQuoteRef = (point: CurvePoint): boolean => {
    return !!(point.point as any).quote_id;
  };

  return (
    <div className="space-y-1.5">
      {points.map((point, index) => (
        <div
          key={index}
          className="flex items-center gap-3 px-3 py-2.5 bg-white border border-[#e5e5e5] rounded-lg hover:border-[#d4d4d4] transition-colors group"
        >
          {/* Type badge */}
          <span className={`flex-shrink-0 w-10 text-center px-1.5 py-0.5 text-[10px] font-bold text-white rounded ${TYPE_COLORS[point.point_type] || 'bg-gray-400'}`}>
            {TYPE_LABELS[point.point_type] || '?'}
          </span>

          {/* Tenor */}
          <span className="text-sm font-medium text-[#0a0a0a] w-20 truncate">
            {getPointTenorLabel(point)}
          </span>

          {/* Rate */}
          <span className="flex-1 text-right">
            <span className="font-mono text-sm text-[#0a0a0a]">{formatRate(point)}</span>
            {isQuoteRef(point) && (
              <span className="ml-1.5 text-[10px] font-medium text-[#8a6a2f] bg-[#f5f0e6] px-1.5 py-0.5 rounded">
                {(point.point as any).quote_id}
              </span>
            )}
            {getPointIndexRefId(point) && (
              <span className="ml-1.5 text-[10px] font-medium text-blue-700 bg-blue-50 px-1.5 py-0.5 rounded">
                {getPointIndexRefId(point)}
              </span>
            )}
          </span>

          {/* Actions */}
          <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
            <button onClick={() => onEdit(index)} className="p-1 text-[#737373] hover:text-[#0a0a0a]" title="Edit">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button onClick={() => onDelete(index)} className="p-1 text-[#737373] hover:text-red-500" title="Delete">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
