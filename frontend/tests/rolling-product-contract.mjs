import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, "src", name), "utf8");
const route = read("main.tsx");
const nav = read("ProductNav.tsx");
const live = read("LiveStatePage.tsx");
const optimisation = read("OptimisationPage.tsx");
const diagnostics = read("DiagnosticsPage.tsx");
const api = read("api.ts");
const charts = read("CockpitChart.tsx");

assert.match(route, /\/live.*<LiveStatePage/);
assert.match(route, /\/optimisation.*<OptimisationPage/);
assert.match(route, /\/diagnostics.*<DiagnosticsPage/);
assert.equal((nav.match(/<a /g) ?? []).length, 3, "primary navigation must contain exactly three links");
assert.match(nav, />Live State</);
assert.match(nav, />Optimisation</);
assert.match(nav, />Diagnostics</);

for (const label of ["Data Flow", "Forecast & Position", "Market & Liquidity", "Battery Flexibility", "Battery Path", "Optionality"]) assert.match(diagnostics, new RegExp(label.replace("&", "&")));
assert.match(diagnostics, /consume the current rolling-state snapshot/);

assert.match(live, /Live Market State/);
assert.match(live, /SECONDARY · DATA TAPE/);
assert.match(live, /Browser clock/);
assert.match(live, /UK market clock/);
assert.match(live, /Next Gate Closure/);
assert.match(live, /Sample simulation assumes model actions are followed|simulation_assumption/);
assert.match(live, /refreshLiveState/);
assert.match(live, /Auto-refresh/);
assert.match(live, /Refresh cadence/);
for (const title of ["Renewable production history", "Forecast vintages and uncertainty", "Market price and order-book quotes", "GB system frequency", "Portfolio Q and pre-action exposure"]) assert.match(live, new RegExp(title));

assert.match(optimisation, /Rolling Optimisation/);
assert.match(optimisation, /HERO DECISION TABLE/);
assert.match(optimisation, /Run optimisation now/);
assert.doesNotMatch(optimisation, /Advance one settlement period/);
assert.match(optimisation, /Reset sample state/);
assert.match(optimisation, /Auto-refresh/);
assert.match(optimisation, /Refresh cadence/);
assert.match(optimisation, /Horizon mode/);
assert.match(optimisation, /Scenario regime/);
assert.match(optimisation, /reserve_up_mw/);
assert.match(optimisation, /residual_p10_mwh/);
assert.match(optimisation, /binding_constraints/);
assert.match(optimisation, /WHY THE MODEL CHOSE THIS/);
assert.match(optimisation, /SINCE PREVIOUS RUN/);
assert.ok(optimisation.indexOf("HERO DECISION TABLE") < optimisation.indexOf("DECISION CHARTS"), "hero decision table must precede secondary charts");
for (const title of ["Optimised market action path", "Projected SoC path", "Reserve capability and commitments", "Residual exposure fan", "Market execution prices", "Objective and risk breakdown", "Driver contribution scores", "One-factor sensitivity"]) assert.match(optimisation, new RegExp(title));
assert.match(optimisation, /NOT REAL EXECUTION/);
assert.match(charts, /onMouseEnter/);
assert.match(charts, /safeBand/);
assert.match(charts, /Start:/);
assert.match(charts, /End:/);

for (const endpoint of ["/live-state", "/live-state/refresh", "/live-state/reset", "/live-state/regime", "/live-state/horizon", "/optimisation/current", "/optimisation/run", "/optimisation/runs"]) assert.match(api, new RegExp(endpoint.replaceAll("/", "\\/")));

console.log("Rolling product routes, three-link navigation, controls, trajectory, charts and API wiring passed.");
