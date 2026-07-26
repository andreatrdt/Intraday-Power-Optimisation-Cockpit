# Decision orchestration (trader-in-the-loop backend)

The orchestrator ([`decision_orchestrator.py`](../backend/src/cockpit/decision_orchestrator.py))
wires the existing services together so a **material single-period forecast
revision** creates a corresponding **single-period `TradeDecision`** with an
initial model recommendation:

```
rolling snapshot
  → forecast revision run (per settlement period)      [forecast_revision]
  → material trigger candidates
  → one TradeDecision per period, grouped in a DecisionBatch   [decision_service]
  → recommendation populated from the current optimiser result
  → stored in DECISIONS, retrievable via the API
```

It owns **coordination only** — no forecast mathematics, no optimisation
mathematics, no lifecycle rules. It invents no values: fields that are not
credibly available are left null / `UNAVAILABLE`. Nothing here submits an order
or controls an asset.

## Adapter behaviour

`DecisionOrchestrator.build_rolling_snapshot()` converts the current
rolling/cockpit structures into an `AdapterSnapshot`. Forecast points come from
the **retained complete vintages** (see
[forecast-vintages.md](forecast-vintages.md)):

* it selects the **two newest eligible** full vintages (`published_at ≤ as_of`,
  ordered by `(published_at, vintage_id)`) and emits their per-period
  `VintageForecastPoint`s — a genuine latest and previous per settlement period;
* each point preserves the vintage's `published_at`, `source_mode`, `quality`
  and the quantiles' genuine `lineage_value_ids` — nothing is fabricated;
* `q_by_period` and `gate_closure_by_period` come from the current period inputs;
* `optimiser_by_sp` from the current optimiser run's `projected_trajectory`;
* `market_snapshot_id` and `optimisation_run_id` preserved.

No future vintage can enter the calculation: the store filters to
`published_at ≤ as_of` and the revision service's look-ahead guard rejects any
*selected* future point.

## Incomplete-vintage handling

When only one eligible vintage exists for a period (e.g. the very first refresh),
no previous full quantiles are available and the revision service reports that
period as `MISSING_PREVIOUS_VINTAGE` (a `FORECAST`-stage `DecisionSkip`). A
previous P10/P90 is **never** manufactured from the latest spread or from P50 —
the fix is genuine retention, not reconstruction.

The orchestrator's `process(snapshot)` accepts any `AdapterSnapshot`, so tests
drive it directly with two-vintage snapshots.

## Decision creation rules

For each **material** `ForecastRevision` that is not skipped:

1. **Trust gate** — if `quality` is `INVALID`/`MISSING`, skip
   (`SOURCE_TRUST_NOT_CALCULABLE`).
2. **Deduplication** — if a stored decision already has the same dedupe key, it
   is an already-existing duplicate (not re-created).
3. **Optimiser match** — if there is no optimiser result for the settlement
   period, skip (`NO_MATCHING_OPTIMISER_PERIOD`).
4. **Gate** — if Gate Closure has already passed (`minutes_to_gate < 0`), skip
   (`GATE_CLOSURE_PASSED`).
5. **Action mapping** — map the optimiser market decision to a recommended
   action; reject simultaneous buy+sell (`INCONSISTENT_BUY_SELL`).
6. Build a `DecisionContext` and `ModelRecommendation`, then create one decision.

All decisions created in a single refresh are grouped in **one `DecisionBatch`**
(IDs + affected periods only — no aggregate exposure/execution/P&L).

`DecisionContext` is populated with: trigger info; source/trust (`run_mode`,
`source_mode`, `quality`, `calculation_allowed`, `trustworthy_for_live_trading`);
`forecast_vintage_id` / `previous_forecast_vintage_id` / `forecast_revision_id`;
`market_snapshot_id`; `optimisation_run_id`; `minutes_to_gate_closure`;
`position_before_mwh` (= contracted Q); `forecast_revision_mwh`; and the **latest**
(post-revision, pre-hedge) P10/P50/P90 exposures. The **previous** exposures live
on the linked `ForecastRevision` (`forecast_revision_id`) — the single source of
truth, not duplicated on the context.

`ModelRecommendation` is populated **only** with what exists credibly:
`action`, `buy_mwh`, `sell_mwh`, and transparent `reasoning` (the revision's
materiality reasons plus a one-line optimiser-action note). `limit_price`,
`urgency`, `confidence_*`, `risk_if_no_action_gbp`, and expected action/wait
£-values are left null / `UNAVAILABLE` — none are produced credibly yet (urgency
is deferred to the hedge-timing milestone).

