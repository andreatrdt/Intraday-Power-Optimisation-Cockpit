# BM & Ancillary Optionality Diagnostics

Milestone 1F estimates how diagnostic battery paths affect committed-service
deliverability and uncertain BM/ancillary value. It does not optimise, recommend,
submit orders or control a battery.

## Obligations versus optional value

Committed upward and downward service are obligations. The configured MW and
required duration must remain power-and-energy deliverable after every candidate
path action.

BM optionality is different: it represents a possible future activation whose
probability, duration and margin are uncertain. Every BM and service value is
therefore labelled as a probability-weighted estimate and not guaranteed revenue.

## Deliverability

For each path period:

```text
upward_power_available = P_discharge_max - y_t
downward_power_available = P_charge_max + y_t

upward_duration = (E_t - E_min) * eta_d / committed_upward_MW
downward_duration = (E_max - E_t) / (eta_c * committed_downward_MW)
```

Power-and-energy deliverable MW is the minimum of power availability and the MW
that can be sustained for the committed service duration. Optional MW is the
remaining deliverable MW after committed capacity is protected.

The commitment coverage ratio is the minimum upward/downward power-and-energy
coverage, capped at 100%. A value below 100% creates an explicit non-delivery
warning and never gets presented as available optionality.

## Heuristic valuation

BM value uses:

```text
gross BM value
  = acceptance probability * expected activation MWh * expected margin

expected BM optionality
  = gross BM value
    - probability-weighted non-delivery risk penalty
    - probability-weighted battery activation opportunity cost
```

Ancillary-service value uses:

```text
service value
  = availability fee * committed MW * settlement-period hours
    + expected activation value
    - committed non-delivery risk penalty
```

The assumptions feed is deliberately `SAMPLE` and `ASSUMPTION`. It includes BM
acceptance probability, BM activation duration and margin, service availability
fee, service activation probability/duration/margin, and separate non-delivery
penalties.

## Path comparisons

The service evaluates:

- no battery action;
- cover P50 exposure;
- preserve flexibility using the visible 25% path assumption;
- user-entered custom charge/discharge paths.

Each path is compared period-by-period with the no-action baseline. The API reports
value before, value after, signed optionality lost, commitment risk, the worst
affected period, and a descriptive explanation.

## Readiness

- `READY`: commitments, assumptions, SoC, limits and path inputs are live, fresh
  and valid. Calculation and live-trading trust are both true.
- `DEGRADED`: sample, stale, latest-available or synthetic inputs are still
  calculable, but live-trading trust is false.
- `BLOCKED`: required commitments, assumptions, SoC, limits or path data are
  missing or invalid.

Source mode is preserved. Synthetic data is never silently relabelled or used as
a fallback for a failed live source.

## API

- `GET /api/v1/optionality` returns standard-path optionality diagnostics.
- `POST /api/v1/optionality/simulate` validates a custom sequential battery path
  and returns its optionality impact alongside the standard comparisons.
- `GET /api/v1/lineage/{value_id}` resolves assumptions, commitments and derived
  optionality values.
