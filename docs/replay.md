# Milestone 8 — Point-in-Time Replay and Strategy Evaluation

The replay engine evaluates the **existing** decision workflow across many episodes
without look-ahead. It reuses the production services — decision orchestrator,
forecast-revision, hedge-timing, execution simulator, settlement and evaluation — so
the replay economics are identical to the live SAMPLE workflow. It is **not** a
separate backtester, and it makes **no** claim of live or historical performance.

> SAMPLE_REPLAY is diagnostic and deterministic. It is not historical performance and
> is never labelled historical. HISTORICAL_REPLAY is available only when inputs come
> from a genuine historical dataset (none is bundled today).

## Replay architecture

```
ReplayDataset ──(PointInTimeView guard)──▶ AdapterSnapshot ──▶ DecisionOrchestrator.process
                                        └─▶ rolling adapter ──▶ HedgeTimingService / ExecutionService
                                        └─▶ realised provider ─▶ SettlementService / EvaluationService
replay_engine.run_replay ──▶ ReplayResult(run, episodes, integrity) ──▶ replay_metrics ──▶ ReplayMetrics
replay_service.REPLAY ──▶ storage + idempotency + dataset registry ──▶ thin API
```

Modules (concerns kept separate; **no replay logic leaks into the production
services or the API**):

| Module | Responsibility |
| --- | --- |
| `replay_models.py` | Frozen contracts, enums, result records, request/response. |
| `replay_dataset.py` | Immutable dataset contract, SAMPLE builder, **look-ahead guard**. |
| `replay_engine.py` | Deterministic point-in-time event loop, trader policies, guard-routed rolling adapter, isolated per-run stores. |
| `replay_metrics.py` | Pure aggregate metrics. |
| `replay_service.py` | Run storage, idempotency, dataset registry, API coordination. |

## Point-in-time data rules

At replay clock `t`, only information with `publication_time <= t` **and**
`source_available_at <= t` may be used. The engine never uses later forecast
vintages, realised generation before delivery, settlement prices before they are
available, future market depth, later lineage, or any hindsight input inside decision
creation. **Perfect foresight is computed only after settlement** and is isolated from
decision creation (it uses realised generation, which the guard refuses to release
before `delivery_end`).

## Look-ahead guard (`PointInTimeView`)

