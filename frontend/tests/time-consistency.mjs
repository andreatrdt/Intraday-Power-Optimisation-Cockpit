import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pages = ["App.tsx", "ForecastPositionPage.tsx", "MarketLiquidityPage.tsx", "BatteryFlexibilityPage.tsx", "BatteryPathPage.tsx", "OptionalityPage.tsx", "CoordinatorPage.tsx", "LiveStatePage.tsx", "OptimisationPage.tsx", "DiagnosticsPage.tsx"];
for (const page of pages) {
  const source = readFileSync(resolve(root, "src", page), "utf8");
  assert.match(source, /<ConnectionStatus\b/, `${page} must use the shared ConnectionStatus`);
  assert.doesNotMatch(source, /API connected|className="connection"/, `${page} must not implement its own connection header`);
}

const component = readFileSync(resolve(root, "src", "ConnectionStatus.tsx"), "utf8");
assert.match(component, /Backend connected/);
assert.match(component, /Last poll:.*local time/);
assert.match(component, /formatLocalTime\(lastPoll\)/);

const timeUtility = readFileSync(resolve(root, "src", "time.ts"), "utf8");
assert.match(timeUtility, /formatUkMarketTime/);
assert.match(timeUtility, /timeZone: "Europe\/London"/);

const batteryPage = readFileSync(resolve(root, "src", "BatteryFlexibilityPage.tsx"), "utf8");
assert.match(batteryPage, /DEGRADED · SAMPLE/);
assert.match(batteryPage, /STALE · FRESHNESS SLA EXCEEDED/);

console.log(`Header timestamp consistency passed for ${pages.length} routes.`);
