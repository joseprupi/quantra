// Pricing results display component
import { useState } from 'react';
import PricingErrorCard from './PricingErrorCard';
import PricingTraceLink from './PricingTraceLink';
import type { PricingErrorInfo } from '../../lib/quantra-types';

interface FlowData {
  flow_type: string;
  flow: {
    amount?: number;
    date?: string;
    accrual_start_date?: string;
    accrual_end_date?: string;
    discount?: number;
    rate?: number;
    price?: number;
  };
}

interface BondResultData {
  npv?: number;
  clean_price?: number;
  dirty_price?: number;
  accrued_amount?: number;
  yield?: number;
  accrued_days?: number;
  macaulay_duration?: number;
  modified_duration?: number;
  convexity?: number;
  bps?: number;
  flows?: FlowData[];
}

interface PricingResultsProps {
  loading: boolean;
  error: string | null;
  errorInfo?: PricingErrorInfo;
  result: BondResultData | null;
  durationMs?: number;
  /** Correlation id for this pricing call; enables the dev "View trace" link. */
  requestId?: string;
}

export default function PricingResults({ loading, error, errorInfo, result, durationMs, requestId }: PricingResultsProps) {
  const [showFlows, setShowFlows] = useState(false);
  
  if (loading) {
    return (
      <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
        <div className="flex items-center justify-center py-8">
          <div className="w-6 h-6 border-2 border-[#e5e5e5] border-t-[#8a6a2f] rounded-full animate-spin" />
          <span className="ml-3 text-sm text-[#737373]">Pricing...</span>
        </div>
      </div>
    );
  }
  
  if (error) {
    return <PricingErrorCard error={error} info={errorInfo} durationMs={durationMs} />;
  }
  
  if (!result) {
    return (
      <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
        <div className="text-center py-8">
          <svg className="w-12 h-12 mx-auto text-[#d4d4d4] mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
          </svg>
          <p className="text-sm text-[#737373]">Configure bond parameters and click Price</p>
        </div>
      </div>
    );
  }
  
  const formatNumber = (n: number | undefined, decimals = 4) => {
    if (n === undefined || n === null) return '—';
    return n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  };
  
  const formatPercent = (n: number | undefined, decimals = 4) => {
    if (n === undefined || n === null) return '—';
    return (n * 100).toFixed(decimals) + '%';
  };
  
  return (
    <div className="bg-white border border-[#e5e5e5] rounded-xl p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-[#0a0a0a]">Pricing Results</h3>
        <div className="flex items-center gap-3">
          <PricingTraceLink requestId={requestId} />
          {durationMs && (
            <span className="text-xs text-[#a3a3a3]">{durationMs.toFixed(0)}ms</span>
          )}
        </div>
      </div>
      
      {/* Main metrics */}
      <div className="grid grid-cols-2 gap-4 mb-4">
        <div className="bg-[#f5f5f5] rounded-lg p-3">
          <p className="text-xs text-[#737373] mb-1">NPV</p>
          <p className="text-lg font-semibold text-[#0a0a0a] font-mono">{formatNumber(result.npv, 2)}</p>
        </div>
        <div className="bg-[#f5f5f5] rounded-lg p-3">
          <p className="text-xs text-[#737373] mb-1">Yield</p>
          <p className="text-lg font-semibold text-[#0a0a0a] font-mono">{formatPercent(result.yield)}</p>
        </div>
      </div>
      
      {/* Price details */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div>
          <p className="text-xs text-[#737373]">Clean Price</p>
          <p className="text-sm font-medium font-mono">{formatNumber(result.clean_price, 4)}</p>
        </div>
        <div>
          <p className="text-xs text-[#737373]">Dirty Price</p>
          <p className="text-sm font-medium font-mono">{formatNumber(result.dirty_price, 4)}</p>
        </div>
        <div>
          <p className="text-xs text-[#737373]">Accrued</p>
          <p className="text-sm font-medium font-mono">{formatNumber(result.accrued_amount, 4)}</p>
        </div>
      </div>
      
      {/* Risk metrics */}
      {(result.macaulay_duration !== undefined || result.modified_duration !== undefined) && (
        <div className="border-t border-[#e5e5e5] pt-4 mb-4">
          <p className="text-xs text-[#737373] mb-2 font-medium">Risk Metrics</p>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <p className="text-xs text-[#737373]">Macaulay Duration</p>
              <p className="text-sm font-medium font-mono">{formatNumber(result.macaulay_duration, 4)}</p>
            </div>
            <div>
              <p className="text-xs text-[#737373]">Modified Duration</p>
              <p className="text-sm font-medium font-mono">{formatNumber(result.modified_duration, 4)}</p>
            </div>
            <div>
              <p className="text-xs text-[#737373]">Convexity</p>
              <p className="text-sm font-medium font-mono">{formatNumber(result.convexity, 4)}</p>
            </div>
          </div>
        </div>
      )}
      
      {/* Cash flows */}
      {result.flows && result.flows.length > 0 && (
        <div className="border-t border-[#e5e5e5] pt-4">
          <button
            onClick={() => setShowFlows(!showFlows)}
            className="flex items-center justify-between w-full text-left"
          >
            <span className="text-xs text-[#737373] font-medium">
              Cash Flows ({result.flows.length})
            </span>
            <svg 
              className={`w-4 h-4 text-[#737373] transition-transform ${showFlows ? 'rotate-180' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          
          {showFlows && (
            <div className="mt-3 max-h-48 overflow-auto">
              <table className="w-full text-xs">
                <thead className="text-[#737373]">
                  <tr>
                    <th className="text-left py-1">Type</th>
                    <th className="text-right py-1">Date</th>
                    <th className="text-right py-1">Amount</th>
                    <th className="text-right py-1">Discount</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {result.flows.map((flow, i) => (
                    <tr key={i} className="border-t border-[#f5f5f5]">
                      <td className="py-1 text-[#525252]">
                        {flow.flow_type?.replace('Flow', '')}
                      </td>
                      <td className="text-right py-1">
                        {flow.flow.date || flow.flow.accrual_end_date || '—'}
                      </td>
                      <td className="text-right py-1">
                        {formatNumber(flow.flow.amount, 2)}
                      </td>
                      <td className="text-right py-1 text-[#737373]">
                        {formatNumber(flow.flow.discount, 6)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
