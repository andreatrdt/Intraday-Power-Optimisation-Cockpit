import type { BatteryFlexibilitySnapshot, BatteryPathComparison, BatteryPathPeriodAction, BatteryPathSimulation, CockpitSnapshot, DataFlowEvent, FeedHealth, ForecastPositionSnapshot, LineageResponse, MarketSnapshot } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export async function loadCockpit(): Promise<{
  snapshot: CockpitSnapshot;
  feeds: FeedHealth[];
  events: DataFlowEvent[];
}> {
  return request("/cockpit");
}

export async function refreshFeed(feedId: string): Promise<void> {
  await request(`/data-sources/${feedId}/refresh`, { method: "POST", body: "{}" });
}

export async function loadLineage(valueId: string): Promise<LineageResponse> {
  return request(`/lineage/${valueId}`);
}

export async function loadForecastPosition(): Promise<ForecastPositionSnapshot> {
  const response = await request<{ forecast_position: ForecastPositionSnapshot }>("/forecast-position");
  return response.forecast_position;
}

export async function loadMarketLiquidity(): Promise<MarketSnapshot> {
  const response = await request<{ market: MarketSnapshot }>("/market-liquidity");
  return response.market;
}

export async function loadBatteryFlexibility(): Promise<BatteryFlexibilitySnapshot> {
  const response = await request<{ battery: BatteryFlexibilitySnapshot }>("/battery-flexibility");
  return response.battery;
}

export async function loadBatteryPathComparison(): Promise<BatteryPathComparison> {
  const response = await request<{ comparison: BatteryPathComparison }>("/battery-paths/comparison");
  return response.comparison;
}

export async function simulateBatteryPath(actions: BatteryPathPeriodAction[]): Promise<BatteryPathSimulation> {
  const response = await request<{ simulation: BatteryPathSimulation }>("/battery-paths/simulate", {
    method: "POST",
    body: JSON.stringify({ path_name: "CUSTOM", actions }),
  });
  return response.simulation;
}
