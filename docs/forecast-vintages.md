# Complete forecast vintages & retention (Milestone 3.5)

Previously the rolling SAMPLE environment kept only the **latest** full
P10/P50/P90 vintage plus a P50-only "previous", so no genuine previous-vintage
quantiles existed and `POST /api/v1/decisions/refresh` honestly created zero
decisions. Milestone 3.5 fixes the **data retention** — it does **not** weaken
the full-quantile requirement and it never reconstructs missing quantiles.

## Immutable records

[`forecast_vintages.py`](../backend/src/cockpit/forecast_vintages.py) defines two
frozen records:

* **`ForecastVintagePeriod`** — one settlement period's complete P10/P50/P90:
  `settlement_period`, `delivery_period`, `delivery_start/end`, `p10/p50/p90_mwh`,
  `unit`, and the genuine `lineage_value_ids` of the three quantile values.
* **`ForecastVintageSnapshot`** — a complete vintage: `vintage_id`,
  `published_at`, `as_of`, `source_mode`, `quality`, and a tuple of
  `ForecastVintagePeriod`. `period_for(delivery_period)` looks one up.

Both are frozen; a vintage is never mutated after publication.

## Retention rule

`ForecastVintageStore` (default `max_vintages = 16`):

* retains the most recent **16** complete vintages;
* vintages are appended in creation (chronological) order and never mutated;
* when capacity is exceeded the **oldest by publication time** is dropped;
* `min` capacity is 2 (a predecessor must be retainable);
* **selection of latest/previous is by publication time** (tie-broken by
  `vintage_id`), never by insertion accident — `eligible(as_of)` returns
  vintages with `published_at ≤ as_of` sorted by `(published_at, vintage_id)`,
  and `latest_and_previous(as_of)` returns the last two.

The environment retains one complete vintage per refresh
(`SimulatedEnvironment._retain_vintage`), preserving full P10/P50/P90 and their
lineage IDs verbatim. Because the previous vintage is already in the store from
the prior refresh, nothing is reconstructed from the latest spread, the current
P50, fixed spreads, assumptions, or later information.

## Adapter selection

The decision orchestrator's adapter (`build_rolling_snapshot`) consumes the
retained vintages via `RollingService.forecast_vintage_snapshots()`. At a given
`as_of` it selects the **two newest eligible** full vintages and emits their
per-period `VintageForecastPoint`s (latest + previous). The Forecast Revision
Service then, per period:

* `latest` = newest full vintage with `published_at ≤ as_of`;
* `previous` = immediately preceding eligible full vintage;
* future vintages are excluded;
* a period present in only one of the two vintages is explicitly skipped
  (`MISSING_PREVIOUS_VINTAGE`).

Q, Gate Closure and the optimiser recommendation still come from the current
rolling/optimiser state.

## End-to-end SAMPLE behaviour

After at least two refreshes with a **material** forecast change (e.g. an
initialise followed by a regime change), `POST /api/v1/decisions/refresh` now
creates a `ForecastRevisionRun`, material `ForecastRevision` records, one
`DecisionBatch`, and one `TradeDecision` per material settlement period. A
non-material change (a plain refresh with no forecast movement) still creates
**zero** decisions and reports `NON_MATERIAL` skips.

## Linked forecast revisions API

A `TradeDecision` links to its supporting revision through
`context.forecast_revision_id`; the record is retrievable:

* `GET /api/v1/forecast-revisions` — all revisions produced this session
* `GET /api/v1/forecast-revisions/{revision_id}` — one, or 404
* `GET /api/v1/forecast-revision-runs` — the recent revision runs

Previous/latest exposures are **not** duplicated onto the decision — the
`ForecastRevision` (with `portfolio.previous_*` and `portfolio.latest_*`
exposures) is the single source of truth.

## Reset & isolation

`SimulatedEnvironment.reset()` clears the vintage store and re-seeds a clean,
deterministic initial vintage. The decision store (`DECISIONS`) and the
orchestrator's revision/run cache are **independent** and are **not** cleared by
a rolling-environment reset — a trader's decision history should not vanish when
the simulation resets. For full test isolation, clear them explicitly:

```python
DECISIONS.clear()          # decision store
ORCHESTRATOR.reset()       # retained revisions + runs
ROLLING.reset()            # rolling environment + vintage store
```

Note: because deterministic reset regenerates the same `vintage_id`s, a decision
created before a reset can still match the dedupe key of a post-reset refresh —
tests that expect isolation clear `DECISIONS`/`ORCHESTRATOR` alongside the
environment. Tests that must not touch the global singletons build an isolated
`RollingService(DataFlowPipeline(), SimulatedEnvironment())` instead.
