import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(resolve(root, "src", "OptionalityPage.tsx"), "utf8");
const api = readFileSync(resolve(root, "src", "api.ts"), "utf8");
const route = readFileSync(resolve(root, "src", "main.tsx"), "utf8");

assert.match(route, /\/optionality.*<OptionalityPage/);
assert.match(page, /<ConnectionStatus/);
assert.match(page, /Live-trading trust/);
assert.match(page, /BM value is optional and not guaranteed/);
assert.match(page, /COMMITTED · MUST REMAIN DELIVERABLE|item\.obligation_status/);
assert.match(page, /formatUkMarketTime\(period\.delivery_start\).*UK time/);
assert.match(page, /simulateOptionalityPath\(customActions\)/);
assert.match(page, /commitment_coverage_value/);
assert.match(page, /optionality_lost_value/);
assert.match(page, /impact #\{period\.risk_rank\}/);
assert.match(page, /lineage\.semantic_kind/);
assert.match(api, /\/optionality/);
assert.match(api, /\/optionality\/simulate/);

console.log("Optionality route, diagnostic labels, lineage wiring and custom-path flow passed.");
