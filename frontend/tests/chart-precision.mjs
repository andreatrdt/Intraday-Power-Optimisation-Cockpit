import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  FREQUENCY_BANDS,
  FREQUENCY_DEVIATION,
  formatYAxisTick,
  frequencyDeviationMHz,
  frequencyDeviationSummary,
  frequencyTooltip,
  tickPrecision,
  visibleSummary,
} from "../src/chartDomain.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (name) => readFileSync(resolve(root, "src", name), "utf8");
// Decimal separator is locale-dependent under Node; accept "." or ",".
const dec = "[.,]";

// --- Frequency y-axis ticks must NOT collapse 49.98 / 50.00 / 50.02 into "50" -------
{
  const min = 49.98;
  const max = 50.02;
  const labels = [49.98, 50.0, 50.02].map((value) => formatYAxisTick(value, "Hz", min, max));
  assert.equal(new Set(labels).size, 3, `frequency ticks must stay distinct, got ${JSON.stringify(labels)}`);
  for (const label of labels) assert.notEqual(label, "50", "a frequency tick must never round to a bare 50");
  assert.match(labels[0], new RegExp(`^49${dec}980$`));
  assert.match(labels[1], new RegExp(`^50${dec}000$`));
  assert.match(labels[2], new RegExp(`^50${dec}020$`));
  assert.ok(tickPrecision("Hz", min, max) >= 3, "Hz keeps at least 3 decimals");
}

// --- Frequency deviation conversion (Hz -> mHz) -------------------------------------
{
  assert.equal(frequencyDeviationMHz(50.001), 1);
  assert.equal(frequencyDeviationMHz(49.97), -30);
  assert.equal(frequencyDeviationMHz(50.05), 50);
  assert.equal(frequencyDeviationMHz(50.0), 0);
  assert.equal(FREQUENCY_DEVIATION.displayUnit, "mHz");
  assert.equal(FREQUENCY_DEVIATION.rawUnit, "Hz");
  assert.equal(FREQUENCY_DEVIATION.toDisplay(50.001), 1);
}

// --- Tooltip carries BOTH raw Hz and mHz deviation ---------------------------------
{
  const up = frequencyTooltip(50.001);
  assert.match(up, new RegExp(`50${dec}001 Hz`));
  assert.match(up, /\+1 mHz/);
  const down = frequencyTooltip(49.97);
  assert.match(down, new RegExp(`49${dec}970 Hz`));
  assert.match(down, /[-−]30 mHz/);
}

// --- Summary text shows a precise range, in deviation and in raw Hz -----------------
{
  const points = [
    { seriesKey: "frequency", label: "a", t: 1, value: 49.942 },
    { seriesKey: "frequency", label: "b", t: 2, value: 50.058 },
    { seriesKey: "frequency", label: "c", t: 3, value: 50.001 },
  ];
  const deviation = frequencyDeviationSummary(points, "Today");
  assert.match(deviation, /Latest \+1 mHz/);
  assert.match(deviation, /versus 50 Hz/);
  assert.match(deviation, /[-−]58 to \+58 mHz/, `range must span the real deviation, got: ${deviation}`);
  assert.doesNotMatch(deviation, /range 0 to 0/);

  // Raw-Hz summary must also be precise, never "50–50 Hz".
  const raw = visibleSummary(points, "Hz", "Today");
  assert.match(raw, new RegExp(`Latest 50${dec}001 Hz`));
  assert.match(raw, new RegExp(`range 49${dec}942[-–]50${dec}058 Hz`));
  assert.doesNotMatch(raw, /range 50[-–]50 Hz/);
}

// --- Small-range series auto-escalate precision (e.g. tight SoC movement) -----------
{
  const socLabels = [58.2, 58.25, 58.3].map((value) => formatYAxisTick(value, "MWh", 58.2, 58.3));
  assert.equal(new Set(socLabels).size, 3, `tight SoC ticks must stay distinct, got ${JSON.stringify(socLabels)}`);
  for (const label of socLabels) assert.match(label, new RegExp(`58${dec}\\d`), "tight SoC ticks keep decimals");
  assert.ok(tickPrecision("MWh", 58.2, 58.3) >= 2, "a 0.1 MWh span escalates precision");

  // Small GBP/MWh spreads keep 2 decimals.
  assert.match(formatYAxisTick(1.42, "GBP/MWh", 1.4, 3.5), new RegExp(`1${dec}42`));
}

// --- MW / MWh over wide ranges stay readable and NOT over-precise -------------------
{
  const wide = formatYAxisTick(200, "MW", 100, 300);
  assert.equal(wide, "200", `wide MW ranges stay integer, got ${wide}`);
  assert.doesNotMatch(wide, new RegExp(`${dec}\\d`), "wide MW ticks must not carry decimals");
  assert.equal(tickPrecision("MW", 100, 300), 0);
  assert.equal(formatYAxisTick(158.08, "MW", 76, 199), "158");
}

// --- Source contract: deviation mode, bands and dual tooltip are wired in -----------
const timeSeriesChart = read("TimeSeriesChart.tsx");
assert.match(timeSeriesChart, /formatYAxisTick/, "time-series y-axis uses unit-aware formatting");
assert.match(timeSeriesChart, /referenceBands/, "time-series chart supports reference bands");
assert.match(timeSeriesChart, /transform\.formatTooltip/, "line tooltips show the transform (dual-unit) text");
assert.match(timeSeriesChart, /reference-band/, "reference bands are rendered");
assert.match(timeSeriesChart, /time-series-hit/, "hover targets provide per-point tooltips");
assert.doesNotMatch(timeSeriesChart, /\{number\(value\)\}/, "axis labels no longer use generic compact formatting");

const live = read("LiveStatePage.tsx");
assert.match(live, /transform=\{FREQUENCY_DEVIATION\}/, "frequency chart defaults to mHz deviation");
assert.match(live, /referenceBands=\{FREQUENCY_BANDS\}/, "frequency chart shows operational bands");

const cockpitChart = read("CockpitChart.tsx");
assert.match(cockpitChart, /formatYAxisTick\(value, track\.unit, track\.min, track\.max\)/, "Optimisation axes use unit-aware formatting");

// --- Frequency bands cover nominal, ±50 mHz and ±100 mHz ----------------------------
{
  const labels = FREQUENCY_BANDS.map((band) => band.label);
  assert.ok(labels.some((label) => /nominal 50 Hz/.test(label)));
  assert.ok(labels.some((label) => /±50 mHz/.test(label)));
  assert.ok(FREQUENCY_BANDS.some((band) => Math.abs(band.min) === 100 || Math.abs(band.max) === 100), "±100 mHz warning markers exist");
  assert.ok(FREQUENCY_BANDS.some((band) => band.tone === "nominal" && band.min === 0 && band.max === 0));
}

console.log("Chart y-axis precision, frequency deviation mode, threshold bands and dual-unit tooltip contracts passed.");
