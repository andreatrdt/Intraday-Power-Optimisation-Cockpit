import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, name), "utf8");
const controls = read("src/DecisionLifecycleControls.tsx");
const page = read("src/DecisionQueuePage.tsx");
const api = read("src/api.ts");
const types = read("src/types.ts");

// Controls are status-derived: PROPOSED and DELAYED only.
assert.match(controls, /status !== "PROPOSED" && status !== "DELAYED"/);
assert.match(controls, /No trader actions are available for status/);
assert.match(controls, /status === "PROPOSED" &&/);
assert.match(controls, /status === "DELAYED" &&/);
for (const button of ["Accept recommendation", "Modify", "Reject", "Delay", "Reopen"]) assert.match(controls, new RegExp(button));

// Diagnostic wording only — never execution wording.
assert.match(controls, /This records the trader decision only\. No order will be submitted\./);
for (const label of ["Record acceptance", "Record modification", "Record rejection", "Delay decision", "Reopen decision"]) assert.match(controls, new RegExp(label));
for (const forbidden of [/\bExecute\b/, /Send order/, /Submit to market/, /\bTrade\b/]) assert.doesNotMatch(controls, forbidden);

// Modify validation: no both-positive, meaningful change required, rationale required.
assert.match(controls, /const bothPositive = numBuy > 0 && numSell > 0/);
assert.match(controls, /Buy and sell cannot both be positive/);
assert.match(controls, /Instruction must differ from the recommendation/);
assert.match(controls, /Model recommendation/);

// Reject requires rationale and shows the revision + timing verdict.
assert.match(controls, /disabled=\{busy \|\| !rationale\.trim\(\)\}/);
assert.match(controls, /timing verdict/);

// Delay: shows current time / Gate Closure / max delay and blocks beyond gate.
assert.match(controls, /Current time/);
assert.match(controls, /Gate Closure/);
assert.match(controls, /Maximum allowable delay/);
assert.match(controls, /delayDate >= gateDate/);
assert.match(controls, /Delay must be before Gate Closure/);

// Optimistic concurrency: expected_status + expected_sequence sent.
assert.match(controls, /expected_status: status, expected_sequence: lastSequence/);
// 409 handled distinctly with a reload action.
assert.match(controls, /cause instanceof ApiError && cause\.status === 409/);
assert.match(controls, /Stale decision \(409\)/);
assert.match(controls, /onReload/);

// Success reload + failure keeps the form (no reset on error).
assert.match(controls, /await onReload\(\)/);
assert.match(controls, /lifecycle-success/);

// Page wiring: status filter + status tag + controls in the drawer.
assert.match(page, /Status<\/span>/);
assert.match(page, /DECISION_STATUSES/);
assert.match(page, /ACTIVE_ONLY/);
assert.match(page, /status-tag status-\$\{decision\.status\.toLowerCase\(\)\}/);
assert.match(page, /<DecisionLifecycleControls decision=\{decision\}/);
// Rejected/delayed are not hidden by default (default filter is ALL).
assert.match(page, /status: "ALL"/);

// API client: typed mutation functions + ApiError.
assert.match(api, /export class ApiError extends Error/);
for (const fn of ["acceptDecision", "modifyDecision", "rejectDecision", "delayDecision", "reopenDecision"]) assert.match(api, new RegExp(`export function ${fn}`));
for (const endpoint of ["/accept", "/modify", "/reject", "/delay", "/reopen"]) assert.match(api, new RegExp(`\\/decisions\\/\\$\\{decisionId\\}${endpoint}`));

// Types: request contracts + conflict, no broad any.
for (const iface of ["AcceptDecisionRequest", "ModifyDecisionRequest", "RejectDecisionRequest", "DelayDecisionRequest", "ReopenDecisionRequest", "DecisionMutationResponse", "ConflictDetail", "LifecycleConcurrency"]) assert.match(types, new RegExp(`export interface ${iface}`));
assert.match(types, /gate_closure_at: string \| null/);
assert.match(types, /actor_id: string \| null/);
assert.doesNotMatch(controls, /: any\b/);
assert.doesNotMatch(controls, /as any\b/);

console.log("Decision lifecycle controls, wording, validation, concurrency, API and types passed.");
