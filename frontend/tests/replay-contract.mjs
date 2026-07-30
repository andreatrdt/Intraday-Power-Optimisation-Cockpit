import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, name), "utf8");
const page = read("src/ReplayPage.tsx");
const main = read("src/main.tsx");
const nav = read("src/ProductNav.tsx");
const api = read("src/api.ts");
const types = read("src/types.ts");

// /replay route + nav (not the default page).
assert.match(main, /startsWith\("\/replay"\) \? <ReplayPage \/>/);
assert.match(nav, /href="\/replay">Replay<\/a>/);
assert.doesNotMatch(main, /pathname === "\/" \? <ReplayPage/); // never the default page

// Title + mode warning; SAMPLE is never labelled historical by default.
assert.match(page, /Point-in-Time Replay/);
assert.match(page, /Replay results are diagnostic\. SAMPLE replay is not historical performance\./);
assert.match(page, /run_mode: "SAMPLE_REPLAY"/);
assert.match(page, /SAMPLE_REPLAY/);
assert.match(page, /DIAGNOSTIC ONLY/);
assert.match(page, /NOT EXECUTABLE/);

// Summary metrics + sample size always visible.
for (const label of ["Sample size", "Eligible periods", "Material decisions", "Submitted", "Evaluated", "Skipped", "Hit rate", "Max drawdown", "Fill rate", "Avg regret vs perfect foresight"]) assert.match(page, new RegExp(label));
assert.match(page, /metrics\.sample_size/);
assert.match(page, /sample_size_note/);

// Cumulative-P&L chart distinguishing all four series with a hindsight label for PF.
assert.match(page, /Cumulative incremental P&amp;L vs no action/);
for (const series of ["cumulative_trader_gbp", "cumulative_model_gbp", "cumulative_no_action_gbp", "cumulative_perfect_foresight_gbp"]) assert.match(page, new RegExp(series));
assert.match(page, /Perfect foresight · HINDSIGHT \(not attainable\)/);
assert.match(page, /HINDSIGHT UPPER BOUND — NOT ATTAINABLE/);

// Outcome distribution + segmentation + episode table.
assert.match(page, /Outcome distribution/);
assert.match(page, /className="episode-table"/);
for (const col of ["Timing", "Policy", "Execution", "Incr. P&amp;L", "Regret vs PF", "Warnings"]) assert.match(page, new RegExp(col));
assert.match(page, /Segmentation|Breakdown/);
assert.match(page, /metrics\.segments/);

// Skipped-data state (episode table shows skip reasons) + integrity panel with look-ahead.
assert.match(page, /skip_reason/);
assert.match(page, /className="skip-reason"/);
assert.match(page, /Point-in-time integrity/);
assert.match(page, /lookahead_violation_count/);
assert.match(page, /Zero look-ahead violations/);

// Empty state + backend error state.
assert.match(page, /No replay yet\./);
assert.match(page, /error-banner/);
assert.match(page, /instanceof ApiError/);

// Episode drawer links to existing decision evidence.
assert.match(page, /href=\{`\/decisions#\$\{decisionId\}`\}/);

// API client + typed contracts (no broad any).
for (const fn of ["createReplayRun", "loadReplayRuns", "loadReplayEpisodes", "loadReplayMetrics", "loadReplayCumulativePnl", "loadReplayDatasets"]) assert.match(api, new RegExp(`export (async )?function ${fn}`));
assert.match(api, /\/replay-runs/);
assert.match(api, /\/replay-runs\/\$\{replayRunId\}\/metrics/);
assert.match(api, /\/replay-runs\/\$\{replayRunId\}\/cumulative-pnl/);
for (const iface of ["ReplayRun", "ReplayEpisodeResult", "ReplayMetrics", "SegmentMetric", "CumulativePnlPoint", "ReplayCreateRequest", "ReplayCreateResponse", "IntegrityReport", "LookAheadViolationRecord", "ReplayErrorDetail"]) assert.match(types, new RegExp(`export interface ${iface}`));
for (const t of ["ReplayMode", "TraderPolicy", "LifecyclePath", "IntegrityStatus"]) assert.match(types, new RegExp(`export type ${t} `));
assert.doesNotMatch(page, /: any\b/);
assert.doesNotMatch(page, /as any\b/);

console.log("Replay page route, warnings, summary, cumulative chart, segmentation, episodes, integrity, API and types passed.");
