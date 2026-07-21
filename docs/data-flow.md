# Milestone 1A data flow

## Pipeline

Every refresh follows the same visible stages:

```text
source feed
  -> ingestion attempt
  -> raw payload status
  -> normalisation
  -> validation
  -> canonical data point
  -> cockpit snapshot
  -> optimiser readiness
```

Each stage emits a `DataFlowEvent`. A failed live adapter records `ERROR` and stops;
it never calls a sample or synthetic adapter. If a previously successful live value
exists, the value may remain visible as `LATEST_AVAILABLE / STALE`, with the refresh
failure attached as a warning.

## Status dimensions

Source mode, semantic meaning, and quality are deliberately independent:

- source mode: `LIVE`, `LATEST_AVAILABLE`, `SAMPLE`, `SYNTHETIC`, `ERROR`;
- semantic kind: `OBSERVATION`, `FORECAST`, `ESTIMATE`, `ASSUMPTION`;
- quality: `FRESH`, `STALE`, `PARTIAL`, `MISSING`, `REVISED`, `INVALID`.

`LATEST_AVAILABLE` describes a real prior observation that is older than its SLA or
whose latest refresh failed. It does not mean sample or synthetic.

## Readiness

Snapshot readiness and optimiser readiness are separate.

The cockpit snapshot is:

- `BLOCKED` when forecast, contracted position, or battery state is missing/invalid;
- `DEGRADED` when those core display inputs exist but contextual or optimiser inputs
  are stale/missing;
- `READY` when all configured required inputs are fresh and valid.

The optimiser is:

- `BLOCKED` when any required optimiser feed is missing, especially executable
  bid/ask and depth;
- `DEGRADED` when required inputs exist but are stale and require review;
- `READY` only when all required inputs are present, fresh, and valid.

Milestone 1A intentionally has an unconfigured executable market adapter. The
snapshot is therefore visible as `DEGRADED`, while the optimiser is `BLOCKED`.

## Persistence boundary

The first vertical slice stores events, attempts, canonical values, and snapshots in
process memory. This makes the contracts and UI inspectable before a database schema
is frozen. Restarting the backend clears prior events and snapshot history; durable
PostgreSQL persistence is a known next hardening step.
