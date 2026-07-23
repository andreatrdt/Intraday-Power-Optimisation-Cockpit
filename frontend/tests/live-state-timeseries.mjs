import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  computeValueDomain,
  countPoints,
  emptyStateReason,
  visibleSummary,
  visibleTimePoints,
} from "../src/chartDomain.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, "src", name), "utf8");

// --- Executable behaviour: y-axis domain comes from the actual visible values -------
{
  // Real renewable-production range (86–189 MW). The domain must hug those values,
  // never collapse to the degenerate 0..1 fallback that produced the -0.1..1.1 axis.
  const domain = computeValueDomain([86.294, 188.858], false);
  assert.ok(domain, "non-empty values must yield a domain");
  assert.ok(domain.min > 0 && domain.min < 86.294, `min ${domain.min} must sit just below the data, and well above 0`);
  assert.ok(domain.max > 188.858, `max ${domain.max} must sit just above the data`);
  assert.ok(domain.max - domain.min > 100, "domain spans the real value range, not a fixed 0..1");

  // No visible values -> null, so the caller renders an explicit empty state instead.
  assert.equal(computeValueDomain([], false), null);
  assert.equal(computeValueDomain([Number.NaN, Number.POSITIVE_INFINITY], false), null);

  // includeZero anchors the axis at zero; a flat series still gets a readable band.
  const zeroed = computeValueDomain([5, 8], true);
  assert.ok(zeroed.min <= 0 && zeroed.max >= 8, "includeZero must bracket zero");
  const flat = computeValueDomain([8, 8], false);
  assert.ok(flat.max > flat.min, "a flat series must not produce a zero-height axis");
}

// --- Executable behaviour: only timestamped points are renderable on a time axis ----
{
  const timeSeries = {
    key: "production", label: "Production", unit: "MW", kind: "line", region: "historical",
    points: [
      { label: "a", value: 100, timestamp: "2026-07-23T18:00:00+00:00", settlement_period: null, delivery_period: null },
      { label: "b", value: 150, timestamp: "2026-07-23T19:00:00+00:00", settlement_period: null, delivery_period: null },
      { label: "c", value: Number.NaN, timestamp: "2026-07-23T20:00:00+00:00", settlement_period: null, delivery_period: null },
    ],
  };
  const visible = visibleTimePoints([timeSeries], new Set());
  assert.equal(visible.length, 2, "two finite, timestamped points are renderable (NaN dropped)");
  assert.equal(visibleTimePoints([timeSeries], new Set(["production"])).length, 0, "hidden series contribute nothing");

  // The original failure mode: historical points carry only a delivery period, no
  // timestamp. Those are correctly NOT renderable on the time axis (previously they
  // were fed to the settlement-period chart and silently vanished as "No SPs").
  const spOnly = {
    key: "sp", label: "SP only", unit: "MW", kind: "line", region: "historical",
    points: [{ label: "SP43", value: 158, timestamp: null, settlement_period: 43, delivery_period: "2026-07-23 SP43" }],
  };
  assert.equal(visibleTimePoints([spOnly], new Set()).length, 0);
  assert.equal(countPoints([timeSeries, spOnly]), 4);
}

// --- Executable behaviour: empty charts always explain themselves --------------------
{
  const base = { seriesCount: 1, hiddenCount: 0, sourceMode: "SAMPLE", windowLabel: "Last 30d" };
  assert.equal(emptyStateReason({ ...base, rawCount: 5, filteredCount: 5, visibleCount: 5 }), null, "visible data is not empty");
  assert.match(emptyStateReason({ ...base, rawCount: 0, filteredCount: 0, visibleCount: 0 }), /No backend data/);
  assert.match(
    emptyStateReason({ ...base, windowLabel: "Today", rawCount: 720, filteredCount: 0, visibleCount: 0 }),
    /outside the selected Today window/,
  );
  assert.match(
    emptyStateReason({ ...base, seriesCount: 2, hiddenCount: 2, rawCount: 720, filteredCount: 720, visibleCount: 0 }),
    /All series are hidden/,
  );
  assert.match(
    emptyStateReason({ ...base, sourceMode: "ERROR", rawCount: 0, filteredCount: 0, visibleCount: 0 }),
    /Source returned ERROR/,
  );
}

