import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, name), "utf8");
const panel = read("src/DecisionOutcomePanel.tsx");
const page = read("src/DecisionQueuePage.tsx");
const api = read("src/api.ts");
const types = read("src/types.ts");

// Outcome section appears only for delivered/settled/evaluated (completed) decisions.
assert.match(panel, /const COMPLETED = new Set\(\["DELIVERED", "SETTLED", "EVALUATED"\]\)/);
assert.match(panel, /if \(!completed\) return null/);
assert.match(panel, /Outcome &amp; Evaluation/);
assert.match(page, /<DecisionOutcomePanel decision=\{decision\}/);

// Delivery: realised generation, final contracted position, realised imbalance + LONG/SHORT/FLAT.
for (const label of ["Realised generation", "Final contracted position", "Realised imbalance", "Executed buy / sell"]) assert.match(panel, new RegExp(label));
assert.match(panel, /imbalance-\$\{direction\.toLowerCase\(\)\}/);
for (const dir of ["LONG", "SHORT", "FLAT"]) assert.match(types, new RegExp(`"${dir}"`));

// Settlement: cash-flow lines + incremental P&L vs no action, signed cash formatting.
for (const label of ["Execution cash flow", "Execution fees", "Imbalance cash flow", "Total realised cash flow", "Incremental P&amp;L vs no action"]) assert.match(panel, new RegExp(label));
assert.match(panel, /v < 0 \? "−" : "\+"/); // cash flows always show a sign
assert.match(panel, /Cash received positive, cash paid negative/);

// Attribution: four effects + reconciliation status.
for (const label of ["Execution-price effect", "Fees effect", "Imbalance-reduction effect", "Residual effect", "Total incremental P&amp;L"]) assert.match(panel, new RegExp(label));
assert.match(panel, /Reconciled|Reconciliation error/);
assert.match(panel, /reconciliation_error_gbp/);

// Benchmarks: compact comparison of all four, hedge / total cash flow / incremental / regret.
for (const label of ["No action", "Model recommendation", "Trader instruction", "Perfect foresight"]) assert.match(panel, new RegExp(label));
for (const col of ["Benchmark", "Hedge", "Total cash flow", "Incremental vs no action", "Regret"]) assert.match(panel, new RegExp(col));
assert.match(panel, /incremental_pnl_vs_no_action_gbp - realised/); // regret = benchmark − realised
// Perfect foresight is clearly marked as an unattainable hindsight upper bound.
assert.match(panel, /HINDSIGHT UPPER BOUND — NOT ATTAINABLE/);

// Decision quality: label + note + regrets + warnings, with a cautious caption.
assert.match(panel, /quality-\$\{evaluation\.decision_quality_label\.toLowerCase\(\)\}/);
for (const label of ["Regret vs no action", "Regret vs model recommendation", "Regret vs perfect foresight"]) assert.match(panel, new RegExp(label));
assert.match(panel, /not statistical evidence of strategy quality/);

// Missing evaluation handled safely (settled but not evaluated; no delivery yet).
assert.match(panel, /Settled but not yet evaluated/);
assert.match(panel, /No delivery recorded for this decision yet/);

// No claim of live/historical performance (panel + process-completed action).
assert.match(panel, /not a claim of live or historical trading performance/);
assert.match(page, /does not represent live or historical trading performance/);
assert.match(page, /Process completed SAMPLE periods/);

// Active decisions remain primary; completed/evaluated kept in a separate, accessible section.
assert.match(page, /\.filter\(\(decision\) => !COMPLETED_STATES\.has\(decision\.status\)\)/);
assert.match(page, /COMPLETED &amp; EVALUATED/);
assert.match(page, /const completed = useMemo/);
// Queue filters for outcome (delivery/settlement/evaluation state) and decision quality.
assert.match(page, /Outcome<\/span>/);
assert.match(page, /Decision quality<\/span>/);
assert.match(page, /OUTCOME_FILTERS/);

// API client + typed contracts (no broad any).
for (const fn of ["deliverDecision", "settleDecision", "evaluateDecision", "processCompletedDecisions", "loadDecisionOutcome", "loadEvaluations"]) assert.match(api, new RegExp(`export (async )?function ${fn}`));
assert.match(api, /\/decisions\/\$\{decisionId\}\/deliver/);
assert.match(api, /\/decisions\/\$\{decisionId\}\/settle/);
assert.match(api, /\/decisions\/\$\{decisionId\}\/evaluate/);
assert.match(api, /\/decisions\/process-completed/);
for (const iface of ["DeliveryResult", "SettlementCalculation", "RealisedPnlAttribution", "SettlementBenchmarkResult", "DecisionEvaluationResult", "ProcessCompletedResult", "SettlementErrorDetail", "DecisionOutcomeBundle"]) assert.match(types, new RegExp(`export interface ${iface}`));
for (const t of ["ImbalanceDirection", "BenchmarkName", "DecisionQualityLabel", "ProcessSkipReason"]) assert.match(types, new RegExp(`export type ${t} `));
for (const src of [panel, page]) {
  assert.doesNotMatch(src, /: any\b/);
  assert.doesNotMatch(src, /as any\b/);
}

console.log("Settlement & evaluation outcome section, benchmarks, regret, quality, filters, API and types passed.");
