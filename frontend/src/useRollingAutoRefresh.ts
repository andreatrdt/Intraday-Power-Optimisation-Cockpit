import { useEffect, useState } from "react";

export type RefreshCadence = "manual" | "5" | "15" | "30" | "boundary";

export function useRollingAutoRefresh(refresh: () => Promise<void>) {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [cadence, setCadence] = useState<RefreshCadence>("manual");

  useEffect(() => {
    if (!autoRefresh || cadence === "manual") return;
    let timer: number | undefined;
    let cancelled = false;
    const scheduleBoundary = () => {
      const now = new Date();
      const next = new Date(now);
      next.setSeconds(1, 0);
      next.setMinutes(now.getMinutes() < 30 ? 30 : 60);
      timer = window.setTimeout(async () => {
        if (cancelled) return;
        await refresh();
        scheduleBoundary();
      }, Math.max(1000, next.getTime() - now.getTime()));
    };
    if (cadence === "boundary") scheduleBoundary();
    else timer = window.setInterval(() => void refresh(), Number(cadence) * 60_000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [autoRefresh, cadence, refresh]);

  return { autoRefresh, setAutoRefresh, cadence, setCadence };
}
