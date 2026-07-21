import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadLineage, loadMarketLiquidity } from "./api";
import type { CanonicalDataPoint, HedgeCostDiagnostic, LineageResponse, MarketPeriodSnapshot, MarketSnapshot } from "./types";

export function MarketLiquidityPage() {
  const [market, setMarket] = useState<MarketSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null);

  const reload = useCallback(async (quiet = false) => {
    try {
      const next = await loadMarketLiquidity();
      setMarket(next);
      setSelectedId((current) => current ?? next.periods[0]?.delivery_period ?? null);
      setLastLoaded(new Date());
      setError(null);
    } catch (cause) {
      if (!quiet) setError(cause instanceof Error ? cause.message : "Unable to load market diagnostics");
    }
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(true), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const selected = useMemo(
    () => market?.periods.find((period) => period.delivery_period === selectedId) ?? market?.periods[0] ?? null,
    [market, selectedId],
  );

  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try { setLineage(await loadLineage(point.value_id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); }
  };

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Market &amp; Liquidity</h1></div></div>
      <nav><a href="/data-flow">Data flow</a><a href="/forecast-position">Forecast &amp; position</a><a className="active" href="/market-liquidity">Market &amp; liquidity</a><a href="/battery-flexibility">Battery flexibility</a><span>Optimisation</span><span>Actions</span></nav>
      <div className="connection"><span className={`connection-dot ${error ? "down" : ""}`} /><span>{error ? "API issue" : "API connected"}</span><small>{lastLoaded ? time(lastLoaded.toISOString()) : "connecting…"}</small></div>
    </header>
    <main>
      <section className="hero-row market-hero"><div><p className="eyebrow">MILESTONE 1C · EXECUTABLE LIQUIDITY DIAGNOSTICS</p><h2>Can the current exposure be hedged—and what does the displayed depth imply?</h2><p className="intro">Long exposure maps to bid-side selling · short exposure maps to ask-side buying · descriptive only</p></div>{market && <MarketReadiness market={market} />}</section>
      {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
      {market?.warnings.map((warning) => <div className="diagnostic-banner" key={warning}>{warning}</div>)}
      {market && selected ? <>
        <section className="market-top-grid">
          <MarketStatus market={market} selected={selected} />
          <GatePanel period={selected} />
        </section>
        <section className="market-main-grid">
          <OrderBook period={selected} onValue={openLineage} />
          <SelectedHedge period={selected} onValue={openLineage} />
        </section>
        <div className="section-heading"><div><p className="eyebrow">04 · PERIOD HEDGE COSTS</p><h3>Exposure versus executable depth</h3></div><span>WAP sweeps the first {market.levels_considered} price levels · click values for lineage</span></div>
        <HedgeGrid periods={market.periods} selected={selected.delivery_period} onSelect={setSelectedId} onValue={openLineage} />
        <div className="section-heading"><div><p className="eyebrow">05 · DIAGNOSTIC EXPLANATION</p><h3>Market interpretation</h3></div><span>No trading recommendation is produced</span></div>
        <Explanation period={selected} sourceMode={market.source_mode} levels={market.levels_considered} />
      </> : <div className="empty panel">Waiting for executable bid/ask and depth data…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function MarketReadiness({ market }: { market: MarketSnapshot }) {
  return <div className="fp-readiness panel"><div><span>Market readiness</span><strong className={`readiness ${market.readiness.status.toLowerCase()}`}>{market.readiness.status}</strong></div><dl><dt>Calculation</dt><dd>{market.readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-trading trust</dt><dd>{market.readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd></dl></div>;
}

function MarketStatus({ market, selected }: { market: MarketSnapshot; selected: MarketPeriodSnapshot }) {
  return <article className="compact-panel panel"><header><div><p className="eyebrow">01 · MARKET STATUS</p><h3>{market.active_provider.replaceAll("_", " ")}</h3></div><div className="badges"><Badge value={market.source_mode} /><Badge value={market.quality} /></div></header><dl className="market-status-list"><dt>Active provider</dt><dd>{market.active_provider}</dd><dt>Licensed live provider</dt><dd><Badge value={market.live_provider_status} /></dd><dt>Last market refresh</dt><dd>{dateTime(selected.best_bid.lineage.retrieved_at)}</dd><dt>Displayed levels</dt><dd>{market.levels_considered} per side used for WAP</dd></dl><p className="sample-disclaimer">Sample order-book data demonstrates execution logic. It is not a live executable quote.</p></article>;
}

function GatePanel({ period }: { period: MarketPeriodSnapshot }) {
  const gate = period.gate_closure;
  return <article className="compact-panel panel"><header><div><p className="eyebrow">02 · GATE CLOSURE</p><h3>{period.delivery_period}</h3></div><span className={`gate-badge ${gate.status.toLowerCase()}`}>{gate.status}</span></header><div className="gate-clock"><strong>{gate.minutes_to_gate_closure > 0 ? format(gate.minutes_to_gate_closure) : format(Math.abs(gate.minutes_to_gate_closure))}</strong><span>{gate.minutes_to_gate_closure > 0 ? "minutes remaining" : "minutes past closure"}</span></div><dl className="market-status-list"><dt>Delivery</dt><dd>{time(period.delivery_start)}–{time(period.delivery_end)}</dd><dt>Gate Closure</dt><dd>{dateTime(gate.gate_closure_at)}</dd></dl>{gate.warning && <p className="gate-warning">{gate.warning}</p>}</article>;
}

function OrderBook({ period, onValue }: { period: MarketPeriodSnapshot; onValue: (point: CanonicalDataPoint | null) => void }) {
  return <article className="order-book-panel panel"><header><div><p className="eyebrow">03 · ORDER BOOK</p><h3>{period.delivery_period} · {time(period.delivery_start)}</h3><div className="badges order-book-badges"><Badge value={period.best_bid.lineage.source_mode} /><Badge value={period.best_bid.lineage.quality} /></div></div><div className="best-market"><Value label="Best bid" point={period.best_bid} onValue={onValue} /><Value label="Best ask" point={period.best_ask} onValue={onValue} /><Value label="Spread" point={period.liquidity.spread_value} onValue={onValue} /></div></header><div className="book-depth-summary"><Value label="Bid depth" point={period.liquidity.bid_depth_value} onValue={onValue} /><Value label="Ask depth" point={period.liquidity.ask_depth_value} onValue={onValue} /><Value label="Liquidity score" point={period.liquidity.liquidity_score_value} onValue={onValue} percent /></div><div className="book-table"><div><h4>Bids</h4>{period.bids.map((level) => <BookLevel key={`b${level.level}`} level={level} onValue={onValue} />)}</div><div><h4>Asks</h4>{period.asks.map((level) => <BookLevel key={`a${level.level}`} level={level} onValue={onValue} />)}</div></div></article>;
}

function BookLevel({ level, onValue }: { level: MarketPeriodSnapshot["bids"][number]; onValue: (point: CanonicalDataPoint | null) => void }) {
  return <div className={`book-level ${level.side.toLowerCase()}`}><span>L{level.level}</span><button onClick={() => onValue(level.price_value)}>£{format(level.price_gbp_per_mwh)}</button><button onClick={() => onValue(level.volume_value)}>{format(level.volume_mwh)} MWh</button></div>;
}

function SelectedHedge({ period, onValue }: { period: MarketPeriodSnapshot; onValue: (point: CanonicalDataPoint | null) => void }) {
  return <article className="selected-hedge panel"><header><div><p className="eyebrow">P50 HEDGE DIAGNOSTIC</p><h3>{period.p50_hedge.hedge_side} {format(period.p50_hedge.required_volume_mwh)} MWh</h3></div><span className={`hedge-side ${period.p50_hedge.hedge_side.toLowerCase()}`}>{period.p50_hedge.hedge_side}</span></header><div className="hedge-metrics"><Value label="P50 exposure" point={period.p50_hedge.exposure_value} onValue={onValue} signed /><Value label="Executable WAP" point={period.p50_hedge.execution.wap_value} onValue={onValue} /><Value label="Executable volume" point={period.p50_hedge.execution.executable_volume_value} onValue={onValue} /><Value label="Unfilled residual" point={period.p50_hedge.execution.unfilled_volume_value} onValue={onValue} /><Value label="Estimated cashflow" point={period.p50_hedge.cashflow_value} onValue={onValue} signed /></div><p>{period.p50_hedge.explanation}</p>{period.p50_hedge.liquidity_warning && <div className="liquidity-warning">{period.p50_hedge.liquidity_warning}</div>}</article>;
}

function HedgeGrid({ periods, selected, onSelect, onValue }: { periods: MarketPeriodSnapshot[]; selected: string; onSelect: (id: string) => void; onValue: (point: CanonicalDataPoint | null) => void }) {
  return <div className="table-wrap panel hedge-grid"><table><thead><tr><th>SP / delivery</th><th>P10 / P50 / P90 exposure</th><th>P50 side</th><th>Required</th><th>WAP</th><th>Executable</th><th>Unfilled</th><th>Cashflow</th><th>Bid / ask depth</th><th>Spread</th><th>Gate Closure</th><th>Liquidity</th></tr></thead><tbody>{periods.map((period) => { const hedge = period.p50_hedge; return <tr key={period.delivery_period} className={selected === period.delivery_period ? "selected-period" : ""} onClick={() => onSelect(period.delivery_period)}><td><strong>SP{period.settlement_period}</strong><small>{time(period.delivery_start)}–{time(period.delivery_end)}</small></td><td><button className="triple-exposure" onClick={(event) => { event.stopPropagation(); onValue(hedge.exposure_value); }}>{signed(period.p10_exposure_mwh)} / <b>{signed(period.p50_exposure_mwh)}</b> / {signed(period.p90_exposure_mwh)}</button></td><td><span className={`hedge-side ${hedge.hedge_side.toLowerCase()}`}>{hedge.hedge_side}</span></td><td>{format(hedge.required_volume_mwh)} MWh</td><GridValue point={hedge.execution.wap_value} onValue={onValue} /><GridValue point={hedge.execution.executable_volume_value} onValue={onValue} /><GridValue point={hedge.execution.unfilled_volume_value} onValue={onValue} warn={hedge.execution.unfilled_volume_mwh > 0} /><GridValue point={hedge.cashflow_value} onValue={onValue} /><td>{format(period.liquidity.bid_depth_mwh)} / {format(period.liquidity.ask_depth_mwh)} MWh</td><GridValue point={period.liquidity.spread_value} onValue={onValue} /><td><span className={`gate-badge ${period.gate_closure.status.toLowerCase()}`}>{period.gate_closure.status}</span><small>{signed(period.gate_closure.minutes_to_gate_closure)} min</small></td><td>{period.p50_hedge.liquidity_warning ? <span className="warning-count">Depth risk</span> : <span className="clear-state">Fillable</span>}</td></tr>; })}</tbody></table></div>;
}

function Explanation({ period, sourceMode, levels }: { period: MarketPeriodSnapshot; sourceMode: string; levels: number }) {
  const downside = period.downside_hedge;
  const depthText = downside.execution.unfilled_volume_mwh > 0 ? `only ${format(downside.execution.executable_volume_mwh)} MWh is executable within the first ${levels} ${downside.hedge_side === "BUY" ? "ask" : "bid"} levels, leaving ${format(downside.execution.unfilled_volume_mwh)} MWh unfilled` : `the first ${levels} levels can fill the full ${format(downside.required_volume_mwh)} MWh downside hedge`;
  return <section className="explanation-panel panel"><div className={`direction-orb ${downside.hedge_side.toLowerCase()}`}>{downside.hedge_side}</div><div><p>{period.delivery_period} is {format(Math.abs(period.p10_exposure_mwh))} MWh {period.p10_exposure_mwh < 0 ? "short" : "long"} under P10 and {format(Math.abs(period.p50_exposure_mwh))} MWh {period.p50_exposure_mwh < 0 ? "short" : "long"} under P50. The displayed order book shows that {depthText}. Market data is {sourceMode}, so this is executable-price logic for demonstration, not a live trading signal.</p><small>Downside diagnostic uses P10 · hedge cashflow is positive for sales and negative for purchases</small></div></section>;
}

function Value({ label, point, onValue, signed: signedValue = false, percent = false }: { label: string; point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint | null) => void; signed?: boolean; percent?: boolean }) {
  const number = point ? Number(point.value) : 0; return <button className="market-value" disabled={!point} onClick={() => onValue(point)}><span>{label}</span><strong>{point ? (percent ? `${Math.round(number * 100)}%` : signedValue ? signed(number) : format(number)) : "—"}</strong><small>{percent ? "" : point?.unit ?? "unavailable"}</small></button>;
}
function GridValue({ point, onValue, warn = false }: { point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint | null) => void; warn?: boolean }) { return <td><button className={`grid-value ${warn ? "short" : ""}`} disabled={!point} onClick={(event) => { event.stopPropagation(); onValue(point); }}>{point ? format(Number(point.value)) : "—"}<small>{point?.unit ?? ""}</small></button></td>; }
function format(value: number): string { return value.toLocaleString(undefined, { maximumFractionDigits: 2 }); }
function signed(value: number): string { return `${value > 0 ? "+" : ""}${format(value)}`; }
function time(value: string): string { return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function dateTime(value: string): string { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value)); }
