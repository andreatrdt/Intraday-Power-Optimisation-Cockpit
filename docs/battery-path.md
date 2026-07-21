# Sequential Battery Path & What-if Simulator

Milestone 1E propagates battery state sequentially across upcoming settlement
periods. It is an interactive diagnostic simulator, not an optimiser, dispatch
recommendation, BM/service valuation or battery-control interface.

## State propagation

For each period, the ending SoC becomes the next period's starting SoC:

```text
E[t+1] = E[t] + eta_c * charge_MW[t] * dt
                  - discharge_MW[t] * dt / eta_d
```

The simulator reports charge/discharge in both MW and settlement-period MWh.
Positive battery net export reduces a short position or increases a long position:

```text
residual_after[t,s] = exposure_before[t,s]
                      + discharge_MWh[t] - charge_MWh[t]
```

Invalid custom actions are still propagated so the user can see their downstream
consequences. They are explicitly marked invalid and must not be interpreted as
feasible actions.

## Diagnostic paths

- `NO_ACTION`: zero charge and discharge in every period.
- `P50_COVERAGE`: sequentially covers P50 exposure up to physical feasibility.
- `PRESERVE_FLEXIBILITY`: uses 25% of the P50-coverage directional energy.
- `CUSTOM`: uses manually entered charge and discharge MW by period.

The P50 path is a deterministic comparison path, not a recommendation. None of
the standard paths compares expected trading value or selects an optimal action.

## Constraints and violations

Every period checks:

- minimum and maximum SoC;
- charge and discharge power limits;
- simultaneous charge and discharge;
- upward/downward reserved-power headroom;
- upward-service energy duration;
- downward-service empty-capacity duration;
- non-negative charge/discharge inputs.

The service also reports maximum feasible charge/discharge from each sequential
starting state, the first binding constraint, terminal SoC target shortfall and
P10/P50/P90 residual exposure.

## Readiness

- `READY`: telemetry, limits, settlement periods and exposure data are live,
  fresh and valid.
- `DEGRADED`: sample or stale inputs remain calculable.
- `BLOCKED`: SoC, limits, period duration or scenario exposure is missing or invalid.

Input readiness and candidate-path validity are separate. A custom path may have
`DEGRADED` sample readiness while also being `INVALID` because its actions violate
physical constraints.

## API

- `GET /api/v1/battery-paths/comparison`
- `GET /api/v1/battery-paths/standard/{path_name}`
- `POST /api/v1/battery-paths/simulate`
- `GET /api/v1/lineage/{value_id}` for starting/ending SoC, actions, energy,
  headroom, residual exposure and violation observations
