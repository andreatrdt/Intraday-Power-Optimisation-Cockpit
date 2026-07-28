import { useState } from "react";
import { ApiError, acceptDecision, delayDecision, modifyDecision, rejectDecision, reopenDecision } from "./api";
import { formatTimestampWithZone } from "./time";
import type { ForecastRevision, HedgeTimingAssessment, TradeDecision } from "./types";

type Mode = null | "accept" | "modify" | "reject" | "delay" | "reopen";

// Deliberate wording: these record a trader decision only. Never "execute",
// "trade", "send order" or "submit to market".
const NO_ORDER = "This records the trader decision only. No order will be submitted.";

export function DecisionLifecycleControls({ decision, revision, assessment, onReload }: {
  decision: TradeDecision;
  revision: ForecastRevision | undefined;
  assessment: HedgeTimingAssessment | undefined;
  onReload: () => Promise<void> | void;
}) {
  const [mode, setMode] = useState<Mode>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [rationale, setRationale] = useState("");
  const [buy, setBuy] = useState(String(decision.recommendation.buy_mwh));
  const [sell, setSell] = useState(String(decision.recommendation.sell_mwh));
  const [limit, setLimit] = useState(decision.recommendation.limit_price !== null ? String(decision.recommendation.limit_price) : "");
  const [delayedUntil, setDelayedUntil] = useState("");

  const status = decision.status;
  const lastSequence = decision.transitions.length ? decision.transitions[decision.transitions.length - 1].sequence : 0;
  const concurrency = { expected_status: status, expected_sequence: lastSequence };
  const gate = decision.context.gate_closure_at;

  const run = async (action: () => Promise<TradeDecision>, message: string) => {
    setBusy(true); setError(null); setConflict(null); setSuccess(null);
    try {
      const updated = await action();
      setMode(null);
      setSuccess(`${message} — status ${updated.status}.`);
      await onReload();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict("This decision changed since you opened it (stale). Reload to see the current status before acting.");
      } else {
        setError(cause instanceof Error ? cause.message : "Action failed.");
      }
    } finally {
      setBusy(false);
    }
  };

  const numBuy = Number(buy);
  const numSell = Number(sell);
  const bothPositive = numBuy > 0 && numSell > 0;
  const negative = numBuy < 0 || numSell < 0;
  const changed = numBuy !== decision.recommendation.buy_mwh || numSell !== decision.recommendation.sell_mwh
    || (limit !== "" && Number(limit) !== (decision.recommendation.limit_price ?? NaN));
  const modifyInvalid = bothPositive || negative || !rationale.trim() || !changed || Number.isNaN(numBuy) || Number.isNaN(numSell);

  const delayDate = delayedUntil ? new Date(delayedUntil) : null;
  const now = new Date();
  const gateDate = gate ? new Date(gate) : null;
  const delayInvalid = !delayDate || Number.isNaN(delayDate.getTime()) || delayDate <= now
    || (gateDate !== null && delayDate >= gateDate) || !rationale.trim();

  const banner = () => <>
    {conflict && <div className="lifecycle-conflict"><strong>Stale decision (409)</strong><span>{conflict}</span><button onClick={() => void onReload()}>Reload</button></div>}
    {error && <div className="lifecycle-error">{error}</div>}
    {success && <div className="lifecycle-success">{success}</div>}
  </>;

  if (status !== "PROPOSED" && status !== "DELAYED") {
    return <section className="lifecycle-controls readonly"><h4>Trader decision</h4><p>No trader actions are available for status <strong>{status}</strong> (read-only).</p>{banner()}</section>;
  }

  return <section className="lifecycle-controls">
    <h4>Trader decision</h4>
    {banner()}
    {mode === null && <div className="lifecycle-buttons">
      {status === "PROPOSED" && <>
        <button onClick={() => { setMode("accept"); setSuccess(null); }}>Accept recommendation</button>
        <button onClick={() => { setMode("modify"); setSuccess(null); }}>Modify</button>
        <button onClick={() => { setMode("reject"); setSuccess(null); }}>Reject</button>
        <button onClick={() => { setMode("delay"); setSuccess(null); }}>Delay</button>
      </>}
      {status === "DELAYED" && <>
        <button onClick={() => { setMode("reopen"); setSuccess(null); }}>Reopen</button>
        <button onClick={() => { setMode("reject"); setSuccess(null); }}>Reject</button>
      </>}
    </div>}

    {mode === "accept" && <form className="lifecycle-form" onSubmit={(e) => { e.preventDefault(); void run(() => acceptDecision(decision.decision_id, { ...concurrency, trader_rationale: rationale || null }), "Acceptance recorded"); }}>
      <p>Recommended: <strong>{decision.recommendation.action}</strong> · {Math.abs(decision.recommendation.buy_mwh - decision.recommendation.sell_mwh).toFixed(1)} MWh{decision.recommendation.limit_price !== null ? ` · limit £${decision.recommendation.limit_price}` : " · no limit price"}</p>
      <label>Rationale (optional)<textarea value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      <p className="no-order-note">{NO_ORDER}</p>
      <div className="form-actions"><button type="submit" disabled={busy}>Record acceptance</button><button type="button" onClick={() => setMode(null)}>Cancel</button></div>
    </form>}

    {mode === "modify" && <form className="lifecycle-form" onSubmit={(e) => { e.preventDefault(); void run(() => modifyDecision(decision.decision_id, { ...concurrency, trader_buy_mwh: numBuy, trader_sell_mwh: numSell, trader_limit_price: limit === "" ? null : Number(limit), trader_rationale: rationale }), "Modification recorded"); }}>
      <div className="modify-compare">
        <div><span>Model recommendation</span><strong>buy {decision.recommendation.buy_mwh} · sell {decision.recommendation.sell_mwh}{decision.recommendation.limit_price !== null ? ` · £${decision.recommendation.limit_price}` : ""}</strong></div>
        <div className={changed ? "changed" : ""}><span>Trader instruction</span><strong>buy {numBuy} · sell {numSell}{limit !== "" ? ` · £${limit}` : ""}</strong></div>
      </div>
      <div className="modify-fields">
        <label>Buy MWh<input type="number" min="0" value={buy} onChange={(e) => setBuy(e.target.value)} /></label>
        <label>Sell MWh<input type="number" min="0" value={sell} onChange={(e) => setSell(e.target.value)} /></label>
        <label>Limit £/MWh<input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} /></label>
      </div>
      <label>Rationale (required)<textarea value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      {bothPositive && <p className="field-error">Buy and sell cannot both be positive.</p>}
      {!changed && <p className="field-error">Instruction must differ from the recommendation.</p>}
      <p className="no-order-note">{NO_ORDER}</p>
      <div className="form-actions"><button type="submit" disabled={busy || modifyInvalid}>Record modification</button><button type="button" onClick={() => setMode(null)}>Cancel</button></div>
    </form>}

    {mode === "reject" && <form className="lifecycle-form" onSubmit={(e) => { e.preventDefault(); void run(() => rejectDecision(decision.decision_id, { ...concurrency, trader_rationale: rationale }), "Rejection recorded"); }}>
      <p>Rejecting: ΔP50 revision <strong>{revision ? `${revision.comparison.delta_p50_mwh >= 0 ? "+" : ""}${revision.comparison.delta_p50_mwh.toFixed(1)} MWh` : "—"}</strong> · timing verdict <strong>{assessment ? assessment.verdict.replaceAll("_", " ") : "not assessed"}</strong>.</p>
      <label>Rationale (required)<textarea value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      <p className="no-order-note">{NO_ORDER}</p>
      <div className="form-actions"><button type="submit" disabled={busy || !rationale.trim()}>Record rejection</button><button type="button" onClick={() => setMode(null)}>Cancel</button></div>
    </form>}

    {mode === "delay" && <form className="lifecycle-form" onSubmit={(e) => { e.preventDefault(); void run(() => delayDecision(decision.decision_id, { ...concurrency, delayed_until: new Date(delayedUntil).toISOString(), trader_rationale: rationale }), "Delay recorded"); }}>
      <dl className="delay-context">
        <div><dt>Current time</dt><dd>{formatTimestampWithZone(now.toISOString(), "local time")}</dd></div>
        <div><dt>Gate Closure</dt><dd>{gate ? formatTimestampWithZone(gate, "UK time") : "unknown"}</dd></div>
        <div><dt>Maximum allowable delay</dt><dd>{gate ? formatTimestampWithZone(gate, "UK time") : "before Gate Closure"}</dd></div>
      </dl>
      <label>Delay until<input type="datetime-local" value={delayedUntil} onChange={(e) => setDelayedUntil(e.target.value)} /></label>
      <label>Rationale (required)<textarea value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      {gateDate !== null && delayDate !== null && delayDate >= gateDate && <p className="field-error">Delay must be before Gate Closure.</p>}
      {delayDate !== null && delayDate <= now && <p className="field-error">Delay must be later than the current time.</p>}
      <p className="no-order-note">{NO_ORDER}</p>
      <div className="form-actions"><button type="submit" disabled={busy || delayInvalid}>Delay decision</button><button type="button" onClick={() => setMode(null)}>Cancel</button></div>
    </form>}

    {mode === "reopen" && <form className="lifecycle-form" onSubmit={(e) => { e.preventDefault(); void run(() => reopenDecision(decision.decision_id, { ...concurrency, trader_rationale: rationale || null }), "Reopened"); }}>
      <p>Reopen this delayed decision back to <strong>PROPOSED</strong>.</p>
      <label>Rationale (optional)<textarea value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      <p className="no-order-note">{NO_ORDER}</p>
      <div className="form-actions"><button type="submit" disabled={busy}>Reopen decision</button><button type="button" onClick={() => setMode(null)}>Cancel</button></div>
    </form>}
  </section>;
}
