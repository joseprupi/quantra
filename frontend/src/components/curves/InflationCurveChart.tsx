import { useMemo } from 'react';
import type { BootstrapCurveResult } from '../../lib/quantra-types';
import type { InflationCurveMeasure, InflationPointWrapper } from '../../lib/types';

interface Props {
  points: InflationPointWrapper[];
  bootstrapResult?: BootstrapCurveResult | null;
  showMeasure: InflationCurveMeasure;
  height?: number;
}

interface PlotPoint {
  x: number;
  y: number;
  label: string;
}

function toYears(n: number, unit: string) {
  switch (unit) {
    case 'Days': return n / 365;
    case 'Weeks': return n / 52;
    case 'Months': return n / 12;
    case 'Years': return n;
    default: return n;
  }
}

export default function InflationCurveChart({ points, bootstrapResult, showMeasure, height = 320 }: Props) {
  const inputPoints = useMemo<PlotPoint[]>(() => (
    points
      .map((wrapper) => {
        const tenor = wrapper.point.tenor;
        if (!tenor) return null;
        return {
          x: toYears(tenor.n, tenor.unit),
          y: (wrapper.point.quote_value ?? 0) * 100,
          label: `${tenor.n}${tenor.unit[0]}`,
        };
      })
      .filter((point): point is PlotPoint => point !== null)
      .sort((a, b) => a.x - b.x)
  ), [points]);

  const bootstrappedPoints = useMemo<PlotPoint[]>(() => {
    if (!bootstrapResult || bootstrapResult.error) return [];
    const series = bootstrapResult.series.find((entry) => entry.measure === showMeasure);
    if (!series) return [];
    const referenceDate = new Date(bootstrapResult.reference_date);
    return bootstrapResult.grid_dates.map((date, index) => {
      const gridDate = new Date(date);
      return {
        x: (gridDate.getTime() - referenceDate.getTime()) / (365.25 * 24 * 60 * 60 * 1000),
        y: (series.values[index] ?? 0) * 100,
        label: date,
      };
    }).filter((point) => point.x > 0);
  }, [bootstrapResult, showMeasure]);

  const allPoints = [...inputPoints, ...bootstrappedPoints];
  if (allPoints.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 bg-[#f5f5f5] rounded-lg border border-[#e5e5e5]">
        <p className="text-[#a3a3a3] text-sm">Add inflation helpers to see visualization</p>
      </div>
    );
  }

  const width = 600;
  const padding = { top: 20, right: 30, bottom: 45, left: 55 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const xMax = Math.max(...allPoints.map((point) => point.x)) * 1.05;
  const yValues = allPoints.map((point) => point.y);
  const yMin = Math.min(...yValues) * 0.98;
  const yMax = Math.max(...yValues) * 1.02;
  const xScale = (x: number) => padding.left + (x / xMax) * chartWidth;
  const yScale = (y: number) => padding.top + chartHeight - ((y - yMin) / Math.max(yMax - yMin, 1e-6)) * chartHeight;
  const curvePath = bootstrappedPoints.length > 1
    ? bootstrappedPoints.map((point, index) => `${index === 0 ? 'M' : 'L'} ${xScale(point.x)} ${yScale(point.y)}`).join(' ')
    : '';

  return (
    <div className="bg-white rounded-lg border border-[#e5e5e5] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-[#0a0a0a]">
          {bootstrapResult ? 'Bootstrapped Inflation Curve' : 'Inflation Helpers'}
        </h3>
        <span className="text-xs text-[#737373]">
          {showMeasure === 'ZeroRate' ? 'Zero Rate (%)' : 'YoY Rate (%)'}
        </span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" style={{ maxHeight: height }}>
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
        {inputPoints.map((point, index) => (
          <g key={index}>
            <circle cx={xScale(point.x)} cy={yScale(point.y)} r={6} fill="#2563eb" stroke="white" strokeWidth={2} />
            <title>{`${point.label}: ${point.y.toFixed(3)}%`}</title>
          </g>
        ))}
        <line x1={padding.left} y1={height - padding.bottom} x2={width - padding.right} y2={height - padding.bottom} stroke="#d4d4d4" />
        <line x1={padding.left} y1={padding.top} x2={padding.left} y2={height - padding.bottom} stroke="#d4d4d4" />
      </svg>
    </div>
  );
}
