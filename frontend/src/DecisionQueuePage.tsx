import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "./App";
import { ConnectionStatus } from "./ConnectionStatus";
import { DecisionExecutionPanel } from "./DecisionExecutionPanel";
import { DecisionLifecycleControls } from "./DecisionLifecycleControls";
import { DecisionOutcomePanel } from "./DecisionOutcomePanel";
import { ProductNav } from "./ProductNav";
import { formatTimestampWithZone } from "./time";
import {
  loadDecisionBatches,
  loadDecisionBatchSummaries,
  loadDecisions,
  loadEvaluations,
  loadForecastRevisions,
  loadHedgeTimingAssessments,
  processCompletedDecisions,
  refreshDecisions,
  reassessTiming,
} from "./api";
import type {
  DecisionBatch,
  DecisionBatchSummary,
  DecisionEvaluationResult,
  DecisionQualityLabel,
  DecisionStatus,
  ForecastRevision,
  HedgeTimingAssessment,
  TimingPriority,
  TimingVerdict,
  TradeDecision,
} from "./types";

// Lifecycle states beyond execution: the outcome/evaluation stages.
const COMPLETED_STATES = new Set<DecisionStatus>(["DELIVERED", "SETTLED", "EVALUATED"]);
const QUALITY_LABEL: Record<DecisionQualityLabel, string> = {
  OUTPERFORMED_NO_ACTION: "Outperformed no-action",
  UNDERPERFORMED_NO_ACTION: "Underperformed no-action",
  IN_LINE_WITH_NO_ACTION: "In line with no-action",
  UNAVAILABLE: "Unavailable",
};
// SAMPLE-only batch action; explicit that it is not live/historical performance.
const PROCESS_WARNING = "This uses simulated realised generation and settlement prices. It does not represent live or historical trading performance.";

