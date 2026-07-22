import type { ChartSeries } from "./types";

export type HistoryWindow = "today" | "24h" | "7d" | "30d" | "custom";

export interface CustomWindow { from: string; to: string; }

export const historyWindowLabels: Record<HistoryWindow, string> = {
  today: "Today",
  "24h": "Last 24h",
  "7d": "Last 7d",
  "30d": "Last 30d",
  custom: "Custom",
};

export function historyWindowStart(window: HistoryWindow, now: Date, custom?: CustomWindow): Date {
  if (window === "custom" && custom?.from) return new Date(custom.from);
  if (window === "today") return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const hours = window === "24h" ? 24 : window === "7d" ? 24 * 7 : 24 * 30;
  return new Date(now.getTime() - hours * 60 * 60 * 1000);
}

export function filterChartSeries(
  series: ChartSeries[],
  window: HistoryWindow,
  now: string | Date,
  custom?: CustomWindow,
): ChartSeries[] {
  const current = now instanceof Date ? now : new Date(now);
  const start = historyWindowStart(window, current, custom);
  const customEnd = window === "custom" && custom?.to ? new Date(custom.to) : current;
  return series.map((item) => ({
    ...item,
    points: item.points.filter((point) => {
      if (!point.timestamp) return true;
      if (item.region === "future" || item.region === "current") return true;
      const timestamp = new Date(point.timestamp);
      return timestamp >= start && timestamp <= customEnd;
    }),
  }));
}

