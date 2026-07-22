import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { FlatSeriesSummary, LargeChart } from "./CockpitChart";
import { ConnectionStatus } from "./ConnectionStatus";
import { HistoryWindowSelector } from "./HistoryWindowSelector";
import { loadLineage, loadLiveState, refreshLiveState } from "./api";
import { filterChartSeries, historyWindowLabels, type CustomWindow, type HistoryWindow } from "./historyWindow";
import { ProductNav } from "./ProductNav";
import { formatLocalTime, formatTimestampWithZone, formatUkMarketTime } from "./time";
import { TrustStatusStrip } from "./TrustStatusStrip";
import { useRollingAutoRefresh, type RefreshCadence } from "./useRollingAutoRefresh";
import type { CanonicalDataPoint, LineageResponse, LiveStateSnapshot } from "./types";

const n = (value: number, digits = 1) => value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const cadenceLabels: Record<RefreshCadence, string> = { manual: "Manual", "5": "5 min", "15": "15 min", "30": "30 min", boundary: "Settlement-period boundary" };

export function LiveStatePage() {
  const [live, setLive] = useState<LiveStateSnapshot | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [browserNow, setBrowserNow] = useState(new Date());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [historyWindow, setHistoryWindow] = useState<HistoryWindow>("30d");
  const [customWindow, setCustomWindow] = useState<CustomWindow>({ from: "", to: "" });
  const accept = useCallback((next: LiveStateSnapshot) => { setLive(next); setLastPoll(new Date()); setError(null); }, []);
  const load = useCallback(async () => { try { accept(await loadLiveState()); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load rolling state"); } }, [accept]);
  const refresh = useCallback(async () => { setBusy(true); try { accept(await refreshLiveState()); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to refresh rolling state"); } finally { setBusy(false); } }, [accept]);
  const auto = useRollingAutoRefresh(refresh);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const timer = window.setInterval(() => setBrowserNow(new Date()), 1000); return () => window.clearInterval(timer); }, []);
  const byId = useMemo(() => new Map(live?.lineage_values.map((point) => [point.value_id, point]) ?? []), [live]);
  const open = async (point: CanonicalDataPoint | null | undefined) => { if (!point) return; try { setLineage(await loadLineage(point.value_id)); } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); } };
  const chart = (key: string) => live ? filterChartSeries(live.chart_series[key] ?? [], historyWindow, live.state.current_time, customWindow) : [];
  const largestRevision = live?.forecast_vintage_series.reduce((best, point) => Math.abs(point.delta_mwh) > Math.abs(best.delta_mwh) ? point : best, live.forecast_vintage_series[0]);
  const reserveSeries = chart("battery").filter((item) => item.unit === "MW");
  const reserveIsFlat = reserveSeries.length > 0 && reserveSeries.every((item) => Boolean(item.flat_explanation));

  return <div className="app-shell live-page graph-led-page">
    <header className="topbar"><div className="brand-lockup"><div className="brand-mark live-pulse">IP</div><div><p className="eyebrow">ROLLING INTRADAY COCKPIT</p><h1>Live Market State</h1></div></div><ProductNav active="live" /><ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} /></header>
    <main>
      {error && <div className="error-banner"><strong>Backend error</strong><span>{error}</span><button onClick={() => void load()}>Retry</button></div>}
      {live ? <>
        <section className="live-clock-strip panel">
          <div><span>Browser clock</span><strong>{formatLocalTime(browserNow)}</strong><small>local time</small></div>
          <div><span>UK market clock</span><strong>{formatUkMarketTime(live.state.current_time)}</strong><small>backend time · UK time</small></div>
          <div><span>Current / next</span><strong>SP{live.state.current_settlement_period} → SP{live.state.next_settlement_period}</strong><small>{live.state.next_settlement_label}</small></div>
          <div><span>Next Gate Closure</span><strong>{n(live.state.minutes_to_gate_closure, 0)} min</strong><small>{formatTimestampWithZone(live.state.next_gate_closure_at, "UK time")}</small></div>
          <div><span>Horizon start</span><strong>{formatUkMarketTime(live.state.optimisation_horizon_start)}</strong><small>UK time · {live.state.effective_horizon_mode.replaceAll("_", " ")}</small></div>
          <div><span>Horizon end</span><strong>{formatUkMarketTime(live.state.optimisation_horizon_end)}</strong><small>UK time</small></div>
          <div><span>Latest run</span><strong className="mono compact-id">{live.state.latest_optimisation_run_id ?? "Pending"}</strong><small>{live.state.current_regime.replaceAll("_", " ")}</small></div>
        </section>
        <section className="market-context-header panel"><div><p className="eyebrow">MARKET CONTEXT</p><strong>SP{live.state.current_settlement_period} · {live.state.current_regime.replaceAll("_", " ")} · {live.state.state_source_mode}</strong><span>{historyWindowLabels[historyWindow]} SAMPLE history through {formatTimestampWithZone(live.state.current_time, "UK time")}</span></div><HistoryWindowSelector value={historyWindow} onChange={setHistoryWindow} custom={customWindow} onCustomChange={setCustomWindow} /></section>
        <section className="cockpit-controls panel" aria-label="Live refresh controls">
          <button className="primary-action" disabled={busy} onClick={() => void refresh()}>{busy ? "Refreshing…" : "Refresh now"}</button>
          <button aria-pressed={auto.autoRefresh} onClick={() => auto.setAutoRefresh(!auto.autoRefresh)}>{auto.autoRefresh ? "Auto-refresh on" : "Auto-refresh off"}</button>
          <label><span>Refresh cadence</span><select value={auto.cadence} onChange={(event) => { const value = event.target.value as RefreshCadence; auto.setCadence(value); if (value === "manual") auto.setAutoRefresh(false); }}>{Object.entries(cadenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <span className="control-note">Backend time is authoritative. Refresh naturally reconciles any completed SAMPLE path.</span>
        </section>
        <TrustStatusStrip state={live.state} warnings={live.warnings} />

        <div className="section-heading"><div><p className="eyebrow">01 · CURRENT STATE HISTORY</p><h2>Production, demand and forecast</h2></div><span>{n(live.history.length, 0)} observations up to backend time</span></div>
        <section className="large-chart-stack">
          <LargeChart title="Renewable production history" insight={live.chart_insights.production} subtitle={`Production ${n(live.production_demand.production_delta_mw)} MW since the previous refresh. Actual and forecast are SAMPLE.`} series={chart("production")} />
          <LargeChart title="Demand and residual demand history" insight={live.chart_insights.demand} subtitle={`Demand ${live.production_demand.demand_delta_mw >= 0 ? "+" : ""}${n(live.production_demand.demand_delta_mw)} MW since the previous refresh.`} series={chart("demand")} />
          <LargeChart title="Forecast vintage history" insight={live.chart_insights.forecast_history} subtitle="Rolling P50, previous vintage, simulated actual and forecast error across the selected history." series={chart("forecast_history")} includeZero />
          <LargeChart title="Future forecast vintages and uncertainty" insight={live.chart_insights.forecast_vintage} subtitle={largestRevision ? `Largest revision SP${largestRevision.settlement_period} · confidence ${n(largestRevision.confidence_score * 100, 0)}% · ${largestRevision.driver.replaceAll("_", " ")}.` : "Latest and previous forecast vintages."} series={chart("forecast_vintage")} includeZero forecastBoundary={live.state.optimisation_horizon_start} />
        </section>

        <div className="section-heading"><div><p className="eyebrow">02 · MARKET AND SYSTEM</p><h2>Executable market state</h2></div><span>Reference is diagnostic; bid/ask depth is executable SAMPLE data</span></div>
        <section className="large-chart-stack">
          <LargeChart title="Market price and order-book quotes" insight={live.chart_insights.market_price} subtitle={`Current spread £${n(live.market.spread_gbp_per_mwh, 2)}/MWh · WAP 10 sell/buy £${n(live.market.sell_wap_10_mwh ?? 0, 2)} / £${n(live.market.buy_wap_10_mwh ?? 0, 2)}.`} series={chart("market_price")} />
          <LargeChart title="Visible order-book depth" insight={live.chart_insights.market_depth} subtitle="Execution walks bid-side depth for sells and ask-side depth for buys." series={chart("market_depth")} includeZero />
          <LargeChart title="GB system frequency" insight={live.chart_insights.frequency} subtitle={`${n(live.market.frequency_hz, 3)} Hz now · backend SAMPLE observation tape.`} series={chart("frequency")} />
          <LargeChart title="System tightness" insight={live.chart_insights.system} subtitle={`Regime ${live.market.market_regime.replaceAll("_", " ")} · tightness ${live.market.system_tightness_score >= 0 ? "+" : ""}${n(live.market.system_tightness_score, 2)}.`} series={chart("system").filter((item) => item.unit === "score")} includeZero />
          <LargeChart title="Demand and production surprises" insight="Demand and production surprises show which simulated system driver is moving away from its recent baseline." subtitle="Recent deviations used by the SAMPLE forecast update, in MW." series={chart("system").filter((item) => item.unit === "MW")} includeZero />
        </section>

        <div className="section-heading"><div><p className="eyebrow">03 · PORTFOLIO AND BATTERY</p><h2>Carried state</h2></div><span>Previous projected path is reconciled only as backend time passes</span></div>
        <section className="large-chart-stack">
          <LargeChart title="Portfolio Q and pre-action exposure" insight={live.chart_insights.portfolio} subtitle={`Current Q ${n(live.portfolio_battery.current_q_mwh)} MWh · exposure ${live.portfolio_battery.exposure_before_action_mwh >= 0 ? "+" : ""}${n(live.portfolio_battery.exposure_before_action_mwh)} MWh.`} series={chart("portfolio")} includeZero />
          <LargeChart title="Battery SoC history" insight={live.chart_insights.battery} subtitle={`Current SoC ${n(live.portfolio_battery.current_soc_mwh)} MWh · previous projected ${live.portfolio_battery.previous_projected_soc_mwh === null ? "not yet available" : `${n(live.portfolio_battery.previous_projected_soc_mwh)} MWh`}.`} series={chart("battery").filter((item) => item.unit === "MWh")} focusedScale />
          {reserveIsFlat ? <FlatSeriesSummary title="Reserve held" insight={live.chart_insights.battery} series={reserveSeries} /> : <LargeChart title="Reserve held" insight="Reserve history shows how committed up/down capability changes with the carried battery state." subtitle={`${n(live.portfolio_battery.reserve_up_held_mw)} MW up · ${n(live.portfolio_battery.reserve_down_held_mw)} MW down.`} series={reserveSeries} includeZero />}
        </section>

        <section className="live-secondary-grid">
          <article className="panel tape-panel"><header><div><p className="eyebrow">SECONDARY · DATA TAPE</p><h3>Chronological state updates</h3></div><span className="tape-live">● FLOWING</span></header><div className="event-tape">{live.events.map((event) => <button key={event.event_id} onClick={() => void open(event.value_id ? byId.get(event.value_id) : null)} disabled={!event.value_id}><time>{formatLocalTime(event.occurred_at)}</time><span className={`event-dot ${event.event_type}`} /><div><strong>{event.event_type.replaceAll("_", " ")}</strong><p>{event.message}</p></div><Badge value={event.source_mode} /></button>)}</div></article>
          <article className="panel trust-panel"><p className="eyebrow">DATA TRUST</p><h3>Calculation and trading trust</h3><div className="trust-row"><Badge value={live.state.state_source_mode} /><Badge value={live.state.quality} /><strong className={`readiness ${live.state.trust.readiness.toLowerCase()}`}>{live.state.trust.readiness}</strong></div><dl><dt>Calculation allowed</dt><dd>{live.state.trust.calculation_allowed ? "YES" : "NO"}</dd><dt>Trustworthy for live trading</dt><dd>{live.state.trust.trustworthy_for_live_trading ? "YES" : "NO"}</dd></dl>{live.warnings.map((warning) => <p key={warning}>{warning}</p>)}</article>
        </section>
      </> : <div className="empty panel">Connecting to rolling state…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}
