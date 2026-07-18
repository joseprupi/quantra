// Curve visualization component using SVG
// Shows both input points and bootstrapped curve from API
import { useMemo } from 'react';
import { CurvePoint } from '../../lib/types';
import { BootstrapCurveResult } from '../../lib/quantra-types';

interface CurveChartProps {
  points: CurvePoint[];
  bootstrapResult?: BootstrapCurveResult | null;
  height?: number;
  showMeasure?: 'ZERO' | 'FWD' | 'DF';
}

interface PlotPoint {
  x: number; // years from reference date
  y: number; // rate or DF
  label: string;
  type: string;
}

// Convert curve input points to plottable data
function getInputPoints(points: CurvePoint[]): PlotPoint[] {
  const result: PlotPoint[] = [];
  for (const p of points) {
    // A malformed / foreign-shaped point (e.g. a curve written through the
    // API by another client) must never blank the whole page — skip it.
    try {
      let tenor: number;
      let tenorLabel: string;
      let rate: number;

      if (p.point_type === 'DepositHelper') {
        const { tenor_number, tenor_time_unit } = p.point;
        tenorLabel = `${tenor_number}${tenor_time_unit[0]}`;
        tenor = convertToYears(tenor_number, tenor_time_unit);
        rate = (p.point.rate ?? 0) * 100; // Convert to percentage
      } else if (p.point_type === 'SwapHelper') {
        const { tenor_number, tenor_time_unit } = p.point;
        tenorLabel = `${tenor_number}${tenor_time_unit[0]}`;
        tenor = convertToYears(tenor_number, tenor_time_unit);
        rate = (p.point.rate ?? 0) * 100; // Convert to percentage
      } else if (p.point_type === 'FRAHelper') {
        const { months_to_end } = p.point;
        tenorLabel = `${months_to_end}M`;
        tenor = months_to_end / 12;
        rate = (p.point.rate ?? 0) * 100; // Convert to percentage
      } else if (p.point_type === 'FutureHelper') {
        const { future_months } = p.point;
        tenorLabel = `${future_months}M Fut`;
        tenor = future_months / 12;
        // Futures use price convention: price = 100 - rate
        // So rate = 100 - price (already in percentage terms)
        rate = 100 - (p.point.rate ?? 0);
      } else if (p.point_type === 'BondHelper') {
        const termDate = new Date(p.point.schedule.termination_date);
        const today = new Date();
        tenor = (termDate.getTime() - today.getTime()) / (365.25 * 24 * 60 * 60 * 1000);
        tenorLabel = `Bond ${Math.round(tenor)}Y`;
        // Bond rate field is price, not rate - show coupon rate instead for visualization
        rate = p.point.coupon_rate * 100;
      } else if (p.point_type === 'OISHelper') {
        const { tenor_number, tenor_time_unit } = p.point;
        tenorLabel = `${tenor_number}${(tenor_time_unit || '')[0] || ''} OIS`;
        tenor = convertToYears(tenor_number, tenor_time_unit || 'Years');
        rate = (p.point.rate ?? 0) * 100;
      } else if (p.point_type === 'DatedOISHelper') {
        const startDate = new Date(p.point.start_date);
        const endDate = new Date(p.point.end_date);
        tenor = (endDate.getTime() - startDate.getTime()) / (365.25 * 24 * 60 * 60 * 1000);
        tenorLabel = `DOIS`;
        rate = (p.point.rate ?? 0) * 100;
      } else {
        continue;
      }

      if (!Number.isFinite(tenor)) continue;
      result.push({ x: tenor, y: rate, label: tenorLabel, type: p.point_type });
    } catch {
      continue;
    }
  }
  return result.sort((a, b) => a.x - b.x);
}

// Convert bootstrap result to plottable curve
function getBootstrappedCurve(
  result: BootstrapCurveResult, 
  measure: 'ZERO' | 'FWD' | 'DF',
  referenceDate: Date
): PlotPoint[] {
  // FlatBuffers default: if measure is missing/empty, it's DF
  const series = result.series.find(s => {
    if (measure === 'DF') {
      return s.measure === 'DF' || !s.measure;
    }
    return s.measure === measure;
  });
  
  if (!series) return [];
  
  return result.grid_dates.map((dateStr, i) => {
    const date = new Date(dateStr);
    const years = (date.getTime() - referenceDate.getTime()) / (365.25 * 24 * 60 * 60 * 1000);
    const value = measure === 'DF' ? series.values[i] : series.values[i] * 100; // Convert rates to %
    
    return {
      x: years,
      y: value,
      label: dateStr,
      type: 'bootstrapped',
    };
  }).filter(p => p.x > 0); // Filter out reference date
}

