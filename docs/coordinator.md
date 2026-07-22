# Integrated Coordinator

Milestone 1G is the first decision-support layer. It compares six candidate actions using the
same immutable cockpit snapshot and the existing Forecast & Position, Market & Liquidity,
Sequential Battery Path, Battery Opportunity Cost, and BM / ancillary Optionality layers.

It does not submit orders, control the battery, persist recommendations, or represent sample
data as live. Every recommendation is labelled **Diagnostic recommendation** and **Not
executable**.

## Sign conventions

Pre-action exposure is positive when long and negative when short. For a candidate period:

```text
residual scenario exposure
  = pre-action scenario exposure
  + battery net export
  - signed market trade
```

- battery net export is discharge MWh minus charge MWh;
- a market sell is a positive signed trade and reduces a long position;
- a market buy is a negative signed trade and reduces a short position;
- sell cashflow is positive, so it enters the cost objective as a negative cost;
- buy cashflow is negative, so it enters the objective as a positive cost.

The sign convention is recorded in the transformation lineage for residual exposure and market
execution cost.

## Candidate actions

| Candidate | Market diagnostic | Battery path |
|---|---|---|
| No action | None | No action |
| Market-only hedge | Executable hedge of the selected confidence scenario | No action |
| Battery-only P50 coverage | None | Sequential P50 coverage |
| Battery preserve-flexibility | None | Sequential 25% preserve-flexibility path |
| Market + battery hybrid | Executable hedge after the selected standard battery path | User-selected standard path |
| Optionality-preserving action | Half hedge after limited battery use | Preserve-flexibility path |

The optionality-preserving action is a transparent heuristic candidate, not an optimiser. A
configured per-period market cap is applied to market candidates before the order book is swept.

## Diagnostic objective

The coordinator ranks candidates by the lowest horizon cost:

```text
total diagnostic cost
  = market execution cost
  + scenario-weighted expected imbalance cost
  + tail-risk penalty
  + battery opportunity cost
  + optionality-loss weight × optionality lost
  + committed-service risk penalty
```

P10/P50/P90 expected imbalance weights are currently 25%/50%/25%. Tail risk is the configured
weight times the larger absolute P10/P90 residual times the imbalance-price assumption. Battery
opportunity cost comes from the Battery Flexibility layer. Optionality loss and service
non-delivery risk come from the Optionality layer. These simple terms are deliberately visible
and lineage-bearing.

## Readiness and trust

- `READY`: Forecast & Position, executable Market & Liquidity, Sequential Battery Path,
  Optionality, and required assumptions are all LIVE/FRESH/VALID.
- `DEGRADED`: calculations are possible, but at least one required layer or assumption is sample,
  synthetic, latest-available, stale, or otherwise not live-trading trustworthy.
- `BLOCKED`: a critical layer is missing/invalid, executable bid/ask data is unavailable, the
  confidence/path input is invalid, or sample market data exists without explicit sample-mode
  selection.

`calculation_allowed`, `trustworthy_for_live_trading`, `diagnostic_only`, and
`executable_live_ready` remain separate. Milestone 1G always reports `diagnostic_only=true` and
`executable_live_ready=false`.

Elexon MID/reference data is never treated as an executable order book. The labelled sample order
book is only used when `explicit_sample_market=true`; it remains `SAMPLE` throughout derived
lineage.

## API

- `GET /api/v1/coordinator` builds the coordinator for the current cockpit snapshot.
- `GET /api/v1/coordinator/{snapshot_id}` deterministically rebuilds it for an in-memory snapshot.
- `POST /api/v1/coordinator/simulate` accepts imbalance price, tail/optionality weights,
  per-period market cap, selected standard battery path, confidence scenario, and explicit sample
  market selection.

The coordinator output is not persisted. Derived scores, cost components, market WAPs, volumes,
battery actions, and residuals are registered with the existing lineage endpoint.

## Sensitivities

The page shows one-factor screening changes for ask price, bid depth, lower SoC, doubled
optionality value, greater P10 emphasis, and a missing market feed. These are approximate
counterfactual re-scores, not full candidate re-simulations.

## Current limitations

- The objective is a transparent heuristic rather than a mathematical co-optimiser.
- Scenario probabilities and imbalance prices are assumptions, not calibrated distributions.
- Market depth is a point-in-time displayed sample book; queue position and fill uncertainty are
  not modelled.
- Battery and optionality candidates use the existing three standard sequential paths.
- Sensitivities use one-factor approximations.
- There is no trader accept/modify/reject workflow, audit persistence, order submission, or battery
  control in this milestone.
