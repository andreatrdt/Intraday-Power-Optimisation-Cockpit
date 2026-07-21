import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(resolve(root, "src", "BatteryPathPage.tsx"), "utf8");
const api = readFileSync(resolve(root, "src", "api.ts"), "utf8");

assert.match(page, /Live-control trust/);
assert.match(page, /Reserve duration U \/ D/);
assert.match(page, /upward_energy_duration_value/);
assert.match(page, /downward_energy_duration_value/);
assert.match(page, /formatUkMarketTime\(period\.delivery_start\).*UK time/);
assert.match(page, /simulateBatteryPath\(customActions\)/);
assert.match(page, /customRequestVersion/);
assert.match(api, /\/battery-paths\/comparison/);
assert.match(api, /\/battery-paths\/simulate/);

console.log("Battery Path UI contract and sequential custom-path wiring passed.");
