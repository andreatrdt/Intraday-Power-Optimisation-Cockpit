# Trade decision lifecycle & state machine

This document describes the `TradeDecision` object and the state machine that
governs it. It is the foundation of the trader-in-the-loop workflow and is
**additive** to the existing rolling-optimisation cockpit — it does not change
any existing behaviour, route, or optimiser output.

## Why a distinct decision object

The rolling optimiser produces a *recommended action* for the current snapshot.
That is only one step of a real intraday workflow. A `TradeDecision`
deliberately keeps the following as **separate stage records**, so they are
never conflated:

1. **Context** — the single period, trigger and pre-action exposure (`DecisionContext`).
2. **Recommendation** — what the model suggests (`ModelRecommendation`).
3. **Trader instruction** — what the trader decides (`TraderInstruction`).
4. **Execution** — what the simulated market actually fills (`ExecutionResult`).
5. **Settlement** — the realised delivery + cash outcome (`SettlementResult`).
6. **Evaluation** — how the decision scored versus benchmarks (`EvaluationResult`).

Executed volume — not requested volume — is what later milestones propagate to
the contracted position and battery state.

## Core design rules

* **One decision = exactly one settlement period.** A `TradeDecision` carries a
  single `settlement_period` / `delivery_period` (in its `DecisionContext`).
  Every field — exposure, execution, realised generation, imbalance, P&L —
  refers unambiguously to that one period. There is no scalar spread across
  periods.
* **Multi-period triggers use a batch.** A trigger affecting several periods is
  represented by a `DecisionBatch`: an immutable index holding the child
  `decision_ids` and their `affected_delivery_periods` — and **no** exposure,
  execution or P&L of its own.
* **Composition, not a flat model.** `TradeDecision` composes the six stage
  records above rather than carrying ~60 fields flat. Records are attached as
  the decision advances.
* **Structural immutability.** Every model is a frozen Pydantic model; history
  and benchmark collections are tuples. `decision.status = …` raises, and
  `decision.transitions.append(…)` raises — the append-only guarantee is
  structural, not a store/deep-copy convention.
* **Single source of lifecycle truth.** `DecisionStatus` is the only persisted
  lifecycle state. `trader_status`, `execution_status` and `evaluation_status`
  are **computed properties** derived from the status and the stage records, so
  they can never drift out of sync.

## Modules

| Module | Responsibility |
|---|---|
| [`decision_models.py`](../backend/src/cockpit/decision_models.py) | Frozen schema + enums: `TradeDecision`, stage records, `DecisionTransition`, `DecisionBatch`, `BenchmarkResult` |
| [`decision_state_machine.py`](../backend/src/cockpit/decision_state_machine.py) | Transition graph, `InvalidTransitionError`, stage-payload validation (`StagePayloadError`), immutable `apply_transition` |
| [`decision_service.py`](../backend/src/cockpit/decision_service.py) | In-memory `DECISIONS` store (+ batches) with trader/execution/settlement helpers |

## Schema hierarchy

```
DecisionBatch                     (index over one trigger's decisions)
└── decision_ids: (str, …)

TradeDecision                     (frozen aggregate; one settlement period)
├── context:            DecisionContext        # period, trigger, provenance, exposure-before
├── recommendation:     ModelRecommendation
├── trader_instruction: TraderInstruction | None
├── execution_result:   ExecutionResult   | None
├── settlement_result:  SettlementResult  | None   # delivery + settlement
├── evaluation_result:  EvaluationResult  | None   # holds BenchmarkResult tuple
└── transitions:        (DecisionTransition, …)     # append-only audit log

computed: trader_status · execution_status · evaluation_status · is_terminal
          net_recommended_mwh · net_executed_mwh · settlement_period · as_of
```

## State graph

```
PROPOSED  ──▶ ACCEPTED · MODIFIED · REJECTED · DELAYED
DELAYED   ──▶ PROPOSED · REJECTED
ACCEPTED  ──▶ SUBMITTED · REJECTED
MODIFIED  ──▶ SUBMITTED · REJECTED
SUBMITTED ──▶ PARTIALLY_FILLED · FILLED · CANCELLED · EXPIRED
PARTIALLY_FILLED ─▶ FILLED · DELIVERED · CANCELLED · EXPIRED
FILLED    ──▶ DELIVERED
REJECTED  ──▶ DELIVERED
CANCELLED ──▶ DELIVERED
EXPIRED   ──▶ DELIVERED
DELIVERED ──▶ SETTLED
SETTLED   ──▶ EVALUATED
EVALUATED     (terminal — the only terminal state)
```

