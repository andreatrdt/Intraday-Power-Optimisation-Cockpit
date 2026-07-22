import { useMemo, useState } from "react";
import type { ChartSeries } from "./types";

const colours = ["#56e0bd", "#7a9cff", "#f6b95f", "#ff7d8a", "#c77dff", "#7fd2ff", "#a5e36e", "#ff9f68"];
const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1 });

export function LargeChart({ title, subtitle, series, includeZero = false, safeBand, warning }: {
  title: string; subtitle: string; series: ChartSeries[]; includeZero?: boolean;
  safeBand?: { min: number; max: number; label: string }; warning?: string | null;
}) {
  const [hover, setHover] = useState<{ label: string; series: string; value: number; unit: string } | null>(null);
  const data = useMemo(() => series.filter((item) => item.points.length > 0), [series]);
  const values = data.flatMap((item) => item.points.map((point) => point.value));
  let min = Math.min(...values, includeZero ? 0 : Infinity);
  let max = Math.max(...values, includeZero ? 0 : -Infinity);
  if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
  const rawSpan = max - min;
  const pad = rawSpan === 0 ? Math.max(1, Math.abs(max) * 0.08) : rawSpan * 0.12;
  min -= pad; max += pad;
  if (safeBand) { min = Math.min(min, safeBand.min - pad * 0.2); max = Math.max(max, safeBand.max + pad * 0.2); }
  const span = max - min || 1;
  const width = 900, height = 300, left = 66, right = 22, top = 24, bottom = 44;
  const innerW = width - left - right, innerH = height - top - bottom;
  const maxPoints = Math.max(1, ...data.map((item) => item.points.length));
  const x = (index: number) => left + (maxPoints === 1 ? innerW / 2 : index * innerW / (maxPoints - 1));
  const y = (value: number) => top + (max - value) / span * innerH;
  const flat = values.length > 1 && Math.max(...values) - Math.min(...values) < 1e-8;
  const units = [...new Set(data.map((item) => item.unit))].join(" / ");
  const labels = data[0]?.points ?? [];
  return <article className="panel large-chart-card">
    <header><div><p className="eyebrow">CHART</p><h3>{title}</h3><p>{subtitle}</p></div><strong>{units}</strong></header>
    <div className="chart-legend">{data.map((item, index) => <span key={item.key}><i style={{ background: colours[index % colours.length] }} />{item.label}</span>)}</div>
    {warning && <div className="chart-warning">{warning}</div>}
    {flat && <div className="flat-label">Flat because the current solved path chose a constant value.</div>}
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}. Units ${units}.`}>
      {[0, .25, .5, .75, 1].map((ratio) => { const value = max - ratio * span; const yy = top + ratio * innerH; return <g key={ratio}><line x1={left} x2={width-right} y1={yy} y2={yy} className="chart-gridline" /><text x={left-10} y={yy+4} textAnchor="end">{number(value)}</text></g>; })}
      {safeBand && <><rect x={left} width={innerW} y={y(safeBand.max)} height={Math.max(1, y(safeBand.min)-y(safeBand.max))} className="safe-band" /><text x={left+8} y={y(safeBand.max)+15} className="safe-band-label">{safeBand.label}</text></>}
      {min < 0 && max > 0 && <line x1={left} x2={width-right} y1={y(0)} y2={y(0)} className="chart-zero" />}
      {data.map((item, seriesIndex) => {
        const color = colours[seriesIndex % colours.length];
        if (item.kind === "bar" || item.kind === "waterfall") {
          const groupWidth = innerW / Math.max(1, maxPoints); const barWidth = Math.max(3, groupWidth / (data.length + 1));
          return <g key={item.key}>{item.points.map((point, index) => { const xx = left + index * groupWidth + seriesIndex * barWidth + 2; const zero = y(0); const yy = y(point.value); return <rect key={`${point.label}-${index}`} x={xx} y={Math.min(zero, yy)} width={barWidth-2} height={Math.max(2, Math.abs(zero-yy))} fill={color} opacity=".82" onMouseEnter={() => setHover({ label: point.label, series: item.label, value: point.value, unit: item.unit })} onMouseLeave={() => setHover(null)}><title>{point.label}: {item.label} {number(point.value)} {item.unit}</title></rect>; })}</g>;
        }
        const points = item.points.map((point, index) => `${x(index)},${y(point.value)}`).join(" ");
        return <g key={item.key}><polyline points={points} fill="none" stroke={color} strokeWidth="3" />{item.points.map((point, index) => <circle key={`${point.label}-${index}`} cx={x(index)} cy={y(point.value)} r="4" fill={color} onMouseEnter={() => setHover({ label: point.label, series: item.label, value: point.value, unit: item.unit })} onMouseLeave={() => setHover(null)}><title>{point.label}: {item.label} {number(point.value)} {item.unit}</title></circle>)}</g>;
      })}
      {labels.map((point, index) => index % Math.max(1, Math.ceil(labels.length / 8)) === 0 ? <text key={`${point.label}-${index}`} x={x(index)} y={height-15} textAnchor="middle">{point.label}</text> : null)}
    </svg>
    <footer><span>Start: {data.map((item) => `${item.label} ${number(item.points[0]?.value ?? 0)}`).join(" · ")}</span><span>End: {data.map((item) => `${item.label} ${number(item.points.at(-1)?.value ?? 0)}`).join(" · ")}</span></footer>
    {hover && <div className="chart-tooltip"><strong>{hover.label}</strong><span>{hover.series}: {number(hover.value)} {hover.unit}</span></div>}
  </article>;
}
