import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, name), "utf8");
const panel = read("src/DecisionExecutionPanel.tsx");
const page = read("src/DecisionQueuePage.tsx");
const api = read("src/api.ts");
const types = read("src/types.ts");

// Submit control only for ACCEPTED/MODIFIED; outcome for executed states.
assert.match(panel, /const SUBMITTABLE = new Set\(\["ACCEPTED", "MODIFIED"\]\)/);
assert.match(panel, /const EXECUTED = new Set\(\["SUBMITTED", "PARTIALLY_FILLED", "FILLED", "EXPIRED"\]\)/);
assert.match(panel, /if \(!SUBMITTABLE\.has\(status\) && !executed\) return null/);
assert.match(panel, /Submit to simulator/);

// Simulator-only wording; never live-venue wording.
assert.match(panel, /This sends the trader instruction to the internal execution simulator only\. No real order will be placed\./);
for (const forbidden of [/Send order/, /Submit to market/, /submit to venue/i, /\bExecute\b/]) assert.doesNotMatch(panel, forbidden);
assert.match(panel, /SIMULATED/);
assert.match(panel, /NOT EXECUTABLE/);

// Mode selection with all three modes, assumption-driven labels.
for (const mode of ["IDEAL", "REALISTIC", "STRESS"]) assert.match(panel, new RegExp(`value="${mode}"`));
assert.match(panel, /assumption-driven/i);
assert.match(panel, /Not calibrated to a real exchange/);

// Submission panel shows instruction/market/expected sequence + idempotency key.
assert.match(panel, /Trader instruction/);
assert.match(panel, /Best bid \/ ask/);
assert.match(panel, /Visible depth/);
assert.match(panel, /Expected sequence/);
assert.match(panel, /crypto\.randomUUID/);
assert.match(panel, /idempotency_key: idempotencyKey/);
assert.match(panel, /expected_sequence: lastSequence/);

// Outcome display: requested/filled/unfilled, prices, slippage, fees, fill breakdown.
for (const field of ["Requested / filled / unfilled", "Average fill price", "Best price before execution", "Total slippage", "Total fees", "Total execution cost", "Fill-level breakdown"]) assert.match(panel, new RegExp(field));
assert.match(panel, /outcome\.fills\.map/);
assert.match(panel, /assumptions_used\.map/);

// 409 handled distinctly, incl. idempotency conflict; keeps mode after failure (no reset in catch).
assert.match(panel, /cause instanceof ApiError && cause\.status === 409/);
assert.match(panel, /idempotency_conflict/);
assert.doesNotMatch(panel, /setMode\("REALISTIC"\);[\s\S]*catch/);

// Recommendation / trader instruction / outcome kept visually separate (three sections in the drawer).
assert.match(page, /<DecisionLifecycleControls decision=\{decision\}/);
assert.match(page, /<DecisionExecutionPanel decision=\{decision\}/);

// API client + types (no any).
assert.match(api, /export function submitSimulated/);
assert.match(api, /export async function loadDecisionExecution/);
assert.match(api, /\/decisions\/\$\{decisionId\}\/submit-simulated/);
assert.match(api, /\/decisions\/\$\{decisionId\}\/execution/);
for (const iface of ["SimulatedOrder", "SimulatedFill", "ExecutionOutcome", "SubmitSimulatedRequest", "SubmitSimulatedResponse", "DecisionExecutionResponse"]) assert.match(types, new RegExp(`export interface ${iface}`));
assert.match(types, /export type ExecutionMode = "IDEAL" \| "REALISTIC" \| "STRESS"/);
assert.doesNotMatch(panel, /: any\b/);
assert.doesNotMatch(panel, /as any\b/);

console.log("Execution simulation panel, wording, modes, outcome, API and types passed.");
