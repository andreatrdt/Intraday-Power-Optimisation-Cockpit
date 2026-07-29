import { useEffect, useState } from "react";
import { ApiError, loadDecisionExecution, submitSimulated } from "./api";
import type { ExecutionMode, ExecutionOutcome, HedgeTimingAssessment, TradeDecision } from "./types";

// Deliberate wording: submits to the INTERNAL SIMULATOR only. Never "execute",
// "trade", "send order" or "submit to market/venue".
const SIM_WARNING = "This sends the trader instruction to the internal execution simulator only. No real order will be placed.";
const MODE_NOTES: Record<ExecutionMode, string> = {
  IDEAL: "Benchmark: full fill at the best visible price, zero slippage/latency (fees apply).",
  REALISTIC: "Assumption-driven SAMPLE: walks visible depth with a 10% haircut and 250 ms latency. Not calibrated to a real exchange.",
  STRESS: "Assumption-driven SAMPLE: 40% depth haircut, 1500 ms latency and +£8/MWh adverse price shift. Not calibrated to a real exchange.",
};
const SUBMITTABLE = new Set(["ACCEPTED", "MODIFIED"]);
const EXECUTED = new Set(["SUBMITTED", "PARTIALLY_FILLED", "FILLED", "EXPIRED"]);
const n = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));

export function DecisionExecutionPanel({ decision, assessment, onReload }: {
  decision: TradeDecision;
  assessment: HedgeTimingAssessment | undefined;
  onReload: () => Promise<void> | void;
}) {
  const [mode, setMode] = useState<ExecutionMode>("REALISTIC");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ExecutionOutcome | null>(null);
  const [idempotencyKey] = useState(() => (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now())));

  const status = decision.status;
  const executed = EXECUTED.has(status);

  useEffect(() => {
    if (executed) void loadDecisionExecution(decision.decision_id).then(setOutcome).catch(() => setOutcome(null));
  }, [executed, decision.decision_id]);

  if (!SUBMITTABLE.has(status) && !executed) return null;

  const instr = decision.trader_instruction;
  const side = (instr?.buy_mwh ?? 0) > (instr?.sell_mwh ?? 0) ? "BUY" : (instr?.sell_mwh ?? 0) > 0 ? "SELL" : "NONE";
  const volume = side === "BUY" ? (instr?.buy_mwh ?? 0) : (instr?.sell_mwh ?? 0);
  const market = assessment?.market;
  const lastSequence = decision.transitions.length ? decision.transitions[decision.transitions.length - 1].sequence : 0;

  const submit = async () => {
    setBusy(true); setError(null); setConflict(null);
    try {
      const response = await submitSimulated(decision.decision_id, {
        execution_mode: mode,
        expected_status: status,
        expected_sequence: lastSequence,
        idempotency_key: idempotencyKey,
      });
      setOutcome(response.outcome);
      await onReload();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        const body = cause.body as { detail?: { error?: string } } | undefined;
        setConflict(body?.detail?.error === "idempotency_conflict"
          ? "Duplicate submission with a different payload (idempotency conflict). Reload before retrying."
          : "This decision changed since you opened it (stale). Reload to see the current status.");
      } else {
        setError(cause instanceof Error ? cause.message : "Submission failed.");
      }
    } finally {
      setBusy(false);
    }
  };

  return <section className="execution-panel">
    <h4>Simulated execution</h4>
    <p className="pill-row"><span className="pill pill-sample">SIMULATED</span><span className="pill pill-nonexec">NOT EXECUTABLE</span></p>
    {conflict && <div className="lifecycle-conflict"><strong>Conflict (409)</strong><span>{conflict}</span><button onClick={() => void onReload()}>Reload</button></div>}
    {error && <div className="lifecycle-error">{error}</div>}

    {SUBMITTABLE.has(status) && <div className="submit-form">
      <dl className="detail-list">
        <div><dt>Trader instruction</dt><dd>{side} {n(volume, 1)} MWh{instr?.limit_price != null ? ` · limit £${n(instr.limit_price)}` : " · no limit"}</dd></div>
        <div><dt>Settlement period</dt><dd>SP{decision.context.settlement_period}</dd></div>
        <div><dt>Minutes to Gate Closure</dt><dd>{decision.context.minutes_to_gate_closure === null ? "—" : n(decision.context.minutes_to_gate_closure, 0)}</dd></div>
        <div><dt>Best bid / ask</dt><dd>{market ? `£${n(market.best_bid_gbp_per_mwh)} / £${n(market.best_ask_gbp_per_mwh)}` : "not loaded"}</dd></div>
        <div><dt>Visible depth (bid/ask)</dt><dd>{market ? `${n(market.bid_depth_mwh, 1)} / ${n(market.ask_depth_mwh, 1)} MWh` : "—"}</dd></div>
        <div><dt>Expected sequence</dt><dd>{lastSequence}</dd></div>
      </dl>
      <label className="mode-select"><span>Execution mode</span>
        <select value={mode} onChange={(e) => setMode(e.target.value as ExecutionMode)}>
          <option value="IDEAL">IDEAL (benchmark)</option>
          <option value="REALISTIC">REALISTIC (assumption-driven)</option>
          <option value="STRESS">STRESS (assumption-driven)</option>
        </select>
      </label>
      <p className="mode-note">{MODE_NOTES[mode]}</p>
      <p className="no-order-note">{SIM_WARNING}</p>
      <button className="submit-sim" disabled={busy} onClick={() => void submit()}>{busy ? "Submitting…" : "Submit to simulator"}</button>
    </div>}

    {executed && (outcome ? <ExecutionOutcomeView outcome={outcome} /> : <p className="evidence-missing">Loading simulated execution outcome…</p>)}
  </section>;
}

