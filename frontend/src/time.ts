export type TimestampInput = string | number | Date;

function asDate(timestamp: TimestampInput): Date {
  const value = timestamp instanceof Date ? timestamp : new Date(timestamp);
  if (Number.isNaN(value.getTime())) throw new RangeError(`Invalid timestamp: ${String(timestamp)}`);
  return value;
}

export function formatLocalTime(timestamp: TimestampInput): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(asDate(timestamp));
}

export function formatUkMarketTime(timestamp: TimestampInput): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/London",
  }).format(asDate(timestamp));
}

export function formatAge(timestamp: TimestampInput, now: TimestampInput = new Date()): string {
  const seconds = Math.max(0, (asDate(now).getTime() - asDate(timestamp).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function formatTimestampWithZone(timestamp: TimestampInput, zoneLabel: string): string {
  const isUkTime = zoneLabel.trim().toLowerCase() === "uk time";
  const formatted = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    ...(isUkTime ? { timeZone: "Europe/London" } : {}),
  }).format(asDate(timestamp));
  return `${formatted} ${zoneLabel}`;
}