Every dataset read used by decision creation, timing and execution passes through the
guard. It raises a typed `LookAheadViolation` (recorded on the run's integrity report)
for:

- future publication timestamps (`FUTURE_PUBLICATION`) — future vintages are excluded
  from `vintage_points_asof()`;
- realised fields requested before delivery (`REALISED_BEFORE_DELIVERY`);
- settlement prices before availability (`SETTLEMENT_BEFORE_AVAILABLE`);
- future market snapshots (`FUTURE_MARKET_SNAPSHOT`);
- records outside the replay clock (`OUTSIDE_REPLAY_CLOCK`).

A valid run shows **zero** look-ahead violations; the frontend integrity panel and the
`ReplayRun.lookahead_violation_count` surface this.

## Dataset contract

`ReplayDataset` is immutable and provides, by event time: forecast vintages with
publication timestamps and P10/P50/P90 per settlement period; contracted position Q;
order books (bid/ask levels); Gate Closure; realised generation; realised reference
price; imbalance buy/sell prices; lineage IDs; source mode; quality; units. Each period
record carries event time, publication/market-available time, delivery-period identity,
`source_available_at`, dataset identifier and lineage. Missing fields are **never**
silently filled — `validate()` returns explicit issues and the engine returns explicit
skip reasons.

The bundled `build_sample_dataset()` is fully deterministic: two vintages per period (a
prior vintage and a materially revised latest vintage — a wind-forecast miss), a
contracted position hedged to the prior p50 (so the revision creates exposure), an
order book around the reference price, and realised generation = latest p50 + a
deterministic per-period deviation (mirroring the live SAMPLE `actual_generation`
model), available only at `delivery_end`. The revision magnitude clears the materiality
threshold for every period; it is **not** tuned to improve outcomes.

## Trader policies

The trader policy is an explicit replay input (no manual UI clicks):

- **MODEL_FOLLOW** — accepts the model recommendation and submits it to the simulator
  (never modifies/rejects). Zero-volume recommendations become a no-trade.
- **NO_ACTION** — rejects every recommendation; provides the no-action comparison path.
- **TIMING_POLICY** — follows the current hedge-timing verdict: `HEDGE_NOW` → accept and
  submit the full "now" volume; `PARTIAL_HEDGE_NOW` → modify to the recommended-now
  volume and submit; `WAIT` → do not submit at that event; `NO_ACTION` → reject.

**Repeated WAIT** — a `WAIT` decision is re-assessed at fixed time-to-gate checkpoints
(`REASSESS_MINUTES_TO_GATE = (90, 60, 30, 10)` minutes before Gate Closure, those after
the decision time). Because urgency rises as the clock nears the gate, a `WAIT` can flip
to `HEDGE_NOW`/`PARTIAL_HEDGE_NOW` and submit. A decision still un-submitted at Gate
Closure is rejected as a realised no-trade. A decision that has already submitted is
**never** re-submitted (duplicate-order guard: at most one simulated order per episode).

## Event loop

A deterministic, event-driven clock is injected explicitly; wall-clock `now()` is never
read inside the engine. The event schedule is the sorted union of: the decision time
(latest vintage publication), the TIMING_POLICY reassessment checkpoints, each period's
Gate Closure, and each period's `delivery_end`. At each event the engine (idempotently)
creates decisions + applies the policy (decision time only), reassesses waiting
decisions, enforces Gate Closure, and delivers/settles/evaluates completed periods. The
same dataset + config always produces identical output (verified by test).

## Replay isolation

Each run uses fully isolated stores (its own `DecisionStore`, orchestrator,
hedge-timing, execution, settlement and evaluation services). A replay never mutates the
global cockpit singletons (`DECISIONS`, `SETTLEMENT`, `EVALUATION`, `ROLLING`) — verified
by test.

## Execution modes

`IDEAL`, `REALISTIC`, `STRESS` are selectable and stored on the run. Results across
modes are only compared with the mode clearly identified (it is shown on the run header
and stored on `ReplayRun.execution_mode`).

## Benchmark treatment and perfect-foresight limitations

The existing per-decision benchmarks are reused: NO_ACTION (baseline), MODEL_RECOMMENDATION
(explicitly-labelled IDEAL execution), TRADER_INSTRUCTION (the realised decision), and
PERFECT_FORESIGHT. **Perfect foresight is hindsight-only, unattainable and an upper
bound.** It is never folded into an "achievable strategy return"; it is kept as a
separate cumulative series and labelled `HINDSIGHT UPPER BOUND — NOT ATTAINABLE`. The
perfect-foresight capture ratio (`trader incremental / perfect incremental`) returns
`None` with an explanatory note when the denominator is zero or negative.

## Metric definitions

All metrics are incremental **versus NO_ACTION** unless named `cashflow`, carry an
explicit denominator, and exclude skipped episodes from every denominator. `sample_size`
accompanies every aggregate.

- **Coverage** — eligible periods, periods with valid revisions, material decisions,
  submitted, filled/partial/expired, evaluated, skipped, action rate (submitted /
  material; `None` if 0). Decision counts and settlement-period counts are not mixed.
- **P&L** — total/mean/median/stdev/min/max incremental P&L (denominator = evaluated
  episodes), total realised cash flow, total fees, total slippage, cumulative series.
- **Hit & regret** — % out/under/in-line vs no action, mean regret vs model and vs
  perfect foresight, perfect-foresight capture ratio (zero/negative denominator → `None`).
- **Risk** — max drawdown of cumulative incremental P&L, worst single-period loss,
  downside deviation, 5th-percentile outcome, loss frequency. This is a descriptive
  diagnostic on a tiny SAMPLE, **not** a statistically robust VaR study.
- **Execution** — fill rate, partial-fill rate, average slippage, average fee, average
  levels consumed, volume-weighted execution price (denominator = submitted episodes).
- **Timing** — HEDGE_NOW / PARTIAL_HEDGE_NOW / WAIT / NO_ACTION counts, outcomes by
  verdict and by priority.
- **Segmentation** — typed breakdowns by forecast-horizon bucket, settlement-period
  bucket, timing verdict, priority and recommended action (not all shown in the UI).

### Statistical caution

For SAMPLE_REPLAY the UI shows descriptive diagnostics only. No annualised Sharpe,
annualised returns, confidence intervals, p-values or significance claims are produced —
the sample is far too small to justify them.

## SAMPLE vs HISTORICAL boundaries

`SAMPLE_REPLAY` uses the deterministic SAMPLE dataset and is never labelled historical.
`HISTORICAL_REPLAY` is rejected (HTTP 422) unless a genuine historical dataset is
registered; none is bundled. Every result carries run mode, source mode, quality,
dataset id, replay interval, integrity status, `diagnostic_only`, `not_executable` and
`trustworthy_for_live_trading = false`.

## API routes

`POST /api/v1/replay-runs`, `GET /api/v1/replay-runs`, `GET /api/v1/replay-runs/{id}`,
`GET …/{id}/episodes`, `GET …/{id}/metrics`, `GET …/{id}/cumulative-pnl`,
`GET /api/v1/replay-datasets`. Idempotency: same key + same canonical request → the
existing run; same key + different request → HTTP 409; a run without a key may create a
new run. Bounded runs: `max_periods` (default 200); exceeding it returns HTTP 422.

## Known limitations

- The SAMPLE dataset is a small, deterministic single regime; results are illustrative
  diagnostics, not evidence of strategy quality, and carry no statistical significance.
- Execution realism is assumption-driven (the SAMPLE order book and simulator), not
  calibrated to any real exchange.
- Only one bundled dataset (SAMPLE); HISTORICAL_REPLAY needs a real historical dataset
  adapter, which is out of scope here.
- Perfect foresight is an unattainable hindsight upper bound, never an achievable return.
- Storage is in-memory per process (no persistence); the model/optimiser recommendation
  in the SAMPLE dataset is a documented deterministic hedging rule, while all downstream
  economics (execution, settlement, benchmarks) are the production services.
