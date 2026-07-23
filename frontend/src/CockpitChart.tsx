import { useEffect, useMemo, useState, type ReactNode } from "react";
import { formatYAxisTick } from "./chartDomain";
import type { AuctionPathPhase, ChartAnnotation, ChartPoint, ChartSeries } from "./types";

const colours = ["var(--chart-series-1)", "var(--chart-series-2)", "var(--chart-series-3)", "var(--chart-series-4)", "var(--chart-series-5)", "var(--chart-series-6)", "var(--chart-series-7)", "var(--chart-series-8)"];
const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1 });

export interface InteractiveChartPeriod {
  id: string;
  label: string;
  timestamp: string;
  phase: AuctionPathPhase;
  deliveryLabel: string;
}

export interface ChartTrack {
  label: string;
  unit: string;
  keys: string[];
}

export function FlatSeriesSummary({ title, insight, series }: { title: string; insight: string; series: ChartSeries[] }) {
  return <article className="panel flat-series-summary" aria-label={title}>
    <div><p className="eyebrow">COMPACT PATH</p><h3>{title}</h3><p className="chart-insight">{insight}</p></div>
    <div className="flat-series-values">{series.map((item) => <div key={item.key}><span>{item.label}</span><strong>{number(item.points.at(-1)?.value ?? 0)} {item.unit}</strong></div>)}</div>
    <p className="flat-series-reason">{[...new Set(series.map((item) => item.flat_explanation).filter(Boolean))].join(" ")}</p>
  </article>;
}