// --- Executable behaviour: the summary line is derived from the visible points -------
{
  const points = [
    { seriesKey: "production", label: "a", t: 1, value: 100 },
    { seriesKey: "production", label: "b", t: 2, value: 200 },
  ];
  const summary = visibleSummary(points, "MW", "Last 30d");
  assert.match(summary, /Latest 200/);
  assert.match(summary, /range 100–200 MW/);
  assert.match(summary, /2 points over Last 30d/);
  assert.equal(visibleSummary([], "MW", "Last 30d"), "", "no visible points -> no summary text");
}

// --- Source contract: the Live State time-series component ---------------------------
const timeSeriesChart = read("TimeSeriesChart.tsx");
assert.match(timeSeriesChart, /No data available for the selected window\./, "explicit empty-state copy is required");
assert.match(timeSeriesChart, /import\.meta\.env\.DEV/, "development diagnostics must be gated on DEV");
for (const field of ["raw points", "filtered points", "visible points", "window", "source mode", "reason"]) {
  assert.match(timeSeriesChart, new RegExp(field), `dev diagnostics must report ${field}`);
}
assert.match(timeSeriesChart, /computeValueDomain/, "y-domain must come from visible values");
assert.match(timeSeriesChart, /visibleTimePoints/, "rendering must use timestamped visible points");
assert.match(timeSeriesChart, />UK time</, "x-axis is wall-clock UK time");
// The Live State chart must NOT carry any settlement-period / auction-window vocabulary.
for (const forbidden of ["No SPs", "Full auction window", "Future only", "Around NOW", "Next 6 SPs", "Next 12 SPs", "GB settlement period"]) {
  assert.doesNotMatch(timeSeriesChart, new RegExp(forbidden.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), `time-series chart must not mention "${forbidden}"`);
}

// --- Source contract: the Live State page uses time-series, not auction-window, charts
const live = read("LiveStatePage.tsx");
assert.doesNotMatch(live, /LargeChart/, "Live State must not use the settlement-period LargeChart");
assert.equal((live.match(/<TimeSeriesChart/g) ?? []).length, 12, "all twelve Live State charts use the time-series chart");
assert.match(live, /HistoryWindowSelector/, "Live State keeps the Today / 24h / 7d / 30d / Custom control");
assert.match(live, /unavailableInsight=/, "each Live State chart supplies an unavailable-window message");
assert.match(live, /is unavailable for the selected window\./, "insight falls back to an unavailable message, not empty-data text");
// The misleading 'Production 0.0 MW since the previous refresh' subtitle is gone.
assert.doesNotMatch(live, /since the previous refresh/, "insight text must not assert a per-refresh delta as the production level");

// --- Source contract: the two modes stay separate -----------------------------------
const optimisation = read("OptimisationPage.tsx");
const cockpitChart = read("CockpitChart.tsx");
assert.equal((optimisation.match(/<LargeChart/g) ?? []).length, 4, "Optimisation still uses the four settlement-period charts");
assert.doesNotMatch(optimisation, /TimeSeriesChart/, "Optimisation does not use the historical time-series chart");
for (const control of ["Full auction window", "Next 6 SPs", "Future only"]) {
  assert.match(cockpitChart, new RegExp(control), `Optimisation charts keep the "${control}" auction-window control`);
}

// --- Source contract: historical-window control offers only time windows -------------
const history = read("historyWindow.ts");
for (const label of ["Today", "Last 24h", "Last 7d", "Last 30d", "Custom"]) assert.match(history, new RegExp(label));
for (const forbidden of ["Full auction window", "Next 6 SPs", "settlement period"]) {
  assert.doesNotMatch(history, new RegExp(forbidden), `history window control must not mention "${forbidden}"`);
}

console.log("Live State time-series domain, empty-state, summary logic and Live/Optimisation chart-mode separation passed.");
