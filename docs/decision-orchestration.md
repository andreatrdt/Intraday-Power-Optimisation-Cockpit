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
rolling/cockpit structures into an `AdapterSnapshot`:

* **one `VintageForecastPoint` per settlement period**, built from the current
  `OptimisationPeriodInput` (full P10/P50/P90);
* `published_at` taken from the period's canonical `generation_p50_mwh` lineage
  (genuine publication time), never fabricated;
* `source_mode` and `quality` taken from the same lineage;
* `lineage_value_ids` = the P10/P50/P90 canonical value IDs;
* `q_by_period` and `gate_closure_by_period` from the period inputs;
* `optimiser_by_sp` from the current optimiser run's `projected_trajectory`
  (buy/sell/charge/discharge/prices/tradeable per settlement period);
* `market_snapshot_id` and `optimisation_run_id` preserved.

No future vintage can enter the calculation: the revision service filters to
`published_at ≤ as_of` and a look-ahead guard rejects any *selected* future
point.

## Incomplete-vintage handling

The SAMPLE environment retains only the **latest** vintage's full quantiles (plus
a P50-only "previous"). The adapter therefore emits **only the latest** full
point per period and **does not manufacture** a previous P10/P90 from the latest
spread or from P50. With no previous full vintage, the revision service reports
those periods as `MISSING_PREVIOUS_VINTAGE`, which surfaces as a `FORECAST`-stage
`DecisionSkip`. This is the honest current behaviour (see limitations).

The orchestrator's `process(snapshot)` accepts any `AdapterSnapshot`, so a caller
(or a future environment) that genuinely has two full vintages per period will
produce revisions and decisions without code changes.

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

* The SAMPLE environment does not retain the previous vintage's full P10/P90, so
  `POST /api/v1/decisions/refresh` on the live SAMPLE state creates **no**
  decisions and returns `MISSING_PREVIOUS_VINTAGE` skips. This is intended and
  honest — decision creation is demonstrated in tests by injecting a two-vintage
  `AdapterSnapshot`. A later environment change (or a real feed) that preserves
  full previous quantiles enables end-to-end creation with no orchestrator
  change.
* `trustworthy_for_live_trading` is always `False` in this milestone (no live
  source is configured).
* Recommendation urgency / limit price / £-risk / expected values are
  intentionally unpopulated (hedge-timing and richer economics arrive later).
