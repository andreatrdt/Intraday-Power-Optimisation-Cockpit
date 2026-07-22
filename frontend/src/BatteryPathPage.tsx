import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadBatteryPathComparison, loadLineage, simulateBatteryPath } from "./api";
import { ConnectionStatus } from "./ConnectionStatus";
import { ProductNav } from "./ProductNav";
import { formatUkMarketTime } from "./time";
import type { BatteryPathComparison, BatteryPathPeriodAction, BatteryPathPeriodResult, BatteryPathSimulation, CanonicalDataPoint, LineageResponse, ScenarioExposure } from "./types";

type PathKind = "NO_ACTION" | "P50_COVERAGE" | "PRESERVE_FLEXIBILITY" | "CUSTOM";

export function BatteryPathPage() {
  const [comparison, setComparison] = useState<BatteryPathComparison | null>(null);
  const [selected, setSelected] = useState<PathKind>("NO_ACTION");
  const [customActions, setCustomActions] = useState<BatteryPathPeriodAction[]>([]);
  const [custom, setCustom] = useState<BatteryPathSimulation | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const customRequestVersion = useRef(0);

  const reload = useCallback(async () => {
    try {
      const next = await loadBatteryPathComparison();
      setComparison(next);
      setCustomActions(next.no_action.periods.map((period) => ({ delivery_period: period.delivery_period, charge_mw: 0, discharge_mw: 0 })));
      setLastPoll(new Date());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load sequential battery paths");
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    const requestVersion = ++customRequestVersion.current;
    if (selected !== "CUSTOM" || customActions.length === 0) return;
    const timer = window.setTimeout(() => {
      void simulateBatteryPath(customActions)
        .then((simulation) => {
          if (requestVersion !== customRequestVersion.current) return;
          setCustom(simulation); setLastPoll(new Date()); setError(null);
        })
        .catch((cause) => {
          if (requestVersion === customRequestVersion.current) {
            setError(cause instanceof Error ? cause.message : "Custom simulation failed");
          }
        });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [customActions, selected]);

  const simulation = useMemo(() => {
    if (!comparison) return null;
    if (selected === "P50_COVERAGE") return comparison.p50_coverage;
    if (selected === "PRESERVE_FLEXIBILITY") return comparison.preserve_flexibility;
    if (selected === "CUSTOM") return custom ?? comparison.no_action;
    return comparison.no_action;
  }, [comparison, custom, selected]);

  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try { setLineage(await loadLineage(point.value_id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to load lineage"); }
  };

  const editAction = (deliveryPeriod: string, field: "charge_mw" | "discharge_mw", value: number) => {
    setSelected("CUSTOM");
    setCustomActions((actions) => actions.map((action) => action.delivery_period === deliveryPeriod ? { ...action, [field]: Number.isFinite(value) ? value : 0 } : action));
  };

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Battery Path</h1></div></div>
      <ProductNav active="diagnostics" />
      <ConnectionStatus error={Boolean(error)} lastPoll={lastPoll} />
    </header>
    <main>
      <section className="hero-row path-hero"><div><p className="eyebrow">MILESTONE 1E · SEQUENTIAL WHAT-IF SIMULATION</p><h2>How does using flexibility now change every later settlement period?</h2><p className="intro">Candidate paths only · sequential SoC propagation · no optimal action or recommendation</p></div>{simulation && <Readiness simulation={simulation} />}</section>
      {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
      {simulation && comparison ? <>
        <PathSelector selected={selected} onSelect={setSelected} simulation={simulation} />
        {simulation.warnings.map((warning) => <div className="diagnostic-banner" key={warning}>{warning}</div>)}
        <section className="path-overview-grid">
          <SocChart simulation={simulation} onValue={openLineage} />
          <PathComparison comparison={comparison} selected={simulation} onValue={openLineage} />
        </section>
        <div className="section-heading"><div><p className="eyebrow">03 · SEQUENTIAL PERIOD PATH</p><h3>State, action and exposure propagation</h3></div><span>{selected === "CUSTOM" ? "Edit MW inputs; every later row recalculates" : "Select Custom path to edit charge/discharge MW"}</span></div>
        <PathTable simulation={simulation} custom={selected === "CUSTOM"} actions={customActions} onEdit={editAction} onValue={openLineage} />
        <div className="section-heading"><div><p className="eyebrow">04 · PATH DIAGNOSTIC</p><h3>Consequences and first constraints</h3></div><span>Descriptive only · no dispatch instruction</span></div>
        <Explanation simulation={simulation} />
      </> : <div className="empty panel">Waiting for sequential battery-path inputs…</div>}
    </main>
    {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
  </div>;
}

function Readiness({ simulation }: { simulation: BatteryPathSimulation }) {
  return <div className="fp-readiness panel"><div><span>Path readiness</span><strong className={`readiness ${simulation.readiness.status.toLowerCase()}`}>{simulation.readiness.status}</strong></div><dl><dt>Input calculation</dt><dd>{simulation.readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-control trust</dt><dd>{simulation.readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd><dt>Candidate path</dt><dd className={simulation.valid ? "long" : "short"}>{simulation.valid ? "VALID" : "VIOLATIONS"}</dd></dl><div className="path-readiness-reasons">{simulation.readiness.reasons.map((reason) => <p key={reason}>{reason}</p>)}</div></div>;
}

function PathSelector({ selected, onSelect, simulation }: { selected: PathKind; onSelect: (path: PathKind) => void; simulation: BatteryPathSimulation }) {
  const paths: { key: PathKind; label: string; note: string }[] = [
    { key: "NO_ACTION", label: "No action", note: "Preserve current SoC" },
    { key: "P50_COVERAGE", label: "Cover P50", note: "Diagnostic exposure coverage" },
    { key: "PRESERVE_FLEXIBILITY", label: "Preserve flex", note: "Use 25% of cover path" },
    { key: "CUSTOM", label: "Custom path", note: "User-edited MW inputs" },
  ];
  return <section className="path-selector panel"><div><p className="eyebrow">01 · CANDIDATE PATH</p><h3>{simulation.path_label}</h3></div><div className="path-options">{paths.map((path) => <button className={selected === path.key ? "active" : ""} key={path.key} onClick={() => onSelect(path.key)}><strong>{path.label}</strong><small>{path.note}</small></button>)}</div><div className="badges"><Badge value={simulation.source_mode} /><Badge value={simulation.quality} /><span className="diagnostic-chip">DIAGNOSTIC</span></div></section>;
}

function SocChart({ simulation, onValue }: { simulation: BatteryPathSimulation; onValue: (point: CanonicalDataPoint) => void }) {
  const min = simulation.e_min_mwh ?? 0; const max = simulation.e_max_mwh ?? 100; const target = simulation.terminal_target_mwh ?? min;
  const values = simulation.periods.length ? [simulation.periods[0].starting_soc_mwh, ...simulation.periods.map((period) => period.ending_soc_mwh)] : [];
  const low = min - 5; const high = max + 5; const width = 850; const height = 190; const left = 45; const right = 20; const top = 14; const bottom = 30;
  const x = (index: number) => left + index * (width - left - right) / Math.max(1, values.length - 1);
  const y = (value: number) => top + (high - value) / (high - low) * (height - top - bottom);
  const points = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
  return <article className="soc-chart panel"><header><div><p className="eyebrow">02 · SEQUENTIAL SOC</p><h3>Stored-energy path</h3></div><span>{simulation.periods.length} settlement periods</span></header><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Sequential state of charge for ${simulation.path_label}`}>
    <rect x={left} y={y(max)} width={width-left-right} height={y(min)-y(max)} className="soc-valid-band" />
    <line x1={left} x2={width-right} y1={y(min)} y2={y(min)} className="soc-limit" /><line x1={left} x2={width-right} y1={y(max)} y2={y(max)} className="soc-limit" />
    <line x1={left} x2={width-right} y1={y(target)} y2={y(target)} className="soc-target" />
    <polyline points={points} className="soc-path-line" />
    {values.map((value, index) => <g key={index}><circle cx={x(index)} cy={y(value)} r="4" className="soc-path-point" /><text x={x(index)} y={height-10} textAnchor="middle">{index === 0 ? "NOW" : `SP${simulation.periods[index-1].settlement_period}`}</text></g>)}
    <text x="3" y={y(max)+3}>E max {fmt(max, 0)}</text><text x="3" y={y(target)+3}>Target {fmt(target, 0)}</text><text x="3" y={y(min)+3}>E min {fmt(min, 0)}</text>
  </svg><div className="chart-terminal"><ValueButton label="Terminal SoC" point={simulation.terminal_soc_value} onValue={onValue} /><ValueButton label="Target shortfall" point={simulation.terminal_shortfall_value} onValue={onValue} /></div></article>;
}

function PathComparison({ comparison, selected, onValue }: { comparison: BatteryPathComparison; selected: BatteryPathSimulation; onValue: (point: CanonicalDataPoint) => void }) {
  const baseline = comparison.no_action;
  const terminalDelta = (selected.terminal_soc_mwh ?? 0) - (baseline.terminal_soc_mwh ?? 0);
  const residualReduction = (baseline.total_absolute_p50_residual_mwh ?? 0) - (selected.total_absolute_p50_residual_mwh ?? 0);
  return <article className="path-comparison panel"><header><div><p className="eyebrow">PATH COMPARISON</p><h3>Versus no battery action</h3></div><span className={selected.valid ? "valid-path" : "invalid-path"}>{selected.valid ? "NO HARD VIOLATION" : `${selected.violations.length} VIOLATION(S)`}</span></header><div className="comparison-metrics"><ValueButton label="Selected terminal SoC" point={selected.terminal_soc_value} onValue={onValue} /><ValueButton label="Absolute P50 residual" point={selected.total_absolute_p50_residual_value} onValue={onValue} /><div><span>Terminal Δ</span><strong>{signed(terminalDelta)} MWh</strong></div><div><span>P50 residual reduction</span><strong>{signed(residualReduction)} MWh</strong></div></div><dl><dt>No-action terminal</dt><dd>{fmt(baseline.terminal_soc_mwh ?? 0, 1)} MWh</dd><dt>First binding constraint</dt><dd>{selected.first_binding_constraint?.replaceAll("_", " ") ?? "None"}</dd><dt>Terminal target</dt><dd>{fmt(selected.terminal_target_mwh ?? 0, 1)} MWh</dd></dl><p>{comparison.explanation}</p></article>;
}

function PathTable({ simulation, custom, actions, onEdit, onValue }: { simulation: BatteryPathSimulation; custom: boolean; actions: BatteryPathPeriodAction[]; onEdit: (period: string, field: "charge_mw" | "discharge_mw", value: number) => void; onValue: (point: CanonicalDataPoint) => void }) {
  const actionMap = Object.fromEntries(actions.map((action) => [action.delivery_period, action]));
  return <div className="table-wrap panel path-table"><table><thead><tr><th>Period</th><th>Start SoC</th><th>Charge MW / MWh</th><th>Discharge MW / MWh</th><th>End SoC</th><th>P10 before → after</th><th>P50 before → after</th><th>P90 before → after</th><th>Up / down headroom</th><th>Reserve duration U / D</th><th>Available C / D</th><th>Constraints</th></tr></thead><tbody>{simulation.periods.map((period) => <PathRow key={period.delivery_period} period={period} custom={custom} action={actionMap[period.delivery_period]} onEdit={onEdit} onValue={onValue} />)}</tbody></table></div>;
}

function PathRow({ period, custom, action, onEdit, onValue }: { period: BatteryPathPeriodResult; custom: boolean; action?: BatteryPathPeriodAction; onEdit: (period: string, field: "charge_mw" | "discharge_mw", value: number) => void; onValue: (point: CanonicalDataPoint) => void }) {
  const before = Object.fromEntries(period.exposure_before.map((item) => [item.scenario, item])); const after = Object.fromEntries(period.residual_exposure.map((item) => [item.scenario, item]));
  const chargeMw = custom ? action?.charge_mw ?? period.charge_mw : period.charge_mw;
  const dischargeMw = custom ? action?.discharge_mw ?? period.discharge_mw : period.discharge_mw;
  return <tr className={period.violations.length ? "path-violation-row" : ""}><td><strong>SP{period.settlement_period}</strong><small>{formatUkMarketTime(period.delivery_start)}–{formatUkMarketTime(period.delivery_end)} UK time</small></td><td><GridValue point={period.starting_soc_value} onValue={onValue} /></td><td><ActionCell custom={custom} value={chargeMw} power={period.charge_power_value} energy={period.charge_energy_value} field="charge_mw" period={period.delivery_period} onEdit={onEdit} onValue={onValue} /></td><td><ActionCell custom={custom} value={dischargeMw} power={period.discharge_power_value} energy={period.discharge_energy_value} field="discharge_mw" period={period.delivery_period} onEdit={onEdit} onValue={onValue} /></td><td><GridValue point={period.ending_soc_value} onValue={onValue} /></td>{(["P10", "P50", "P90"] as const).map((scenario) => <td key={scenario}><ExposurePair before={before[scenario]} after={after[scenario]} onValue={onValue} /></td>)}<td><div className="path-value-pair"><GridValue point={period.upward_power_headroom_value} onValue={onValue} /><GridValue point={period.downward_power_headroom_value} onValue={onValue} /></div></td><td><div className="path-value-pair"><GridValue point={period.upward_energy_duration_value} onValue={onValue} /><GridValue point={period.downward_energy_duration_value} onValue={onValue} /></div></td><td><div className="path-value-pair"><GridValue point={period.max_feasible_charge_value} onValue={onValue} /><GridValue point={period.max_feasible_discharge_value} onValue={onValue} /></div></td><td><ConstraintCell period={period} onValue={onValue} /></td></tr>;
}

function ActionCell({ custom, value, power, energy, field, period, onEdit, onValue }: { custom: boolean; value: number; power: CanonicalDataPoint; energy: CanonicalDataPoint; field: "charge_mw" | "discharge_mw"; period: string; onEdit: (period: string, field: "charge_mw" | "discharge_mw", value: number) => void; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="action-cell">{custom ? <input aria-label={`${field === "charge_mw" ? "Charge" : "Discharge"} MW ${period}`} type="number" min="0" step="0.5" value={value} onChange={(event) => onEdit(period, field, Number(event.target.value))} /> : <button className="action-power" onClick={() => onValue(power)}>{fmt(value, 1)} MW</button>}{custom && <button onClick={() => onValue(power)}>MW lineage</button>}<button onClick={() => onValue(energy)}>{fmt(Number(energy.value), 1)} MWh</button></div>;
}

function ExposurePair({ before, after, onValue }: { before: ScenarioExposure; after: ScenarioExposure; onValue: (point: CanonicalDataPoint) => void }) {
  return <div className="exposure-pair"><button onClick={() => onValue(before.exposure_value)}>{signed(before.residual_position_mwh)}</button><span>→</span><button className={after.direction.toLowerCase()} onClick={() => onValue(after.exposure_value)}>{signed(after.residual_position_mwh)}</button></div>;
}

function ConstraintCell({ period, onValue }: { period: BatteryPathPeriodResult; onValue: (point: CanonicalDataPoint) => void }) {
  if (period.violations.length) return <div className="path-violations">{period.violations.map((violation) => <button key={violation.code} onClick={() => violation.observed_value && onValue(violation.observed_value)}>{violation.code.replaceAll("_", " ")}</button>)}</div>;
  if (period.binding_constraints.length) return <div className="path-bindings">{period.binding_constraints.map((item) => <code key={item}>{item.replaceAll("_", " ")}</code>)}</div>;
  return <span className="clear-state">CLEAR</span>;
}

function Explanation({ simulation }: { simulation: BatteryPathSimulation }) {
  return <article className={`path-explanation panel ${simulation.valid ? "" : "invalid"}`}><div className="path-orb">{simulation.valid ? "PATH" : "!"}</div><div><p>{simulation.explanation}</p><dl><dt>First binding constraint</dt><dd>{simulation.first_binding_constraint?.replaceAll("_", " ") ?? "None in displayed horizon"}</dd><dt>Terminal shortfall</dt><dd>{fmt(simulation.terminal_shortfall_mwh ?? 0, 1)} MWh</dd><dt>Hard violations</dt><dd>{simulation.violations.length}</dd></dl>{simulation.violations.length > 0 && <div className="violation-summary">{simulation.violations.slice(0, 5).map((violation) => <p key={`${violation.delivery_period}-${violation.code}`}><strong>{violation.delivery_period}</strong> · {violation.message}</p>)}</div>}<small>Candidate path simulation only. No trade, dispatch, BM or service-value recommendation is produced.</small></div></article>;
}

function ValueButton({ label, point, onValue }: { label: string; point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="path-metric" disabled={!point} onClick={() => point && onValue(point)}><span>{label}</span><strong>{point ? fmt(Number(point.value), 1) : "—"}</strong><small>{point?.unit ?? ""}</small></button>;
}

function GridValue({ point, onValue }: { point: CanonicalDataPoint; onValue: (point: CanonicalDataPoint) => void }) {
  return <button className="grid-value" onClick={() => onValue(point)}>{fmt(Number(point.value), 1)}<small>{point.unit}</small></button>;
}

function fmt(value: number, digits = 1) { return value.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
function signed(value: number) { return `${value >= 0 ? "+" : "−"}${fmt(Math.abs(value), 1)}`; }