function convertToYears(value: number, unit: string): number {
  switch (unit) {
    case 'Days': return value / 365;
    case 'Weeks': return value / 52;
    case 'Months': return value / 12;
    case 'Years': return value;
    default: return value;
  }
}

export default function CurveChart({ 
  points, 
  bootstrapResult, 
  height = 300,
  showMeasure = 'ZERO'
}: CurveChartProps) {
  const inputPoints = useMemo(() => getInputPoints(points), [points]);
  
  const bootstrappedCurve = useMemo(() => {
    if (!bootstrapResult || bootstrapResult.error) return [];
    const refDate = new Date(bootstrapResult.reference_date);
    return getBootstrappedCurve(bootstrapResult, showMeasure, refDate);
  }, [bootstrapResult, showMeasure]);
  
  const hasData = inputPoints.length > 0 || bootstrappedCurve.length > 0;
  
  if (!hasData) {
    return (
      <div className="flex items-center justify-center h-64 bg-[#f5f5f5] rounded-lg border border-[#e5e5e5]">
        <p className="text-[#a3a3a3] text-sm">Add curve points to see visualization</p>
      </div>
    );
  }
  
  // Chart dimensions
  const width = 600;
  const padding = { top: 20, right: 30, bottom: 45, left: 55 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  
  // Combine all points for scale calculation
  const allPoints = [...inputPoints, ...bootstrappedCurve];
  
  // Calculate scales
  // x range starts at 0
  const xMax = Math.max(...allPoints.map(p => p.x)) * 1.05;
  const yValues = allPoints.map(p => p.y);
  const yMin = Math.min(...yValues) * 0.98;
  const yMax = Math.max(...yValues) * 1.02;
  
  const xScale = (x: number) => padding.left + (x / xMax) * chartWidth;
  const yScale = (y: number) => padding.top + chartHeight - ((y - yMin) / (yMax - yMin)) * chartHeight;
  
  // Generate bootstrapped curve path
  const curvePath = bootstrappedCurve.length > 1
    ? bootstrappedCurve
        .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xScale(p.x)} ${yScale(p.y)}`)
        .join(' ')
    : '';
  
  // Area fill for bootstrapped curve
  const areaPath = bootstrappedCurve.length > 1
    ? `${curvePath} L ${xScale(bootstrappedCurve[bootstrappedCurve.length - 1].x)} ${yScale(yMin)} L ${xScale(bootstrappedCurve[0].x)} ${yScale(yMin)} Z`
    : '';
  
  // Y-axis ticks
  const yTicks = 5;
  const yTickValues = Array.from({ length: yTicks }, (_, i) => yMin + (i * (yMax - yMin)) / (yTicks - 1));
  
  // X-axis ticks
  const xTickCount = 6;
  const xTickValues = Array.from({ length: xTickCount }, (_, i) => (i * xMax) / (xTickCount - 1));
  
  // Point colors by type
  const getPointColor = (type: string) => {
    switch (type) {
      case 'DepositHelper': return '#8a6a2f';
      case 'SwapHelper': return '#2563eb';
      case 'FRAHelper': return '#16a34a';
      case 'FutureHelper': return '#9333ea';
      case 'BondHelper': return '#ea580c';
      default: return '#737373';
    }
  };
  
  const measureLabels = {
    ZERO: 'Zero Rate (%)',
    FWD: 'Forward Rate (%)',
    DF: 'Discount Factor',
  };
  
  return (
    <div className="bg-white rounded-lg border border-[#e5e5e5] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-[#0a0a0a]">
          {bootstrapResult ? 'Bootstrapped Curve' : 'Input Points'}
        </h3>
        <div className="flex items-center gap-3 text-xs flex-wrap">
          {bootstrapResult && (
            <span className="flex items-center gap-1.5">
              <span className="w-6 h-0.5 bg-[#8a6a2f]"></span>
              <span className="text-[#737373]">{measureLabels[showMeasure]}</span>
            </span>
          )}
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-[#8a6a2f]"></span>
            <span className="text-[#737373]">Deposit</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-[#2563eb]"></span>
            <span className="text-[#737373]">Swap</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-[#16a34a]"></span>
            <span className="text-[#737373]">FRA</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-[#9333ea]"></span>
            <span className="text-[#737373]">Future</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-[#ea580c]"></span>
            <span className="text-[#737373]">Bond</span>
          </div>
        </div>
      </div>
      
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" style={{ maxHeight: height }}>
        {/* Grid lines */}
        <g className="text-[#e5e5e5]">
          {yTickValues.map((tick, i) => (
            <line
              key={i}
              x1={padding.left}
              y1={yScale(tick)}
              x2={width - padding.right}
              y2={yScale(tick)}
              stroke="currentColor"
              strokeDasharray="4 4"
            />
          ))}
        </g>
        
        {/* Gradient definition */}
        <defs>
          <linearGradient id="curveGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#8a6a2f" stopOpacity={0.3} />
            <stop offset="100%" stopColor="#8a6a2f" stopOpacity={0} />
          </linearGradient>
        </defs>
        
        {/* Area fill for bootstrapped curve */}
        {areaPath && (
          <path
            d={areaPath}
            fill="url(#curveGradient)"
          />
        )}
        
        {/* Bootstrapped curve line */}
        {curvePath && (
          <path
            d={curvePath}
            fill="none"
            stroke="#8a6a2f"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        )}
        
        {/* Input data points */}
        {inputPoints.map((point, i) => (
          <g key={i}>
            <circle
              cx={xScale(point.x)}
              cy={yScale(point.y)}
              r={6}
              fill={getPointColor(point.type)}
              stroke="white"
              strokeWidth={2}
            />
            <title>{`${point.label}: ${point.y.toFixed(3)}%`}</title>
          </g>
        ))}
        
        {/* Pillar date markers (if available) */}
        {bootstrapResult?.pillar_dates?.map((dateStr, i) => {
          const date = new Date(dateStr);
          const refDate = new Date(bootstrapResult.reference_date);
          const years = (date.getTime() - refDate.getTime()) / (365.25 * 24 * 60 * 60 * 1000);
          if (years <= 0 || years > xMax) return null;
          
          return (
            <line
              key={i}
              x1={xScale(years)}
              y1={padding.top}
              x2={xScale(years)}
              y2={height - padding.bottom}
              stroke="#d4d4d4"
              strokeWidth={1}
              strokeDasharray="2 4"
            />
          );
        })}
        
        {/* X-axis */}
        <line
          x1={padding.left}
          y1={height - padding.bottom}
          x2={width - padding.right}
          y2={height - padding.bottom}
          stroke="#d4d4d4"
        />
        
        {/* Y-axis */}
        <line
          x1={padding.left}
          y1={padding.top}
          x2={padding.left}
          y2={height - padding.bottom}
          stroke="#d4d4d4"
        />
        
        {/* X-axis labels */}
        {xTickValues.map((val, i) => (
          <text
            key={i}
            x={xScale(val)}
            y={height - padding.bottom + 20}
            textAnchor="middle"
            className="text-[10px] fill-[#737373]"
          >
            {val.toFixed(val < 1 ? 1 : 0)}Y
          </text>
        ))}
        
        {/* X-axis title */}
        <text
          x={padding.left + chartWidth / 2}
          y={height - 5}
          textAnchor="middle"
          className="text-[11px] fill-[#737373]"
        >
          Tenor (Years)
        </text>
        
        {/* Y-axis labels */}
        {yTickValues.map((tick, i) => (
          <text
            key={i}
            x={padding.left - 10}
            y={yScale(tick) + 4}
            textAnchor="end"
            className="text-[10px] fill-[#737373]"
          >
            {showMeasure === 'DF' ? tick.toFixed(4) : tick.toFixed(2) + '%'}
          </text>
        ))}
        
        {/* Y-axis title */}
        <text
          x={15}
          y={padding.top + chartHeight / 2}
          textAnchor="middle"
          className="text-[11px] fill-[#737373]"
          transform={`rotate(-90, 15, ${padding.top + chartHeight / 2})`}
        >
          {measureLabels[showMeasure]}
        </text>
      </svg>
      
      {/* Bootstrap error message */}
      {bootstrapResult?.error && (
        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-600">
          {bootstrapResult.error.error_message}
        </div>
      )}
    </div>
  );
}
