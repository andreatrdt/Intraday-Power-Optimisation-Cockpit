import type { AssessTimingResponse, BatteryFlexibilitySnapshot, BatteryPathComparison, BatteryPathPeriodAction, BatteryPathSimulation, CockpitSnapshot, CoordinatorSimulationInput, CoordinatorSnapshot, DataFlowEvent, DecisionBatch, DecisionBatchSummary, DecisionRefreshResponse, FeedHealth, ForecastPositionSnapshot, ForecastRevision, HedgeTimingAssessment, HorizonMode, LineageResponse, LiveStateSnapshot, MarketSnapshot, OptimisationRun, OptionalitySnapshot, SampleRegime, TradeDecision } from "./types";

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

export async function loadOptionality(): Promise<OptionalitySnapshot> {
  const response = await request<{ optionality: OptionalitySnapshot }>("/optionality");
  return response.optionality;
}

export async function simulateOptionalityPath(actions: BatteryPathPeriodAction[]): Promise<OptionalitySnapshot> {
  const response = await request<{ optionality: OptionalitySnapshot }>("/optionality/simulate", {
    method: "POST",
    body: JSON.stringify({ path_name: "CUSTOM", actions }),
  });
  return response.optionality;
}

export async function loadCoordinator(): Promise<CoordinatorSnapshot> {
  const response = await request<{ coordinator: CoordinatorSnapshot }>("/coordinator");
  return response.coordinator;
}

export async function simulateCoordinator(settings: CoordinatorSimulationInput): Promise<CoordinatorSnapshot> {
  const response = await request<{ coordinator: CoordinatorSnapshot }>("/coordinator/simulate", {
    method: "POST",
    body: JSON.stringify(settings),
  });
  return response.coordinator;
}

export async function loadLiveState(): Promise<LiveStateSnapshot> {
  const response = await request<{ live_state: LiveStateSnapshot }>("/live-state");
  return response.live_state;
}

export async function refreshLiveState(): Promise<LiveStateSnapshot> {
  const response = await request<{ live_state: LiveStateSnapshot }>("/live-state/refresh", { method: "POST" });
  return response.live_state;
}

export async function refreshRollingCockpit(): Promise<{ live_state: LiveStateSnapshot; optimisation: OptimisationRun }> {
  return request("/live-state/refresh", { method: "POST" });
}

export async function resetLiveState(): Promise<{ live_state: LiveStateSnapshot; optimisation: OptimisationRun }> {
  return request("/live-state/reset", { method: "POST" });
}

export async function setLiveRegime(regime: SampleRegime): Promise<{ live_state: LiveStateSnapshot; optimisation: OptimisationRun }> {
  return request("/live-state/regime", {
    method: "POST",
    body: JSON.stringify({ regime }),
  });
}

export async function setHorizonMode(mode: HorizonMode): Promise<{ live_state: LiveStateSnapshot; optimisation: OptimisationRun }> {
  return request("/live-state/horizon", { method: "POST", body: JSON.stringify({ mode }) });
}

export async function loadCurrentOptimisation(): Promise<OptimisationRun> {
  const response = await request<{ optimisation: OptimisationRun }>("/optimisation/current");
  return response.optimisation;
}

export async function runRollingOptimisation(): Promise<{ optimisation: OptimisationRun; live_state: LiveStateSnapshot }> {
  return request("/optimisation/run", { method: "POST" });
}

export async function loadOptimisationRuns(): Promise<OptimisationRun[]> {
  const response = await request<{ runs: OptimisationRun[] }>("/optimisation/runs");
  return response.runs;
}

// ---------------------------------------------------------------------------
// Trader decision queue (Milestone 5) — all decision/revision/timing logic
// stays on the backend; the client only reads, joins by id, filters and sorts.
// ---------------------------------------------------------------------------

export async function loadDecisions(): Promise<TradeDecision[]> {
  const response = await request<{ decisions: TradeDecision[] }>("/decisions");
  return response.decisions;
}

export async function loadDecision(decisionId: string): Promise<TradeDecision> {
  const response = await request<{ decision: TradeDecision }>(`/decisions/${decisionId}`);
  return response.decision;
}

export async function loadDecisionBatches(): Promise<DecisionBatch[]> {
  const response = await request<{ batches: DecisionBatch[] }>("/decision-batches");
  return response.batches;
}

export async function loadForecastRevisions(): Promise<ForecastRevision[]> {
  const response = await request<{ revisions: ForecastRevision[] }>("/forecast-revisions");
  return response.revisions;
}

export async function loadForecastRevision(revisionId: string): Promise<ForecastRevision> {
  const response = await request<{ revision: ForecastRevision }>(`/forecast-revisions/${revisionId}`);
  return response.revision;
}

export async function loadHedgeTimingAssessments(): Promise<HedgeTimingAssessment[]> {
  const response = await request<{ assessments: HedgeTimingAssessment[] }>("/hedge-timing-assessments");
  return response.assessments;
}

export async function loadDecisionBatchSummaries(): Promise<DecisionBatchSummary[]> {
  const response = await request<{ summaries: DecisionBatchSummary[] }>("/decision-batch-summaries");
  return response.summaries;
}

export async function refreshDecisions(): Promise<DecisionRefreshResponse> {
  return request<DecisionRefreshResponse>("/decisions/refresh", { method: "POST", body: "{}" });
}

export async function reassessTiming(): Promise<AssessTimingResponse> {
  return request<AssessTimingResponse>("/decisions/assess-timing", { method: "POST", body: "{}" });
}
