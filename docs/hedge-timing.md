# Hedge timing & decision prioritisation (Milestone 4)

An **explainable timing policy** that turns each material single-period
`TradeDecision` into one of `HEDGE_NOW` / `PARTIAL_HEDGE_NOW` / `WAIT` /
`NO_ACTION`, plus a decomposed priority so a trader is never shown dozens of
equal-priority items.

**What it is not.** It is not "optimal timing", an expected value, an economic
confidence, or a probability of success. It is **not a price forecast and not an
optimal-stopping model** — it does not model future prices, future revisions,
fill probability, imbalance prices, or calibrated forecast reliability. It ranks
and times based only on **currently observable conditions**; when an input is
missing it says so in `warnings` rather than inventing one.

## Modules

| Module | Responsibility |
|---|---|
| [`hedge_timing_models.py`](../backend/src/cockpit/hedge_timing_models.py) | Frozen enums/records: `TimingConfig`, `TimingMarketView`, `TimingRevisionSignals`, `PriorityComponents`, `HedgeTimingAssessment`, `DecisionBatchSummary` |
| [`hedge_timing.py`](../backend/src/cockpit/hedge_timing.py) | Pure `assess_timing(decision, market, signals, config)` — verdict + decomposed priority |
| [`decision_prioritisation.py`](../backend/src/cockpit/decision_prioritisation.py) | Market adapter, dedup store, ranking, batch summaries, `HEDGE_TIMING` singleton |

## Inputs (observable only)

Forecast revision magnitude & **significance** (statistical unusualness, *not*
reliability), materiality reasons, P10/P50/P90 exposure, contracted position Q,
the current recommended buy/sell volume, minutes to Gate Closure, best bid/ask,
spread, visible bid/ask depth, and the executable WAP (and slippage) for the
recommended volume, plus source mode / quality / trust status. Nothing else is
used, and nothing is fabricated.

## Policy rules

Evaluated in order; the first matching rule wins.

**NO_ACTION** when: trust forbids calculation (`calculation_allowed = False`); or
the period is no longer tradeable (Gate Closure passed); or the optimiser
recommendation is `≈ 0`; or `|P50 exposure| ≤ exposure_tolerance`.

Otherwise (actionable):

* **WAIT** if the executable market is unavailable; or the recommendation is
  small versus exposure (`required < small_recommendation_ratio × |P50 exposure|`);
  or Gate Closure is not near (`minutes > gate_closure_near_minutes`).
* Near Gate Closure (`minutes ≤ gate_closure_near_minutes`):
  * **HEDGE_NOW** if depth covers `≥ depth_sufficiency_ratio` of the volume **and**
    spread/slippage are within limits — hedge the full volume now.
  * **PARTIAL_HEDGE_NOW** if depth covers `≥ partial_min_ratio` but not enough for
    a full, clean fill (thin depth or spread/slippage over limit) — hedge the
    executable portion now, defer the remainder.
  * **WAIT** if depth covers `< partial_min_ratio` — too little to hedge
    meaningfully now.

The only transparent "act now" trigger is **Gate-Closure proximity**; without it
`WAIT` is the honest default (we cannot claim now is better than later). The
`recommended_now_*` / `deferred_*` split reflects the verdict: full for
HEDGE_NOW, executable portion for PARTIAL, zero for WAIT/NO_ACTION.

## Thresholds (`TimingConfig`, all configurable)

| Name | Default | Meaning |
|---|---|---|
| `gate_closure_near_minutes` | 45 | "near Gate Closure" for acting now |
| `gate_closure_score_horizon_minutes` | 240 | horizon over which the gate score scales 0→1 |
| `exposure_tolerance_mwh` | 5 | inside this → NO_ACTION |
| `exposure_scale_mwh` | 60 | normalises the exposure/tail components |
| `small_recommendation_ratio` | 0.25 | recommendation small vs exposure → WAIT |
| `max_spread_gbp_per_mwh` | 6 | spread limit for a clean fill |
| `max_slippage_gbp_per_mwh` | 4 | WAP-slippage limit for a clean fill |
| `depth_sufficiency_ratio` | 0.9 | executable/required for HEDGE_NOW |
| `partial_min_ratio` | 0.1 | minimum executable/required for PARTIAL |
| `critical/high/medium/low_threshold` | 0.72/0.55/0.38/0.20 | priority bands on the 0..1 score |
| `top_n_cap` | 8 | max items in a prioritised/top view |
| `policy_version` | `hedge-timing-v1` | part of the assessment dedupe key |

## Priority score decomposition

`priority_score` = `priority_components.weighted_total`, a clamped weighted mean
(0..1) of visible components, each in 0..1:

```
gate_closure   (time pressure)            exposure / tail_exposure (|P50| and worst tail)
significance   (revision unusualness)     direction_flip (exposure crossed zero)
liquidity      (depth × price-quality)    trust_quality (data quality × calc-allowed)
                       − spread_slippage_penalty (overage beyond limits)
```

Band = CRITICAL/HIGH/MEDIUM/LOW/INFORMATIONAL by threshold; `NO_ACTION` is always
`INFORMATIONAL`, and a `HEDGE_NOW` verdict is floored at `MEDIUM` so an act-now
item can never be buried. Every component is on the assessment for inspection.

## Deduplication

Assessment dedupe key:

```
(decision_id, market_snapshot_id, optimisation_run_id, policy_version)
```

Re-assessing an unchanged decision under the same market snapshot, optimiser run
and policy version returns the existing assessment (idempotent). A new market
snapshot, a new optimiser run, or a policy-version bump yields a new assessment.

## Batch prioritisation prevents alert overload

A single material forecast change can produce ~41 decisions. `HedgeTimingService`
ranks them deterministically (priority band, then score, then gate score, then
decision id) and exposes only the top `top_n_cap` (default 8) in prioritised
views and in each `DecisionBatchSummary.top_decision_ids`. **All** decisions and
assessments remain stored and individually retrievable — the cap affects
presentation/ranking only, never storage. `DecisionBatchSummary` reports counts
by priority and by verdict, the periods needing action now / partial / wait /
informational, the capped top IDs, and the affected period range. It carries **no**
aggregate execution, P&L or portfolio exposure, and `DecisionBatch` itself is
unchanged.

## API

| Method & path | Returns |
|---|---|
| `POST /api/v1/decisions/assess-timing` | assess stored decisions (optional `{decision_ids}`); `{ assessment: AssessTimingResult, diagnostic_only, trustworthy_for_live_trading }` |
| `GET /api/v1/hedge-timing-assessments` | all assessments (newest first) |
| `GET /api/v1/hedge-timing-assessments/{id}` | one, or 404 |
| `GET /api/v1/decision-batch-summaries` | one summary per decision batch |
| `GET /api/v1/decision-batch-summaries/{batch_id}` | one, or 404 |

No accept/modify/reject/submit endpoints are added here — trader lifecycle
mutation is a later milestone. Everything is diagnostic-only and non-executable.

## Limitations

* Deterministic rule policy, not a learned or optimising timer; thresholds are
  configuration, not calibrated economics.
* Uses only observable inputs; where the executable market is unavailable for a
  period, it returns `WAIT` with an explicit warning rather than guessing.
* Significance comes from the forecast-revision layer and is statistical
  unusualness, not forecast reliability; when unavailable the component is 0 and
  `significance_available` is False.
* No £ economics: priority is a unitless, decomposed score — not an expected
  value or probability.