// The backend owns all decision / revision / timing / priority logic. This page
// only reads, joins by id, filters and sorts. The client sort mirrors the
// backend's deterministic prioritise() order and top-cap for presentation.
const TOP_CAP = 8;
const PRIORITY_RANK: Record<TimingPriority, number> = {
  CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFORMATIONAL: 4,
};
const VERDICT_LABEL: Record<TimingVerdict, string> = {
  HEDGE_NOW: "HEDGE NOW", PARTIAL_HEDGE_NOW: "PARTIAL HEDGE", WAIT: "WAIT", NO_ACTION: "NO ACTION",
};
const n = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const signed = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${n(value, digits)}`;

interface QueueData {
  decisions: TradeDecision[];
  revisions: Map<string, ForecastRevision>;
  assessments: Map<string, HedgeTimingAssessment>; // latest per decision_id
  evaluations: Map<string, DecisionEvaluationResult>; // by decision_id
  batches: DecisionBatch[];
  summaries: DecisionBatchSummary[];
}

interface Filters {
  priority: string;
  verdict: string;
  action: string;
  status: string;
  quality: string;
  outcome: string;
  decisionQuality: string;
  batch: string;
  spFrom: string;
  spTo: string;
}

const EMPTY_FILTERS: Filters = { priority: "ALL", verdict: "ALL", action: "ALL", status: "ALL", quality: "ALL", outcome: "ALL", decisionQuality: "ALL", batch: "ALL", spFrom: "", spTo: "" };
const DECISION_STATUSES = ["PROPOSED", "ACCEPTED", "MODIFIED", "REJECTED", "DELAYED", "ACTIVE_ONLY"];
// Outcome (delivery/settlement/evaluation) lifecycle filter.
const OUTCOME_FILTERS = ["ALL", "ACTIVE", "COMPLETED", "DELIVERED", "SETTLED", "EVALUATED"];

export function DecisionQueuePage() {
  const [data, setData] = useState<QueueData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null);
  const [busy, setBusy] = useState<null | "refresh" | "reassess" | "process">(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [expandedBatch, setExpandedBatch] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    try {
      const [decisions, revisions, assessments, evaluations, batches, summaries] = await Promise.all([
        loadDecisions(), loadForecastRevisions(), loadHedgeTimingAssessments(), loadEvaluations(), loadDecisionBatches(), loadDecisionBatchSummaries(),
      ]);
      const revisionMap = new Map(revisions.map((r) => [r.revision_id, r]));
      const assessmentMap = new Map<string, HedgeTimingAssessment>();
      for (const assessment of assessments) {
        // Assessments are newest-first; keep the first (latest) per decision.
        if (!assessmentMap.has(assessment.decision_id)) assessmentMap.set(assessment.decision_id, assessment);
      }
      const evaluationMap = new Map(evaluations.map((e) => [e.decision_id, e]));
      setData({ decisions, revisions: revisionMap, assessments: assessmentMap, evaluations: evaluationMap, batches, summaries });
      setLastLoaded(new Date());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load the decision queue");
    }
  }, []);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const onRefresh = async () => {
    setBusy("refresh");
    setNotice(null);
    try {
      const response = await refreshDecisions();
      const created = response.refresh.created_decision_ids.length;
      const skipped = response.refresh.skipped.length;
      setNotice(`Decision refresh: ${created} created, ${skipped} skipped. This does not submit a trade.`);
      await loadAll();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision refresh failed");
    } finally {
      setBusy(null);
    }
  };

  const onReassess = async () => {
    setBusy("reassess");
    setNotice(null);
    try {
      const response = await reassessTiming();
      const created = response.assessment.created_assessment_ids.length;
      setNotice(`Timing reassessed: ${created} new assessment(s). Diagnostic only — no order is submitted.`);
      await loadAll();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Timing reassessment failed");
    } finally {
      setBusy(null);
    }
  };

  const onProcessCompleted = async () => {
    setBusy("process");
    setNotice(null);
    try {
      const response = await processCompletedDecisions();
      const { processed, existing, skipped } = response.result;
      setNotice(`Processed ${processed.length} completed SAMPLE period(s); ${existing.length} already evaluated; ${skipped.length} skipped. ${PROCESS_WARNING}`);
      await loadAll();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Processing completed periods failed");
    } finally {
      setBusy(null);
    }
  };

  const meta = data?.decisions[0]?.context;

  const passesFilters = useCallback((decision: TradeDecision, assessment: HedgeTimingAssessment | undefined) => {
    if (filters.priority !== "ALL" && assessment?.priority !== filters.priority) return false;
    if (filters.verdict !== "ALL" && assessment?.verdict !== filters.verdict) return false;
    if (filters.action !== "ALL" && decision.recommendation.action !== filters.action) return false;
    if (filters.status === "ACTIVE_ONLY" && decision.status === "REJECTED") return false;
    else if (filters.status !== "ALL" && filters.status !== "ACTIVE_ONLY" && decision.status !== filters.status) return false;
    if (filters.quality !== "ALL" && decision.context.quality !== filters.quality) return false;
    // Outcome (delivery/settlement/evaluation) lifecycle filter.
    const isCompleted = COMPLETED_STATES.has(decision.status);
    if (filters.outcome === "ACTIVE" && isCompleted) return false;
    if (filters.outcome === "COMPLETED" && !isCompleted) return false;
    if (["DELIVERED", "SETTLED", "EVALUATED"].includes(filters.outcome) && decision.status !== filters.outcome) return false;
    // Decision-quality filter (from the joined evaluation).
    if (filters.decisionQuality !== "ALL" && data?.evaluations.get(decision.decision_id)?.decision_quality_label !== filters.decisionQuality) return false;
    if (filters.batch !== "ALL" && decision.batch_id !== filters.batch) return false;
    const sp = decision.context.settlement_period;
    if (filters.spFrom && sp < Number(filters.spFrom)) return false;
    if (filters.spTo && sp > Number(filters.spTo)) return false;
    return true;
  }, [filters, data]);

  const ranked = useMemo(() => {
    if (!data) return [];
    return data.decisions
      // Active/actionable decisions first; completed/evaluated live in their own section.
      .filter((decision) => !COMPLETED_STATES.has(decision.status))
      .map((decision) => ({ decision, assessment: data.assessments.get(decision.decision_id) }))
      .filter((row): row is { decision: TradeDecision; assessment: HedgeTimingAssessment } => Boolean(row.assessment))
      .filter((row) => passesFilters(row.decision, row.assessment))
      .sort((a, b) =>
        PRIORITY_RANK[a.assessment.priority] - PRIORITY_RANK[b.assessment.priority]
        || b.assessment.priority_score - a.assessment.priority_score
        || b.assessment.gate_closure_score - a.assessment.gate_closure_score
        || a.decision.decision_id.localeCompare(b.decision.decision_id))
      .slice(0, TOP_CAP);
  }, [data, passesFilters]);

  // Completed / evaluated decisions are kept accessible but out of the active queue.
  const completed = useMemo(() => {
    if (!data) return [];
    return data.decisions
      .filter((decision) => COMPLETED_STATES.has(decision.status))
      .filter((decision) => passesFilters(decision, data.assessments.get(decision.decision_id)))
      .sort((a, b) => a.context.settlement_period - b.context.settlement_period);
  }, [data, passesFilters]);

  const totals = useMemo(() => {
    const assessments = data ? [...data.assessments.values()] : [];
    const count = (predicate: (a: HedgeTimingAssessment) => boolean) => assessments.filter(predicate).length;
    return {
      decisions: data?.decisions.length ?? 0,
      batches: data?.batches.length ?? 0,
      critical: count((a) => a.priority === "CRITICAL"),
      high: count((a) => a.priority === "HIGH"),
      hedgeNow: count((a) => a.verdict === "HEDGE_NOW"),
      partial: count((a) => a.verdict === "PARTIAL_HEDGE_NOW"),
      wait: count((a) => a.verdict === "WAIT"),
      noAction: count((a) => a.verdict === "NO_ACTION"),
      unassessed: (data?.decisions.length ?? 0) - assessments.length,
    };
  }, [data]);

  const selectedDecision = data?.decisions.find((d) => d.decision_id === selected) ?? null;

  return <div className="app-shell decisions-page">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Trader Decision Queue</h1></div></div>
      <ProductNav active="decisions" />
      <ConnectionStatus error={Boolean(error)} lastPoll={lastLoaded} />
    </header>
    <main>
      {error && <div className="error-banner"><strong>Backend unavailable</strong><span>{error}</span><button onClick={() => void loadAll()}>Retry</button></div>}

      {/* A. Header meta + actions */}
      <section className="decision-header panel">
        <div className="decision-header-meta">
          <div><span>Run mode</span><strong>{(meta?.run_mode ?? "SAMPLE_DEMO").replaceAll("_", " ")}</strong></div>
          <div><span>Source</span><Badge value={meta?.source_mode ?? "SAMPLE"} /></div>
          <div><span>Quality</span><Badge value={meta?.quality ?? "FRESH"} /></div>
          <div><span>Last refresh</span><strong>{lastLoaded ? formatTimestampWithZone(lastLoaded.toISOString(), "local time") : "—"}</strong></div>
        </div>
        <div className="decision-header-badges">
          <span className="pill pill-sample">SAMPLE</span>
          <span className="pill pill-diagnostic">DIAGNOSTIC ONLY</span>
          <span className="pill pill-nonexec">NOT EXECUTABLE</span>
          {meta && meta.calculation_allowed === false && <span className="pill pill-degraded">DEGRADED</span>}
        </div>
        <div className="decision-actions">
          <button className="primary-action" disabled={busy !== null} onClick={() => void onRefresh()}>{busy === "refresh" ? "Refreshing…" : "Refresh decisions"}</button>
          <button disabled={busy !== null} onClick={() => void onReassess()}>{busy === "reassess" ? "Reassessing…" : "Reassess timing"}</button>
          <button disabled={busy !== null} onClick={() => void onProcessCompleted()}>{busy === "process" ? "Processing…" : "Process completed SAMPLE periods"}</button>
          <span className="control-note">Neither action submits a trade. All output is diagnostic and non-executable.</span>
        </div>
        <p className="process-warning">{PROCESS_WARNING}</p>
        {notice && <p className="decision-notice">{notice}</p>}
      </section>

      {/* B. Summary strip */}
      <section className="decision-summary-strip panel">
        <div><span>Active decisions</span><strong>{totals.decisions}</strong></div>
        <div className="crit"><span>Critical</span><strong>{totals.critical}</strong></div>
        <div className="high"><span>High</span><strong>{totals.high}</strong></div>
        <div><span>Hedge now</span><strong>{totals.hedgeNow}</strong></div>
        <div><span>Partial</span><strong>{totals.partial}</strong></div>
        <div><span>Wait</span><strong>{totals.wait}</strong></div>
        <div><span>No action</span><strong>{totals.noAction}</strong></div>
        <div><span>Batches</span><strong>{totals.batches}</strong></div>
        <div><span>Unassessed</span><strong>{totals.unassessed}</strong></div>
      </section>

      {/* G. Filters */}
      <section className="decision-filters panel" aria-label="Decision filters">
        <label><span>Priority</span><select value={filters.priority} onChange={(e) => setFilters({ ...filters, priority: e.target.value })}>{["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Verdict</span><select value={filters.verdict} onChange={(e) => setFilters({ ...filters, verdict: e.target.value })}>{["ALL", "HEDGE_NOW", "PARTIAL_HEDGE_NOW", "WAIT", "NO_ACTION"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Action</span><select value={filters.action} onChange={(e) => setFilters({ ...filters, action: e.target.value })}>{["ALL", "BUY", "SELL", "NO_ACTION"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Status</span><select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>{["ALL", ...DECISION_STATUSES].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Quality</span><select value={filters.quality} onChange={(e) => setFilters({ ...filters, quality: e.target.value })}>{["ALL", "FRESH", "REVISED", "PARTIAL", "STALE", "MISSING", "INVALID"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Outcome</span><select value={filters.outcome} onChange={(e) => setFilters({ ...filters, outcome: e.target.value })}>{OUTCOME_FILTERS.map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Decision quality</span><select value={filters.decisionQuality} onChange={(e) => setFilters({ ...filters, decisionQuality: e.target.value })}>{["ALL", "OUTPERFORMED_NO_ACTION", "UNDERPERFORMED_NO_ACTION", "IN_LINE_WITH_NO_ACTION", "UNAVAILABLE"].map((v) => <option key={v}>{v}</option>)}</select></label>
        <label><span>Batch</span><select value={filters.batch} onChange={(e) => setFilters({ ...filters, batch: e.target.value })}><option>ALL</option>{data?.batches.map((b) => <option key={b.batch_id} value={b.batch_id}>{b.batch_id}</option>)}</select></label>
        <label><span>SP from</span><input type="number" value={filters.spFrom} onChange={(e) => setFilters({ ...filters, spFrom: e.target.value })} /></label>
        <label><span>SP to</span><input type="number" value={filters.spTo} onChange={(e) => setFilters({ ...filters, spTo: e.target.value })} /></label>
        <button className="ghost" onClick={() => setFilters(EMPTY_FILTERS)}>Clear</button>
      </section>

      {/* Empty / degraded states */}
      {data && data.decisions.length === 0 && <section className="empty panel decision-empty">
        {data.revisions.size > 0
          ? <p><strong>Forecast revisions were calculated, but none crossed the materiality thresholds.</strong> No material revision means no decision to queue.</p>
          : <p><strong>No decisions yet.</strong> At least two complete forecast vintages are required. Refresh the rolling environment (Live State), then use <em>Refresh decisions</em>. No decision may simply mean no material forecast revision currently exists.</p>}
      </section>}

      {/* C. Ranked decision queue */}
      {data && data.decisions.length > 0 && <>
        <div className="section-heading"><div><p className="eyebrow">RANKED QUEUE · TOP {TOP_CAP}</p><h2>Decisions needing attention</h2></div><span>Backend priority ranking · {ranked.length} shown</span></div>
        {ranked.length === 0 && data.assessments.size === 0
          ? <section className="empty panel">Timing has not been assessed yet. Use <em>Reassess timing</em> to rank the queue.</section>
          : <section className="decision-queue">
            {ranked.map(({ decision, assessment }) => <DecisionCard key={decision.decision_id} decision={decision} assessment={assessment} revision={decision.context.forecast_revision_id ? data.revisions.get(decision.context.forecast_revision_id) : undefined} onOpen={() => setSelected(decision.decision_id)} />)}
          </section>}
      </>}

      {/* D. Completed & evaluated outcomes (kept out of the active queue) */}
      {data && completed.length > 0 && <>
        <div className="section-heading"><div><p className="eyebrow">COMPLETED &amp; EVALUATED</p><h2>Delivered · settled · evaluated</h2></div><span>Read-only realised outcomes · {completed.length} shown</span></div>
        <section className="completed-queue">
          {completed.map((decision) => {
            const evaluation = data.evaluations.get(decision.decision_id);
            return <article key={decision.decision_id} className="completed-card" tabIndex={0} onClick={() => setSelected(decision.decision_id)} onKeyDown={(e) => e.key === "Enter" && setSelected(decision.decision_id)}>
              <div className="cc-primary">
                <span className={`status-tag status-${decision.status.toLowerCase()}`}>{decision.status}</span>
                <span className="dc-sp">SP{decision.context.settlement_period}</span>
              </div>
              {evaluation
                ? <div className="cc-outcome">
                    <span className={`quality-label quality-${evaluation.decision_quality_label.toLowerCase()}`}>{QUALITY_LABEL[evaluation.decision_quality_label]}</span>
                    <span className="cc-pnl">Incr. P&amp;L {signed(evaluation.realised_outcome.realised_pnl_gbp, 2)}</span>
                  </div>
                : <span className="cc-pending">Awaiting evaluation</span>}
              <div className="dc-badges"><Badge value={decision.context.source_mode} /><Badge value={decision.context.quality} /></div>
            </article>;
          })}
        </section>
      </>}

      {/* E. Batch summaries */}
      {data && data.summaries.length > 0 && <>
        <div className="section-heading"><div><p className="eyebrow">BATCHES</p><h2>Full batch overview</h2></div><span>Avoids showing every period as an equal-priority card</span></div>
        <section className="batch-grid">
          {data.summaries.map((summary) => {
            const batch = data.batches.find((b) => b.batch_id === summary.batch_id);
            const expanded = expandedBatch === summary.batch_id;
            return <article className="panel batch-card" key={summary.batch_id}>
              <header><div><p className="eyebrow">{summary.affected_period_range ?? "—"}</p><h3>{summary.total_decisions} decisions</h3></div><code className="compact-id">{summary.batch_id}</code></header>
              <div className="batch-counts">
                <span className="crit">Critical {summary.critical_count}</span>
                <span className="high">High {summary.high_count}</span>
                <span>Hedge now {summary.hedge_now_periods}</span>
                <span>Partial {summary.partial_hedge_periods}</span>
                <span>Wait {summary.wait_periods}</span>
                <span>Info {summary.informational_count}</span>
              </div>
              <div className="batch-top"><span>Top decisions</span><div>{summary.top_decision_ids.map((id) => <button key={id} className="link-id" onClick={() => setSelected(id)}>{id.slice(0, 16)}…</button>)}</div></div>
              <button className="ghost" onClick={() => setExpandedBatch(expanded ? null : summary.batch_id)}>{expanded ? "Hide all periods" : `Show all ${summary.total_decisions} periods`}</button>
              {expanded && batch && <ul className="batch-all-periods">{batch.decision_ids.map((id) => {
                const decision = data.decisions.find((d) => d.decision_id === id);
                const assessment = data.assessments.get(id);
                return <li key={id}><button className="link-id" onClick={() => setSelected(id)}>SP{decision?.context.settlement_period ?? "?"}</button><span>{assessment ? VERDICT_LABEL[assessment.verdict] : "unassessed"}</span>{assessment && <span className={`priority-tag priority-${assessment.priority.toLowerCase()}`}>{assessment.priority}</span>}</li>;
              })}</ul>}
            </article>;
          })}
        </section>
      </>}
    </main>

    {selectedDecision && <DecisionDrawer
      decision={selectedDecision}
      assessment={data?.assessments.get(selectedDecision.decision_id)}
      revision={selectedDecision.context.forecast_revision_id ? data?.revisions.get(selectedDecision.context.forecast_revision_id) : undefined}
      onClose={() => setSelected(null)}
      onReassess={() => void onReassess()}
      onReload={loadAll}
    />}
  </div>;
}

function DecisionCard({ decision, assessment, revision, onOpen }: { decision: TradeDecision; assessment: HedgeTimingAssessment; revision: ForecastRevision | undefined; onOpen: () => void }) {
  const ctx = decision.context;
  const rec = decision.recommendation;
  const total = Math.abs(rec.buy_mwh - rec.sell_mwh);
  const nowVol = assessment.recommended_now_buy_mwh + assessment.recommended_now_sell_mwh;
  const deferred = assessment.deferred_buy_mwh + assessment.deferred_sell_mwh;
  const direction = rec.action === "BUY" ? "BUY" : rec.action === "SELL" ? "SELL" : "NO ACTION";
  const significance = assessment.significance_available ? assessment.confidence_or_significance_component : null;
  const market = assessment.market;
  return <article className={`decision-card priority-${assessment.priority.toLowerCase()} verdict-${assessment.verdict.toLowerCase()}`} tabIndex={0} onClick={onOpen} onKeyDown={(e) => e.key === "Enter" && onOpen()}>
    <div className="dc-primary">
      <span className={`priority-tag priority-${assessment.priority.toLowerCase()}`}>{assessment.priority}</span>
      <span className={`verdict-tag verdict-${assessment.verdict.toLowerCase()}`}>{VERDICT_LABEL[assessment.verdict]}</span>
      <span className={`status-tag status-${decision.status.toLowerCase()}`}>{decision.status}</span>
      <span className="dc-sp">SP{ctx.settlement_period}</span>
      <span className="dc-gate">{ctx.minutes_to_gate_closure === null ? "gate —" : `${n(ctx.minutes_to_gate_closure, 0)} min to gate`}</span>
    </div>
    <div className="dc-action">
      <strong className={`dir dir-${direction.toLowerCase().replace(" ", "-")}`}>{direction}</strong>
      <span>{n(total)} MWh</span>
      <small>now {n(nowVol)} · defer {n(deferred)}</small>
    </div>
    <div className="dc-exposure">
      <span>P50 exposure <strong>{signed(ctx.p50_exposure_before_mwh)}</strong> MWh</span>
      <small>P10/P90 {signed(ctx.p10_exposure_before_mwh)} / {signed(ctx.p90_exposure_before_mwh)}</small>
      <small>ΔP50 revision {signed(ctx.forecast_revision_mwh)} MWh{significance !== null ? ` · significance ${n(significance, 2)}` : " · significance n/a"}</small>
    </div>
    <div className="dc-liquidity">
      <small>spread {market?.spread_gbp_per_mwh != null ? `£${n(market.spread_gbp_per_mwh, 2)}` : "—"}</small>
      <small>depth {market?.executable_volume_mwh != null ? `${n(market.executable_volume_mwh)} MWh` : "—"}</small>
      <small>WAP {market?.wap_gbp_per_mwh != null ? `£${n(market.wap_gbp_per_mwh, 2)}` : "—"}</small>
    </div>
    <div className="dc-reason">
      <p>{assessment.reasons[0] ?? "No reason provided."}</p>
      <div className="dc-badges">
        <Badge value={ctx.source_mode} /><Badge value={ctx.quality} />
        {assessment.warnings.length > 0 && <span className="warn-count">{assessment.warnings.length} warning{assessment.warnings.length === 1 ? "" : "s"}</span>}
        {ctx.delivery_start && <span className="dc-window">{formatTimestampWithZone(ctx.delivery_start, "UK time")}</span>}
      </div>
    </div>
    {!revision && ctx.forecast_revision_id && <p className="evidence-missing">Supporting evidence unavailable</p>}
  </article>;
}

function DecisionDrawer({ decision, assessment, revision, onClose, onReassess, onReload }: { decision: TradeDecision; assessment: HedgeTimingAssessment | undefined; revision: ForecastRevision | undefined; onClose: () => void; onReassess: () => void; onReload: () => Promise<void> | void }) {
  const ctx = decision.context;
  const comp = revision?.comparison;
  const port = revision?.portfolio;
  const pc = assessment?.priority_components;
  return <div className="drawer-backdrop" onMouseDown={onClose}>
    <aside className="drawer decision-drawer" onMouseDown={(e) => e.stopPropagation()}>
      <div className="drawer-head">
        <div><p className="eyebrow">DECISION · SP{ctx.settlement_period}</p><h3>{ctx.delivery_period}</h3><p>{ctx.trigger_description}</p></div>
        <button onClick={onClose} aria-label="Close decision drawer">×</button>
      </div>

      <DecisionLifecycleControls decision={decision} revision={revision} assessment={assessment} onReload={onReload} />
      <DecisionExecutionPanel decision={decision} assessment={assessment} onReload={onReload} />
      <DecisionOutcomePanel decision={decision} onReload={onReload} />

      <section className="drawer-section">
        <h4>What changed</h4>
        {comp ? <dl className="detail-list">
          <Stat label="Previous P10/P50/P90" value={`${n(comp.previous_p10_mwh)} / ${n(comp.previous_p50_mwh)} / ${n(comp.previous_p90_mwh)} MWh`} />
          <Stat label="Latest P10/P50/P90" value={`${n(comp.latest_p10_mwh)} / ${n(comp.latest_p50_mwh)} / ${n(comp.latest_p90_mwh)} MWh`} />
          <Stat label="ΔP10/ΔP50/ΔP90" value={`${signed(comp.delta_p10_mwh)} / ${signed(comp.delta_p50_mwh)} / ${signed(comp.delta_p90_mwh)} MWh`} />
          <Stat label="Uncertainty-width change" value={`${signed(comp.uncertainty_width_change_mwh)} MWh`} />
          <Stat label="Forecast horizon" value={`${n(comp.forecast_horizon_minutes, 0)} min`} />
        </dl> : <p className="evidence-missing">Supporting evidence unavailable</p>}
        {revision?.materiality.materiality_reasons.map((reason) => <p className="reason-line" key={reason}>{reason}</p>)}
      </section>

      <section className="drawer-section">
        <h4>Portfolio impact</h4>
        <p className="sign-note">Exposure = forecast generation − contracted position. Positive = LONG, negative = SHORT.</p>
        {port ? <dl className="detail-list">
          <Stat label="Contracted position Q" value={`${n(port.contracted_position_q_mwh)} MWh`} />
          <Stat label="Previous P10/P50/P90 exposure" value={`${signed(port.previous_p10_exposure_mwh)} / ${signed(port.previous_p50_exposure_mwh)} / ${signed(port.previous_p90_exposure_mwh)} MWh`} />
          <Stat label="Latest P10/P50/P90 exposure" value={`${signed(port.latest_p10_exposure_mwh)} / ${signed(port.latest_p50_exposure_mwh)} / ${signed(port.latest_p90_exposure_mwh)} MWh`} />
          <Stat label="Direction before → after" value={`${port.direction_before} → ${port.direction_after}`} />
          <Stat label="Crossed zero" value={port.crossed_zero_exposure ? "YES" : "no"} />
        </dl> : <p className="evidence-missing">Supporting evidence unavailable</p>}
      </section>

      <section className="drawer-section">
        <h4>Timing assessment</h4>
        {assessment && pc ? <>
          <div className="drawer-verdict"><span className={`verdict-tag verdict-${assessment.verdict.toLowerCase()}`}>{VERDICT_LABEL[assessment.verdict]}</span><span className={`priority-tag priority-${assessment.priority.toLowerCase()}`}>{assessment.priority}</span><span>score {n(assessment.priority_score, 2)}</span></div>
          <dl className="detail-list">
            <Stat label="Recommended now / deferred" value={`${n(assessment.recommended_now_buy_mwh + assessment.recommended_now_sell_mwh)} / ${n(assessment.deferred_buy_mwh + assessment.deferred_sell_mwh)} MWh`} />
            <Stat label="Urgency component" value={n(pc.gate_closure_component, 2)} />
            <Stat label="Liquidity component" value={n(pc.liquidity_component, 2)} />
            <Stat label="Exposure-risk component" value={n(assessment.exposure_risk_score, 2)} />
            <Stat label="Gate-Closure component" value={n(pc.gate_closure_component, 2)} />
            <Stat label="Revision-significance component" value={assessment.significance_available ? n(pc.significance_component, 2) : "unavailable"} />
            <Stat label="Spread/slippage penalty" value={n(pc.spread_slippage_penalty, 2)} />
          </dl>
          {assessment.reasons.map((reason) => <p className="reason-line" key={reason}>{reason}</p>)}
          {assessment.warnings.map((warning) => <p className="warn-line" key={warning}>{warning}</p>)}
        </> : <p className="evidence-missing">Timing not assessed. <button className="link" onClick={onReassess}>Reassess timing</button></p>}
      </section>

      <section className="drawer-section">
        <h4>Recommendation provenance</h4>
        <dl className="detail-list">
          <Stat label="Forecast revision ID" value={ctx.forecast_revision_id ?? "—"} mono />
          <Stat label="Forecast vintage (latest / previous)" value={`${ctx.forecast_vintage_id ?? "—"} / ${ctx.previous_forecast_vintage_id ?? "—"}`} mono />
          <Stat label="Market snapshot ID" value={ctx.market_snapshot_id ?? "—"} mono />
          <Stat label="Optimisation run ID" value={ctx.optimisation_run_id ?? "—"} mono />
          <Stat label="Source / quality / run mode" value={`${ctx.source_mode} / ${ctx.quality} / ${ctx.run_mode}`} />
          <Stat label="Calculation allowed" value={ctx.calculation_allowed ? "YES" : "NO"} />
          <Stat label="Trustworthy for live trading" value={ctx.trustworthy_for_live_trading ? "YES" : "NO"} />
          <Stat label="Diagnostic only / not executable" value={`${decision.diagnostic_only ? "YES" : "no"} / ${decision.not_executable ? "YES" : "no"}`} />
        </dl>
      </section>

      <section className="drawer-section">
        <h4>Lifecycle</h4>
        <p className="lifecycle-status">Status <strong>{decision.status}</strong> <span className="readonly-note">(read-only)</span></p>
        <ol className="transition-list">{decision.transitions.map((t) => <li key={t.sequence}><span>{t.from_status ?? "∅"} → {t.to_status}</span><small>{t.actor} · {formatTimestampWithZone(t.occurred_at, "local time")}</small><p>{t.reason}</p></li>)}</ol>
      </section>
    </aside>
  </div>;
}

function Stat({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><dt>{label}</dt><dd className={mono ? "mono" : ""}>{value}</dd></div>;
}
