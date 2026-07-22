import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(resolve(root, "src", "CoordinatorPage.tsx"), "utf8");
const api = readFileSync(resolve(root, "src", "api.ts"), "utf8");
const route = readFileSync(resolve(root, "src", "main.tsx"), "utf8");

assert.match(route, /\/coordinator.*<CoordinatorPage/);
assert.match(page, /<ConnectionStatus/);
assert.match(page, /DIAGNOSTIC RECOMMENDATION/);
assert.match(page, /NOT EXECUTABLE/);
assert.match(page, /Not trustworthy for live trading unless all required inputs are LIVE\/FRESH\/VALID/);
assert.match(page, /Exposure \+ battery net export − signed market trade = residual/);
assert.match(page, /battery_net_export_mwh/);
assert.match(page, /residual_exposure_mwh/);
assert.match(page, /market_execution_cost_value/);
assert.match(page, /expected_imbalance_cost_value/);
assert.match(page, /optionality_lost_value/);
assert.match(page, /service_risk_penalty_value/);
assert.match(page, /formatUkMarketTime\(period\.delivery_start\).*UK time/);
assert.match(page, /simulateCoordinator\(settings\)/);
assert.match(page, /explicit_sample_market/);
assert.match(api, /\/coordinator/);
assert.match(api, /\/coordinator\/simulate/);

console.log("Coordinator route, warning labels, lineage values and simulation flow passed.");
