import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadLineage, loadOptionality, simulateOptionalityPath } from "./api";
import { ConnectionStatus } from "./ConnectionStatus";
import { formatUkMarketTime } from "./time";
import type { BatteryPathPeriodAction, CanonicalDataPoint, LineageResponse, OptionalityAssumption, OptionalityPathImpact, OptionalityPeriodDiagnostic, OptionalitySnapshot, ServiceCommitment } from "./types";

type PathKind = "NO_ACTION" | "P50_COVERAGE" | "PRESERVE_FLEXIBILITY" | "CUSTOM";

export function OptionalityPage() {
  const [snapshot, setSnapshot] = useState<OptionalitySnapshot | null>(null);
  const [selected, setSelected] = useState<PathKind>("NO_ACTION");
  const [customActions, setCustomActions] = useState<BatteryPathPeriodAction[]>([]);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [calculating, setCalculating] = useState(false);
  const requestVersion = useRef(0);

  const reload = useCallback(async () => {
    try {
      const next = await loadOptionality();
      setSnapshot(next);
      const periods = next.path_impacts[0]?.periods ?? [];
      setCustomActions((current) => current.length ? current : periods.map((period) => ({ delivery_period: period.delivery_period, charge_mw: 0, discharge_mw: 0 })));
      setLastPoll(new Date()); setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load optionality diagnostics");
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    const version = ++requestVersion.current;
    if (selected !== "CUSTOM" || customActions.length === 0) {
      setCalculating(false);
      return;
    }
    setCalculating(true);
    const timer = window.setTimeout(() => {
      void simulateOptionalityPath(customActions)
        .then((next) => {
          if (version !== requestVersion.current) return;
          setSnapshot(next); setLastPoll(new Date()); setError(null); setCalculating(false);
        })
        .catch((cause) => {
          if (version !== requestVersion.current) return;
          setError(cause instanceof Error ? cause.message : "Custom optionality simulation failed");
          setCalculating(false);
        });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [customActions, selected]);

  const impact = useMemo(() => {
    const selectedImpact = snapshot?.path_impacts.find((item) => item.path_name === selected) ?? null;
    if (selected === "CUSTOM" && selectedImpact === null) {
      return snapshot?.path_impacts.find((item) => item.path_name === "NO_ACTION") ?? null;
    }
    return selectedImpact;
  }, [selected, snapshot]);
  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try { setLineage(await loadLineage(point.value_id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); }
  };
  const editAction = (period: string, field: "charge_mw" | "discharge_mw", value: number) => {
    setSelected("CUSTOM");
    setCustomActions((actions) => actions.map((action) => action.delivery_period === period ? { ...action, [field]: Number.isFinite(value) ? value : 0 } : action));
  };

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>BM &amp; Ancillary Optionality</h1></div></div>
      <nav><a href="/data-flow">Data flow</a><a href="/forecast-position">Forecast &amp; position</a><a href="/market-liquidity">Market &amp; liquidity</a><a href="/battery-flexibility">Battery flexibility</a><a href="/battery-path">Battery path</a><a className="active" href="/optionality">Optionality</a><span>Optimisation</span><span>Actions</span></nav>
      <ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} />
    </header>
    <main>
      <section className="hero-row optionality-hero"><div><p className="eyebrow">MILESTONE 1F · OPTIONALITY DIAGNOSTICS</p><h2>How does a candidate battery path change committed-service deliverability and optional future value?</h2><p className="intro">Committed obligations are separate from uncertain optional value · no action recommendation</p></div>{snapshot && <Readiness snapshot={snapshot} />}</section>
      {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
      {snapshot ? <>
        {snapshot.warnings.map((warning) => <div className="diagnostic-banner" key={warning}>{warning}</div>)}
        <section className="optionality-input-grid">
          <Commitments commitments={snapshot.commitments} onValue={openLineage} />
          <Assumptions assumptions={snapshot.assumptions} onValue={openLineage} />
        </section>
        <PathSelector selected={selected} onSelect={setSelected} impact={impact} calculating={calculating} />
        {impact ? <>
          <ImpactSummary impact={impact} onValue={openLineage} />
          <div className="section-heading"><div><p className="eyebrow">05 · PERIOD IMPACT</p><h3>Deliverability and optional value by settlement period</h3></div><span>{selected === "CUSTOM" ? "Edit MW inputs; all later periods recalculate" : "Select Custom path for manual what-if inputs"}</span></div>
          <PeriodTable impact={impact} custom={selected === "CUSTOM"} actions={customActions} onEdit={editAction} onValue={openLineage} />
          <Explanation impact={impact} onValue={openLineage} />
        </> : <div className="empty panel">{calculating ? "Calculating custom optionality path…" : "Select a path with available diagnostics."}</div>}
      </> : <div className="empty panel">Waiting for optionality inputs…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function Readiness({ snapshot }: { snapshot: OptionalitySnapshot }) {
  return <div className="fp-readiness panel"><div><span>Optionality readiness</span><strong className={`readiness ${snapshot.readiness.status.toLowerCase()}`}>{snapshot.readiness.status}</strong></div><dl><dt>Calculation</dt><dd>{snapshot.readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-trading trust</dt><dd>{snapshot.readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd></dl><div className="badges"><Badge value={snapshot.source_mode} /><Badge value={snapshot.quality} /></div><div className="path-readiness-reasons">{snapshot.readiness.reasons.map((reason) => <p key={reason}>{reason}</p>)}</div></div>;
}

function Commitments({ commitments, onValue }: { commitments: ServiceCommitment[]; onValue: (point: CanonicalDataPoint) => void }) {
  return <article className="optionality-panel panel"><header><div><p className="eyebrow">01 · COMMITTED SERVICE</p><h3>Obligations that must remain deliverable</h3></div><span className="obligation-chip">COMMITTED</span></header><div className="commitment-list">{commitments.map((item) => <div className="commitment-card" key={item.commitment_id}><div><strong>{item.product.name}</strong><span className={item.product.direction.toLowerCase()}>{item.product.direction}</span></div><button onClick={() => onValue(item.reserved_value)}>{fmt(item.reserved_mw)} MW reserved</button><button onClick={() => onValue(item.duration_value)}>{fmt(item.required_duration_hours)} h duration</button><small>{item.delivery_period}</small><div className="badges"><Badge value={item.reserved_value.lineage.source_mode} /><Badge value={item.reserved_value.lineage.quality} /></div><p>{item.obligation_status}</p></div>)}</div></article>;
}

function Assumptions({ assumptions, onValue }: { assumptions: OptionalityAssumption[]; onValue: (point: CanonicalDataPoint) => void }) {
  return <article className="optionality-panel panel"><header><div><p className="eyebrow">02 · VALUE ASSUMPTIONS</p><h3>Transparent heuristic inputs</h3></div><span className="optional-chip">OPTIONAL</span></header><div className="assumption-grid">{assumptions.map((item) => <button key={item.key} onClick={() => onValue(item.value_point)}><span>{item.label}</span><strong>{assumptionValue(item)}</strong><small>{item.description}</small><div className="badges"><Badge value={item.value_point.lineage.source_mode} /><Badge value={item.value_point.lineage.semantic_kind} /><Badge value={item.value_point.lineage.quality} /></div></button>)}</div><p className="non-guaranteed-warning">BM value is optional and not guaranteed</p></article>;
}

function PathSelector({ selected, onSelect, impact, calculating }: { selected: PathKind; onSelect: (value: PathKind) => void; impact: OptionalityPathImpact | null; calculating: boolean }) {
  const paths: { key: PathKind; label: string; note: string }[] = [
    { key: "NO_ACTION", label: "No action", note: "Optionality baseline" },
    { key: "P50_COVERAGE", label: "Cover P50", note: "Full diagnostic coverage path" },
    { key: "PRESERVE_FLEXIBILITY", label: "Preserve flex", note: "25% coverage assumption" },
    { key: "CUSTOM", label: "Custom path", note: "Manual charge/discharge MW" },
  ];
  return <section className="path-selector optionality-selector panel"><div><p className="eyebrow">03 · PATH IMPACT</p><h3>{impact?.path_label ?? (calculating ? "Recalculating…" : "Custom path")}</h3></div><div className="path-options">{paths.map((path) => <button className={selected === path.key ? "active" : ""} key={path.key} onClick={() => onSelect(path.key)}><strong>{path.label}</strong><small>{path.note}</small></button>)}</div><span className="diagnostic-chip">NOT A RECOMMENDATION</span></section>;
}

function ImpactSummary({ impact, onValue }: { impact: OptionalityPathImpact; onValue: (point: CanonicalDataPoint) => void }) {
  return <section className="optionality-impact panel"><header><div><p className="eyebrow">04 · HORIZON SUMMARY</p><h3>{impact.path_label}</h3></div><span className={impact.commitments_at_risk ? "risk-chip" : "covered-chip"}>{impact.commitments_at_risk ? `${impact.commitments_at_risk} PERIOD(S) AT RISK` : "COMMITMENTS COVERED"}</span></header><div className="optionality-metrics"><Value label="Value before" point={impact.optionality_value_before_value} onValue={onValue} /><Value label="Value after" point={impact.optionality_value_after_value} onValue={onValue} /><Value label="Optionality lost" point={impact.optionality_lost_value} onValue={onValue} /><div><span>Worst affected period</span><strong>{impact.worst_affected_period ?? "No material loss"}</strong></div></div><p>{impact.explanation}</p></section>;
}

function PeriodTable({ impact, custom, actions, onEdit, onValue }: { impact: OptionalityPathImpact; custom: boolean; actions: BatteryPathPeriodAction[]; onEdit: (period: string, field: "charge_mw" | "discharge_mw", value: number) => void; onValue: (point: CanonicalDataPoint) => void }) {
  const actionMap = Object.fromEntries(actions.map((action) => [action.delivery_period, action]));
  return <div className="table-wrap panel optionality-table"><table><thead><tr><th>Period</th>{custom && <><th>Charge MW</th><th>Discharge MW</th></>}<th>SoC start → end</th><th>Power U / D before → after</th><th>Duration U / D</th><th>Committed U / D</th><th>Optional U / D</th><th>Coverage</th><th>BM optional</th><th>Service value</th><th>Total after</th><th>Lost vs no action</th><th>Risk</th></tr></thead><tbody>{impact.periods.map((period) => <PeriodRow key={period.delivery_period} period={period} custom={custom} action={actionMap[period.delivery_period]} onEdit={onEdit} onValue={onValue} />)}</tbody></table></div>;
}

function PeriodRow({ period, custom, action, onEdit, onValue }: { period: OptionalityPeriodDiagnostic; custom: boolean; action?: BatteryPathPeriodAction; onEdit: (period: string, field: "charge_mw" | "discharge_mw", value: number) => void; onValue: (point: CanonicalDataPoint) => void }) {
  return <tr className={period.commitment_at_risk ? "optionality-risk-row" : ""}><td><strong>SP{period.settlement_period}</strong><small>{formatUkMarketTime(period.delivery_start)}–{formatUkMarketTime(period.delivery_end)} UK time</small><em>impact #{period.risk_rank}</em></td>{custom && <><td><ActionInput label={`Charge MW ${period.delivery_period}`} value={action?.charge_mw ?? 0} onChange={(value) => onEdit(period.delivery_period, "charge_mw", value)} /></td><td><ActionInput label={`Discharge MW ${period.delivery_period}`} value={action?.discharge_mw ?? 0} onChange={(value) => onEdit(period.delivery_period, "discharge_mw", value)} /></td></>}<td><Pair first={period.starting_soc_value} second={period.ending_soc_value} onValue={onValue} /></td><td><div className="before-after"><Pair first={period.upward_power_available_before_value} second={period.upward_power_available_after_value} onValue={onValue} /><Pair first={period.downward_power_available_before_value} second={period.downward_power_available_after_value} onValue={onValue} /></div></td><td><Pair first={period.upward_duration_available_value} second={period.downward_duration_available_value} separator="/" onValue={onValue} /></td><td>{fmt(period.committed_upward_mw)} / {fmt(period.committed_downward_mw)} MW</td><td><Pair first={period.optional_upward_after_value} second={period.optional_downward_after_value} separator="/" onValue={onValue} /></td><td><button className={`coverage-value ${period.commitment_at_risk ? "at-risk" : ""}`} onClick={() => onValue(period.commitment_coverage_value)}>{fmt(period.commitment_coverage_ratio * 100, 0)}%</button></td><td><GridValue point={period.bm_estimate.expected_value} onValue={onValue} /></td><td><GridValue point={period.service_estimate.expected_service_value} onValue={onValue} /></td><td><GridValue point={period.optionality_value_after_value} onValue={onValue} /></td><td><GridValue point={period.optionality_lost_value} onValue={onValue} /></td><td>{period.violations.length ? <div className="path-violations">{period.violations.map((item) => <button key={item.code} onClick={() => item.observed_value && onValue(item.observed_value)}>{item.code.replaceAll("_", " ")}</button>)}</div> : <span className="covered-chip">COVERED</span>}</td></tr>;
}

function Explanation({ impact, onValue }: { impact: OptionalityPathImpact; onValue: (point: CanonicalDataPoint) => void }) {
  return <article className={`optionality-explanation panel ${impact.commitments_at_risk ? "invalid" : ""}`}><div className="optionality-orb">{impact.commitments_at_risk ? "RISK" : "OPTION"}</div><div><p>{impact.explanation}</p>{impact.violations.length > 0 && <div className="violation-summary">{impact.violations.slice(0, 6).map((item) => <button key={`${item.delivery_period}-${item.code}`} onClick={() => item.observed_value && onValue(item.observed_value)}><strong>{item.delivery_period}</strong> · {item.message}</button>)}</div>}<small>Committed obligations must remain deliverable. Optional BM/service value is uncertain, probability-weighted and never guaranteed revenue.</small></div></article>;
}

function Value({ label, point, onValue }: { label: string; point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint) => void }) {
  return <button disabled={!point} onClick={() => point && onValue(point)}><span>{label}</span><strong>{point ? money(Number(point.value)) : "—"}</strong></button>;
}

function Pair({ first, second, separator = "→", onValue }: { first: CanonicalDataPoint; second: CanonicalDataPoint; separator?: string; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="optionality-pair"><GridValue point={first} onValue={onValue} /><span>{separator}</span><GridValue point={second} onValue={onValue} /></div>;
}

function GridValue({ point, onValue }: { point: CanonicalDataPoint; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="grid-value" onClick={() => onValue(point)}>{point.unit === "GBP" ? money(Number(point.value)) : fmt(Number(point.value))}<small>{point.unit === "GBP" ? "" : point.unit}</small></button>;
}

function ActionInput({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return <input className="optionality-action" aria-label={label} type="number" min="0" step="0.5" value={value} onChange={(event) => onChange(Number(event.target.value))} />;
}

function assumptionValue(item: OptionalityAssumption) {
  if (item.unit === "probability") return `${fmt(item.value * 100, 0)}%`;
  if (item.unit.startsWith("GBP")) return `£${fmt(item.value)}${item.unit.slice(3)}`;
  return `${fmt(item.value, item.unit === "h" ? 2 : 1)} ${item.unit}`;
}

function fmt(value: number, digits = 1) { return value.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
function money(value: number) { return `${value < 0 ? "−" : ""}£${fmt(Math.abs(value), 2)}`; }
