import { useEffect, useState } from 'react';
import { type InflationPointWrapper, type QuoteSpec } from '../../lib/types';
import { getLegacyFlatQuotes } from '../../lib/storage/quoteBook';

interface Props {
  points: InflationPointWrapper[];
  onEdit: (index: number) => void;
  onDelete: (index: number) => void;
}

const TYPE_COLORS: Record<InflationPointWrapper['point_type'], string> = {
  ZeroCouponInflationSwapHelper: 'bg-rose-500',
  YearOnYearInflationSwapHelper: 'bg-fuchsia-500',
};

const TYPE_LABELS: Record<InflationPointWrapper['point_type'], string> = {
  ZeroCouponInflationSwapHelper: 'ZC',
  YearOnYearInflationSwapHelper: 'YOY',
};

function getHelperLabel(point: InflationPointWrapper): string {
  if (point.point.tenor) {
    return `${point.point.tenor.n}${point.point.tenor.unit[0]}`;
  }
  if (point.point.start_date && point.point.end_date) {
    return `${point.point.start_date} → ${point.point.end_date}`;
  }
  if (point.point.end_date) {
    return `End ${point.point.end_date}`;
  }
  return 'Helper';
}

function getHelperRate(point: InflationPointWrapper, quotes: QuoteSpec[]): number | null {
  if (point.point.quote_id) {
    const quote = quotes.find((entry) => entry.id === point.point.quote_id);
    return quote ? quote.value : null;
  }
  return point.point.quote_value ?? null;
}

export default function InflationPointsList({ points, onEdit, onDelete }: Props) {
  const [quotes, setQuotes] = useState<QuoteSpec[]>([]);

  useEffect(() => {
    setQuotes(getLegacyFlatQuotes());
  }, []);

  if (points.length === 0) {
    return (
      <div className="text-center py-8 text-[#a3a3a3]">
        <p className="text-sm">No helpers added yet</p>
        <p className="text-xs mt-1">Click "Add Helper" to add inflation instruments</p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {points.map((point, index) => {
        const rate = getHelperRate(point, quotes);
        return (
          <div
            key={index}
            className="flex items-center gap-3 px-3 py-2.5 bg-white border border-[#e5e5e5] rounded-lg hover:border-[#d4d4d4] transition-colors group"
          >
            <span className={`flex-shrink-0 w-10 text-center px-1.5 py-0.5 text-[10px] font-bold text-white rounded ${TYPE_COLORS[point.point_type]}`}>
              {TYPE_LABELS[point.point_type]}
            </span>

            <span className="text-sm font-medium text-[#0a0a0a] min-w-20 truncate">
              {getHelperLabel(point)}
            </span>

            <span className="flex-1 text-right">
              <span className="font-mono text-sm text-[#0a0a0a]">
                {rate === null ? '—' : `${(rate * 100).toFixed(3)}%`}
              </span>
              {point.point.quote_id && (
                <span className="ml-1.5 text-[10px] font-medium text-[#8a6a2f] bg-[#f5f0e6] px-1.5 py-0.5 rounded">
                  {point.point.quote_id}
                </span>
              )}
              {point.point.nominal_curve_id && (
                <span className="ml-1.5 text-[10px] font-medium text-blue-700 bg-blue-50 px-1.5 py-0.5 rounded">
                  {point.point.nominal_curve_id}
                </span>
              )}
            </span>

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
        );
      })}
    </div>
  );
}
