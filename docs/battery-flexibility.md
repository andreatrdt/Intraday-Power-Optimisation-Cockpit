# Battery Flexibility & Opportunity Cost

Milestone 1D is a descriptive physical-feasibility slice. It does not optimise a
multi-period schedule, recommend a dispatch, value BM or ancillary services, or
submit a battery control instruction.

## Inputs and units

The current development setup keeps telemetry and configuration separate:

- `battery_telemetry_sample` supplies current stored energy in MWh;
- `battery_config_sample` supplies sample energy limits, power limits,
  efficiencies, reserve duration, terminal target and cost assumptions;
- `service_commitments_sample` supplies upward and downward MW reservations;
- the Forecast & Position layer supplies P10/P50/P90 pre-action exposure.

All sample values remain `SAMPLE` through derived calculations. No missing live
value is replaced with synthetic data.

Charge and discharge are grid-side energy. For a settlement-period duration
`dt`, stored energy follows:

```text
E[t+1] = E[t] + eta_c * charge_MW * dt
                  - discharge_MW * dt / eta_d
```

The physical layer preserves the labelled service reservations when calculating
available headroom. The reserved energy-duration floor and ceiling are:

```text
reserve floor   = E_min + U * h / eta_d
reserve ceiling = E_max - eta_c * D * h
```

Maximum charge/discharge is the lesser of the remaining power headroom over the
period and the corresponding energy/space limit.

## Exposure coverage

- Positive exposure is long. Charging can absorb it.
- Negative exposure is short. Discharging can cover it.
- Maximum support is capped by deterministic physical feasibility.
- Residual exposure is shown after that maximum support.

This is a capability envelope, not a recommendation. Every displayed period is
evaluated independently from the current SoC; the maxima cannot be repeated as a
multi-period schedule without a sequential optimisation or dispatch calculation.

## Opportunity-cost heuristic

The displayed cost of one MWh of charge or discharge combines:

1. a degradation assumption;
2. the incremental terminal-SoC shortfall penalty;
3. a future-flexibility penalty scaled by the corresponding reservation fraction.

It deliberately excludes market energy price, imbalance price, BM expected value
and ancillary-service value. Its purpose is to make preservation assumptions
visible before the integrated optimiser is built.

## Readiness

- `READY`: telemetry, limits and reservations are fresh, live and internally valid.
- `DEGRADED`: inputs are stale or sample-labelled but physical calculation is valid.
- `BLOCKED`: required telemetry/configuration is missing or invalid, SoC is outside
  bounds, or labelled reservations cannot be sustained.

Forecast/position absence degrades exposure coverage but does not erase otherwise
valid physical feasibility.

## API

- `GET /api/v1/battery-flexibility`
- `GET /api/v1/battery-flexibility/{snapshot_id}`
- `GET /api/v1/batteries/current`
- `GET /api/v1/lineage/{value_id}` for telemetry, configuration, feasibility,
  coverage residuals and opportunity-cost values
