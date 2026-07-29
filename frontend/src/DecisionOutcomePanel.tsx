import { useEffect, useState } from "react";
import { loadDecisionOutcome } from "./api";
import type {
  DecisionOutcomeBundle,
  DeliveryResult,
  DecisionEvaluationResult,
  ImbalanceDirection,
  SettlementBenchmarkResult,
  SettlementCalculation,
  TradeDecision,
} from "./types";

// Read-only outcome view for completed decisions. Every figure is SAMPLE and
// diagnostic; this is not a claim of live or historical trading performance.
const COMPLETED = new Set(["DELIVERED", "SETTLED", "EVALUATED"]);
const PERFECT_FORESIGHT_BANNER = "HINDSIGHT UPPER BOUND — NOT ATTAINABLE";

const n = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
// Cash flows / P&L: always show the sign so BUY-paid vs SELL-received is unambiguous.
const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v < 0 ? "−" : "+"}£${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;

const BENCHMARK_LABEL: Record<string, string> = {
  NO_ACTION: "No action",
  MODEL_RECOMMENDATION: "Model recommendation",
  TRADER_INSTRUCTION: "Trader instruction",
  PERFECT_FORESIGHT: "Perfect foresight",
};

function DirectionTag({ direction }: { direction: ImbalanceDirection }) {
  return <span className={`imbalance-tag imbalance-${direction.toLowerCase()}`}>{direction}</span>;
}

export function DecisionOutcomePanel({ decision }: { decision: TradeDecision; onReload?: () => Promise<void> | void }) {
  const [bundle, setBundle] = useState<DecisionOutcomeBundle | null>(null);
  const [loading, setLoading] = useState(false);
  const status = decision.status;
  const completed = COMPLETED.has(status);

  useEffect(() => {
    if (!completed) return;
    setLoading(true);
    void loadDecisionOutcome(decision.decision_id)
      .then(setBundle)
      .catch(() => setBundle(null))
      .finally(() => setLoading(false));
  }, [completed, decision.decision_id]);

  if (!completed) return null;

  return <section className="outcome-panel">
    <h4>Outcome &amp; Evaluation</h4>
    <p className="pill-row"><span className="pill pill-sample">SAMPLE</span><span className="pill pill-diagnostic">DIAGNOSTIC ONLY</span><span className="pill pill-nonexec">NOT EXECUTABLE</span></p>
    <p className="outcome-disclaimer">Simulated realised generation and settlement prices. This is not a claim of live or historical trading performance.</p>

    {loading && !bundle && <p className="evidence-missing">Loading outcome…</p>}
    {bundle && !bundle.delivery && <p className="evidence-missing">No delivery recorded for this decision yet.</p>}

    {bundle?.delivery && <DeliveryView delivery={bundle.delivery} />}
    {bundle?.settlement && <SettlementView settlement={bundle.settlement} />}
    {bundle?.evaluation
      ? <EvaluationView evaluation={bundle.evaluation} />
      : bundle?.settlement && <p className="evidence-missing">Settled but not yet evaluated.</p>}
  </section>;
}

function DeliveryView({ delivery }: { delivery: DeliveryResult }) {
  return <div className="outcome-block delivery-block">
    <h5>Delivery</h5>
    <p className="sign-note">Realised imbalance I = realised generation G − final contracted position Q. Positive = LONG generation, negative = SHORT.</p>
    <dl className="detail-list">
      <div><dt>Realised generation</dt><dd>{n(delivery.realised_generation_mwh, 1)} MWh</dd></div>
      <div><dt>Final contracted position Q</dt><dd>{n(delivery.final_contracted_position_mwh, 1)} MWh</dd></div>
      <div><dt>Realised imbalance</dt><dd>{n(delivery.realised_imbalance_mwh, 1)} MWh <DirectionTag direction={delivery.imbalance_direction} /></dd></div>
      <div><dt>Executed buy / sell</dt><dd>{n(delivery.executed_buy_mwh, 1)} / {n(delivery.executed_sell_mwh, 1)} MWh</dd></div>
    </dl>
  </div>;
}

function SettlementView({ settlement }: { settlement: SettlementCalculation }) {
  return <div className="outcome-block settlement-block">
    <h5>Settlement</h5>
    <p className="sign-note">Cash received positive, cash paid negative.</p>
    <dl className="detail-list">
      <div><dt>Execution cash flow</dt><dd className="cash">{money(settlement.execution_cashflow_gbp)}</dd></div>
      <div><dt>Execution fees</dt><dd className="cash">{money(-settlement.execution_fees_gbp)}</dd></div>
      <div><dt>Imbalance cash flow</dt><dd className="cash">{money(settlement.imbalance_cashflow_gbp)}</dd></div>
      <div><dt>Total realised cash flow</dt><dd className="cash total">{money(settlement.total_realised_cashflow_gbp)}</dd></div>
      <div><dt>Incremental P&amp;L vs no action</dt><dd className="cash total">{money(settlement.realised_pnl_gbp)}</dd></div>
    </dl>
    {settlement.warnings.map((w) => <p className="warn-line" key={w}>{w}</p>)}
  </div>;
}

function EvaluationView({ evaluation }: { evaluation: DecisionEvaluationResult }) {
  const attr = evaluation.pnl_attribution;
  const reconciled = Math.abs(attr.reconciliation_error_gbp) <= 1e-6;
  return <>
    <div className="outcome-block attribution-block">
      <h5>Attribution</h5>
      <p className="sign-note">Effects reconcile to the incremental P&amp;L versus no action.</p>
      <dl className="detail-list">
        <div><dt>Execution-price effect</dt><dd className="cash">{money(attr.execution_price_effect_gbp)}</dd></div>
        <div><dt>Fees effect</dt><dd className="cash">{money(attr.execution_fees_effect_gbp)}</dd></div>
        <div><dt>Imbalance-reduction effect</dt><dd className="cash">{money(attr.imbalance_reduction_effect_gbp)}</dd></div>
        <div><dt>Residual effect</dt><dd className="cash">{money(attr.imbalance_residual_effect_gbp)}</dd></div>
        <div><dt>Total incremental P&amp;L</dt><dd className="cash total">{money(attr.total_incremental_pnl_gbp)}</dd></div>
      </dl>
      <p className={reconciled ? "reconcile-ok" : "warn-line"}>
        {reconciled ? "Reconciled ✓" : `Reconciliation error ${money(attr.reconciliation_error_gbp)}`} (error {n(attr.reconciliation_error_gbp, 6)})
      </p>
    </div>

    <div className="outcome-block benchmark-block">
      <h5>Benchmarks</h5>
      <table className="benchmark-table">
        <thead><tr><th>Benchmark</th><th>Hedge</th><th>Total cash flow</th><th>Incremental vs no action</th><th>Regret</th></tr></thead>
        <tbody>
          {evaluation.benchmark_results.map((b) => <BenchmarkRow key={b.benchmark_name} benchmark={b} realised={evaluation.realised_outcome.realised_pnl_gbp} />)}
        </tbody>
      </table>
      <p className="hindsight-banner">Perfect foresight: <strong>{PERFECT_FORESIGHT_BANNER}</strong></p>
    </div>

    <div className="outcome-block quality-block">
      <h5>Decision quality</h5>
      <p className="quality-caption">A single SAMPLE outcome is not statistical evidence of strategy quality.</p>
      <span className={`quality-label quality-${evaluation.decision_quality_label.toLowerCase()}`}>{evaluation.decision_quality_label.replaceAll("_", " ")}</span>
      <p className="quality-note">{evaluation.decision_quality_note}</p>
      <dl className="detail-list">
        <div><dt>Regret vs no action</dt><dd className="cash">{money(evaluation.regret_vs_no_action_gbp)}</dd></div>
        <div><dt>Regret vs model recommendation</dt><dd className="cash">{evaluation.regret_vs_model_recommendation_gbp === null ? "—" : money(evaluation.regret_vs_model_recommendation_gbp)}</dd></div>
        <div><dt>Regret vs perfect foresight</dt><dd className="cash">{money(evaluation.regret_vs_perfect_foresight_gbp)}</dd></div>
      </dl>
      <p className="regret-note">Regret = benchmark incremental P&amp;L − realised incremental P&amp;L. Positive means the benchmark did better.</p>
      {evaluation.warnings.map((w) => <p className="warn-line" key={w}>{w}</p>)}
    </div>
  </>;
}

function BenchmarkRow({ benchmark, realised }: { benchmark: SettlementBenchmarkResult; realised: number }) {
  const hedge = benchmark.hedge_buy_mwh > 0
    ? `BUY ${n(benchmark.hedge_buy_mwh, 1)}`
    : benchmark.hedge_sell_mwh > 0 ? `SELL ${n(benchmark.hedge_sell_mwh, 1)}` : "none";
  const regret = benchmark.incremental_pnl_vs_no_action_gbp - realised;
  return <tr className={benchmark.hindsight_only ? "benchmark-hindsight" : ""}>
    <td>{BENCHMARK_LABEL[benchmark.benchmark_name] ?? benchmark.benchmark_name}{benchmark.hindsight_only ? <span className="not-attainable"> · not attainable</span> : ""}</td>
    <td>{hedge}</td>
    <td className="cash">{money(benchmark.total_cashflow_gbp)}</td>
    <td className="cash">{money(benchmark.incremental_pnl_vs_no_action_gbp)}</td>
    <td className="cash">{benchmark.benchmark_name === "TRADER_INSTRUCTION" ? "—" : money(regret)}</td>
  </tr>;
}
