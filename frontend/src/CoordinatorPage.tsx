import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadCoordinator, loadLineage, simulateCoordinator } from "./api";
import { ConnectionStatus } from "./ConnectionStatus";
import { ProductNav } from "./ProductNav";
import { formatUkMarketTime } from "./time";
import type { CanonicalDataPoint, CoordinatorCandidate, CoordinatorPeriodResult, CoordinatorSimulationInput, CoordinatorSnapshot, LineageResponse } from "./types";

const defaults: CoordinatorSimulationInput = {
  imbalance_price_gbp_per_mwh: 125,
  tail_risk_weight: 0.35,
  optionality_loss_weight: 1,
  maximum_market_hedge_volume_mwh: null,
  selected_battery_path: "PRESERVE_FLEXIBILITY",
  confidence_scenario: "P50",
  explicit_sample_market: true,
  assumption_source_mode: "SAMPLE",
};

export function CoordinatorPage() {
  const [snapshot, setSnapshot] = useState<CoordinatorSnapshot | null>(null);
  const [settings, setSettings] = useState<CoordinatorSimulationInput>(defaults);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [calculating, setCalculating] = useState(false);

  const acceptSnapshot = useCallback((next: CoordinatorSnapshot) => {
    setSnapshot(next);
    setSelectedId((current) => next.candidates.some((item) => item.candidate_id === current) ? current : next.recommendation?.selected_candidate_id ?? next.candidates[0]?.candidate_id ?? null);
    setLastPoll(new Date());
    setError(null);
  }, []);
  const reload = useCallback(async () => {
    try { acceptSnapshot(await loadCoordinator()); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load coordinator diagnostics"); }
  }, [acceptSnapshot]);
  useEffect(() => { void reload(); }, [reload]);

  const simulate = async () => {
    setCalculating(true);
    try { acceptSnapshot(await simulateCoordinator(settings)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Coordinator simulation failed"); }
    finally { setCalculating(false); }
  };
  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try { setLineage(await loadLineage(point.value_id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); }
  };
  const selected = useMemo(() => snapshot?.candidates.find((item) => item.candidate_id === selectedId) ?? snapshot?.candidates[0] ?? null, [selectedId, snapshot]);

  return <div className="app-shell coordinator-page">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Integrated Coordinator</h1></div></div>
      <ProductNav active="diagnostics" />
      <ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} />
    </header>
    <main>
      <section className="hero-row coordinator-hero"><div><p className="eyebrow">MILESTONE 1G · DECISION SUPPORT</p><h2>What should I do now, given the latest forecast, position, market, battery and system information?</h2><p className="intro">Transparent candidate comparison · diagnostic recommendation · no execution or control</p></div>{snapshot && <Readiness snapshot={snapshot} />}</section>
      {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
      {snapshot ? <>
        <div className="coordinator-warning"><strong>DIAGNOSTIC RECOMMENDATION</strong><span>NOT EXECUTABLE</span><p>Not trustworthy for live trading unless all required inputs are LIVE/FRESH/VALID.</p></div>
        <section className="coordinator-top-grid">
          <AssumptionControls settings={settings} onChange={setSettings} onRun={() => void simulate()} calculating={calculating} assumptions={snapshot.assumptions} onValue={openLineage} />
          {snapshot.recommendation && <RecommendationCard snapshot={snapshot} candidate={snapshot.candidates.find((item) => item.candidate_id === snapshot.recommendation?.selected_candidate_id) ?? snapshot.candidates[0]} onValue={openLineage} />}
        </section>
        {snapshot.candidates.length ? <>
          <div className="section-heading"><div><p className="eyebrow">03 · CANDIDATE COMPARISON</p><h3>Lower diagnostic cost ranks first</h3></div><span>Click a row to inspect settlement-period detail</span></div>
          <CandidateTable candidates={snapshot.candidates} selectedId={selectedId} onSelect={setSelectedId} onValue={openLineage} />
          {selected && <>
            <div className="section-heading"><div><p className="eyebrow">04 · PERIOD DETAIL</p><h3>{selected.action_name}</h3></div><span>Exposure + battery net export − signed market trade = residual</span></div>
            <PeriodTable candidate={selected} onValue={openLineage} />
          </>}
          <div className="section-heading"><div><p className="eyebrow">05 · WHAT WOULD CHANGE THIS?</p><h3>One-factor screening sensitivities</h3></div><span>Approximate counterfactuals · not full re-optimisations</span></div>
          <SensitivityGrid snapshot={snapshot} />
          {snapshot.recommendation && <Explanation snapshot={snapshot} />}
        </> : <Blocked snapshot={snapshot} />}
      </> : <div className="empty panel">Waiting for integrated coordinator inputs…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function Readiness({ snapshot }: { snapshot: CoordinatorSnapshot }) {
  const readiness = snapshot.readiness;
  return <div className="fp-readiness panel coordinator-readiness"><div><span>Coordinator readiness</span><strong className={`readiness ${readiness.status.toLowerCase()}`}>{readiness.status}</strong></div><dl><dt>Calculation</dt><dd>{readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-trading trust</dt><dd>{readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd><dt>Executable/live-ready</dt><dd>{readiness.executable_live_ready ? "Yes" : "No"}</dd></dl><div className="badges"><Badge value={snapshot.source_mode} /><Badge value={snapshot.quality} /></div><div className="path-readiness-reasons">{readiness.reasons.map((reason) => <p key={reason}>{reason}</p>)}</div></div>;
}

function AssumptionControls({ settings, onChange, onRun, calculating, assumptions, onValue }: { settings: CoordinatorSimulationInput; onChange: (next: CoordinatorSimulationInput) => void; onRun: () => void; calculating: boolean; assumptions: CanonicalDataPoint[]; onValue: (point: CanonicalDataPoint) => void }) {
  const setNumber = (key: keyof CoordinatorSimulationInput, value: string) => onChange({ ...settings, [key]: value === "" ? null : Number(value) });
  return <article className="coordinator-controls panel"><header><div><p className="eyebrow">01 · DIAGNOSTIC ASSUMPTIONS</p><h3>Transparent scoring inputs</h3></div><Badge value={settings.assumption_source_mode} /></header><div className="control-grid">
    <label>Imbalance price <span>£/MWh</span><input type="number" min="0" value={settings.imbalance_price_gbp_per_mwh} onChange={(event) => setNumber("imbalance_price_gbp_per_mwh", event.target.value)} /></label>
    <label>Tail-risk weight <span>ratio</span><input type="number" min="0" step="0.05" value={settings.tail_risk_weight} onChange={(event) => setNumber("tail_risk_weight", event.target.value)} /></label>
    <label>Optionality-loss weight <span>ratio</span><input type="number" min="0" step="0.1" value={settings.optionality_loss_weight} onChange={(event) => setNumber("optionality_loss_weight", event.target.value)} /></label>
    <label>Market cap <span>MWh / period</span><input type="number" min="0" placeholder="Uncapped" value={settings.maximum_market_hedge_volume_mwh ?? ""} onChange={(event) => setNumber("maximum_market_hedge_volume_mwh", event.target.value)} /></label>
    <label>Confidence scenario<select value={settings.confidence_scenario} onChange={(event) => onChange({ ...settings, confidence_scenario: event.target.value as CoordinatorSimulationInput["confidence_scenario"] })}><option>P10</option><option>P50</option><option>P90</option></select></label>
    <label>Hybrid battery path<select value={settings.selected_battery_path} onChange={(event) => onChange({ ...settings, selected_battery_path: event.target.value as CoordinatorSimulationInput["selected_battery_path"] })}><option value="NO_ACTION">No action</option><option value="P50_COVERAGE">P50 coverage</option><option value="PRESERVE_FLEXIBILITY">Preserve flexibility</option></select></label>
  </div><label className="sample-confirm"><input type="checkbox" checked={settings.explicit_sample_market} onChange={(event) => onChange({ ...settings, explicit_sample_market: event.target.checked })} /> Explicitly use labelled SAMPLE executable-book diagnostics</label><div className="assumption-lineage">{assumptions.map((point) => <button key={point.value_id} onClick={() => onValue(point)}>{point.metric.replace("coordinator_", "").replaceAll("_", " ")} ↗</button>)}</div><button className="run-coordinator" disabled={calculating} onClick={onRun}>{calculating ? "Recalculating…" : "Rerun diagnostic coordinator"}</button></article>;
}

function RecommendationCard({ snapshot, candidate, onValue }: { snapshot: CoordinatorSnapshot; candidate: CoordinatorCandidate; onValue: (point: CanonicalDataPoint) => void }) {
  const recommendation = snapshot.recommendation!;
  return <article className="recommendation-card panel"><header><div><p className="eyebrow">02 · {recommendation.label.toUpperCase()}</p><h3>{recommendation.selected_action_name}</h3></div><span className="rank-chip">RANK 01</span></header><button className="hero-score" onClick={() => onValue(recommendation.diagnostic_score_value)}><span>Total diagnostic cost</span><strong>{gbp(recommendation.diagnostic_score_gbp)}</strong><small>lower is better · inspect lineage ↗</small></button><div className="recommendation-metrics"><Value label="Market" text={`${fmt(candidate.market_trade_volume_mwh)} MWh ${candidate.market_hedge_side}`} point={candidate.market_trade_volume_value} onValue={onValue} /><Value label="Battery" text={`${fmt(candidate.battery_charge_mwh)} charge / ${fmt(candidate.battery_discharge_mwh)} discharge`} point={candidate.battery_charge_value} onValue={onValue} /><Value label="P50 residual" text={signed(candidate.residual_p50_mwh)} point={candidate.residual_p50_value} onValue={onValue} /><Value label="Optionality lost" text={gbp(candidate.optionality_lost_gbp)} point={candidate.optionality_lost_value} onValue={onValue} /></div><div className="badges">{candidate.warning_badges.map((item) => <span className="warning-badge" key={item}>{item}</span>)}</div><p>{recommendation.explanation}</p></article>;
}

function CandidateTable({ candidates, selectedId, onSelect, onValue }: { candidates: CoordinatorCandidate[]; selectedId: string | null; onSelect: (id: string) => void; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="table-wrap panel coordinator-table"><table><thead><tr><th>Rank / candidate</th><th>Market</th><th>WAP</th><th>Unfilled</th><th>Battery path</th><th>Residual P10 / P50 / P90</th><th>Market cost</th><th>Imbalance</th><th>Tail</th><th>Battery opp.</th><th>Optionality</th><th>Service risk</th><th>Total</th><th>Status</th></tr></thead><tbody>{candidates.map((item) => <tr className={item.candidate_id === selectedId ? "selected-row" : ""} key={item.candidate_id} onClick={() => onSelect(item.candidate_id)}><td><strong>#{item.rank} {item.action_name}</strong><small>{item.action}</small></td><td><ValueLink text={`${fmt(item.market_trade_volume_mwh)} ${item.market_hedge_side}`} point={item.market_trade_volume_value} onValue={onValue} /></td><td><ValueLink text={item.market_wap_gbp_per_mwh === null ? "—" : gbp(item.market_wap_gbp_per_mwh, "/MWh")} point={item.market_wap_value} onValue={onValue} /></td><td><ValueLink text={`${fmt(item.market_unfilled_mwh)} MWh`} point={item.market_unfilled_value} onValue={onValue} /></td><td>{item.battery_path}<small>{fmt(item.battery_charge_mwh)} C / {fmt(item.battery_discharge_mwh)} D MWh</small></td><td><div className="scenario-stack"><ValueLink text={signed(item.residual_p10_mwh)} point={item.residual_p10_value} onValue={onValue} /><ValueLink text={signed(item.residual_p50_mwh)} point={item.residual_p50_value} onValue={onValue} /><ValueLink text={signed(item.residual_p90_mwh)} point={item.residual_p90_value} onValue={onValue} /></div></td><Cost value={item.cost.market_execution_cost_gbp} point={item.cost.market_execution_cost_value} onValue={onValue} /><Cost value={item.cost.expected_imbalance_cost_gbp} point={item.cost.expected_imbalance_cost_value} onValue={onValue} /><Cost value={item.cost.tail_risk_penalty_gbp} point={item.cost.tail_risk_penalty_value} onValue={onValue} /><Cost value={item.cost.battery_opportunity_cost_gbp} point={item.cost.battery_opportunity_cost_value} onValue={onValue} /><Cost value={item.cost.optionality_lost_gbp} point={item.cost.optionality_lost_value} onValue={onValue} /><Cost value={item.cost.service_risk_penalty_gbp} point={item.cost.service_risk_penalty_value} onValue={onValue} /><td className="total-cost"><ValueLink text={gbp(item.cost.total_diagnostic_cost_gbp)} point={item.cost.total_diagnostic_cost_value} onValue={onValue} /></td><td><strong className={`readiness ${item.readiness.status.toLowerCase()}`}>{item.readiness.status}</strong>{item.service_commitments_at_risk > 0 && <small className="risk-text">SERVICE RISK</small>}</td></tr>)}</tbody></table></div>;
}

function PeriodTable({ candidate, onValue }: { candidate: CoordinatorCandidate; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="table-wrap panel coordinator-period-table"><table><thead><tr><th>Period</th><th>Exposure P10 / P50 / P90</th><th>Market hedge</th><th>WAP / unfilled</th><th>Battery net</th><th>Residual P10 / P50 / P90</th><th>SoC before → after</th><th>Optionality lost</th><th>Service</th><th>Binding constraints</th><th>Period cost</th></tr></thead><tbody>{candidate.periods.map((period) => <PeriodRow key={period.delivery_period} period={period} onValue={onValue} />)}</tbody></table></div>;
}

function PeriodRow({ period, onValue }: { period: CoordinatorPeriodResult; onValue: (point: CanonicalDataPoint) => void }) {
  const before = Object.fromEntries(period.exposure_before.map((item) => [item.scenario, item]));
  return <tr className={period.service_commitment_at_risk ? "coordinator-risk-row" : ""}><td><strong>SP{period.settlement_period}</strong><small>{formatUkMarketTime(period.delivery_start)}–{formatUkMarketTime(period.delivery_end)} UK time</small></td><td><div className="scenario-stack">{(["P10", "P50", "P90"] as const).map((scenario) => <ValueLink key={scenario} text={`${scenario} ${signed(before[scenario].residual_position_mwh)}`} point={before[scenario].exposure_value} onValue={onValue} />)}</div></td><td><ValueLink text={`${fmt(period.market_trade_volume_mwh)} MWh ${period.market_hedge_side}`} point={period.market_trade_value} onValue={onValue} /></td><td><ValueLink text={period.market_wap_gbp_per_mwh === null ? "No fill" : gbp(period.market_wap_gbp_per_mwh, "/MWh")} point={period.market_wap_value} onValue={onValue} /><ValueLink text={`${fmt(period.market_unfilled_mwh)} MWh unfilled`} point={period.market_unfilled_value} onValue={onValue} /></td><td><ValueLink text={`${signed(period.battery_net_export_mwh)} MWh`} point={period.battery_action_value} onValue={onValue} /><small>{fmt(period.battery_charge_mwh)} C / {fmt(period.battery_discharge_mwh)} D</small></td><td><div className="scenario-stack">{period.residuals.map((item) => <ValueLink key={item.scenario} text={`${item.scenario} ${signed(item.residual_exposure_mwh)} ${item.direction}`} point={item.residual_value} onValue={onValue} />)}</div></td><td><ValueLink text={`${fmt(period.soc_before_mwh)} → ${fmt(period.soc_after_mwh)} MWh`} point={period.soc_after_value} onValue={onValue} /></td><td><ValueLink text={gbp(period.optionality_lost_gbp)} point={period.optionality_lost_value} onValue={onValue} /></td><td><ValueLink text={period.service_commitment_at_risk ? `${fmt(period.service_coverage_ratio * 100, 0)}% AT RISK` : `${fmt(period.service_coverage_ratio * 100, 0)}% covered`} point={period.service_risk_value} onValue={onValue} /></td><td>{period.binding_constraints.length ? <div className="constraint-stack">{period.binding_constraints.map((item) => <span key={item}>{item.replaceAll("_", " ")}</span>)}</div> : <span className="covered-chip">NONE</span>}</td><td><ValueLink text={gbp(period.cost.total_diagnostic_cost_gbp)} point={period.cost.total_diagnostic_cost_value} onValue={onValue} /></td></tr>;
}

function SensitivityGrid({ snapshot }: { snapshot: CoordinatorSnapshot }) {
  return <section className="sensitivity-grid">{snapshot.sensitivities.map((item) => <article className={`panel sensitivity-card ${item.changed_preference ? "changed" : ""}`} key={item.sensitivity_id}><header><span>{item.change}</span><strong>{item.changed_preference ? "CHANGES ACTION" : "SAME ACTION"}</strong></header><h4>{item.counterfactual_preferred_action.replaceAll("_", " ")}</h4><p>{item.explanation}</p><small>{gbp(item.counterfactual_cost_gbp)} approximate cost</small></article>)}</section>;
}

function Explanation({ snapshot }: { snapshot: CoordinatorSnapshot }) {
  const recommendation = snapshot.recommendation!;
  return <article className="coordinator-explanation panel"><div className="coordinator-orb">WHY</div><div><p>{recommendation.explanation}</p><h4>What would change the diagnostic preference?</h4><ul>{recommendation.what_would_change.map((item) => <li key={item}>{item}</li>)}</ul><small>Not executable · no order submission · no battery control · no persisted trader action</small></div></article>;
}

function Blocked({ snapshot }: { snapshot: CoordinatorSnapshot }) {
  return <section className="blocked-panel panel"><p className="eyebrow">COORDINATOR BLOCKED</p><h3>No diagnostic recommendation is available</h3>{snapshot.readiness.critical_blockers.map((item) => <p key={item}>{item}</p>)}</section>;
}

function Value({ label, text, point, onValue }: { label: string; text: string; point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint) => void }) {
  return <button onClick={() => point && onValue(point)}><span>{label}</span><strong>{text}</strong></button>;
}
function ValueLink({ text, point, onValue }: { text: string; point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="coordinator-value" disabled={!point} onClick={(event) => { event.stopPropagation(); if (point) onValue(point); }}>{text}</button>;
}
function Cost({ value, point, onValue }: { value: number; point: CanonicalDataPoint; onValue: (point: CanonicalDataPoint) => void }) {
  return <td><ValueLink text={gbp(value)} point={point} onValue={onValue} /></td>;
}
function fmt(value: number, digits = 1) { return new Intl.NumberFormat("en-GB", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value); }
function gbp(value: number, suffix = "") { return `${value < 0 ? "−" : ""}£${fmt(Math.abs(value), 0)}${suffix}`; }
function signed(value: number) { return `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value))} MWh`; }
