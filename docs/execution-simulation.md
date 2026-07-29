# Simulated order submission & execution (Milestone 6B)

A clearly separated **diagnostic SAMPLE** execution stage after a trader has
recorded an `ACCEPTED` or `MODIFIED` decision:

```
ACCEPTED / MODIFIED → SUBMITTED → PARTIALLY_FILLED / FILLED / EXPIRED
```

Nothing here routes a live order, reaches a broker/exchange, or settles. It is a
synchronous **IOC-style** simulator: full fill → `FILLED`, partial fill →
`PARTIALLY_FILLED`, zero fill → `EXPIRED`. There are no persistent open orders,
so there is **no cancel endpoint** (cancellation is deferred — an IOC order
immediately fills, partially fills or expires).

## Semantic boundary

`ModelRecommendation` (model) → `TraderInstruction` (trader) → `SimulatedOrder`
(submitted to the simulator) → `ExecutionOutcome` (what the simulator filled).
`SUBMITTED` means submitted to the **simulator** only — not that an exchange
accepted an order. Wording is explicit throughout: *Submit to simulator*,
*Simulated order*, *Simulated fill*.

## Modules

| Module | Responsibility |
|---|---|
| [`execution_models.py`](../backend/src/cockpit/execution_models.py) | frozen `SimulatedOrder`, `SimulatedFill`, `ExecutionOutcome`, `ExecutionConfig`, `ExecutionMode`, request DTO |
| [`execution_simulator.py`](../backend/src/cockpit/execution_simulator.py) | pure deterministic fill logic |
| [`execution_service.py`](../backend/src/cockpit/execution_service.py) | eligibility, order construction, decision integration, storage, idempotency, concurrency |

Execution logic is **not** placed in `decision_service`, `decision_orchestrator`,
`hedge_timing` or `full_action_optimiser`; the API stays thin.

## Execution modes

* **IDEAL** — benchmark: full requested volume at the best visible price, zero
  slippage, zero latency; fees apply.
* **REALISTIC** — assumption-driven SAMPLE: walks visible depth with a
  configurable depth haircut (default 10%) and latency (250 ms); partial fills
  allowed; limit price respected.
* **STRESS** — assumption-driven SAMPLE: larger haircut (40%), extra latency
  (1500 ms) and an adverse per-level price shift (£8/MWh); partial/zero fill
  possible; limit respected.

Latency is modelled deterministically — recorded in the assumptions, with the
depth haircut representing visible depth assumed consumed during that latency.
**No stochastic process**; the same inputs + config reproduce identical fills.
REALISTIC and STRESS are labelled assumption-driven and are **not** calibrated to
a real exchange.

## Order-book execution

The order is built from the **TraderInstruction** (never silently from the model
recommendation). For a BUY it walks visible asks best→worse; for a SELL, bids
best→worse; stopping when the volume is filled, depth is exhausted, or the limit
price is violated. Per-level `SimulatedFill` evidence is preserved (level,
volume, price, fee, slippage vs best). Limit behaviour: BUY never fills above the
limit, SELL never below; a violated limit yields a partial or zero fill and never
overrides the limit.

## Submission eligibility

Allowed only when the decision is `ACCEPTED`/`MODIFIED`, a valid one-sided trader
instruction exists (exactly one of buy/sell positive), the period is tradeable,
Gate Closure has not passed, trust permits calculation, and the expected
status/sequence matches. Rejections: `409` for stale/illegal lifecycle conflicts
(non-submittable status, stale sequence, idempotency conflict); `422` for invalid
content (both-positive, zero volume, Gate Closure passed, no market snapshot,
trust not calculable); `404` for an unknown decision.

## Decision integration

The simulated outcome populates the decision's existing `ExecutionResult` stage
record (`executed_buy_mwh`/`executed_sell_mwh`, `average_execution_price`,
`execution_cost_gbp`, `execution_fees_gbp`, `execution_slippage_gbp_per_mwh`,
`unfilled_volume_mwh`) and advances the lifecycle (SUBMITTED → FILLED/
PARTIALLY_FILLED/EXPIRED). It never overwrites the ModelRecommendation, the
TraderInstruction, the forecast-revision evidence or the hedge-timing assessment.
Transition history remains append-only.

## Idempotency & concurrency

`submit-simulated` accepts `expected_status`, `expected_sequence` and an optional
`idempotency_key`. Behaviour:

* stale expected state → **409** `stale_decision`;
* same idempotency key + identical payload (`decision_id:mode`) → returns the
  existing outcome (idempotent);
* same key + different payload → **409** `idempotency_conflict`;
* repeated submission without a key after the transition → **409** (the decision
  is no longer in a submittable state).

The dedupe key is `(decision_id, execution_mode)` stored against the idempotency
key in the in-memory `ExecutionStore` (orders, outcomes, idempotency records —
no SQLite).

## API

| Method & path | Returns |
|---|---|
| `POST /api/v1/decisions/{id}/submit-simulated` | `{ outcome, decision, execution_mode, simulator_version, assumptions_used, diagnostic_only, not_executable, trustworthy_for_live_trading }` |
| `GET /api/v1/simulated-orders` / `…/{order_id}` | stored simulated orders |
| `GET /api/v1/execution-outcomes` / `…/{order_id}` | stored outcomes |
| `GET /api/v1/decisions/{id}/execution` | the latest outcome for a decision |

Every response exposes the simulation mode, `diagnostic_only = true`,
`not_executable = true`, `trustworthy_for_live_trading = false`, the assumption
basis and the simulator version. No route name implies a real submission.

## Frontend (`/decisions` drawer)

A **Simulated execution** panel with `SIMULATED` / `NOT EXECUTABLE` badges:

* `ACCEPTED`/`MODIFIED` → a submission panel showing the trader-instruction side/
  volume/limit, settlement period, minutes to Gate Closure, best bid/ask, visible
  depth, a mode selector (IDEAL/REALISTIC/STRESS with assumption labels), the
  expected sequence and a client-generated idempotency key, plus the warning
  *“This sends the trader instruction to the internal execution simulator only.
  No real order will be placed.”* and a **Submit to simulator** button.
* `SUBMITTED`/`PARTIALLY_FILLED`/`FILLED`/`EXPIRED` → a read-only outcome:
  execution status, requested/filled/unfilled, average fill price, best price
  before execution, slippage, fees, total cost, the fill-level breakdown, and the
  assumptions/warnings.

Recommendation, trader instruction and execution outcome are shown in separate
sections. `409` is surfaced distinctly (stale vs idempotency conflict) with a
*Reload*; the selected mode is kept after a failure. No control uses execute /
trade / send order / submit-to-market wording.

## Limitations

* Diagnostic SAMPLE only — no live routing, broker/exchange connectivity, real
  submission, settlement, realised generation/imbalance, realised P&L, benchmark
  evaluation, replay, objective/CVaR changes or battery control.
* Synchronous IOC with no persistent open orders → no cancel.
* REALISTIC/STRESS parameters are transparent assumptions, not calibrated to any
  real venue.
