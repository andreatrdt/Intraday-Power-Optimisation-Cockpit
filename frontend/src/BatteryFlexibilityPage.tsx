import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadBatteryFlexibility, loadLineage } from "./api";
import { ConnectionStatus } from "./ConnectionStatus";
import { ProductNav } from "./ProductNav";
import { formatTimestampWithZone, formatUkMarketTime } from "./time";
import type { BatteryExposureCoverage, BatteryFlexibilitySnapshot, BatteryPeriodSnapshot, CanonicalDataPoint, LineageResponse } from "./types";

export function BatteryFlexibilityPage() {
  const [battery, setBattery] = useState<BatteryFlexibilitySnapshot | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null);

  const reload = useCallback(async (quiet = false) => {
    try {
      const next = await loadBatteryFlexibility();
      setBattery(next);
      setSelectedPeriod((current) => current ?? next.periods[0]?.delivery_period ?? null);
      setLastLoaded(new Date());
      setError(null);
    } catch (cause) {
      if (!quiet) setError(cause instanceof Error ? cause.message : "Unable to load battery diagnostics");
    }
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(true), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const selected = useMemo(
    () => battery?.periods.find((period) => period.delivery_period === selectedPeriod) ?? battery?.periods[0] ?? null,
    [battery, selectedPeriod],
  );

  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try { setLineage(await loadLineage(point.value_id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); }
  };

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Battery Flexibility</h1></div></div>
      <ProductNav active="diagnostics" />
      <ConnectionStatus error={Boolean(error)} lastPoll={lastLoaded} />
    </header>
    <main>
      <section className="hero-row battery-hero"><div><p className="eyebrow">MILESTONE 1D · PHYSICAL FLEXIBILITY DIAGNOSTICS</p><h2>What can the battery physically do now—and what flexibility would that consume?</h2><p className="intro">Deterministic feasibility · labelled opportunity-cost assumptions · no dispatch recommendation</p></div>{battery && <Readiness battery={battery} />}</section>
      {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
      {battery?.warnings.map((warning) => <div className="diagnostic-banner" key={warning}>{warning}</div>)}
      {battery && selected && battery.current_soc && battery.limits && battery.opportunity_cost ? <>
        <section className="battery-summary-grid">
          <BatteryStatus battery={battery} onValue={openLineage} />
          <Feasibility period={selected} onValue={openLineage} />
          <OpportunityCost battery={battery} onValue={openLineage} />
        </section>
        <div className="section-heading"><div><p className="eyebrow">04 · EXPOSURE COVERAGE</p><h3>Maximum support by scenario</h3></div><span>Directional capacity only · not a proposed dispatch</span></div>
        <CoveragePanel period={selected} onValue={openLineage} />
        <div className="section-heading"><div><p className="eyebrow">05 · SETTLEMENT PERIODS</p><h3>Feasibility and residual exposure grid</h3></div><span>Each row starts from current SoC · click values for lineage</span></div>
        <PeriodGrid periods={battery.periods} selected={selected.delivery_period} onSelect={setSelectedPeriod} onValue={openLineage} />
        <div className="section-heading"><div><p className="eyebrow">06 · DIAGNOSTIC EXPLANATION</p><h3>Physical interpretation</h3></div><span>No optimisation, BM value or service valuation</span></div>
        <Explanation period={selected} sourceMode={battery.source_mode} />
      </> : <div className="empty panel">Battery telemetry or approved operating limits are unavailable.</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function Readiness({ battery }: { battery: BatteryFlexibilitySnapshot }) {
  return <div className="fp-readiness panel"><div><span>Battery readiness</span><strong className={`readiness ${battery.readiness.status.toLowerCase()}`}>{battery.readiness.status}</strong></div><dl><dt>Feasibility</dt><dd>{battery.readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-control trust</dt><dd>{battery.readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd></dl></div>;
}

function BatteryStatus({ battery, onValue }: { battery: BatteryFlexibilitySnapshot; onValue: (point: CanonicalDataPoint) => void }) {
  const soc = battery.current_soc!;
  const limits = battery.limits!;
  const usable = Number(limits.e_max.value) - Number(limits.e_min.value);
  const fill = Math.max(0, Math.min(100, (Number(soc.value) - Number(limits.e_min.value)) / usable * 100));
  return <article className="battery-panel panel"><header><div><p className="eyebrow">01 · BATTERY STATUS</p><h3>Confirmed physical state</h3></div><div className="badges"><Badge value={battery.source_mode} /><Badge value={battery.quality} /></div></header>
    <button className="soc-display" onClick={() => onValue(soc)}><span>State of charge</span><strong>{fmt(Number(soc.value), 1)}</strong><small>MWh</small></button>
    <div className="soc-track"><i style={{ width: `${fill}%` }} /></div><div className="soc-bounds"><button onClick={() => onValue(limits.e_min)}>E min {fmt(Number(limits.e_min.value), 0)}</button><button onClick={() => onValue(limits.e_max)}>E max {fmt(Number(limits.e_max.value), 0)} MWh</button></div>
    <dl className="battery-status-list"><dt>Telemetry time</dt><dd>{formatTimestampWithZone(soc.lineage.published_at ?? soc.lineage.retrieved_at, "UK time")}</dd><dt>Charge / discharge limit</dt><dd><button onClick={() => onValue(limits.charge_power_max)}>{fmt(Number(limits.charge_power_max.value), 0)}</button> / <button onClick={() => onValue(limits.discharge_power_max)}>{fmt(Number(limits.discharge_power_max.value), 0)} MW</button></dd><dt>Efficiencies ηc / ηd</dt><dd><button onClick={() => onValue(limits.charge_efficiency)}>{fmt(Number(limits.charge_efficiency.value) * 100, 0)}%</button> / <button onClick={() => onValue(limits.discharge_efficiency)}>{fmt(Number(limits.discharge_efficiency.value) * 100, 0)}%</button></dd></dl>
    <div className="useful-periods"><span>Largest feasible exposure reduction</span>{battery.most_useful_periods.map((period) => <code key={period}>{period}</code>)}</div>
    <ReadinessCauses battery={battery} />
    <p className="sample-disclaimer">{battery.readiness.reasons.join(" · ")}</p>
  </article>;
}

function Feasibility({ period, onValue }: { period: BatteryPeriodSnapshot; onValue: (point: CanonicalDataPoint) => void }) {
  const f = period.feasibility;
  return <article className="battery-panel panel"><header><div><p className="eyebrow">02 · FEASIBILITY</p><h3>{period.delivery_period}</h3></div><span className="duration-chip">30 MIN</span></header>
    <div className="flex-direction-grid"><Value label="Max charge" point={f.max_charge_value} onValue={onValue} /><Value label="Max discharge" point={f.max_discharge_value} onValue={onValue} /></div>
    <div className="battery-mini-grid"><Value label="Downward headroom" point={f.downward_power_headroom_value} onValue={onValue} /><Value label="Upward headroom" point={f.upward_power_headroom_value} onValue={onValue} /><Value label="SoC after max charge" point={f.projected_soc_after_max_charge_value} onValue={onValue} /><Value label="SoC after max discharge" point={f.projected_soc_after_max_discharge_value} onValue={onValue} /></div>
    <div className="constraint-list"><span>Binding constraints</span>{f.binding_constraints.map((item) => <code key={item}>{item.replaceAll("_", " ")}</code>)}</div>
  </article>;
}

function OpportunityCost({ battery, onValue }: { battery: BatteryFlexibilitySnapshot; onValue: (point: CanonicalDataPoint) => void }) {
  const cost = battery.opportunity_cost!;
  return <article className="battery-panel panel"><header><div><p className="eyebrow">03 · OPPORTUNITY COST</p><h3>Heuristic preservation value</h3></div><span className="heuristic-chip">HEURISTIC</span></header>
    <div className="cost-pair"><Value label="Discharge 1 MWh" point={cost.discharge_cost_value} onValue={onValue} /><Value label="Charge 1 MWh" point={cost.charge_cost_value} onValue={onValue} /></div>
    <dl className="cost-inputs"><dt>Degradation</dt><dd><button onClick={() => onValue(cost.degradation_cost)}>£{fmt(Number(cost.degradation_cost.value), 2)}/MWh</button></dd><dt>Terminal target</dt><dd><button onClick={() => onValue(cost.terminal_soc_target)}>{fmt(Number(cost.terminal_soc_target.value), 1)} MWh</button></dd><dt>Terminal shortfall penalty</dt><dd><button onClick={() => onValue(cost.terminal_soc_penalty)}>£{fmt(Number(cost.terminal_soc_penalty.value), 2)}/MWh</button></dd><dt>Future-flex base penalty</dt><dd><button onClick={() => onValue(cost.future_flexibility_penalty)}>£{fmt(Number(cost.future_flexibility_penalty.value), 2)}/MWh</button></dd></dl>
    <p className="sample-disclaimer">Excludes energy price, imbalance price, BM value and ancillary-service value.</p>
  </article>;
}

function CoveragePanel({ period, onValue }: { period: BatteryPeriodSnapshot; onValue: (point: CanonicalDataPoint) => void }) {
  return <section className="coverage-grid">{period.coverage.map((item) => <CoverageCard key={item.scenario} item={item} onValue={onValue} />)}</section>;
}

function CoverageCard({ item, onValue }: { item: BatteryExposureCoverage; onValue: (point: CanonicalDataPoint) => void }) {
  return <article className="coverage-card panel"><header><strong>{item.scenario}</strong><span className={`support-chip ${item.support_direction.toLowerCase()}`}>{item.support_direction}</span></header><div className="coverage-flow"><button onClick={() => onValue(item.exposure_value)}><span>Exposure</span><strong>{signed(item.exposure_mwh)}</strong><small>MWh</small></button><i>→</i><button onClick={() => onValue(item.covered_value)}><span>Covered</span><strong>{fmt(item.covered_mwh, 1)}</strong><small>MWh</small></button><i>→</i><button onClick={() => onValue(item.residual_value)}><span>Residual</span><strong>{signed(item.residual_after_support_mwh)}</strong><small>MWh</small></button></div><div className="coverage-bar"><i style={{ width: `${Math.min(100, item.coverage_percent)}%` }} /></div><small>{fmt(item.coverage_percent, 0)}% coverable using maximum feasible {item.support_direction.toLowerCase()}</small></article>;
}

function PeriodGrid({ periods, selected, onSelect, onValue }: { periods: BatteryPeriodSnapshot[]; selected: string; onSelect: (id: string) => void; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="table-wrap panel battery-grid"><table><thead><tr><th>Period</th><th>Delivery</th><th>Max charge</th><th>Max discharge</th><th>Up / down MW</th><th>SoC range after action</th><th>P10 residual</th><th>P50 residual</th><th>P90 residual</th><th>Binding</th></tr></thead><tbody>{periods.map((period) => {
    const f = period.feasibility; const coverage = Object.fromEntries(period.coverage.map((item) => [item.scenario, item]));
    return <tr key={period.delivery_period} className={selected === period.delivery_period ? "selected-period" : ""} onClick={() => onSelect(period.delivery_period)}><td><strong>SP{period.settlement_period}</strong><small>{period.delivery_period}</small></td><td>{formatUkMarketTime(period.delivery_start)}–{formatUkMarketTime(period.delivery_end)}<small> · UK time</small></td><td><GridValue point={f.max_charge_value} onValue={onValue} /></td><td><GridValue point={f.max_discharge_value} onValue={onValue} /></td><td><div className="grid-pair"><GridValue point={f.upward_power_headroom_value} onValue={onValue} /><span>/</span><GridValue point={f.downward_power_headroom_value} onValue={onValue} /></div></td><td><div className="grid-pair"><GridValue point={f.projected_soc_after_max_discharge_value} onValue={onValue} /><span>–</span><GridValue point={f.projected_soc_after_max_charge_value} onValue={onValue} /></div></td>{(["P10", "P50", "P90"] as const).map((scenario) => <td key={scenario}><button className={`grid-value ${direction(coverage[scenario].residual_after_support_mwh).toLowerCase()}`} onClick={(event) => { event.stopPropagation(); onValue(coverage[scenario].residual_value); }}>{signed(coverage[scenario].residual_after_support_mwh)}<small>{direction(coverage[scenario].residual_after_support_mwh)}</small></button></td>)}<td><span className="binding-cell">{f.binding_constraints.map((item) => item.split("_").slice(0, 2).join(" ")).join(" · ")}</span></td></tr>;
  })}</tbody></table></div>;
}

function Explanation({ period, sourceMode }: { period: BatteryPeriodSnapshot; sourceMode: string }) {
  return <article className="battery-explanation panel"><div className="flex-orb"><span>±</span><small>FLEX</small></div><div><p>{period.explanation}</p><p>The period is assessed independently from current SoC. Repeating the displayed maximum action across periods would require a sequential schedule and is outside this milestone.</p><small>{sourceMode} inputs · diagnostic only · positive residual is long, negative residual is short</small></div></article>;
}

function Value({ label, point, onValue }: { label: string; point: CanonicalDataPoint; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="battery-value" onClick={() => onValue(point)}><span>{label}</span><strong>{point.unit.startsWith("GBP") ? "£" : ""}{fmt(Number(point.value), point.unit === "MW" ? 0 : 2)}</strong><small>{point.unit}</small></button>;
}

function GridValue({ point, onValue }: { point: CanonicalDataPoint; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="grid-value" onClick={(event) => { event.stopPropagation(); onValue(point); }}>{fmt(Number(point.value), 1)}<small>{point.unit}</small></button>;
}

function ReadinessCauses({ battery }: { battery: BatteryFlexibilitySnapshot }) {
  const stale = battery.quality === "STALE" || battery.readiness.reasons.some((reason) => reason.toLowerCase().includes("stale"));
  return <div className="readiness-causes">
    {battery.source_mode === "SAMPLE" && <p><strong>DEGRADED · SAMPLE</strong><span>Inputs are explicitly sample-labelled: usable for diagnostics, not live control.</span></p>}
    {stale && <p><strong>STALE · FRESHNESS SLA EXCEEDED</strong><span>At least one input age exceeds its freshness SLA; review its source timestamp.</span></p>}
  </div>;
}

function fmt(value: number, digits = 1) { return value.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
function signed(value: number) { return `${value >= 0 ? "+" : "−"}${fmt(Math.abs(value), 1)}`; }
function direction(value: number) { return value > 0.05 ? "LONG" : value < -0.05 ? "SHORT" : "FLAT"; }
