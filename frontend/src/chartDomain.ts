// Pure, framework-free helpers for the historical time-series charts on the Live
// State page. Kept free of React and DOM so they can be unit-tested directly under
// Node (see frontend/tests/live-state-timeseries.mjs) and reused by TimeSeriesChart.
import type { ChartSeries, SourceMode } from "./types";

export interface TimeSeriesPoint {
  seriesKey: string;
  label: string;
  t: number;
  value: number;
}

export interface ValueDomain {
  min: number;
  max: number;
}

/** Parse an ISO timestamp to epoch milliseconds, or null when it is missing/unparseable. */
export function parseTimestamp(timestamp: string | null | undefined): number | null {
  if (!timestamp) return null;
  const ms = new Date(timestamp).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** Total number of raw points across every series (before hiding or timestamp parsing). */
export function countPoints(series: ChartSeries[]): number {
  return series.reduce((total, item) => total + item.points.length, 0);
}

/**
 * Renderable points for a time-series chart: only points that belong to a visible
 * (non-hidden) series, carry a parseable timestamp, and have a finite numeric value.
 * A point that only carries a settlement/delivery period (no timestamp) is NOT
 * renderable here — that is exactly the data shape that silently disappeared when the
 * historical series were fed through the settlement-period LargeChart.
 */
export function visibleTimePoints(series: ChartSeries[], hidden: ReadonlySet<string>): TimeSeriesPoint[] {
  const points: TimeSeriesPoint[] = [];
  for (const item of series) {
    if (hidden.has(item.key)) continue;
    for (const point of item.points) {
      const t = parseTimestamp(point.timestamp);
      if (t === null) continue;
      if (!Number.isFinite(point.value)) continue;
      points.push({ seriesKey: item.key, label: point.label, t, value: point.value });
    }
  }
  return points;
}

/**
 * Y-axis domain computed from the ACTUAL visible values (with 10% padding). Returns
 * null when there are no finite values so the caller can render an explicit empty
 * state instead of the degenerate 0..1 fallback (which is what produced the reported
 * -0.1..1.1 renewable-production axis).
 */
export function computeValueDomain(values: number[], includeZero = false): ValueDomain | null {
  const finite = values.filter((value) => Number.isFinite(value));
  if (finite.length === 0) return null;
  let min = Math.min(...finite);
  let max = Math.max(...finite);
  if (includeZero) {
    min = Math.min(min, 0);
    max = Math.max(max, 0);
  }
  const span = max - min;
  const pad = span === 0 ? Math.max(0.25, Math.abs(max) * 0.08) : span * 0.1;
  return { min: min - pad, max: max + pad };
}

export interface EmptyStateInput {
  rawCount: number;
  filteredCount: number;
  visibleCount: number;
  seriesCount: number;
  hiddenCount: number;
  sourceMode: SourceMode;
  windowLabel: string;
}

/**
 * Explain why a chart has nothing to draw, or null when it does have visible data.
 * The reason is surfaced to the user in development so a blank chart is never silent.
 */
export function emptyStateReason(input: EmptyStateInput): string | null {
  const { rawCount, filteredCount, visibleCount, seriesCount, hiddenCount, sourceMode, windowLabel } = input;
  if (visibleCount > 0) return null;
  if (sourceMode === "ERROR") {
    return "Source returned ERROR; no usable data was produced for this series.";
  }
  if (rawCount === 0) return "No backend data was returned for this series.";
  if (seriesCount > 0 && hiddenCount >= seriesCount) {
    return "All series are hidden. Enable a series in the legend to show data.";
  }
  if (filteredCount === 0) return `All ${rawCount} point(s) fall outside the selected ${windowLabel} window.`;
  return "Timestamps could not be parsed or all values are null for the selected window.";
}

// --- Unit-aware tick / value formatting --------------------------------------------
// Small-range series (frequency, spreads, coverage ratios) were being flattened to a
// single label ("50") by generic compact formatting. Decimal precision is chosen per
// unit AND per visible range so adjacent ticks never collapse into identical labels.

function baseDecimals(unit: string): number {
  switch (unit.trim().toLowerCase()) {
    case "hz": return 3;
    case "mhz": return 0;
    case "%": case "percent": return 1;
    case "gbp/mwh": case "£/mwh": return 1;
    case "gbp": case "£": return 0;
    case "mw": case "mwh": return 0;
    case "score": case "ratio": case "coverage": case "h": return 2;
    default: return 1;
  }
}

/**
 * Decimal places for a tick, driven by the unit's baseline AND the visible span so that
 * a narrow range (e.g. 49.94–50.06 Hz) resolves into distinguishable labels instead of
 * five identical "50"s. Escalates precision as the range shrinks; caps at 6 dp.
 */
export function tickPrecision(unit: string, visibleMin: number, visibleMax: number): number {
  const base = baseDecimals(unit);
  const span = Math.abs(visibleMax - visibleMin);
  if (!Number.isFinite(span) || span === 0) return base;
  const step = span / 4; // ~5 gridlines -> 4 intervals
  const needed = Math.max(0, Math.ceil(-Math.log10(step)) + 1);
  return Math.min(6, Math.max(base, needed));
}

/**
 * Format a single y-axis tick (or summary value) with unit-appropriate, range-aware
 * precision. Fixed decimals keep every tick aligned and prevent label collapse.
 */
export function formatYAxisTick(value: number, unit: string, visibleMin: number, visibleMax: number): string {
  const decimals = tickPrecision(unit, visibleMin, visibleMax);
  return value.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

// --- Frequency deviation display ----------------------------------------------------

/** Convert raw grid frequency in Hz to deviation from nominal in mHz. */
export function frequencyDeviationMHz(frequencyHz: number): number {
  return Math.round((frequencyHz - 50) * 1_000_000) / 1_000;
}

function formatMilliHz(deviation: number): string {
  const rounded = Math.round(deviation * 10) / 10;
  const body = Number.isInteger(rounded) ? Math.abs(rounded).toFixed(0) : Math.abs(rounded).toFixed(1);
  const sign = rounded > 0 ? "+" : rounded < 0 ? "−" : "";
  return `${sign}${body}`;
}

/** Tooltip text carrying BOTH the raw Hz reading and its mHz deviation. */
export function frequencyTooltip(frequencyHz: number): string {
  return `${frequencyHz.toFixed(3)} Hz (${formatMilliHz(frequencyDeviationMHz(frequencyHz))} mHz)`;
}

/** Summary line for frequency in deviation mode, derived from the visible raw-Hz points. */
export function frequencyDeviationSummary(points: TimeSeriesPoint[], windowLabel: string): string {
  if (points.length === 0) return "";
  const latest = points.reduce((newest, point) => (point.t >= newest.t ? point : newest), points[0]);
  const deviations = points.map((point) => frequencyDeviationMHz(point.value));
  const min = Math.min(...deviations);
  const max = Math.max(...deviations);
  return `Latest ${formatMilliHz(frequencyDeviationMHz(latest.value))} mHz · range ${formatMilliHz(min)} to ${formatMilliHz(max)} mHz versus 50 Hz · ${points.length} point${points.length === 1 ? "" : "s"} over ${windowLabel}`;
}

export interface DisplayTransform {
  displayUnit: string;
  rawUnit: string;
  toDisplay: (raw: number) => number;
  formatTooltip: (raw: number) => string;
  summarise: (points: TimeSeriesPoint[], windowLabel: string) => string;
}

/** Default display for GB system frequency: deviation from 50 Hz, in mHz. */
export const FREQUENCY_DEVIATION: DisplayTransform = {
  displayUnit: "mHz",
  rawUnit: "Hz",
  toDisplay: frequencyDeviationMHz,
  formatTooltip: frequencyTooltip,
  summarise: frequencyDeviationSummary,
};

export interface ReferenceBand {
  min: number; // display units; min === max renders as a single reference line
  max: number;
  label: string;
  tone: "nominal" | "info" | "warning";
}

/** Operational reference markers for the frequency deviation chart (values in mHz). */
export const FREQUENCY_BANDS: ReferenceBand[] = [
  { min: 0, max: 0, label: "nominal 50 Hz", tone: "nominal" },
  { min: -50, max: 50, label: "±50 mHz", tone: "info" },
  { min: 100, max: 100, label: "+100 mHz", tone: "warning" },
  { min: -100, max: -100, label: "−100 mHz", tone: "warning" },
];

/**
 * A short summary line derived exclusively from the visible points, so the text a user
 * reads always matches what the chart actually shows. Values use unit- and range-aware
 * precision (so a frequency range reads "49.942–50.058 Hz", not "50–50"). Empty when
 * there is nothing visible (callers show the unavailable message instead).
 */
export function visibleSummary(points: TimeSeriesPoint[], unit: string, windowLabel: string): string {
  if (points.length === 0) return "";
  const values = points.map((point) => point.value);
  const latest = points.reduce((newest, point) => (point.t >= newest.t ? point : newest), points[0]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const unitLabel = unit ? ` ${unit}` : "";
  const fmt = (value: number) => formatYAxisTick(value, unit, min, max);
  return `Latest ${fmt(latest.value)}${unitLabel} · range ${fmt(min)}–${fmt(max)}${unitLabel} · ${points.length} point${points.length === 1 ? "" : "s"} over ${windowLabel}`;
}
