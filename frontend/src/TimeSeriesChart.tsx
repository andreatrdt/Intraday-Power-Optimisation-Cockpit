import { useMemo, useState } from "react";
import {
  computeValueDomain,
  countPoints,
  emptyStateReason,
  formatYAxisTick,
  parseTimestamp,
  visibleSummary,
  visibleTimePoints,
  type DisplayTransform,
  type ReferenceBand,
} from "./chartDomain";
import { filterChartSeries, historyWindowLabels, type CustomWindow, type HistoryWindow } from "./historyWindow";
import type { ChartSeries, SourceMode } from "./types";

const colours = ["var(--chart-series-1)", "var(--chart-series-2)", "var(--chart-series-3)", "var(--chart-series-4)", "var(--chart-series-5)", "var(--chart-series-6)", "var(--chart-series-7)", "var(--chart-series-8)"];
const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1 });

function formatAxisTime(ms: number, spanMs: number): string {
  const options: Intl.DateTimeFormatOptions = spanMs <= 36 * 3600 * 1000
    ? { hour: "2-digit", minute: "2-digit", timeZone: "Europe/London" }
    : { day: "2-digit", month: "short", timeZone: "Europe/London" };
  return new Intl.DateTimeFormat("en-GB", options).format(new Date(ms));
}

/**
 * Historical time-series chart for the Live State page. The x-axis is wall-clock UK
 * time (not settlement periods), the window is chosen with the Today / Last 24h / 7d /
 * 30d / Custom control, and the y-domain is derived from the values actually visible.
 * It never renders a silent blank: when nothing is visible it shows an explicit empty
 * state (plus a development diagnostic panel explaining why).
 */
