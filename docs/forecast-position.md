# Forecast & Position vertical slice

Milestone 1B is descriptive and diagnostic. It does not recommend trades, dispatch
a battery, value BM or ancillary-service optionality, or run an optimiser.

## Calculation

For each upcoming GB settlement period and forecast scenario:

```text
I_t^s = G_t^s - Q_t
```

- `G_t^s` is renewable generation in MWh for P10, P50, or P90.
- `Q_t` is the existing contracted net-export position in MWh.
- positive `I_t^s` is long; negative is short; values within 0.05 MWh are flat.
- a forecast supplied as average MW is multiplied by the 0.5-hour settlement
  duration before exposure is calculated.

Every forecast delta and scenario exposure is a canonical derived value. It keeps
the source modes and worst input quality of its parents, the cockpit snapshot ID,
the expression used, validation checks, timestamps, and warnings. A sample input
therefore produces a `SAMPLE` exposure; it is never promoted to live.

## Readiness

- `READY`: all P10/P50/P90 and `Q_t` inputs are present, fresh, live, aligned, and
  internally consistent.
- `DEGRADED`: the calculation is possible, but an input is stale or explicitly
  non-live such as `SAMPLE` or `LATEST_AVAILABLE`.
- `BLOCKED`: a forecast scenario or `Q_t` is missing, invalid, or inconsistent.

`calculation_allowed` and `trustworthy_for_live_trading` are separate fields. A
sample calculation can be valid and allowed while still being untrustworthy for
live trading.

## API

- `GET /api/v1/forecast-position`
- `GET /api/v1/forecast-position/{snapshot_id}`
- `GET /api/v1/forecasts/current`
- `GET /api/v1/positions/current`
- `GET /api/v1/lineage/{value_id}` for both input and derived values

The UI polls the combined endpoint every five seconds and opens the existing
lineage drawer for forecast, contracted-position, delta, and exposure values.
