# Rolling intraday optimisation cockpit

## Product structure

The main product routes are `/live` and `/optimisation`. The earlier vertical slices are retained
under `/diagnostics`; they use the current rolling cockpit snapshot and remain useful for tracing
individual inputs and calculations.

No endpoint submits an order or controls a battery. In SAMPLE mode every page states:

> SAMPLE simulation assumes previous model actions are followed. This is not real execution or live control.

## State lifecycle

The backend clock is authoritative. The in-memory `SimulatedEnvironment` owns current SoC,
contracted positions, regime, forecast/market version identifiers, rolling observation history
and the event tape. Each manual or scheduled refresh:

1. reads backend time and derives the current DST-aware GB settlement period;
2. reconciles every completed delivery period from the latest immutable SAMPLE run;
3. carries projected SoC and suggested market trades into current SoC and Q;
4. appends simulated actuals to the rolling historical tape;
5. generates a new forecast vintage and order book from history up to now; and
6. creates a new immutable optimisation run over the selected future horizon.

The main UI has no manual time-advance control. The legacy `/live-state/advance` endpoint remains
only as a developer compatibility hook; it is not part of the product flow.

Horizon modes are `next_8_periods`, `end_of_day`, and `next_auction`. No auction calendar is
configured yet, so explicitly selecting `next_auction` produces a visible warning and uses the next
eight settlement periods. Gate-Closed periods remain available to battery/residual/service logic,
but their bid/ask decision bounds are zero.

The evolved canonical points are published as the current `CockpitSnapshot`, so Forecast &
Position, Market & Liquidity, Battery Flexibility, Battery Path and Optionality diagnostics inspect
the same rolling state.

## Full action-space MILP

For each period the model solves non-negative `buy`, `sell`, `charge`, `discharge`,
`reserve_up`, `reserve_down`, `residual_long` and `residual_short`, with SoC nodes and binary
charge/discharge and buy/sell direction variables.

```text
residual_long[s,t] - residual_short[s,t]
  = G[s,t] + (discharge[t] - charge[t]) * dt
    + buy[t] - Q[t] - sell[t]

soc[t+1]
  = soc[t] + eta_c * charge[t] * dt
    - discharge[t] * dt / eta_d
```

The formulation includes:

- mutually exclusive charge/discharge and buy/sell directions;
- energy, power, grid import/export and optional ramp limits;
- upward/downward power headroom and energy-duration constraints;
- service commitment coverage with an explicit penalised shortfall variable;
- minimum and preferred terminal SoC;
- maximum discharge-throughput cycles;
- Gate Closure non-tradability;
- bid-side slices for sells and ask-side slices for buys, bounded by visible level depth.

The objective maximises diagnostic value:

```text
market execution value
- expected scenario imbalance cost
- P10/P90 tail-risk penalty
- battery degradation cost
+ upward/downward availability value
+ expected BM activation value
+ optionality preservation value
- service non-delivery risk
+ terminal SoC value
- terminal preferred-band deviation
```

Expected BM activation and service value remain estimates and are never presented as guaranteed
revenue.

## Readiness

The explicit SAMPLE environment is fresh and internally calculable, so
`calculation_allowed=true`. It remains `DEGRADED`, `diagnostic_only=true`,
`executable_live_ready=false`, and `trustworthy_for_live_trading=false`.

There is no silent fallback from a missing live source to SAMPLE or SYNTHETIC. SAMPLE mode is a
separate explicit product state. Elexon MID/reference prices are not accepted as executable market
depth.

## API

- `GET /api/v1/live-state`
- `POST /api/v1/live-state/refresh`
- `POST /api/v1/live-state/advance`
- `POST /api/v1/live-state/reset`
- `POST /api/v1/live-state/regime`
- `POST /api/v1/live-state/horizon`
- `GET /api/v1/optimisation/current`
- `POST /api/v1/optimisation/run`
- `GET /api/v1/optimisation/runs`
- `GET /api/v1/optimisation/runs/{run_id}`

Runs and rolling state are in memory only.