export function TimeSeriesChart({
  title, subtitle, insight, series, window, now, custom, sourceMode,
  includeZero = false, focusedScale = false, forecastBoundary = null, unavailableInsight,
  transform, referenceBands,
}: {
  title: string;
  subtitle: string;
  insight: string;
  series: ChartSeries[];
  window: HistoryWindow;
  now: string;
  custom?: CustomWindow;
  sourceMode: SourceMode;
  includeZero?: boolean;
  focusedScale?: boolean;
  forecastBoundary?: string | null;
  unavailableInsight?: string;
  transform?: DisplayTransform;
  referenceBands?: ReferenceBand[];
}) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const windowLabel = historyWindowLabels[window];

  const filtered = useMemo(() => filterChartSeries(series, window, now, custom), [series, window, now, custom]);
  const points = useMemo(() => visibleTimePoints(filtered, hidden), [filtered, hidden]);
  const rawCount = countPoints(series);
  const filteredCount = countPoints(filtered);
  const hiddenCount = filtered.filter((item) => hidden.has(item.key)).length;
  const reason = emptyStateReason({
    rawCount, filteredCount, visibleCount: points.length,
    seriesCount: filtered.length, hiddenCount, sourceMode, windowLabel,
  });

  const toggle = (key: string) => setHidden((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const legend = <details className="chart-legend-control" open><summary>Series visibility</summary>
    <div className="chart-legend">{series.map((item, index) => <button type="button" aria-pressed={!hidden.has(item.key)} key={item.key} onClick={() => toggle(item.key)}><i style={{ background: colours[index % colours.length] }} />{item.label} <em>{item.unit}</em></button>)}</div>
  </details>;

  const devPanel = import.meta.env.DEV ? <dl className="time-series-debug" aria-label={`${title} data diagnostics`}>
    <div><dt>raw points</dt><dd>{rawCount}</dd></div>
    <div><dt>filtered points</dt><dd>{filteredCount}</dd></div>
    <div><dt>visible points</dt><dd>{points.length}</dd></div>
    <div><dt>window</dt><dd>{windowLabel}</dd></div>
    <div><dt>source mode</dt><dd>{sourceMode}</dd></div>
    <div><dt>reason</dt><dd>{reason ?? "rendering visible data"}</dd></div>
  </dl> : null;

  if (points.length === 0) {
    return <article className={`panel time-series-card ${focusedScale ? "focused-scale" : ""}`} data-chart-title={title} aria-label={title}>
      <header><div><p className="eyebrow">HISTORICAL TIME SERIES</p><h3>{title}</h3><p className="chart-insight">{unavailableInsight ?? `${title} is unavailable for the selected window.`}</p></div><strong className="time-series-window">{windowLabel}</strong></header>
      {legend}
      <div className="time-series-empty" role="status">
        <strong>No data available for the selected window.</strong>
        <span>{reason}</span>
      </div>
      {devPanel}
    </article>;
  }

  const primaryKey = points[0].seriesKey;
  const primaryUnit = filtered.find((item) => item.key === primaryKey)?.unit ?? "";
  const toDisplay = transform ? transform.toDisplay : (value: number) => value;
  const displayUnit = transform ? transform.displayUnit : [...new Set(filtered.map((item) => item.unit))].join(" / ");
  const primaryPoints = points.filter((point) => point.seriesKey === primaryKey);
  const summary = transform
    ? transform.summarise(primaryPoints, windowLabel)
    : visibleSummary(primaryPoints, primaryUnit, windowLabel);

  const domain = computeValueDomain(points.map((point) => toDisplay(point.value)), includeZero || Boolean(transform)) ?? { min: 0, max: 1 };
  const tValues = points.map((point) => point.t);
  const tMin = Math.min(...tValues);
  const tMax = Math.max(...tValues);
  const spanMs = tMax - tMin;

  const width = 1260;
  const height = 430;
  const left = 92;
  const right = 44;
  const top = 30;
  const bottom = 54;
  const innerW = width - left - right;
  const innerH = height - top - bottom;
  const yScale = domain.max - domain.min || 1;
  const x = (t: number) => spanMs <= 0 ? left + innerW / 2 : left + (t - tMin) / spanMs * innerW;
  const y = (value: number) => top + (domain.max - value) / yScale * innerH;

  const activeSeries = filtered
    .filter((item) => !hidden.has(item.key))
    .map((item) => ({
      ...item,
      drawn: item.points
        .map((point) => ({ t: parseTimestamp(point.timestamp), raw: point.value, value: toDisplay(point.value), label: point.label }))
        .filter((point): point is { t: number; raw: number; value: number; label: string } => point.t !== null && Number.isFinite(point.value))
        .sort((first, second) => first.t - second.t),
    }))
    .filter((item) => item.drawn.length > 0);

  const barSeriesCount = activeSeries.filter((item) => item.kind === "bar" || item.kind === "waterfall").length;
  const maxPointsPerSeries = Math.max(1, ...activeSeries.map((item) => item.drawn.length));
  const slotWidth = innerW / Math.max(1, maxPointsPerSeries);
  const barWidth = Math.max(1, Math.min(18, slotWidth / Math.max(1, barSeriesCount + 0.5)));

  const boundaryMs = parseTimestamp(forecastBoundary);
  const boundaryX = boundaryMs !== null && spanMs > 0 && boundaryMs >= tMin && boundaryMs <= tMax ? x(boundaryMs) : null;

  const annotations = activeSeries
    .flatMap((item) => item.annotations ?? [])
    .map((annotation) => ({ ...annotation, t: parseTimestamp(annotation.timestamp) }))
    .filter((annotation): annotation is typeof annotation & { t: number } => annotation.t !== null && annotation.t >= tMin && annotation.t <= tMax)
    .filter((annotation, index, all) => all.findIndex((other) => other.label === annotation.label && other.t === annotation.t) === index)
    .slice(0, 6);

  const gridRatios = [0, 0.25, 0.5, 0.75, 1];
  const tickCount = 6;
  const ticks = Array.from({ length: tickCount }, (_, index) => tMin + (spanMs * index) / (tickCount - 1));
  const hasNominalBand = (referenceBands ?? []).some((band) => band.tone === "nominal" && band.min === band.max);
  const zeroInDomain = domain.min < 0 && domain.max > 0 && !hasNominalBand;
  const visibleBands = (referenceBands ?? []).filter((band) => band.max >= domain.min && band.min <= domain.max);

  let barIndex = -1;
  return <article className={`panel time-series-card interactive-chart ${focusedScale ? "focused-scale" : ""}`} data-chart-title={title} aria-label={title}>
    <header><div><p className="eyebrow">HISTORICAL TIME SERIES</p><h3>{title}</h3><p className="chart-insight">{insight}</p><p className="chart-subtitle">{subtitle}</p></div><strong className="time-series-window">{windowLabel}</strong></header>
    <p className="time-series-summary">{summary}</p>
    {legend}
    <div className="time-series-stage">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}. ${insight}. ${summary}.`}>
        <title>{title}</title><desc>{insight}. Historical time series across the selected {windowLabel} window.</desc>
        {visibleBands.map((band, index) => {
          if (band.min === band.max) {
            const yy = y(band.min);
            return <g key={`band-${index}`} className={`reference-line ${band.tone}`}><line x1={left} x2={width - right} y1={yy} y2={yy} /><text x={width - right - 4} y={yy - 4} textAnchor="end">{band.label}</text></g>;
          }
          const upperY = y(Math.min(band.max, domain.max));
          const lowerY = y(Math.max(band.min, domain.min));
          return <g key={`band-${index}`} className={`reference-band ${band.tone}`}><rect x={left} y={upperY} width={innerW} height={Math.max(1, lowerY - upperY)} /><text x={left + 6} y={upperY + 11}>{band.label}</text></g>;
        })}
        {gridRatios.map((ratio) => { const value = domain.max - ratio * yScale; const yy = top + ratio * innerH; return <g key={ratio}><line x1={left} x2={width - right} y1={yy} y2={yy} className="chart-gridline" /><text x={left - 10} y={yy + 4} textAnchor="end">{formatYAxisTick(value, displayUnit, domain.min, domain.max)}</text></g>; })}
        {zeroInDomain && <line x1={left} x2={width - right} y1={y(0)} y2={y(0)} className="chart-zero" />}
        <text x="18" y={top + innerH / 2} transform={`rotate(-90 18 ${top + innerH / 2})`} textAnchor="middle" className="axis-title">{displayUnit}</text>
        {boundaryX !== null && <g className="forecast-boundary"><line x1={boundaryX} x2={boundaryX} y1={top} y2={top + innerH} /><text x={boundaryX + 6} y={top + 12} className="region-label">FORECAST HORIZON START</text></g>}
        {annotations.map((annotation, index) => <g key={`${annotation.label}-${index}`} className="chart-annotation regime"><line x1={x(annotation.t)} x2={x(annotation.t)} y1={top} y2={top + innerH} /><text x={x(annotation.t) + 4} y={top + 24 + (index % 3) * 12}>{annotation.label}</text></g>)}
        {activeSeries.map((item) => {
          const seriesIndex = series.findIndex((candidate) => candidate.key === item.key);
          const colour = colours[Math.max(0, seriesIndex) % colours.length];
          if (item.kind === "bar" || item.kind === "waterfall") {
            barIndex += 1;
            const offset = (barIndex - (barSeriesCount - 1) / 2) * barWidth;
            const zero = y(0);
            return <g key={item.key}>{item.drawn.map((point, index) => { const yy = y(point.value); return <rect key={index} x={x(point.t) + offset - barWidth / 2} y={Math.min(zero, yy)} width={barWidth} height={Math.max(1, Math.abs(zero - yy))} fill={colour} opacity=".8"><title>{point.label}: {item.label} {number(point.value)} {item.unit}</title></rect>; })}</g>;
          }
          const path = item.drawn.map((point) => `${x(point.t)},${y(point.value)}`).join(" ");
          return <g key={item.key}>
            <polyline points={path} fill="none" stroke={colour} strokeWidth="2.2"><title>{item.label}</title></polyline>
            {transform && item.drawn.map((point, index) => <circle key={index} cx={x(point.t)} cy={y(point.value)} r="5" className="time-series-hit"><title>{point.label} · {transform.formatTooltip(point.raw)}</title></circle>)}
          </g>;
        })}
        {ticks.map((tick, index) => <text key={index} x={x(tick)} y={height - 24} textAnchor="middle">{formatAxisTime(tick, spanMs)}</text>)}
        <text x={left + innerW / 2} y={height - 6} textAnchor="middle" className="axis-title">UK time</text>
      </svg>
    </div>
    {devPanel}
  </article>;
}
