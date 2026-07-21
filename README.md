# Intraday Power Optimisation Cockpit

Milestones 1A through 1F provide visible, inspectable data-flow, Forecast & Position,
Market & Liquidity, Battery Flexibility, and sequential Battery Path vertical slices
for a future UK intraday power trading cockpit.
They deliberately do **not** contain a backtester, replay engine,
strategy-performance page, battery co-optimiser, or trade recommendation engine.

The current application shows:

- feed-by-feed ingestion and health;
- explicit `LIVE`, `LATEST_AVAILABLE`, `SAMPLE`, `SYNTHETIC`, and `ERROR` modes;
- semantic kind and quality independently from source mode;
- raw-to-canonical transformation and validation lineage;
- the immutable input set used by the current cockpit snapshot;
- separate snapshot and optimiser readiness decisions;
- an event timeline for refresh, normalisation, validation, and snapshot building.
- latest, previous, and day-ahead forecast vintages by GB settlement period;
- P10/P50/P90 renewable scenarios, forecast deltas, and reliability diagnostics;
- contracted position `Q_t` and pre-action scenario exposure `I_t^s = G_t^s - Q_t`;
- period risk ranking, plain-English explanation, and Forecast & Position readiness.
- explicit sample bid/ask order books, executable WAP, unfilled depth and hedge cashflow;
- Gate Closure timing, liquidity scoring, and market-specific readiness.
- current SoC, physical limits, service reservations and deterministic period feasibility;
- maximum directional exposure coverage, binding constraints and residual exposure;
- a transparent battery opportunity-cost heuristic with inspectable assumptions.
- sequential SoC propagation for standard and user-edited candidate battery paths;
- per-period path violations, binding constraints, residual scenario exposure and
  terminal-SoC consequences;
- explicit comparison of no-action, P50-coverage and preserve-flexibility paths.
- committed-service deliverability and reserved-duration diagnostics by battery path;
- probability-weighted BM and ancillary optionality estimates, with non-delivery risk,
  activation opportunity cost and explicit non-guaranteed-value labels.

Live failures are never replaced by synthetic data. Sample and synthetic feeds are
only loaded through their explicitly named adapters.

## Project layout

```text
backend/   FastAPI data-flow API and pipeline
frontend/  React/TypeScript Data Flow cockpit
```

## Run locally

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn cockpit.api:app --reload --port 8000
```

Open API documentation at <http://localhost:8000/docs>.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173/data-flow> or
<http://localhost:5173/forecast-position> or
<http://localhost:5173/market-liquidity> or
<http://localhost:5173/battery-flexibility> or
<http://localhost:5173/battery-path> or
<http://localhost:5173/optionality>.

The frontend uses `http://127.0.0.1:8000/api/v1` by default. Override the API root
with `VITE_API_BASE_URL` when required. The Vite server also includes a local
`/api` proxy for same-origin development setups.

## Initial feed policy

| Feed | Initial mode | Snapshot role |
|---|---|---|
| Elexon/BMRS system frequency | Live adapter, not yet refreshed | Optional system context |
| NESO system dataset discovery | Live adapter, not yet refreshed | Optional system context |
| Renewable forecast | Sample | Required cockpit input |
| Portfolio contracted position | Sample | Required cockpit input |
| Battery telemetry | Sample | Required cockpit input |
| Battery operating limits | Sample | Required cockpit input |
| Intraday executable market | Error / unconfigured | Required before optimisation |
| Sample intraday order book | Sample | Diagnostic execution logic only |
| Service commitments | Sample | Optional context |
| BM/service optionality assumptions | Sample | Diagnostic valuation only |
| Synthetic demo | Synthetic, not loaded | Excluded unless explicitly refreshed |

Consequently, the initial cockpit snapshot is `DEGRADED`, while optimiser readiness
is `BLOCKED` because executable intraday prices are unavailable.

## Verification

```powershell
cd backend
pytest

cd ..\frontend
npm run typecheck
npm run build
```

See [docs/data-flow.md](docs/data-flow.md) for the ingestion pipeline and
[docs/forecast-position.md](docs/forecast-position.md) for the Milestone 1B
calculation and readiness rules, [docs/market-liquidity.md](docs/market-liquidity.md)
for Milestone 1C, and [docs/battery-flexibility.md](docs/battery-flexibility.md)
for Milestone 1D, [docs/battery-path.md](docs/battery-path.md) for Milestone 1E,
and [docs/optionality.md](docs/optionality.md) for Milestone 1F.
