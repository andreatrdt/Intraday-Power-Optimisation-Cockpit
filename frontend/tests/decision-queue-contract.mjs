import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, name), "utf8");
const page = read("src/DecisionQueuePage.tsx");
const api = read("src/api.ts");
const types = read("src/types.ts");
const nav = read("src/ProductNav.tsx");
const route = read("src/main.tsx");
const styles = read("src/styles.css");

// Route + navigation, existing routes preserved.
assert.match(route, /startsWith\("\/decisions"\)\s*\?\s*<DecisionQueuePage/);
assert.match(route, /startsWith\("\/live"\)/);
assert.match(route, /startsWith\("\/optimisation"\)/);
assert.match(route, /startsWith\("\/diagnostics"\)/);
assert.match(nav, /"live" \| "optimisation" \| "decisions" \| "replay" \| "diagnostics"/);
assert.match(nav, /href="\/decisions">Decisions</);

// A. Header — title, run/source/quality, actions and trust badges.
assert.match(page, /Trader Decision Queue/);
assert.match(page, /Run mode/);
assert.match(page, /Refresh decisions/);
assert.match(page, /Reassess timing/);
assert.match(page, /Neither action submits a trade/);
for (const badge of ["SAMPLE", "DIAGNOSTIC ONLY", "NOT EXECUTABLE", "DEGRADED"]) assert.match(page, new RegExp(badge));

// B. Summary strip totals.
for (const label of ["Active decisions", "Critical", "High", "Hedge now", "Partial", "Wait", "No action", "Batches", "Unassessed"]) assert.match(page, new RegExp(label));

// C. Ranked queue — top cap and backend-mirrored ordering.
assert.match(page, /const TOP_CAP = 8/);
assert.match(page, /RANKED QUEUE · TOP/);
assert.match(page, /PRIORITY_RANK\[a\.assessment\.priority\] - PRIORITY_RANK\[b\.assessment\.priority\]/);
assert.match(page, /\.slice\(0, TOP_CAP\)/);
// Verdict + priority display labels.
for (const verdict of ["HEDGE NOW", "PARTIAL HEDGE", "WAIT", "NO ACTION"]) assert.match(page, new RegExp(verdict));
for (const priority of ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]) assert.match(page, new RegExp(priority));
// Card fields.
for (const field of ["min to gate", "P50 exposure", "P10/P90", "ΔP50 revision", "significance", "spread", "depth", "WAP"]) assert.match(page, new RegExp(field));

// D. Detail drawer sections + canonical sign convention + provenance + lifecycle.
for (const section of ["What changed", "Portfolio impact", "Timing assessment", "Recommendation provenance", "Lifecycle"]) assert.match(page, new RegExp(section));
assert.match(page, /Exposure = forecast generation − contracted position/);
assert.match(page, /Positive = LONG, negative = SHORT/);
assert.match(page, /Revision-significance component/);
assert.doesNotMatch(page, /forecast confidence/i);
for (const prov of ["Forecast revision ID", "Market snapshot ID", "Optimisation run ID", "Calculation allowed", "Trustworthy for live trading", "Diagnostic only"]) assert.match(page, new RegExp(prov));
assert.match(page, /transition-list/);
assert.match(page, /read-only/);

// Significance-unavailable handled without inventing a value.
assert.match(page, /significance_available \? assessment\.confidence_or_significance_component : null/);
assert.match(page, /significance n\/a/);

// F. Empty / degraded / partial-evidence states.
assert.match(page, /At least two complete forecast vintages are required/);
assert.match(page, /none crossed the materiality thresholds/);
assert.match(page, /Timing has not been assessed yet/);
assert.match(page, /Supporting evidence unavailable/);
assert.match(page, /Backend unavailable/);

// E. Batch summary — separate from the ranked queue, all periods accessible.
assert.match(page, /Full batch overview/);
assert.match(page, /Show all \$\{summary\.total_decisions\} periods/);
assert.match(page, /batch-all-periods/);

// G. Filters.
for (const filter of ["Priority", "Verdict", "Action", "Quality", "Batch", "SP from", "SP to"]) assert.match(page, new RegExp(`<span>${filter}</span>`));

// API client — reads only, no duplicated backend logic, uses existing request pattern.
for (const fn of ["loadDecisions", "loadForecastRevisions", "loadHedgeTimingAssessments", "loadDecisionBatches", "loadDecisionBatchSummaries", "refreshDecisions", "reassessTiming"]) assert.match(api, new RegExp(`export async function ${fn}`));
for (const endpoint of ["/decisions", "/forecast-revisions", "/hedge-timing-assessments", "/decision-batches", "/decision-batch-summaries", "/decisions/refresh", "/decisions/assess-timing"]) assert.match(api, new RegExp(endpoint.replaceAll("/", "\\/")));

// Type contracts (no broad any).
for (const iface of ["TradeDecision", "DecisionBatch", "ForecastRevision", "HedgeTimingAssessment", "DecisionBatchSummary", "DecisionRefreshResponse", "AssessTimingResponse", "PriorityComponents", "TimingMarketView"]) assert.match(types, new RegExp(`export interface ${iface}`));
for (const t of ["TimingVerdict", "TimingPriority", "DecisionStatus", "RunMode"]) assert.match(types, new RegExp(`export type ${t}`));
assert.doesNotMatch(page, /: any\b/);
assert.doesNotMatch(page, /as any\b/);

// Styling additions present.
assert.match(styles, /\.decision-card/);
assert.match(styles, /\.verdict-tag\.verdict-hedge_now/);

console.log("Decision queue route, header, ranked queue, drawer, batch summaries, states, API and types passed.");