function ExecutionOutcomeView({ outcome }: { outcome: ExecutionOutcome }) {
  return <div className="execution-outcome">
    <div className="outcome-head">
      <span className={`exec-status exec-${outcome.execution_status.toLowerCase()}`}>{outcome.execution_status.replaceAll("_", " ")}</span>
      <span className="exec-mode">{outcome.execution_mode}</span>
      <span className="sim-version">{outcome.simulator_version}</span>
    </div>
    <dl className="detail-list">
      <div><dt>Requested / filled / unfilled</dt><dd>{n(outcome.order.requested_volume_mwh, 1)} / {n(outcome.total_filled_volume_mwh, 1)} / {n(outcome.unfilled_volume_mwh, 1)} MWh</dd></div>
      <div><dt>Average fill price</dt><dd>{outcome.average_fill_price_gbp_per_mwh != null ? `£${n(outcome.average_fill_price_gbp_per_mwh)}` : "—"}</dd></div>
      <div><dt>Best price before execution</dt><dd>{outcome.best_price_before_execution_gbp_per_mwh != null ? `£${n(outcome.best_price_before_execution_gbp_per_mwh)}` : "—"}</dd></div>
      <div><dt>Total slippage</dt><dd>£{n(outcome.total_slippage_gbp)}</dd></div>
      <div><dt>Total fees</dt><dd>£{n(outcome.total_fees_gbp)}</dd></div>
      <div><dt>Total execution cost</dt><dd>£{n(outcome.total_execution_cost_gbp)}</dd></div>
    </dl>
    {outcome.fills.length > 0 && <div className="fill-breakdown">
      <span>Fill-level breakdown</span>
      <table><thead><tr><th>Level</th><th>Volume</th><th>Price</th><th>Fee</th><th>Slippage</th></tr></thead>
        <tbody>{outcome.fills.map((fill) => <tr key={fill.fill_id}><td>{fill.order_book_level ?? "—"}</td><td>{n(fill.filled_volume_mwh, 1)}</td><td>£{n(fill.fill_price_gbp_per_mwh)}</td><td>£{n(fill.fee_gbp)}</td><td>£{n(fill.slippage_gbp_per_mwh)}</td></tr>)}</tbody>
      </table>
    </div>}
    {outcome.assumptions_used.map((a) => <p className="assumption-line" key={a}>{a}</p>)}
    {outcome.warnings.map((w) => <p className="warn-line" key={w}>{w}</p>)}
  </div>;
}