## Action mapping

`map_recommended_action(buy_mwh, sell_mwh)` maps **only the market hedge** —
battery charge/discharge is never collapsed into buy/sell:

| Optimiser output | Recommended action |
|---|---|
| `buy > tol` and `sell ≈ 0` | `BUY` |
| `sell > tol` and `buy ≈ 0` | `SELL` |
| both `≈ 0` | `NO_ACTION` (the optimiser chose not to trade; `WAIT` is a hedge-timing verdict, later) |
| both `> tol` | `InconsistentActionError` → logged as an integration skip, no decision |

Tolerance is `1e-6` MWh.

## Deduplication key

```
DedupeKey = (latest_forecast_vintage_id, previous_forecast_vintage_id,
             settlement_period, trigger_type)
```

Computed from stored decisions at the start of every refresh, so repeated
refreshes of the same state are **idempotent** (duplicates are reported, not
re-created). A **new** decision is created when a new valid forecast vintage
arrives, when the predecessor differs, or when the settlement period / trigger
changes.

## Trust semantics

Every created decision preserves the cockpit's trust model. For the current
SAMPLE environment:

* `run_mode = SAMPLE_DEMO`
* `trustworthy_for_live_trading = False`
* `diagnostic_only = True`, `not_executable = True`
* `calculation_allowed = True` (SAMPLE is calculable)

The refresh response echoes `diagnostic_only: true` and
`trustworthy_for_live_trading: false`. The API never implies live tradability.

## API contract

| Method & path | Returns |
|---|---|
| `GET /api/v1/decisions` | `{ "decisions": [TradeDecision, …] }` (newest first) |
| `GET /api/v1/decisions/{decision_id}` | `{ "decision": TradeDecision }` or 404 |
| `GET /api/v1/decision-batches` | `{ "batches": [DecisionBatch, …] }` |
| `GET /api/v1/decision-batches/{batch_id}` | `{ "batch": DecisionBatch }` or 404 |
| `POST /api/v1/decisions/refresh` | `{ refresh: DecisionRefreshResult, created: [...], existing: [...], batch, diagnostic_only, trustworthy_for_live_trading }` |
| `GET /api/v1/forecast-revisions` | `{ "revisions": [ForecastRevision, …] }` |
| `GET /api/v1/forecast-revisions/{revision_id}` | `{ "revision": ForecastRevision }` or 404 |
| `GET /api/v1/forecast-revision-runs` | `{ "runs": [ForecastRevisionRun, …] }` |

`DecisionRefreshResult` carries `created_decision_ids`, `batch_id`,
`duplicate_decision_ids`, and a `skipped` list of `DecisionSkip`
(`{settlement_period, delivery_period, stage, code, message}`). No lifecycle
mutation endpoints (accept/modify/reject) are exposed yet — that is a following
milestone.

### Structured skip reasons

`FORECAST`: `MISSING_PREVIOUS_VINTAGE`, `INVALID_QUANTILE_ORDER`,
`UNIT_MISMATCH`, `SETTLEMENT_PERIOD_MISMATCH`, `LOOK_AHEAD`,
`MISSING_CONTRACTED_POSITION`. `MATERIALITY`: `NON_MATERIAL`. `TRUST`:
`SOURCE_TRUST_NOT_CALCULABLE`. `OPTIMISER`: `NO_MATCHING_OPTIMISER_PERIOD`,
`INCONSISTENT_BUY_SELL`. `GATE`: `GATE_CLOSURE_PASSED`.

## Current SAMPLE limitations

* As of Milestone 3.5 the SAMPLE environment **retains complete previous
  vintages**, so `POST /api/v1/decisions/refresh` creates decisions end-to-end
  after ≥2 refreshes with a material forecast change (see
  [forecast-vintages.md](forecast-vintages.md)). A single vintage still yields
  `MISSING_PREVIOUS_VINTAGE`, and a non-material change yields `NON_MATERIAL`.
* `trustworthy_for_live_trading` is always `False` in this milestone (no live
  source is configured).
* Recommendation urgency / limit price / £-risk / expected values are
  intentionally unpopulated (hedge-timing and richer economics arrive later).