export function LargeChart({
  title, subtitle, insight, series, periods, tracks, includeZero = false, safeBand, warning,
  nowMarker, windowStart, windowEnd, band, focusedScale = false, featured = false,
  hoveredPeriod, selectedPeriod, onHoverPeriod, onSelectPeriod, tooltipContent,
}: {
  title: string;
  subtitle: string;
  insight: string;
  series: ChartSeries[];
  periods?: InteractiveChartPeriod[];
  tracks?: ChartTrack[];
  includeZero?: boolean;
  safeBand?: { min: number; max: number; label: string; trackKey?: string };
  warning?: string | null;
  forecastBoundary?: string | null;
  nowMarker?: string | null;
  windowStart?: string | null;
  windowEnd?: string | null;
  band?: { lowerKey: string; upperKey: string; label: string };
  focusedScale?: boolean;
  featured?: boolean;
  hoveredPeriod?: string | null;
  selectedPeriod?: string | null;
  onHoverPeriod?: (periodId: string | null) => void;
  onSelectPeriod?: (periodId: string) => void;
  tooltipContent?: (periodId: string) => ReactNode;
}) {
  const fallbackPeriods = useMemo<InteractiveChartPeriod[]>(() => {
    const points = series.reduce<ChartPoint[]>((longest, item) => item.points.length > longest.length ? item.points : longest, []);
    return points.filter((point) => point.delivery_period && point.timestamp).map((point) => ({
      id: point.delivery_period as string, label: point.label, timestamp: point.timestamp as string,
      phase: "optimised_future", deliveryLabel: point.label,
    }));
  }, [series]);
  const allPeriods = periods?.length ? periods : fallbackPeriods;
  const [rangeStart, setRangeStart] = useState(0);
  const [rangeEnd, setRangeEnd] = useState(Math.max(0, allPeriods.length - 1));
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState(false);

  useEffect(() => { setRangeStart(0); setRangeEnd(Math.max(0, allPeriods.length - 1)); }, [allPeriods.length]);
  useEffect(() => {
    document.body.classList.toggle("chart-fullscreen-open", expanded);
    return () => document.body.classList.remove("chart-fullscreen-open");
  }, [expanded]);

  const visiblePeriods = allPeriods.slice(rangeStart, rangeEnd + 1);
  const visibleIds = new Set(visiblePeriods.map((period) => period.id));
  const activeSeries = series.filter((item) => !hidden.has(item.key)).map((item) => ({
    ...item, points: item.points.filter((point) => point.delivery_period && visibleIds.has(point.delivery_period)),
  })).filter((item) => item.points.length > 0);
  const chartTracks: ChartTrack[] = tracks?.length ? tracks : [{ label: title, unit: [...new Set(activeSeries.map((item) => item.unit))].join(" / "), keys: activeSeries.map((item) => item.key) }];
  const width = 1260;
  const height = featured ? 600 : 510;
  const left = 90;
  const right = 42;
  const top = 40;
  const bottom = 64;
  const innerW = width - left - right;
  const innerH = height - top - bottom;
  const trackGap = chartTracks.length > 1 ? 16 : 0;
  const trackHeight = (innerH - trackGap * (chartTracks.length - 1)) / Math.max(1, chartTracks.length);
  const periodIndex = new Map(visiblePeriods.map((period, index) => [period.id, index]));
  const xForId = (id: string) => {
    const index = periodIndex.get(id) ?? 0;
    return left + (visiblePeriods.length <= 1 ? innerW / 2 : index * innerW / (visiblePeriods.length - 1));
  };

  const trackStats = chartTracks.map((track, trackIndex) => {
    const values = activeSeries.filter((item) => track.keys.includes(item.key)).flatMap((item) => item.points.map((point) => point.value));
    let min = Math.min(...values, includeZero ? 0 : Infinity);
    let max = Math.max(...values, includeZero ? 0 : -Infinity);
    if (safeBand && (!safeBand.trackKey || track.keys.includes(safeBand.trackKey))) { min = Math.min(min, safeBand.min); max = Math.max(max, safeBand.max); }
    if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
    const rawSpan = max - min;
    const pad = rawSpan === 0 ? Math.max(.25, Math.abs(max) * .08) : rawSpan * .1;
    min -= pad; max += pad;
    const trackTop = top + trackIndex * (trackHeight + trackGap);
    return { ...track, min, max, span: max - min || 1, top: trackTop, bottom: trackTop + trackHeight };
  });
  const trackForKey = (key: string) => trackStats.find((track) => track.keys.includes(key)) ?? trackStats[0];
  const y = (key: string, value: number) => { const track = trackForKey(key); return track.top + (track.max - value) / track.span * trackHeight; };
  const activePeriod = hoveredPeriod ?? selectedPeriod;
  const activeMeta = activePeriod ? allPeriods.find((period) => period.id === activePeriod) : null;
  const activeX = activePeriod && visibleIds.has(activePeriod) ? xForId(activePeriod) : null;
  const flatExplanations = [...new Set(activeSeries.map((item) => item.flat_explanation).filter((item): item is string => Boolean(item)))];
  const annotations = activeSeries.flatMap((item) => item.annotations ?? []).filter((item, index, all) => all.findIndex((other) => other.label === item.label && other.timestamp === item.timestamp) === index)
    .filter((annotation) => annotation.timestamp && visiblePeriods.some((period) => period.timestamp === annotation.timestamp));
  const futureIndex = visiblePeriods.findIndex((period) => period.phase === "optimised_future");
  const futureX = futureIndex >= 0 ? xForId(visiblePeriods[futureIndex].id) : null;
  const lowerBand = band ? activeSeries.find((item) => item.key === band.lowerKey) : undefined;
  const upperBand = band ? activeSeries.find((item) => item.key === band.upperKey) : undefined;
  const bandPolygon = lowerBand && upperBand ? [
    ...upperBand.points.map((point) => `${xForId(point.delivery_period as string)},${y(upperBand.key, point.value)}`),
    ...lowerBand.points.map((point) => `${xForId(point.delivery_period as string)},${y(lowerBand.key, point.value)}`).reverse(),
  ].join(" ") : null;

  const setRange = (start: number, end: number) => {
    const last = Math.max(0, allPeriods.length - 1);
    setRangeStart(Math.max(0, Math.min(start, last)));
    setRangeEnd(Math.max(0, Math.min(Math.max(start, end), last)));
  };
  const quickRange = (mode: "full" | "history" | "future" | "now" | "next6" | "next12") => {
    const last = allPeriods.length - 1;
    const historical = allPeriods.map((period, index) => ({ period, index })).filter(({ period }) => period.phase.startsWith("historical_"));
    const future = allPeriods.map((period, index) => ({ period, index })).filter(({ period }) => period.phase === "optimised_future");
    const nowIndex = Math.max(0, allPeriods.findIndex((period) => period.phase === "current"));
    if (mode === "history" && historical.length) setRange(historical[0].index, historical.at(-1)!.index);
    else if (mode === "future" && future.length) setRange(future[0].index, future.at(-1)!.index);
    else if (mode === "now") setRange(nowIndex - 3, nowIndex + 3);
    else if (mode === "next6") setRange(nowIndex, nowIndex + 5);
    else if (mode === "next12") setRange(nowIndex, nowIndex + 11);
    else setRange(0, last);
  };
  const pan = (direction: -1 | 1) => {
    const size = rangeEnd - rangeStart;
    let start = rangeStart + direction * Math.max(1, Math.round((size + 1) / 3));
    start = Math.max(0, Math.min(start, allPeriods.length - 1 - size));
    setRange(start, start + size);
  };
  const markerX = (timestamp: string | null | undefined) => {
    if (!timestamp || !visiblePeriods.length) return null;
    const target = new Date(timestamp).getTime();
    const start = new Date(visiblePeriods[0].timestamp).getTime();
    const end = new Date(visiblePeriods.at(-1)!.timestamp).getTime();
    if (end <= start) return left;
    return Math.max(left, Math.min(left + innerW, left + (target - start) / (end - start) * innerW));
  };
  const nowX = markerX(nowMarker);
  const startBoundaryX = markerX(windowStart);
  const endBoundaryX = markerX(windowEnd);
  const rangeLabel = visiblePeriods.length ? `${visiblePeriods[0].label}–${visiblePeriods.at(-1)!.label} · ${visiblePeriods.length} SPs` : "No SPs";

  return <article className={`panel large-chart-card interactive-chart ${focusedScale ? "focused-scale" : ""} ${featured ? "featured-path-chart" : "supporting-path-chart"} ${expanded ? "chart-expanded" : ""}`} data-focused-scale={focusedScale ? "true" : "false"} data-chart-title={title}>
    <header><div><p className="eyebrow">INTERACTIVE DECISION PATH</p><h3>{title}</h3><p className="chart-insight">{insight}</p><p className="chart-subtitle">{subtitle}</p></div><div className="chart-header-actions"><strong>{rangeLabel}</strong><button type="button" onClick={() => setExpanded(!expanded)}>{expanded ? "Close full-screen" : "Expand / full-screen"}</button></div></header>
    <div className="chart-range-controls" aria-label={`${title} range controls`}>
      <button type="button" onClick={() => quickRange("full")}>Full auction window</button><button type="button" onClick={() => quickRange("history")}>Historical only</button><button type="button" onClick={() => quickRange("future")}>Future only</button><button type="button" onClick={() => quickRange("now")}>Around NOW</button><button type="button" onClick={() => quickRange("next6")}>Next 6 SPs</button><button type="button" onClick={() => quickRange("next12")}>Next 12 SPs</button><button type="button" onClick={() => pan(-1)} disabled={rangeStart === 0}>Pan left</button><button type="button" onClick={() => pan(1)} disabled={rangeEnd >= allPeriods.length - 1}>Pan right</button><button type="button" className="reset-zoom" onClick={() => quickRange("full")}>Reset zoom</button>
    </div>
    <details className="chart-legend-control" open><summary>Series visibility</summary><div className="chart-legend">{series.map((item, index) => <button type="button" aria-pressed={!hidden.has(item.key)} key={item.key} onClick={() => setHidden((current) => { const next = new Set(current); if (next.has(item.key)) next.delete(item.key); else next.add(item.key); return next; })}><i style={{ background: colours[index % colours.length] }} />{item.label} <em>{item.unit}</em></button>)}</div></details>
    {warning && <div className="chart-warning">{warning}</div>}
    {flatExplanations.length > 0 && <div className="flat-label">{flatExplanations.join(" ")}</div>}
    <div className="interactive-chart-stage">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}. ${insight}. Showing ${rangeLabel}.`} onMouseLeave={() => onHoverPeriod?.(null)}>
        <title>{title}</title><desc>{insight}. Use range controls, series toggles and settlement-period inspection.</desc>
        {futureX !== null && <><rect x={left} y={top} width={Math.max(0, futureX-left)} height={innerH} className="historical-simulated-region" /><rect x={futureX} y={top} width={Math.max(0, width - right - futureX)} height={innerH} className="forecast-region" /><text x={futureX + 8} y={top + 14} className="region-label">CURRENT OPTIMISATION PROJECTION</text>{futureX > left + 150 && <text x={futureX - 8} y={top + 14} textAnchor="end" className="region-label">HISTORICAL SIMULATED ACTIONS</text>}</>}
        {trackStats.map((track, trackIndex) => <g key={track.label} className="chart-track">
          {trackIndex % 2 === 1 && <rect x={left} y={track.top} width={innerW} height={trackHeight} className="chart-track-alt" />}
          {[0, .5, 1].map((ratio) => { const value = track.max - ratio * track.span; const yy = track.top + ratio * trackHeight; return <g key={ratio}><line x1={left} x2={width-right} y1={yy} y2={yy} className="chart-gridline" /><text x={left-10} y={yy+4} textAnchor="end">{formatYAxisTick(value, track.unit, track.min, track.max)}</text></g>; })}
          <text x="17" y={track.top + trackHeight / 2} transform={`rotate(-90 17 ${track.top + trackHeight / 2})`} textAnchor="middle" className="axis-title">{track.unit}</text>
          <text x={left + 7} y={track.top + 16} className="track-title">{track.label}</text>
          {track.min < 0 && track.max > 0 && <line x1={left} x2={width-right} y1={y(track.keys[0], 0)} y2={y(track.keys[0], 0)} className="chart-zero" />}
        </g>)}
        {safeBand && (() => { const key = safeBand.trackKey ?? activeSeries[0]?.key ?? ""; const track = trackForKey(key); return <><rect x={left} width={innerW} y={y(key, safeBand.max)} height={Math.max(1, y(key, safeBand.min)-y(key, safeBand.max))} className="safe-band" /><text x={left+8} y={Math.max(track.top + 15, y(key, safeBand.max)+15)} className="safe-band-label">{safeBand.label}</text></>; })()}
        {bandPolygon && <polygon points={bandPolygon} className="chart-fan-band"><title>{band?.label}</title></polygon>}
        {activeSeries.map((item) => {
          const seriesIndex = series.findIndex((candidate) => candidate.key === item.key);
          const colour = colours[Math.max(0, seriesIndex) % colours.length];
          const itemTrack = trackForKey(item.key);
          if (item.kind === "bar" || item.kind === "waterfall") {
            const barSeries = activeSeries.filter((candidate) => (candidate.kind === "bar" || candidate.kind === "waterfall") && trackForKey(candidate.key).label === itemTrack.label);
            const barIndex = barSeries.findIndex((candidate) => candidate.key === item.key);
            const groupWidth = innerW / Math.max(1, visiblePeriods.length);
            const barWidth = Math.max(3, Math.min(20, groupWidth / Math.max(1, barSeries.length + .5)));
            return <g key={item.key}>{item.points.map((point) => { const xx = xForId(point.delivery_period as string) - (barSeries.length * barWidth) / 2 + barIndex * barWidth; const zero = y(item.key, 0); const yy = y(item.key, point.value); return <rect key={point.delivery_period} x={xx} y={Math.min(zero, yy)} width={barWidth} height={Math.max(2, Math.abs(zero-yy))} fill={colour} opacity=".82"><title>{point.label}: {item.label} {number(point.value)} {item.unit}</title></rect>; })}</g>;
          }
          const path = item.points.map((point) => `${xForId(point.delivery_period as string)},${y(item.key, point.value)}`).join(" ");
          return <g key={item.key}><polyline points={path} fill="none" stroke={colour} strokeWidth="2.5" />{item.points.map((point) => <circle key={point.delivery_period} cx={xForId(point.delivery_period as string)} cy={y(item.key, point.value)} r="2.8" fill={colour}><title>{point.label}: {item.label} {number(point.value)} {item.unit}</title></circle>)}</g>;
        })}
        {annotations.slice(0, 6).map((annotation, index) => <AnnotationMark key={`${annotation.label}-${index}`} annotation={annotation} x={markerX(annotation.timestamp) ?? left} top={top} bottom={top + innerH} labelY={top + 32 + index % 3 * 13} />)}
        {startBoundaryX !== null && <BoundaryMark x={startBoundaryX} top={top} bottom={top+innerH} label="PREVIOUS 15:00 AUCTION" align="start" />}
        {endBoundaryX !== null && <BoundaryMark x={endBoundaryX} top={top} bottom={top+innerH} label="NEXT 15:00 AUCTION" align="end" />}
        {nowX !== null && <g className="now-marker"><line x1={nowX} x2={nowX} y1={top} y2={top + innerH} /><rect x={Math.max(left, Math.min(width-right-48, nowX - 24))} y={top - 24} width="48" height="18" rx="3" /><text x={Math.max(left+24, Math.min(width-right-24, nowX))} y={top - 11} textAnchor="middle">NOW</text></g>}
        {activeX !== null && <g className={`sp-crosshair ${selectedPeriod === activePeriod ? "pinned" : ""}`}><rect x={activeX - Math.max(5, innerW / Math.max(1, visiblePeriods.length) / 2)} y={top} width={Math.max(10, innerW / Math.max(1, visiblePeriods.length))} height={innerH} /><line x1={activeX} x2={activeX} y1={top} y2={top+innerH} /><text x={Math.min(width-right-5, activeX+7)} y={top+innerH-8}>{activeMeta?.label} · {activeMeta?.phase.replaceAll("_", " ").toUpperCase()}</text></g>}
        {visiblePeriods.map((period, index) => { const cellWidth = innerW / Math.max(1, visiblePeriods.length); return <rect key={period.id} x={xForId(period.id)-cellWidth/2} y={top} width={cellWidth} height={innerH} className="chart-hit-area" onMouseEnter={() => onHoverPeriod?.(period.id)} onClick={() => onSelectPeriod?.(period.id)}><title>{period.label} · {period.deliveryLabel} · {period.phase}</title></rect>; })}
        {visiblePeriods.filter((_, index) => index % Math.max(1, Math.ceil(visiblePeriods.length / 8)) === 0 || index === visiblePeriods.length-1).map((period) => <text key={period.id} x={xForId(period.id)} y={height-25} textAnchor="middle">{period.label}</text>)}
        <text x={left + innerW / 2} y={height - 4} textAnchor="middle" className="axis-title">GB settlement period · UK delivery time</text>
      </svg>
      {activePeriod && visibleIds.has(activePeriod) && tooltipContent && <div className="rich-chart-tooltip" aria-live="polite">{tooltipContent(activePeriod)}</div>}
    </div>
    <div className="chart-brush" aria-label={`${title} draggable range brush`}>
      <span>{allPeriods[rangeStart]?.label}</span><div><div className="brush-selection" style={{ left: `${allPeriods.length <= 1 ? 0 : rangeStart/(allPeriods.length-1)*100}%`, right: `${allPeriods.length <= 1 ? 0 : 100-rangeEnd/(allPeriods.length-1)*100}%` }} /><input aria-label="Zoom range start" type="range" min="0" max={Math.max(0, allPeriods.length-1)} value={rangeStart} onChange={(event) => setRange(Math.min(Number(event.target.value), rangeEnd), rangeEnd)} /><input aria-label="Zoom range end" type="range" min="0" max={Math.max(0, allPeriods.length-1)} value={rangeEnd} onChange={(event) => setRange(rangeStart, Math.max(Number(event.target.value), rangeStart))} /></div><span>{allPeriods[rangeEnd]?.label}</span>
    </div>
  </article>;
}

function AnnotationMark({ annotation, x, top, bottom, labelY }: { annotation: ChartAnnotation; x: number; top: number; bottom: number; labelY: number }) {
  return <g className={`chart-annotation ${annotation.kind}`}><line x1={x} x2={x} y1={top} y2={bottom} /><text x={x + 5} y={labelY}>{annotation.label}</text></g>;
}

function BoundaryMark({ x, top, bottom, label, align }: { x: number; top: number; bottom: number; label: string; align: "start" | "end" }) {
  return <g className="auction-boundary"><line x1={x} x2={x} y1={top} y2={bottom} /><text x={align === "start" ? x+5 : x-5} y={bottom-6} textAnchor={align}>{label}</text></g>;
}
