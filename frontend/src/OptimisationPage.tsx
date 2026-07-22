import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { LargeChart, type ChartTrack, type InteractiveChartPeriod } from "./CockpitChart";
import { ConnectionStatus } from "./ConnectionStatus";
import { loadCurrentOptimisation, loadLineage, loadLiveState, refreshRollingCockpit, resetLiveState, runRollingOptimisation, setHorizonMode, setLiveRegime } from "./api";
import { ProductNav } from "./ProductNav";
import { formatTimestampWithZone, formatUkMarketTime } from "./time";
import { TrustStatusStrip } from "./TrustStatusStrip";
import { useRollingAutoRefresh, type RefreshCadence } from "./useRollingAutoRefresh";
import type { AuctionPathPhase, BatteryPathPoint, CanonicalDataPoint, ChartAnnotation, ChartPoint, ChartSeries, HorizonMode, LineageResponse, LiveStateSnapshot, MarketExecutionPathPoint, OptimisationPeriodResult, OptimisationRun, PositionPathPoint, RiskValuePathPoint, SampleRegime } from "./types";

const fmt = (value: number, digits = 1) => value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const gbp = (value: number) => `${value < 0 ? "−" : ""}£${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const signed = (value: number) => `${value > 0 ? "+" : ""}${fmt(value)} MWh`;
const regimes: { value: SampleRegime; label: string }[] = [
  { value: "normal", label: "Normal" }, { value: "tightening", label: "Tightening" },
  { value: "oversupply", label: "Oversupply" }, { value: "price_spike", label: "Price spike" },
  { value: "wind_forecast_miss", label: "Wind forecast miss" }, { value: "demand_surprise", label: "Demand surprise" },
];
const horizonModes: { value: HorizonMode; label: string }[] = [
  { value: "next_auction", label: "15:00 auction (primary)" },
  { value: "next_8_periods", label: "Debug: next 8 SPs" },
  { value: "end_of_day", label: "Debug: end of delivery day" },
];
const cadenceLabels: Record<RefreshCadence, string> = { manual: "Manual", "5": "5 min", "15": "15 min", "30": "30 min", boundary: "Settlement-period boundary" };
const batteryTracks: ChartTrack[] = [
  { label: "Dispatch", unit: "MW", keys: ["charge_mw", "discharge_mw"] },
  { label: "State of charge", unit: "MWh", keys: ["soc_end_mwh", "terminal_soc_target_mwh", "terminal_soc_minimum_mwh"] },
  { label: "Reserve and power headroom", unit: "MW", keys: ["reserve_up_mw", "reserve_down_mw", "upward_headroom_mw", "downward_headroom_mw"] },
  { label: "Reserve-duration coverage", unit: "h", keys: ["upward_duration_coverage_h", "downward_duration_coverage_h"] },
];
const marketTracks: ChartTrack[] = [
  { label: "Executable price", unit: "GBP/MWh", keys: ["bid", "ask", "wap", "reference", "spread"] },
  { label: "Visible and consumed depth", unit: "MWh", keys: ["bid_depth", "ask_depth", "consumed_bid", "consumed_ask", "unfilled"] },
];

const chartPoint = (point: { settlement_period: number; delivery_period: string; timestamp: string }, value: number): ChartPoint => ({
  label: `SP${point.settlement_period}`,
  value,
  timestamp: point.timestamp,
  settlement_period: point.settlement_period,
  delivery_period: point.delivery_period,
});

function seriesFrom<T extends { settlement_period: number; delivery_period: string; timestamp: string }>(
  points: T[], key: string, label: string, unit: string, kind: "line" | "bar", pick: (point: T) => number,
  annotations?: ChartAnnotation[], flatExplanation?: string | null,
): ChartSeries {
  return { key, label, unit, kind, points: points.map((point) => chartPoint(point, pick(point))), annotations, flat_explanation: flatExplanation ?? null };
}

function phaseAnnotation(points: PositionPathPoint[]): ChartAnnotation[] {
  const gates = points.filter((point) => point.phase !== "historical" && !point.market_action_allowed)
    .map((point) => ({ timestamp: point.timestamp, label: `SP${point.settlement_period} Gate Closed`, kind: "warning", value: null }));
  const largestTrades = points.filter((point) => point.phase === "optimised_future" && point.market_action_allowed)
    .sort((left, right) => (right.buy_mwh + right.sell_mwh) - (left.buy_mwh + left.sell_mwh)).slice(0, 3)
    .filter((point) => point.buy_mwh + point.sell_mwh >= 1)
    .map((point) => ({
      timestamp: point.timestamp,
      label: `SP${point.settlement_period} ${point.buy_mwh >= point.sell_mwh ? "buy" : "sell"} ${fmt(Math.max(point.buy_mwh, point.sell_mwh))} MWh`,
      kind: "info", value: null,
    }));
  return [...gates, ...largestTrades];
}

function batterySeries(points: BatteryPathPoint[]): ChartSeries[] {
  const flat = points.find((point) => point.phase === "optimised_future" && point.flat_path_explanation)?.flat_path_explanation ?? null;
  return [
    seriesFrom(points, "charge_mw", "Charge", "MW", "bar", (point) => -point.charge_mw, undefined, flat),
    seriesFrom(points, "discharge_mw", "Discharge", "MW", "bar", (point) => point.discharge_mw),
    seriesFrom(points, "soc_end_mwh", "SoC", "MWh", "line", (point) => point.soc_end_mwh),
    seriesFrom(points, "reserve_up_mw", "Reserve up", "MW", "line", (point) => point.reserve_up_mw),
    seriesFrom(points, "reserve_down_mw", "Reserve down", "MW", "line", (point) => point.reserve_down_mw),
    seriesFrom(points, "upward_headroom_mw", "Upward headroom", "MW", "line", (point) => point.upward_headroom_mw),
    seriesFrom(points, "downward_headroom_mw", "Downward headroom", "MW", "line", (point) => point.downward_headroom_mw),
    seriesFrom(points, "upward_duration_coverage_h", "Up duration", "h", "line", (point) => point.upward_duration_coverage_h),
    seriesFrom(points, "downward_duration_coverage_h", "Down duration", "h", "line", (point) => point.downward_duration_coverage_h),
    seriesFrom(points, "terminal_soc_target_mwh", "Terminal target", "MWh", "line", (point) => point.terminal_soc_target_mwh),
    seriesFrom(points, "terminal_soc_minimum_mwh", "Terminal minimum", "MWh", "line", (point) => point.terminal_soc_minimum_mwh),
  ];
}

function positionSeries(points: PositionPathPoint[]): ChartSeries[] {
  const annotations = phaseAnnotation(points);
  const tail = points.filter((point) => point.phase === "optimised_future").sort((left, right) => Math.max(Math.abs(right.residual_p10_mwh), Math.abs(right.residual_p90_mwh)) - Math.max(Math.abs(left.residual_p10_mwh), Math.abs(left.residual_p90_mwh)))[0];
  if (tail) annotations.push({ timestamp: tail.timestamp, label: `SP${tail.settlement_period} largest tail ${fmt(Math.max(Math.abs(tail.residual_p10_mwh), Math.abs(tail.residual_p90_mwh)))} MWh`, kind: "risk", value: null });
  return [
    seriesFrom(points, "generation_p10_mwh", "Forecast P10", "MWh", "line", (point) => point.generation_p10_mwh),
    seriesFrom(points, "generation_p50_mwh", "Forecast P50", "MWh", "line", (point) => point.generation_p50_mwh),
    seriesFrom(points, "generation_p90_mwh", "Forecast P90", "MWh", "line", (point) => point.generation_p90_mwh),
    seriesFrom(points, "q_before_mwh", "Q before", "MWh", "line", (point) => point.q_before_mwh),
    seriesFrom(points, "q_after_mwh", "Q after", "MWh", "line", (point) => point.q_after_mwh),
    seriesFrom(points, "buy_mwh", "Buy", "MWh", "bar", (point) => point.buy_mwh, annotations),
    seriesFrom(points, "sell_mwh", "Sell", "MWh", "bar", (point) => -point.sell_mwh),
    seriesFrom(points, "exposure_before_p50_mwh", "Exposure before P50", "MWh", "line", (point) => point.exposure_before_p50_mwh),
    seriesFrom(points, "residual_p10_mwh", "Residual P10", "MWh", "line", (point) => point.residual_p10_mwh),
    seriesFrom(points, "residual_p50_mwh", "Residual P50", "MWh", "line", (point) => point.residual_p50_mwh),
    seriesFrom(points, "residual_p90_mwh", "Residual P90", "MWh", "line", (point) => point.residual_p90_mwh),
  ];
}

function marketSeries(points: MarketExecutionPathPoint[]): ChartSeries[] {
  const annotations = points.filter((point) => point.phase !== "historical" && !point.market_action_allowed)
    .map((point) => ({ timestamp: point.timestamp, label: `SP${point.settlement_period} Gate Closed`, kind: "warning", value: null } as ChartAnnotation));
  const wap: ChartSeries = {
    key: "wap", label: "Execution WAP", unit: "GBP/MWh", kind: "line", flat_explanation: null,
    points: points.filter((point) => point.wap_used_gbp_per_mwh !== null).map((point) => chartPoint(point, point.wap_used_gbp_per_mwh as number)),
  };
  return [
    seriesFrom(points, "bid", "Executable bid", "GBP/MWh", "line", (point) => point.bid_price_gbp_per_mwh, annotations),
    seriesFrom(points, "ask", "Executable ask", "GBP/MWh", "line", (point) => point.ask_price_gbp_per_mwh),
    wap,
    seriesFrom(points, "reference", "Reference (non-executable)", "GBP/MWh", "line", (point) => point.reference_price_gbp_per_mwh),
    seriesFrom(points, "spread", "Spread", "GBP/MWh", "bar", (point) => point.spread_gbp_per_mwh),
    seriesFrom(points, "bid_depth", "Bid depth", "MWh", "line", (point) => point.bid_depth_mwh),
    seriesFrom(points, "ask_depth", "Ask depth", "MWh", "line", (point) => point.ask_depth_mwh),
    seriesFrom(points, "consumed_bid", "Bid depth consumed", "MWh", "bar", (point) => point.consumed_bid_depth_mwh),
    seriesFrom(points, "consumed_ask", "Ask depth consumed", "MWh", "bar", (point) => point.consumed_ask_depth_mwh),
    seriesFrom(points, "unfilled", "Unfilled volume", "MWh", "bar", (point) => point.unfilled_volume_mwh),
  ];
}

function riskSeries(points: RiskValuePathPoint[]): ChartSeries[] {
  return [
    seriesFrom(points, "market_value", "Market value / cost", "GBP", "bar", (point) => point.market_value_or_cost_gbp),
    seriesFrom(points, "imbalance_cost", "Imbalance cost", "GBP", "bar", (point) => -point.imbalance_cost_gbp),
    seriesFrom(points, "tail_penalty", "Tail-risk penalty", "GBP", "bar", (point) => -point.tail_risk_penalty_gbp),
    seriesFrom(points, "degradation", "Degradation cost", "GBP", "bar", (point) => -point.degradation_cost_gbp),
    seriesFrom(points, "reserve_value", "Reserve / BM / service value", "GBP", "bar", (point) => point.reserve_bm_service_value_gbp),
    seriesFrom(points, "terminal_value", "Terminal SoC value", "GBP", "bar", (point) => point.terminal_soc_value_gbp),
    seriesFrom(points, "optionality_lost", "Optionality lost", "GBP", "bar", (point) => -point.optionality_lost_gbp),
    seriesFrom(points, "total", "Total period contribution", "GBP", "line", (point) => point.total_period_contribution_gbp),
  ];
}

export function OptimisationPage() {
  const [run, setRun] = useState<OptimisationRun | null>(null);
  const [live, setLive] = useState<LiveStateSnapshot | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hoveredPeriod, setHoveredPeriod] = useState<string | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const accept = useCallback((nextRun: OptimisationRun, nextLive: LiveStateSnapshot) => { setRun(nextRun); setLive(nextLive); setLastPoll(new Date()); setError(null); }, []);
  const load = useCallback(async () => { try { const [nextRun, nextLive] = await Promise.all([loadCurrentOptimisation(), loadLiveState()]); accept(nextRun, nextLive); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load rolling optimisation"); } }, [accept]);
  useEffect(() => { void load(); }, [load]);
  const act = async (name: string, action: () => Promise<{ optimisation: OptimisationRun; live_state: LiveStateSnapshot }>) => { setBusy(name); try { const result = await action(); accept(result.optimisation, result.live_state); } catch (cause) { setError(cause instanceof Error ? cause.message : `${name} failed`); } finally { setBusy(null); } };
  const refresh = useCallback(async () => { await act("refresh", refreshRollingCockpit); }, [accept]);
  const auto = useRollingAutoRefresh(refresh);
  const open = async (point: CanonicalDataPoint | null | undefined) => { if (!point) return; try { setLineage(await loadLineage(point.value_id)); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); } };
  const charts = useMemo(() => run ? {
    battery: batterySeries(run.battery_path_series),
    position: positionSeries(run.position_path_series),
    market: marketSeries(run.market_execution_series),
    risk: riskSeries(run.risk_value_series),
  } : null, [run]);
  const interactionPeriods = useMemo<InteractiveChartPeriod[]>(() => run ? run.interaction_points.map((point) => ({
    id: point.stable_sp_id, label: point.display_label, timestamp: run.position_path_series.find((position) => position.delivery_period === point.delivery_period)?.timestamp ?? run.as_of,
    phase: point.phase, deliveryLabel: point.uk_delivery_time,
  })) : [], [run]);
  const selectFromChart = (periodId: string) => { setSelectedPeriod(periodId); setDetailOpen(true); };
  const clearSelection = () => { setSelectedPeriod(null); setHoveredPeriod(null); setDetailOpen(false); };

  return <div className="app-shell optimisation-page graph-led-page">
    <header className="topbar"><div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">ROLLING INTRADAY COCKPIT</p><h1>Rolling Optimisation</h1></div></div><ProductNav active="optimisation" /><ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} /></header>
    <main>
      {error && <div className="error-banner"><strong>Optimisation error</strong><span>{error}</span><button onClick={() => void load()}>Retry</button></div>}
      {run && live && charts ? <>
        <section className="optimisation-run-head panel"><div><p className="eyebrow">CURRENT IMMUTABLE RUN</p><h2>{run.run_id}</h2><span>{formatTimestampWithZone(run.as_of, "UK time")} · {run.solver} · {run.solver_status}</span></div><div className="run-head-grid"><RunStat label="Current SP" value={`SP${run.current_sp}`} /><RunStat label="Auction boundary" value={run.auction_boundary_time} /><RunStat label="Previous auction" value={formatTimestampWithZone(run.previous_auction_time, "UK time")} /><RunStat label="Next auction" value={formatTimestampWithZone(run.next_auction_time, "UK time")} /><RunStat label="Visual window" value={`${formatUkMarketTime(run.visual_window_start)}–${formatUkMarketTime(run.visual_window_end)} UK time`} /><RunStat label="Optimisation window" value={`${formatUkMarketTime(run.optimisation_window_start)}–${formatUkMarketTime(run.optimisation_window_end)} UK time`} /><RunStat label="SPs shown / optimised" value={`${run.number_of_sps_shown} / ${run.number_of_sps_optimised}`} /><RunStat label="Starting SoC" value={`${fmt(run.starting_state.starting_soc_mwh)} MWh`} /><RunStat label="Starting Qₜ" value={`${fmt(run.starting_state.starting_q_mwh)} MWh`} /><RunStat label="Forecast vintage" value={run.starting_state.forecast_vintage_id} mono /><RunStat label="Market snapshot" value={run.starting_state.market_snapshot_id} mono /></div><div className="run-trust"><Badge value={run.starting_state.source_mode} /><strong className={`readiness ${run.readiness.status.toLowerCase()}`}>{run.readiness.status}</strong><span>Calculation {run.readiness.calculation_allowed ? "allowed" : "blocked"}</span><span>Live trust {run.readiness.trustworthy_for_live_trading ? "yes" : "no"}</span></div></section>

        <section className="auction-change-strip panel"><div><p className="eyebrow">WHAT CHANGED SINCE PREVIOUS RUN</p><strong>{run.change_since_previous.trajectory_change_reason}</strong></div><Change label="Forecast" value={run.change_since_previous.forecast_change_mwh} unit="MWh" /><Change label="Demand" value={run.change_since_previous.demand_change_mw} unit="MW" /><Change label="Price" value={run.change_since_previous.price_change_gbp_per_mwh} unit="£/MWh" /><Change label="Qₜ" value={run.change_since_previous.q_change_mwh} unit="MWh" /><Change label="SoC" value={run.change_since_previous.soc_change_mwh} unit="MWh" /><Change label="Headroom" value={run.change_since_previous.headroom_change_mw} unit="MW" /><div><span>Largest new risk</span><strong>{run.change_since_previous.largest_new_risk}</strong></div></section>

        <section className="cockpit-controls optimisation-controls panel">
          <button className="primary-action" disabled={Boolean(busy)} onClick={() => void act("run", runRollingOptimisation)}>▶ {busy === "run" ? "Solving…" : "Run optimisation now"}</button>
          <button disabled={Boolean(busy)} onClick={() => void refresh()}>{busy === "refresh" ? "Refreshing…" : "Refresh now"}</button>
          <button aria-pressed={auto.autoRefresh} onClick={() => auto.setAutoRefresh(!auto.autoRefresh)}>{auto.autoRefresh ? "Auto-refresh on" : "Auto-refresh off"}</button>
          <label><span>Refresh cadence</span><select value={auto.cadence} onChange={(event) => { const value = event.target.value as RefreshCadence; auto.setCadence(value); if (value === "manual") auto.setAutoRefresh(false); }}>{Object.entries(cadenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label><span>Horizon mode</span><select value={live.state.horizon_mode} disabled={Boolean(busy)} onChange={(event) => void act("horizon", () => setHorizonMode(event.target.value as HorizonMode))}>{horizonModes.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label>
          {live.state.state_source_mode === "SAMPLE" && <label><span>Scenario regime · SAMPLE only</span><select value={live.state.current_regime} disabled={Boolean(busy)} onChange={(event) => void act("regime", () => setLiveRegime(event.target.value as SampleRegime))}>{regimes.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label>}
          <button disabled={Boolean(busy)} onClick={() => void act("reset", resetLiveState)}>Reset sample state</button>
        </section>
        <TrustStatusStrip state={live.state} warnings={run.sanity_warnings} />

        <div className="section-heading"><div><p className="eyebrow">01 · PRIMARY AUCTION PATHS</p><h2>From the previous 15:00 auction to the next</h2></div><span>History is observed/sample context. Optimisation starts at NOW.</span></div>
        <section className="auction-path-stack">
          <LargeChart featured title="Optimised battery path" insight={run.battery_path_series.find((point) => point.flat_path_explanation)?.flat_path_explanation ?? run.whole_path_explanation} subtitle="Dispatch, SoC, reserve/headroom and duration are separated into unit-specific tracks." series={charts.battery} periods={interactionPeriods} tracks={batteryTracks} includeZero safeBand={{ min: run.battery_path_series[0]?.soc_min_mwh ?? 0, max: run.battery_path_series[0]?.soc_max_mwh ?? 100, label: "physical SoC range", trackKey: "soc_end_mwh" }} nowMarker={run.now_marker_time} windowStart={run.visual_window_start} windowEnd={run.visual_window_end} focusedScale hoveredPeriod={hoveredPeriod} selectedPeriod={selectedPeriod} onHoverPeriod={setHoveredPeriod} onSelectPeriod={selectFromChart} tooltipContent={(id) => <SpTooltip run={run} periodId={id} kind="battery" />} />
          <LargeChart featured title="Optimised position rebalancing path" insight={run.whole_path_explanation} subtitle="Sell bars are plotted below zero; Q, exposure and residual risk remain in MWh with a zero line and P10–P90 fan." series={charts.position} periods={interactionPeriods} includeZero band={{ lowerKey: "residual_p10_mwh", upperKey: "residual_p90_mwh", label: "Residual P10–P90 range" }} nowMarker={run.now_marker_time} windowStart={run.visual_window_start} windowEnd={run.visual_window_end} focusedScale hoveredPeriod={hoveredPeriod} selectedPeriod={selectedPeriod} onHoverPeriod={setHoveredPeriod} onSelectPeriod={selectFromChart} tooltipContent={(id) => <SpTooltip run={run} periodId={id} kind="position" />} />
        </section>

        <div className="section-heading hero-table-heading"><div><p className="eyebrow">02 · AUCTION TRAJECTORY TABLE</p><h2>Historical state, current boundary and suggested future path</h2></div><div className="table-selection-controls"><span>All delivery and Gate Closure timestamps are UK time · suggestions only, never submitted</span><button type="button" disabled={!selectedPeriod} onClick={clearSelection}>Clear selection</button></div></div>
        <AuctionTrajectoryTable run={run} open={open} hoveredPeriod={hoveredPeriod} selectedPeriod={selectedPeriod} onHoverPeriod={setHoveredPeriod} onSelectPeriod={setSelectedPeriod} />

        <div className="section-heading"><div><p className="eyebrow">03 · SUPPORTING EVIDENCE</p><h2>Execution and period economics</h2></div><span>Reference price is explicitly non-executable; buys use asks and sells use bids.</span></div>
        <section className="large-chart-stack supporting-auction-charts">
          <LargeChart title="Market execution path" insight={run.chart_insights.market_execution ?? "Suggested trades walk visible SAMPLE depth; Gate Closed periods cannot trade."} subtitle="Executable price and visible/consumed volume are separated into unit-specific tracks." series={charts.market} periods={interactionPeriods} tracks={marketTracks} includeZero nowMarker={run.now_marker_time} windowStart={run.visual_window_start} windowEnd={run.visual_window_end} hoveredPeriod={hoveredPeriod} selectedPeriod={selectedPeriod} onHoverPeriod={setHoveredPeriod} onSelectPeriod={selectFromChart} tooltipContent={(id) => <SpTooltip run={run} periodId={id} kind="market" />} />
          <LargeChart title="Risk and value by settlement period" insight={run.chart_insights.period_value ?? "Each period contribution separates execution value, risk, degradation, optionality and terminal effects."} subtitle="All series use GBP diagnostic contribution on a shared zero-centred scale." series={charts.risk} periods={interactionPeriods} includeZero nowMarker={run.now_marker_time} windowStart={run.visual_window_start} windowEnd={run.visual_window_end} hoveredPeriod={hoveredPeriod} selectedPeriod={selectedPeriod} onHoverPeriod={setHoveredPeriod} onSelectPeriod={selectFromChart} tooltipContent={(id) => <SpTooltip run={run} periodId={id} kind="risk" />} />
        </section>

        <section className="panel whole-path-explanation"><p className="eyebrow">WHY THIS WHOLE PATH</p><h2>Model interpretation</h2><p>{run.whole_path_explanation}</p><div><span>Forecast</span><strong>{run.explanation_drivers.forecast_driver}</strong><span>Market</span><strong>{run.explanation_drivers.price_order_book_driver}</strong><span>Battery</span><strong>{run.explanation_drivers.battery_soc_driver}</strong><span>Tail risk</span><strong>{run.explanation_drivers.imbalance_tail_risk_driver}</strong><span>Constraints</span><strong>{run.explanation_drivers.binding_constraint_driver}</strong></div></section>
      </> : <div className="empty panel">Preparing auction-to-auction rolling optimisation…</div>}
    </main>
    {detailOpen && selectedPeriod && run && <SpDetailDrawer run={run} periodId={selectedPeriod} onClose={() => setDetailOpen(false)} onClear={clearSelection} />}
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function RunStat({ label, value, mono }: { label: string; value: string; mono?: boolean }) { return <div><span>{label}</span><strong className={mono ? "mono compact-id" : ""}>{value}</strong></div>; }
function Change({ label, value, unit }: { label: string; value: number; unit: string }) { return <div><span>{label}</span><strong className={value > 0 ? "positive" : value < 0 ? "negative" : ""}>{value > 0 ? "+" : ""}{fmt(value)} {unit}</strong></div>; }
function PhaseBadge({ phase }: { phase: AuctionPathPhase }) { const text = phase === "optimised_future" ? "OPTIMISED FUTURE" : phase.toUpperCase(); return <span className={`auction-phase-badge ${phase}`}>{text}</span>; }

function AuctionTrajectoryTable({ run, open, hoveredPeriod, selectedPeriod, onHoverPeriod, onSelectPeriod }: { run: OptimisationRun; open: (point?: CanonicalDataPoint | null) => void | Promise<void>; hoveredPeriod: string | null; selectedPeriod: string | null; onHoverPeriod: (id: string | null) => void; onSelectPeriod: (id: string) => void }) {
  const battery = new Map(run.battery_path_series.map((point) => [point.delivery_period, point]));
  const market = new Map(run.market_execution_series.map((point) => [point.delivery_period, point]));
  const risk = new Map(run.risk_value_series.map((point) => [point.delivery_period, point]));
  const solved = new Map(run.projected_trajectory.map((point) => [point.delivery_period, point]));
  const interaction = new Map(run.interaction_points.map((point) => [point.delivery_period, point]));
  const lineaged = (period: OptimisationPeriodResult | undefined, key: string, text: string) => period?.values[key] ? <button className="table-value" onClick={() => void open(period.values[key])}>{text}</button> : <span>{text}</span>;
  return <div className="table-wrap panel trajectory-table hero-decision-table auction-trajectory-table"><table><thead><tr><th>Phase / SP / delivery</th><th>Forecast P10 / P50 / P90</th><th>Demand / residual demand</th><th>Q before / buy / sell / Q after</th><th>Exposure before P50</th><th>Charge / discharge</th><th>SoC start / end</th><th>Reserve up / down</th><th>Power headroom up / down</th><th>Duration up / down</th><th>Residual P10 / P50 / P90</th><th>Bid / ask / WAP</th><th>Depth consumed / unfilled</th><th>Risk / value</th><th>Bindings / reason</th></tr></thead><tbody>{run.position_path_series.map((position) => {
    const batteryPoint = battery.get(position.delivery_period)!;
    const marketPoint = market.get(position.delivery_period)!;
    const riskPoint = risk.get(position.delivery_period)!;
    const period = solved.get(position.delivery_period);
    const interactionPoint = interaction.get(position.delivery_period);
    const highlighted = hoveredPeriod === position.delivery_period || selectedPeriod === position.delivery_period;
    return <tr id={interactionPoint?.linked_trajectory_row_id} data-sp-id={position.delivery_period} key={position.delivery_period} onMouseEnter={() => onHoverPeriod(position.delivery_period)} onMouseLeave={() => onHoverPeriod(null)} onClick={() => onSelectPeriod(position.delivery_period)} className={`auction-row ${position.phase} ${!position.market_action_allowed ? "gate-closed-row" : ""} ${highlighted ? "sp-row-highlighted" : ""} ${selectedPeriod === position.delivery_period ? "sp-row-selected" : ""}`}><td><PhaseBadge phase={position.phase} /><strong>SP{position.settlement_period}</strong><small>{formatUkMarketTime(position.timestamp)}–{formatUkMarketTime(position.delivery_end)} UK time</small><span className={`gate-badge ${position.market_action_allowed ? "open" : "closed"}`}>{position.market_action_allowed ? "TRADABLE" : "GATE CLOSED"}</span><small>GC {formatUkMarketTime(position.gate_closure_at)} UK time</small></td><td><div className="scenario-stack"><span>P10 {fmt(position.generation_p10_mwh)}</span><span>P50 {fmt(position.generation_p50_mwh)}</span><span>P90 {fmt(position.generation_p90_mwh)}</span></div></td><td>{fmt(position.demand_mw, 0)} MW<small>{fmt(position.residual_demand_mw, 0)} MW residual</small></td><td><div className="scenario-stack"><span>Q before {fmt(position.q_before_mwh)}</span>{lineaged(period, "buy_mwh", `${fmt(position.buy_mwh)} buy`)}{lineaged(period, "sell_mwh", `${fmt(position.sell_mwh)} sell`)}<strong>Q after {fmt(position.q_after_mwh)}</strong></div></td><td>{signed(position.exposure_before_p50_mwh)}</td><td><div className="scenario-stack">{lineaged(period, "charge_mw", `${fmt(batteryPoint.charge_mw)} MW charge`)}{lineaged(period, "discharge_mw", `${fmt(batteryPoint.discharge_mw)} MW discharge`)}</div></td><td><div className="scenario-stack"><span>{fmt(batteryPoint.soc_start_mwh)} MWh</span>{lineaged(period, "projected_soc_mwh", `${fmt(batteryPoint.soc_end_mwh)} MWh`)}</div></td><td><div className="scenario-stack">{lineaged(period, "reserve_up_mw", `${fmt(batteryPoint.reserve_up_mw)} up`)}{lineaged(period, "reserve_down_mw", `${fmt(batteryPoint.reserve_down_mw)} down`)}</div></td><td>{fmt(batteryPoint.upward_headroom_mw)} / {fmt(batteryPoint.downward_headroom_mw)} MW</td><td>{fmt(batteryPoint.upward_duration_coverage_h, 2)} / {fmt(batteryPoint.downward_duration_coverage_h, 2)} h</td><td><div className="scenario-stack">{lineaged(period, "residual_p10_mwh", signed(position.residual_p10_mwh))}{lineaged(period, "residual_p50_mwh", signed(position.residual_p50_mwh))}{lineaged(period, "residual_p90_mwh", signed(position.residual_p90_mwh))}</div></td><td><div className="scenario-stack"><span>£{fmt(marketPoint.bid_price_gbp_per_mwh, 1)} / £{fmt(marketPoint.ask_price_gbp_per_mwh, 1)}</span><span>WAP {marketPoint.wap_used_gbp_per_mwh === null ? "—" : `£${fmt(marketPoint.wap_used_gbp_per_mwh, 1)}`}</span><small>{marketPoint.executable_data_mode} executable · {marketPoint.reference_price_mode} reference</small></div></td><td><div className="scenario-stack"><span>{fmt(marketPoint.consumed_bid_depth_mwh)} bid / {fmt(marketPoint.consumed_ask_depth_mwh)} ask</span><span>{fmt(marketPoint.unfilled_volume_mwh)} MWh unfilled</span></div></td><td><div className="scenario-stack"><strong>{gbp(riskPoint.total_period_contribution_gbp)} total</strong><span>{gbp(-riskPoint.imbalance_cost_gbp)} imbalance</span><span>{gbp(-riskPoint.tail_risk_penalty_gbp)} tail</span><span>{gbp(riskPoint.reserve_bm_service_value_gbp)} reserve/BM</span></div></td><td className="why-cell"><div>{batteryPoint.binding_constraints.map((item) => <span key={item}>{item}</span>)}</div><p>{position.one_line_reason}</p></td></tr>;
  })}</tbody></table></div>;
}

function SpTooltip({ run, periodId, kind }: { run: OptimisationRun; periodId: string; kind: "battery" | "position" | "market" | "risk" }) {
  const meta = run.interaction_points.find((point) => point.stable_sp_id === periodId);
  const battery = run.battery_path_series.find((point) => point.delivery_period === periodId);
  const position = run.position_path_series.find((point) => point.delivery_period === periodId);
  const market = run.market_execution_series.find((point) => point.delivery_period === periodId);
  const risk = run.risk_value_series.find((point) => point.delivery_period === periodId);
  if (!meta || !battery || !position || !market || !risk) return null;
  return <div className="sp-tooltip-content"><header><strong>{meta.display_label}</strong><span>{meta.uk_delivery_time}</span><PhaseBadge phase={meta.phase} /></header>
    {kind === "battery" && <><TooltipGrid rows={[
      ["Charge", `${fmt(battery.charge_mw)} MW · ${fmt(battery.charge_mwh)} MWh`], ["Discharge", `${fmt(battery.discharge_mw)} MW · ${fmt(battery.discharge_mwh)} MWh`],
      ["SoC", `${fmt(battery.soc_start_mwh)} → ${fmt(battery.soc_end_mwh)} MWh`], ["Reserve up / down", `${fmt(battery.reserve_up_mw)} / ${fmt(battery.reserve_down_mw)} MW`],
      ["Headroom up / down", `${fmt(battery.upward_headroom_mw)} / ${fmt(battery.downward_headroom_mw)} MW`], ["Duration up / down", `${fmt(battery.upward_duration_coverage_h, 2)} / ${fmt(battery.downward_duration_coverage_h, 2)} h`],
    ]} /><ConstraintLine values={battery.binding_constraints} /><p>{String(meta.tooltip_payload.battery_reason)}</p></>}
    {kind === "position" && <><TooltipGrid rows={[
      ["Buy / sell", `${fmt(position.buy_mwh)} / ${fmt(position.sell_mwh)} MWh`], ["Q before / after", `${fmt(position.q_before_mwh)} / ${fmt(position.q_after_mwh)} MWh`],
      ["Exposure P10/P50/P90", `${fmt(position.exposure_before_p10_mwh)} / ${fmt(position.exposure_before_p50_mwh)} / ${fmt(position.exposure_before_p90_mwh)}`], ["Residual P10/P50/P90", `${fmt(position.residual_p10_mwh)} / ${fmt(position.residual_p50_mwh)} / ${fmt(position.residual_p90_mwh)}`],
      ["Market action", position.market_action_allowed ? "Allowed" : "Not allowed"], ["Gate Closure", position.gate_closure_status.replaceAll("_", " ")],
    ]} /><ConstraintLine values={position.binding_constraints} /><p>{position.one_line_reason}</p></>}
    {kind === "market" && <><TooltipGrid rows={[
      ["Bid / ask", `£${fmt(market.bid_price_gbp_per_mwh, 2)} / £${fmt(market.ask_price_gbp_per_mwh, 2)}`], ["WAP / spread", `${market.wap_used_gbp_per_mwh === null ? "—" : `£${fmt(market.wap_used_gbp_per_mwh, 2)}`} / £${fmt(market.spread_gbp_per_mwh, 2)}`],
      ["Bid / ask depth", `${fmt(market.bid_depth_mwh)} / ${fmt(market.ask_depth_mwh)} MWh`], ["Consumed bid / ask", `${fmt(market.consumed_bid_depth_mwh)} / ${fmt(market.consumed_ask_depth_mwh)} MWh`],
      ["Unfilled", `${fmt(market.unfilled_volume_mwh)} MWh`], ["Executable mode", market.executable_data_mode], ["Reference (non-executable)", `£${fmt(market.reference_price_gbp_per_mwh, 2)} · ${market.reference_price_mode}`],
    ]} /></>}
    {kind === "risk" && <TooltipGrid rows={[
      ["Market value / cost", gbp(risk.market_value_or_cost_gbp)], ["Imbalance cost", gbp(risk.imbalance_cost_gbp)], ["Tail-risk penalty", gbp(risk.tail_risk_penalty_gbp)],
      ["Degradation", gbp(risk.degradation_cost_gbp)], ["Reserve / BM / service", gbp(risk.reserve_bm_service_value_gbp)], ["Optionality lost", gbp(risk.optionality_lost_gbp)],
      ["Total contribution", gbp(risk.total_period_contribution_gbp)], ["Worst-case residual", `${fmt(risk.worst_case_residual_mwh)} MWh`], ["Binding constraints", String(risk.binding_constraint_count)],
    ]} />}
  </div>;
}

function TooltipGrid({ rows }: { rows: [string, string][] }) { return <dl className="tooltip-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>; }
function ConstraintLine({ values }: { values: string[] }) { return values.length ? <div className="tooltip-constraints">{values.map((value) => <code key={value}>{value}</code>)}</div> : <small>No binding constraint recorded.</small>; }

function SpDetailDrawer({ run, periodId, onClose, onClear }: { run: OptimisationRun; periodId: string; onClose: () => void; onClear: () => void }) {
  const meta = run.interaction_points.find((point) => point.stable_sp_id === periodId);
  const battery = run.battery_path_series.find((point) => point.delivery_period === periodId);
  const position = run.position_path_series.find((point) => point.delivery_period === periodId);
  const market = run.market_execution_series.find((point) => point.delivery_period === periodId);
  const risk = run.risk_value_series.find((point) => point.delivery_period === periodId);
  if (!meta || !battery || !position || !market || !risk) return null;
  const scrollToRow = () => { document.getElementById(meta.linked_trajectory_row_id)?.scrollIntoView({ behavior: "smooth", block: "center" }); onClose(); };
  return <div className="sp-detail-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><aside className="sp-detail-drawer" role="dialog" aria-modal="true" aria-label={`${meta.display_label} settlement-period details`}><header><div><p className="eyebrow">PINNED SETTLEMENT PERIOD</p><h2>{meta.display_label}</h2><span>{meta.uk_delivery_time}</span></div><button type="button" onClick={onClose}>Close</button></header><div className="sp-detail-trust"><PhaseBadge phase={meta.phase} /><Badge value={meta.source_mode} /><span>Live trust: no</span></div>
    <section><h3>Battery decision</h3><SpTooltip run={run} periodId={periodId} kind="battery" /></section>
    <section><h3>Position decision</h3><SpTooltip run={run} periodId={periodId} kind="position" /></section>
    <section><h3>Market execution</h3><SpTooltip run={run} periodId={periodId} kind="market" /></section>
    <section><h3>Risk and value</h3><SpTooltip run={run} periodId={periodId} kind="risk" /></section>
    <section><h3>Source provenance</h3><div className="provenance-list">{meta.source_provenance.map((source) => <code key={source}>{source}</code>)}</div><p>{String(meta.tooltip_payload.trust_statement)}</p></section>
    <footer><button type="button" className="primary-action" onClick={scrollToRow}>Scroll table to this SP</button><button type="button" onClick={onClear}>Clear selection</button></footer>
  </aside></div>;
}
