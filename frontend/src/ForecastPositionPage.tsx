import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, LineageDrawer } from "./App";
import { loadForecastPosition, loadLineage } from "./api";
import type {
  CanonicalDataPoint,
  ForecastPositionPeriod,
  ForecastPositionSnapshot,
  LineageResponse,
  ScenarioExposure,
} from "./types";

export function ForecastPositionPage() {
  const [snapshot, setSnapshot] = useState<ForecastPositionSnapshot | null>(null);
  const [selectedPeriodId, setSelectedPeriodId] = useState<string | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null);

  const reload = useCallback(async (quiet = false) => {
    try {
      const next = await loadForecastPosition();
      setSnapshot(next);
      setSelectedPeriodId((current) => current ?? next.periods[0]?.delivery_period ?? null);
      setLastLoaded(new Date());
      setError(null);
    } catch (cause) {
      if (!quiet) setError(cause instanceof Error ? cause.message : "Unable to load Forecast & Position");
    }
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(true), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const selected = useMemo(
    () => snapshot?.periods.find((period) => period.delivery_period === selectedPeriodId) ?? snapshot?.periods[0] ?? null,
    [snapshot, selectedPeriodId],
  );
  const exposed = useMemo(
    () => [...(snapshot?.periods ?? [])].sort((a, b) => a.risk_rank - b.risk_rank).slice(0, 3),
    [snapshot],
  );

  const openLineage = async (point: CanonicalDataPoint | null) => {
    if (!point) return;
    try {
      setLineage(await loadLineage(point.value_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load value lineage");
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark">IP</div>
          <div><p className="eyebrow">UK INTRADAY POWER</p><h1>Forecast &amp; Position</h1></div>
        </div>
        <nav>
          <a href="/data-flow">Data flow</a>
          <a className="active" href="/forecast-position">Forecast &amp; position</a>
          <a href="/market-liquidity">Market &amp; liquidity</a>
          <span>Optimisation</span><span>Actions</span>
        </nav>
        <div className="connection">
          <span className={`connection-dot ${error ? "down" : ""}`} />
          <span>{error ? "API issue" : "API connected"}</span>
          <small>{lastLoaded ? formatTime(lastLoaded.toISOString()) : "connecting…"}</small>
        </div>
      </header>

      <main>
        <section className="hero-row forecast-hero">
          <div>
            <p className="eyebrow">MILESTONE 1B · PRE-ACTION EXPOSURE</p>
            <h2>How has the forecast changed—and where are we long or short now?</h2>
            <p className="intro">Residual position by scenario: I<sub>t</sub><sup>s</sup> = G<sub>t</sub><sup>s</sup> − Q<sub>t</sub> · positive is long · negative is short</p>
          </div>
          {snapshot && <ReadinessCard snapshot={snapshot} />}
        </section>

        {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}
        {snapshot?.readiness.reasons.map((reason) => <div className="diagnostic-banner" key={reason}>{reason}</div>)}

        {snapshot && selected ? (
          <>
            <section className="forecast-summary-grid">
              <ForecastVintagePanel period={selected} snapshot={snapshot} onValue={openLineage} />
              <PositionPanel period={selected} exposed={exposed} onSelect={setSelectedPeriodId} onValue={openLineage} />
            </section>

            <div className="section-heading">
              <div><p className="eyebrow">03 · SETTLEMENT PERIOD DETAIL</p><h3>Scenario exposure grid</h3></div>
              <span>Click a period to focus · click any numeric value for lineage</span>
            </div>
            <PeriodGrid periods={snapshot.periods} selected={selected.delivery_period} onSelect={setSelectedPeriodId} onValue={openLineage} />

            <div className="section-heading">
              <div><p className="eyebrow">04 · DIAGNOSTIC EXPLANATION</p><h3>Why this period is exposed</h3></div>
              <span>Descriptive only · no trade or battery recommendation</span>
            </div>
            <section className="explanation-panel panel">
              <div className={`direction-orb ${selected.base_case_direction.toLowerCase()}`}>{selected.base_case_direction}</div>
              <div><p>{selected.explanation}</p><small>Risk rank {selected.risk_rank} of {snapshot.periods.length} · largest absolute scenario exposure {formatNumber(selected.risk_magnitude_mwh)} MWh</small></div>
            </section>
          </>
        ) : (
          <div className="empty panel">Waiting for a consistent forecast and contracted-position snapshot…</div>
        )}
      </main>
      {lineage && <LineageDrawer response={lineage} onClose={() => setLineage(null)} />}
    </div>
  );
}

function ReadinessCard({ snapshot }: { snapshot: ForecastPositionSnapshot }) {
  const readiness = snapshot.readiness;
  return (
    <div className="fp-readiness panel">
      <div><span>Forecast &amp; Position</span><strong className={`readiness ${readiness.status.toLowerCase()}`}>{readiness.status}</strong></div>
      <dl><dt>Calculation</dt><dd>{readiness.calculation_allowed ? "Allowed" : "Blocked"}</dd><dt>Live-trading trust</dt><dd>{readiness.trustworthy_for_live_trading ? "Yes" : "No"}</dd></dl>
    </div>
  );
}

function ForecastVintagePanel({ period, snapshot, onValue }: { period: ForecastPositionPeriod; snapshot: ForecastPositionSnapshot; onValue: (point: CanonicalDataPoint | null) => void }) {
  const forecast = period.forecast;
  return (
    <article className="summary-panel panel">
      <header><div><p className="eyebrow">01 · FORECAST VINTAGE</p><h3>{period.delivery_period} · {deliveryWindow(period)}</h3></div><div className="badges"><Badge value={forecast.p50.lineage.source_mode} /><Badge value={forecast.p50.lineage.quality} /></div></header>
      <div className="vintage-times"><Metric label="Latest vintage" value={formatDateTime(snapshot.latest_vintage?.issued_at)} /><Metric label="Previous vintage" value={formatDateTime(snapshot.previous_vintage?.issued_at)} /></div>
      <div className="scenario-strip">
        <ValueButton label="P10" point={forecast.p10} onClick={onValue} />
        <ValueButton label="P50 latest" point={forecast.p50} onClick={onValue} hero />
        <ValueButton label="P90" point={forecast.p90} onClick={onValue} />
      </div>
      <div className="metric-grid">
        <ValueButton label="Previous P50" point={forecast.previous_p50} onClick={onValue} />
        <ValueButton label="Δ previous" point={forecast.delta.versus_previous_value} onClick={onValue} signed />
        <ValueButton label="Δ day-ahead" point={forecast.delta.versus_day_ahead_value} onClick={onValue} signed />
        <ValueButton label="Model disagreement" point={forecast.reliability.disagreement_value} onClick={onValue} />
      </div>
      <div className="reliability-row"><span>Reliability</span><button disabled={!forecast.reliability.score_value} onClick={() => onValue(forecast.reliability.score_value)}>{forecast.reliability.label} {forecast.reliability.score === null ? "" : `${Math.round(forecast.reliability.score * 100)}%`}</button>{forecast.reliability.flags.map((flag) => <small key={flag}>{flag}</small>)}</div>
    </article>
  );
}

function PositionPanel({ period, exposed, onSelect, onValue }: { period: ForecastPositionPeriod; exposed: ForecastPositionPeriod[]; onSelect: (id: string) => void; onValue: (point: CanonicalDataPoint | null) => void }) {
  const p50 = exposure(period, "P50");
  return (
    <article className="summary-panel panel">
      <header><div><p className="eyebrow">02 · PORTFOLIO POSITION</p><h3>Pre-action residual exposure</h3></div><span className={`position-badge ${period.base_case_direction.toLowerCase()}`}>{period.base_case_direction} · P50</span></header>
      <div className="position-equation">
        <ValueButton label="Expected generation Gₜ" point={period.forecast.p50} onClick={onValue} />
        <span>−</span><ValueButton label="Contracted Qₜ" point={period.position.contracted_position} onClick={onValue} />
        <span>=</span><ValueButton label="Residual Iₜ" point={p50.exposure_value} onClick={onValue} hero signed />
      </div>
      <div className="scenario-exposure-list">{period.exposures.map((item) => <button key={item.scenario} onClick={() => onValue(item.exposure_value)}><span>{item.scenario}</span><strong className={item.direction.toLowerCase()}>{signed(item.residual_position_mwh)} MWh</strong><small>{item.direction}</small></button>)}</div>
      <div className="most-exposed"><span>Most exposed periods</span>{exposed.map((item) => <button key={item.delivery_period} onClick={() => onSelect(item.delivery_period)}><b>#{item.risk_rank}</b>{item.delivery_period}<strong>{formatNumber(item.risk_magnitude_mwh)} MWh</strong></button>)}</div>
    </article>
  );
}

function PeriodGrid({ periods, selected, onSelect, onValue }: { periods: ForecastPositionPeriod[]; selected: string; onSelect: (id: string) => void; onValue: (point: CanonicalDataPoint | null) => void }) {
  return (
    <div className="table-wrap panel period-grid"><table><thead><tr><th>SP / delivery</th><th>Latest P10</th><th>Latest P50</th><th>Latest P90</th><th>Previous P50</th><th>Forecast Δ</th><th>Qₜ</th><th>Iₜ P10</th><th>Iₜ P50</th><th>Iₜ P90</th><th>Source / quality</th><th>Warnings</th></tr></thead>
      <tbody>{periods.map((period) => {
        const p10 = exposure(period, "P10"); const p50 = exposure(period, "P50"); const p90 = exposure(period, "P90");
        return <tr key={period.delivery_period} className={period.delivery_period === selected ? "selected-period" : ""} onClick={() => onSelect(period.delivery_period)}>
          <td><strong>SP{period.settlement_period}</strong><small>{formatTime(period.delivery_start)}–{formatTime(period.delivery_end)}</small><em>risk #{period.risk_rank}</em></td>
          <GridValue point={period.forecast.p10} onValue={onValue} /><GridValue point={period.forecast.p50} onValue={onValue} /><GridValue point={period.forecast.p90} onValue={onValue} /><GridValue point={period.forecast.previous_p50} onValue={onValue} /><GridValue point={period.forecast.delta.versus_previous_value} onValue={onValue} signedValue />
          <GridValue point={period.position.contracted_position} onValue={onValue} />
          <ExposureCell exposure={p10} onValue={onValue} /><ExposureCell exposure={p50} onValue={onValue} /><ExposureCell exposure={p90} onValue={onValue} />
          <td><div className="badges"><Badge value={period.forecast.p50.lineage.source_mode} /><Badge value={period.forecast.p50.lineage.quality} /></div></td>
          <td>{period.warnings.length ? <span className="warning-count" title={period.warnings.join("\n")}>{period.warnings.length} warning{period.warnings.length === 1 ? "" : "s"}</span> : <span className="clear-state">Clear</span>}</td>
        </tr>;
      })}</tbody></table></div>
  );
}

function ValueButton({ label, point, onClick, hero = false, signed: signedValue = false }: { label: string; point: CanonicalDataPoint | null; onClick: (point: CanonicalDataPoint | null) => void; hero?: boolean; signed?: boolean }) {
  return <button className={`value-button ${hero ? "hero" : ""}`} disabled={!point} onClick={() => onClick(point)}><span>{label}</span><strong>{point ? (signedValue ? signed(Number(point.value)) : formatNumber(Number(point.value))) : "—"}</strong><small>{point?.unit ?? "unavailable"}</small></button>;
}

function GridValue({ point, onValue, signedValue = false }: { point: CanonicalDataPoint | null; onValue: (point: CanonicalDataPoint | null) => void; signedValue?: boolean }) {
  return <td><button className="grid-value" disabled={!point} onClick={(event) => { event.stopPropagation(); onValue(point); }}>{point ? (signedValue ? signed(Number(point.value)) : formatNumber(Number(point.value))) : "—"}<small>{point?.unit ?? ""}</small></button></td>;
}

function ExposureCell({ exposure: item, onValue }: { exposure: ScenarioExposure; onValue: (point: CanonicalDataPoint | null) => void }) {
  return <td><button className={`grid-value exposure ${item.direction.toLowerCase()}`} onClick={(event) => { event.stopPropagation(); onValue(item.exposure_value); }}><strong>{signed(item.residual_position_mwh)}</strong><small>{item.direction}</small></button></td>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div>; }
function exposure(period: ForecastPositionPeriod, scenario: string): ScenarioExposure { return period.exposures.find((item) => item.scenario === scenario)!; }
function formatNumber(value: number): string { return value.toLocaleString(undefined, { maximumFractionDigits: 1 }); }
function signed(value: number): string { return `${value > 0 ? "+" : ""}${formatNumber(value)}`; }
function formatDateTime(value?: string | null): string { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "Unavailable"; }
function formatTime(value: string): string { return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function deliveryWindow(period: ForecastPositionPeriod): string { return `${formatTime(period.delivery_start)}–${formatTime(period.delivery_end)}`; }
