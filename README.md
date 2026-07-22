# Intraday Power Optimisation Cockpit

The product is a rolling UK intraday decision-support cockpit with two primary pages:

- **Live Market State** (`/live`) shows what is changing now.
- **Rolling Optimisation** (`/optimisation`) solves and explains the suggested future path.

The earlier layer pages remain available under **Diagnostics** (`/diagnostics`) and consume the
current rolling-state snapshot. The application deliberately contains no backtester, replay UI,
strategy-performance page, order submission, real battery control, or trader-action workflow.

The rolling product now provides:

- an evolving explicit SAMPLE environment with normal, tightening, oversupply, price-spike,
  wind-miss and demand-surprise regimes;
- backend-time refreshes that derive the current UK settlement period, append rolling history,
  create new forecast vintages/order books and reconcile completed SAMPLE actions;
- a chronological data tape for forecast, market, production, demand, frequency, SoC, Q and
  optimisation events;
- a full-action HiGHS MILP over buy, sell, charge, discharge, SoC, upward/downward reserve and
  scenario residual long/short variables;
- level-slice executable bid/ask depth, WAP, Gate Closure, battery physics, reserve duration,
  service commitments, degradation, terminal value, BM expected value and tail risk;
- immutable in-memory optimisation runs with configurable next-eight-SP, end-of-day and explicit
  next-auction-fallback horizons;
- graph-led live-state history, forecast-vintage, order-book, system, portfolio and battery views;
- large optimisation action, focused SoC, reserve, exposure-fan, execution, objective, driver and
  sensitivity charts with units, hover values and backend-owned risk measures;
- explicit `calculation_allowed`, `trustworthy_for_live_trading`, diagnostic-only and
  non-executable semantics.

Supporting diagnostics retain:

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
- six integrated market/battery candidate actions with scenario residual exposure;
- transparent execution, imbalance, tail-risk, battery opportunity, optionality and service-risk
  cost terms;
- a ranked **Diagnostic recommendation** that is explicitly **Not executable** and separately
  reports calculation permission and live-trading trust;
- one-factor counterfactual diagnostics for market, SoC, optionality and tail-risk changes.

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

Open <http://localhost:5173/live>. The main optimisation page is
<http://localhost:5173/optimisation>; technical layer views are under
<http://localhost:5173/diagnostics>.

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

Consequently, the explicit SAMPLE rolling state is `DEGRADED`, calculation is allowed, and
`trustworthy_for_live_trading` is false. The SAMPLE order book is never represented as live.
Elexon MID/reference data is never used as executable depth.

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
See [docs/coordinator.md](docs/coordinator.md) for the Milestone 1G scoring,
sign conventions, readiness and API contract.
See [docs/rolling-optimisation.md](docs/rolling-optimisation.md) for the rolling state lifecycle,
full action-space formulation, backend-time SAMPLE reconciliation and product APIs.
