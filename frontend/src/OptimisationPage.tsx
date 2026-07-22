import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { LargeChart } from "./CockpitChart";
import { ConnectionStatus } from "./ConnectionStatus";
import { loadCurrentOptimisation, loadLineage, loadLiveState, refreshRollingCockpit, resetLiveState, runRollingOptimisation, setHorizonMode, setLiveRegime } from "./api";
import { ProductNav } from "./ProductNav";
import { formatTimestampWithZone, formatUkMarketTime } from "./time";
import { useRollingAutoRefresh, type RefreshCadence } from "./useRollingAutoRefresh";
import type { CanonicalDataPoint, ChartSeries, HorizonMode, LineageResponse, LiveStateSnapshot, OptimisationPeriodResult, OptimisationRun, SampleRegime } from "./types";

const fmt = (value: number, digits = 1) => value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const gbp = (value: number) => `${value < 0 ? "−" : ""}£${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const signed = (value: number) => `${value > 0 ? "+" : ""}${fmt(value)} MWh`;
const regimes: { value: SampleRegime; label: string }[] = [
  { value: "normal", label: "Normal" }, { value: "tightening", label: "Tightening" },
  { value: "oversupply", label: "Oversupply" }, { value: "price_spike", label: "Price spike" },
  { value: "wind_forecast_miss", label: "Wind forecast miss" }, { value: "demand_surprise", label: "Demand surprise" },
];
const horizonModes: { value: HorizonMode; label: string }[] = [
  { value: "next_8_periods", label: "Next 8 SPs" }, { value: "end_of_day", label: "End of delivery day" }, { value: "next_auction", label: "Next auction" },
];
const cadenceLabels: Record<RefreshCadence, string> = { manual: "Manual", "5": "5 min", "15": "15 min", "30": "30 min", boundary: "Settlement-period boundary" };

export function OptimisationPage() {
  const [run, setRun] = useState<OptimisationRun | null>(null);
  const [live, setLive] = useState<LiveStateSnapshot | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const accept = useCallback((nextRun: OptimisationRun, nextLive: LiveStateSnapshot) => { setRun(nextRun); setLive(nextLive); setLastPoll(new Date()); setError(null); }, []);
  const load = useCallback(async () => { try { const [nextRun, nextLive] = await Promise.all([loadCurrentOptimisation(), loadLiveState()]); accept(nextRun, nextLive); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load rolling optimisation"); } }, [accept]);
  useEffect(() => { void load(); }, [load]);
  const act = async (name: string, action: () => Promise<{ optimisation: OptimisationRun; live_state: LiveStateSnapshot }>) => { setBusy(name); try { const result = await action(); accept(result.optimisation, result.live_state); } catch (cause) { setError(cause instanceof Error ? cause.message : `${name} failed`); } finally { setBusy(null); } };
  const refresh = useCallback(async () => { await act("refresh", refreshRollingCockpit); }, [accept]);
  const auto = useRollingAutoRefresh(refresh);
  const regime = async (value: SampleRegime) => { await act("regime", () => setLiveRegime(value)); };
  const horizon = async (value: HorizonMode) => { await act("horizon", () => setHorizonMode(value)); };
  const open = async (point: CanonicalDataPoint | null | undefined) => { if (!point) return; try { setLineage(await loadLineage(point.value_id)); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); } };
  const first = run?.projected_trajectory[0];
  const currentForecastDelta = run?.inputs[0]?.values.generation_p50_mwh.delta_vs_previous ?? 0;
  const driverEntries = useMemo(() => run ? Object.entries(run.explanation_drivers) : [], [run]);
  const chart = (key: string, unit?: string) => (run?.chart_series[key] ?? []).filter((item) => !unit || item.unit === unit);
  const contributions: ChartSeries[] = run ? [{ key: "drivers", label: "Driver score", unit: "score", kind: "bar", points: run.driver_contributions.map((item) => ({ label: item.label, value: item.score, timestamp: null, settlement_period: null, delivery_period: null })) }] : [];
  const sensitivity: ChartSeries[] = run ? [{ key: "sensitivity", label: "Change in diagnostic value", unit: "GBP", kind: "bar", points: run.sensitivities.map((item) => ({ label: item.label, value: item.delta_gbp, timestamp: null, settlement_period: null, delivery_period: null })) }] : [];

  return <div className="app-shell optimisation-page graph-led-page">
    <header className="topbar"><div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">ROLLING INTRADAY COCKPIT</p><h1>Rolling Optimisation</h1></div></div><ProductNav active="optimisation" /><ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} /></header>
    <main>
      {error && <div className="error-banner"><strong>Optimisation error</strong><span>{error}</span><button onClick={() => void load()}>Retry</button></div>}
      {run && live && first ? <>
        <section className="optimisation-run-head panel"><div><p className="eyebrow">CURRENT IMMUTABLE RUN</p><h2>{run.run_id}</h2><span>{formatTimestampWithZone(run.as_of, "UK time")} · {run.solver} · {run.solver_status}</span></div><div className="run-head-grid"><RunStat label="Current SP" value={`SP${run.starting_state.current_settlement_period}`} /><RunStat label="Horizon range" value={`${formatUkMarketTime(run.starting_state.horizon_start)}–${formatUkMarketTime(run.starting_state.horizon_end)} UK time`} /><RunStat label="Horizon mode" value={run.starting_state.horizon_mode.replaceAll("_", " ")} /><RunStat label="Starting SoC" value={`${fmt(run.starting_state.starting_soc_mwh)} MWh`} /><RunStat label="Starting Qₜ" value={`${fmt(run.starting_state.starting_q_mwh)} MWh`} /><RunStat label="Forecast vintage" value={run.starting_state.forecast_vintage_id} mono /><RunStat label="Market snapshot" value={run.starting_state.market_snapshot_id} mono /></div><div className="run-trust"><Badge value={run.starting_state.source_mode} /><strong className={`readiness ${run.readiness.status.toLowerCase()}`}>{run.readiness.status}</strong><span>Calculation {run.readiness.calculation_allowed ? "allowed" : "blocked"}</span><span>Live trust {run.readiness.trustworthy_for_live_trading ? "yes" : "no"}</span></div></section>
        <section className="run-change-hero panel"><div><p className="eyebrow">WHAT CHANGED SINCE PREVIOUS RUN</p><h3>{run.change_since_previous.trajectory_change_reason}</h3></div><div><Change label="Forecast" value={run.change_since_previous.forecast_change_mwh} unit="MWh" /><Change label="Demand" value={run.change_since_previous.demand_change_mw} unit="MW" /><Change label="Price" value={run.change_since_previous.price_change_gbp_per_mwh} unit="£/MWh" /><Change label="Depth" value={run.change_since_previous.depth_change_mwh} unit="MWh" /><Change label="Qₜ" value={run.change_since_previous.q_change_mwh} unit="MWh" /><Change label="SoC" value={run.change_since_previous.soc_change_mwh} unit="MWh" /></div></section>
        <div className="simulation-banner important"><strong>NOT REAL EXECUTION OR LIVE CONTROL</strong><span>{live.state.simulation_assumption.replace("Sample simulation", "SAMPLE simulation")}</span></div>
        <section className="cockpit-controls optimisation-controls panel">
          <button className="primary-action" disabled={Boolean(busy)} onClick={() => void act("run", runRollingOptimisation)}>▶ {busy === "run" ? "Solving…" : "Run optimisation now"}</button>
          <button disabled={Boolean(busy)} onClick={() => void refresh()}>{busy === "refresh" ? "Refreshing…" : "Refresh now"}</button>
          <button aria-pressed={auto.autoRefresh} onClick={() => auto.setAutoRefresh(!auto.autoRefresh)}>{auto.autoRefresh ? "Auto-refresh on" : "Auto-refresh off"}</button>
          <label><span>Refresh cadence</span><select value={auto.cadence} onChange={(event) => { const value = event.target.value as RefreshCadence; auto.setCadence(value); if (value === "manual") auto.setAutoRefresh(false); }}>{Object.entries(cadenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label><span>Horizon mode</span><select value={live.state.horizon_mode} disabled={Boolean(busy)} onChange={(event) => void horizon(event.target.value as HorizonMode)}>{horizonModes.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label>
          {live.state.state_source_mode === "SAMPLE" && <label><span>Scenario regime · SAMPLE only</span><select value={live.state.current_regime} disabled={Boolean(busy)} onChange={(event) => void regime(event.target.value as SampleRegime)}>{regimes.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label>}
          <button disabled={Boolean(busy)} onClick={() => void act("reset", resetLiveState)}>Reset sample state</button>
        </section>
        {live.state.horizon_warning && <div className="warning-banner"><strong>Horizon fallback</strong> {live.state.horizon_warning}</div>}
        {run.sanity_warnings.map((warning) => <div className="warning-banner sanity" key={warning}>{warning}</div>)}

        <section className="current-state-ribbon">
          <StateCard label="Production" value={`${fmt(live.production_demand.renewable_production_mw)} MW`} sub={`wind ${fmt(live.production_demand.wind_mw)} · solar ${fmt(live.production_demand.solar_mw)}`} />
          <StateCard label="Demand / residual" value={`${fmt(live.production_demand.demand_mw, 0)} MW`} sub={`${fmt(live.production_demand.residual_demand_mw, 0)} MW residual`} />
          <StateCard label="Frequency / tightness" value={`${fmt(live.market.frequency_hz, 3)} Hz`} sub={`${live.market.system_tightness_score >= 0 ? "+" : ""}${fmt(live.market.system_tightness_score, 2)} score`} />
          <StateCard label="Executable book" value={`£${fmt(live.market.best_bid_gbp_per_mwh, 2)} / £${fmt(live.market.best_ask_gbp_per_mwh, 2)}`} sub={`${fmt(live.market.bid_depth_mwh)} / ${fmt(live.market.ask_depth_mwh)} MWh depth`} />
          <StateCard label="Forecast revision" value={`${Number(currentForecastDelta) > 0 ? "+" : ""}${fmt(Number(currentForecastDelta))} MWh`} sub={`P50 ${fmt(first.generation_p50_mwh)} MWh`} />
          <StateCard label="Exposure / SoC" value={signed(first.exposure_before_p50_mwh)} sub={`SoC ${fmt(first.soc_before_mwh)} MWh · reserve ${fmt(first.reserve_up_mw)}/${fmt(first.reserve_down_mw)} MW`} />
        </section>

        <div className="section-heading hero-table-heading"><div><p className="eyebrow">01 · HERO DECISION TABLE</p><h2>Full action path by settlement period</h2></div><span>Delivery and Gate Closure timestamps UK time · suggested, not executed</span></div>
        <TrajectoryTable periods={run.projected_trajectory} open={open} />

        <div className="section-heading"><div><p className="eyebrow">02 · DECISION CHARTS</p><h2>Optimised paths and risk</h2></div><span>Backend-owned chart series with units and focused scales</span></div>
        <section className="large-chart-stack optimisation-charts">
          <LargeChart title="Optimised market action path" subtitle="Buy and sell are mutually exclusive and consume ask-side and bid-side depth respectively." series={chart("action_path", "MWh")} includeZero />
          <LargeChart title="Optimised battery action path" subtitle="Charge and discharge are mutually exclusive. Positive bars are selected power in MW." series={chart("action_path", "MW")} includeZero />
          <LargeChart title="Projected SoC path" subtitle="Physical range 10–100 MWh · safe operating band 35–70 MWh · preferred terminal 55 MWh · minimum terminal 35 MWh." series={chart("soc_path").filter((item) => !["soc_min", "soc_max"].includes(item.key))} safeBand={{ min: 35, max: 70, label: "safe operating band" }} />
          <LargeChart title="Reserve capability and commitments" subtitle="Reserve selected, committed requirements and available power headroom. Near-binding periods are listed in the hero table." series={chart("reserve_path", "MW")} includeZero />
          <LargeChart title="Reserve duration coverage" subtitle="Energy-duration coverage must remain at or above committed duration requirements." series={chart("reserve_path", "h")} includeZero />
          <LargeChart title="Residual exposure fan" subtitle="Exposure before action and residual P10/P50/P90 after action. The zero line separates long from short." series={chart("exposure_fan")} includeZero />
          <LargeChart title="Market execution prices" subtitle="Best bid, best ask and consumed WAP. Reference prices are not executable." series={chart("market_execution", "GBP/MWh").filter((item) => item.key !== "spread")} />
          <LargeChart title="Executable spread" subtitle="Bid/ask spread by settlement period in £/MWh." series={chart("market_execution", "GBP/MWh").filter((item) => item.key === "spread")} includeZero />
          <LargeChart title="Market depth, consumption and unfilled volume" subtitle="Consumed depth is constrained by visible levels; Gate Closed periods have zero market trades." series={chart("market_execution", "MWh")} includeZero />
          <LargeChart title="Gate Closure markers" subtitle="1 means Gate Closed and forces buy/sell to zero; battery, residual and service logic can remain active." series={chart("market_execution", "flag")} includeZero />
          <LargeChart title="Period diagnostic value" subtitle="Net contribution by settlement period after market, imbalance, tail, degradation, service and terminal terms." series={chart("period_value")} includeZero />
          <LargeChart title="Objective and risk breakdown" subtitle="Positive bars add diagnostic value; negative bars are costs or penalties." series={chart("objective_breakdown")} includeZero />
          <LargeChart title="Driver contribution scores" subtitle="Relative strength of solved forecast, system, market, battery, reserve, terminal, tail-risk and binding drivers." series={contributions} includeZero />
          <LargeChart title="One-factor sensitivity" subtitle="Estimated change in total diagnostic value for adverse or alternative assumptions; these are diagnostics, not orders." series={sensitivity} includeZero />
        </section>

        <div className="section-heading"><div><p className="eyebrow">03 · RISK MEASURES</p><h2>Exposed diagnostics</h2></div></div>
        <section className="risk-measure-grid">{run.risk_measures.map((item) => <article className={`panel risk-measure ${item.status.toLowerCase()}`} key={item.key}><span>{item.label}</span><strong>{fmt(item.value, item.unit === "GBP" ? 0 : 2)} {item.unit}</strong><small>{item.status}</small></article>)}</section>
        <section className="optimisation-lower-grid">
          <article className="panel driver-panel"><p className="eyebrow">WHY THE MODEL CHOSE THIS</p><h3>Drivers from solved inputs and constraints</h3><div className="driver-list">{driverEntries.map(([key, value], index) => <div key={key}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{key.replaceAll("_", " ")}</strong><p>{value}</p></div></div>)}</div></article>
          <article className="panel sensitivity-detail"><p className="eyebrow">SENSITIVITY INTERPRETATION</p><h3>What would move value</h3>{run.sensitivities.map((item) => <div key={item.key}><strong>{item.label}: {gbp(item.delta_gbp)}</strong><span>{item.stressed_case}</span><p>{item.explanation}</p></div>)}</article>
        </section>
      </> : <div className="empty panel">Preparing full-action rolling optimisation…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function RunStat({ label, value, mono }: { label: string; value: string; mono?: boolean }) { return <div><span>{label}</span><strong className={mono ? "mono compact-id" : ""}>{value}</strong></div>; }
function StateCard({ label, value, sub }: { label: string; value: string; sub: string }) { return <article className="panel"><span>{label}</span><strong>{value}</strong><small>{sub}</small></article>; }
function Change({ label, value, unit }: { label: string; value: number; unit: string }) { return <article><span>{label}</span><strong className={value > 0 ? "positive" : value < 0 ? "negative" : ""}>{value > 0 ? "+" : ""}{fmt(value)} {unit}</strong></article>; }

function TrajectoryTable({ periods, open }: { periods: OptimisationPeriodResult[]; open: (point?: CanonicalDataPoint | null) => void | Promise<void> }) {
  const value = (period: OptimisationPeriodResult, key: string, text: string) => <button className="table-value" onClick={() => void open(period.values[key])}>{text}</button>;
  return <div className="table-wrap panel trajectory-table hero-decision-table"><table><thead><tr><th>SP / delivery / Gate</th><th>Forecast P10 / P50 / P90</th><th>Demand / residual</th><th>Qₜ / exposure before</th><th>Buy / sell</th><th>Charge / discharge</th><th>Reserve up / down</th><th>Projected SoC</th><th>Residual P10 / P50 / P90</th><th>Bid / ask / WAP</th><th>Depth used / unfilled</th><th>Period value / risk</th><th>Binding constraints / one-line reason</th></tr></thead><tbody>{periods.map((period) => <tr key={period.delivery_period} className={!period.tradeable ? "gate-closed-row" : ""}><td><strong>SP{period.settlement_period}</strong><small>{formatUkMarketTime(period.delivery_start)}–{formatUkMarketTime(period.delivery_end)} UK time</small><span className={`gate-badge ${period.tradeable ? "open" : "closed"}`}>{period.tradeable ? "TRADABLE" : "GATE CLOSED"}</span><small>GC {formatUkMarketTime(period.gate_closure_at)} UK time</small></td><td><div className="scenario-stack"><span>P10 {fmt(period.generation_p10_mwh)}</span><span>P50 {fmt(period.generation_p50_mwh)}</span><span>P90 {fmt(period.generation_p90_mwh)}</span></div></td><td>{fmt(period.demand_mw, 0)} MW<small>{fmt(period.residual_demand_mw, 0)} MW residual</small></td><td><div className="scenario-stack"><span>Q {fmt(period.q_before_action_mwh)} MWh</span><span>P10 {signed(period.exposure_before_p10_mwh)}</span><span>P50 {signed(period.exposure_before_p50_mwh)}</span><span>P90 {signed(period.exposure_before_p90_mwh)}</span></div></td><td><div className="scenario-stack">{value(period, "buy_mwh", `${fmt(period.buy_mwh)} buy`)}{value(period, "sell_mwh", `${fmt(period.sell_mwh)} sell`)}</div></td><td><div className="scenario-stack">{value(period, "charge_mw", `${fmt(period.charge_mw)} MW C`)}{value(period, "discharge_mw", `${fmt(period.discharge_mw)} MW D`)}</div></td><td><div className="scenario-stack">{value(period, "reserve_up_mw", `${fmt(period.reserve_up_mw)} up`)}{value(period, "reserve_down_mw", `${fmt(period.reserve_down_mw)} down`)}<small>req {fmt(period.upward_commitment_mw)}/{fmt(period.downward_commitment_mw)}</small></div></td><td>{value(period, "projected_soc_mwh", `${fmt(period.projected_soc_mwh)} MWh`)}</td><td><div className="scenario-stack">{value(period, "residual_p10_mwh", signed(period.residual_p10_mwh))}{value(period, "residual_p50_mwh", signed(period.residual_p50_mwh))}{value(period, "residual_p90_mwh", signed(period.residual_p90_mwh))}</div></td><td><div className="scenario-stack"><span>£{fmt(period.best_bid_gbp_per_mwh, 1)} / £{fmt(period.best_ask_gbp_per_mwh, 1)}</span><span>WAP {period.market_wap_gbp_per_mwh === null ? "—" : `£${fmt(period.market_wap_gbp_per_mwh, 1)}`}</span><small>slip £{fmt(period.wap_slippage_gbp_per_mwh, 2)}</small></div></td><td><div className="scenario-stack"><span>{fmt(period.visible_depth_consumed_mwh)} MWh used</span><span>{fmt(period.unfilled_market_volume_mwh)} MWh unfilled</span><small>{fmt(period.bid_depth_mwh)}/{fmt(period.ask_depth_mwh)} visible</small></div></td><td><strong>{value(period, "total_period_contribution_gbp", gbp(period.total_period_contribution_gbp))}</strong><small>imbalance + tail {gbp(-period.imbalance_risk_cost_gbp)}</small></td><td className="why-cell"><div>{period.binding_constraints.map((item) => <span key={item}>{item}</span>)}</div><p>{period.why_action}</p></td></tr>)}</tbody></table></div>;
}