A **rejected, cancelled or expired** decision still has a delivery-period
outcome, so it routes `… → DELIVERED → SETTLED → EVALUATED` and is scored
against the realised result (e.g. "did declining the hedge cost us?"). There is
no direct jump to `EVALUATED` that skips delivery and settlement.

## Two enforcement layers on every transition

1. **Graph legality** — `apply_transition` rejects any edge not in the table
   above with `InvalidTransitionError` (whose message lists the legal targets).
2. **Stage-payload consistency** — the authoritative `status` can never move
   without the record that stage requires, enforced by `StagePayloadError`:

   | Target | Requires |
   |---|---|
   | `ACCEPTED` / `MODIFIED` / `REJECTED` / `DELAYED` | a `TraderInstruction` with the matching action |
   | `PROPOSED` (re-propose from `DELAYED`) | the trader instruction cleared to `None` |
   | `SUBMITTED` | an accepted/modified instruction **and** an `ExecutionResult` in `SUBMITTED` |
   | `FILLED` | an `ExecutionResult` in `FILLED` status with zero unfilled volume |
   | `PARTIALLY_FILLED` | an `ExecutionResult` in `PARTIALLY_FILLED` status with non-zero unfilled volume |
   | `CANCELLED` / `EXPIRED` | an `ExecutionResult` in the matching status |
   | `DELIVERED` | a `SettlementResult` with `realised_generation_mwh` |
   | `SETTLED` | a `SettlementResult` with `realised_pnl_gbp` |
   | `EVALUATED` | an `EvaluationResult` |

Transition timestamps must be timezone-aware.

## Append-only audit log

Every transition appends one immutable `DecisionTransition` to
`TradeDecision.transitions`:

* `sequence` is a 1-based, strictly increasing index; the creation record is
  `sequence = 1` with `from_status = None`.
* `apply_transition` builds `(*old_transitions, new_record)` and returns a new
  frozen decision via `model_copy`; the input is never touched.

Guaranteed invariant (tested): for any two successive versions of a decision,
`later.transitions[: len(earlier.transitions)] == earlier.transitions`, and
neither the tuple nor its records can be mutated in place.

## Trust & product-boundary semantics

Trust fields live on `DecisionContext` (they describe the inputs at proposal
time):

* `run_mode` — `SAMPLE_DEMO` · `HISTORICAL_REPLAY` · `LIVE_OBSERVATION`.
* `source_mode` / `quality` — reused from the canonical data model.
* `calculation_allowed` / `trustworthy_for_live_trading` — unchanged semantics.

`TradeDecision.diagnostic_only = True` and `not_executable = True` are set on
creation; nothing in this layer submits an order or controls an asset.

`ModelRecommendation.confidence_basis` (`HISTORICAL_CALIBRATION` ·
`ASSUMPTION_BASED` · `UNAVAILABLE`) defaults to `UNAVAILABLE`: confidence is
never presented as calibrated unless it is backed by historical forecast-error
statistics (populated by the forecast-revision service in the next milestone).

## Example

```python
from cockpit.decision_service import DECISIONS
from cockpit.decision_models import DecisionContext, ModelRecommendation, RecommendedAction, TriggerType, Urgency

ctx = DecisionContext(
    settlement_period=24, delivery_period="SP24",
    delivery_start=start, delivery_end=end, as_of=now,
    trigger_type=TriggerType.FORECAST_REVISION,
    trigger_description="Wind P50 revised down 58 MWh.",
    p50_exposure_before_mwh=-54.0,
)
rec = ModelRecommendation(action=RecommendedAction.BUY, buy_mwh=32.0, urgency=Urgency.HIGH)

d = DECISIONS.create(context=ctx, recommendation=rec)
DECISIONS.accept(d.decision_id)
DECISIONS.submit(d.decision_id)
DECISIONS.apply_fill(d.decision_id, executed_buy_mwh=32.0, average_price=74.5)
DECISIONS.deliver(d.decision_id, realised_generation_mwh=210.0)
DECISIONS.settle(d.decision_id, realised_pnl_gbp=910.0)
DECISIONS.evaluate(d.decision_id, regret_vs_best_benchmark_gbp=490.0)
```

For a multi-period trigger, `DECISIONS.create_batch(contexts=[...], recommendations=[...])`
returns `(DecisionBatch, [TradeDecision, ...])`.

## Scope & limitations (this milestone)

* No API routes yet — the store is not wired into `api.py`. That is Milestone 3
  (trader-in-the-loop backend), which will populate recommendations from the
  live rolling state and optimiser action.
* No frontend yet — Milestone 5 adds the Trade Decision page.
* Persistence is in-memory only, matching the existing run ledger.
* Fills, realised outcomes and benchmarks are set by callers here; the realistic
  execution simulator (Phase 12), settlement, and benchmark suite (Phase 11)
  arrive in later milestones.
