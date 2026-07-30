"""FastAPI surface for data-flow and forecast-position diagnostics."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from cockpit.battery_layer import build_battery_flexibility
from cockpit.battery_path_layer import build_standard_path_comparison, simulate_battery_path
from cockpit.coordinator_layer import build_coordinator_snapshot
from cockpit.forecast_layer import build_forecast_layer
from cockpit.market_layer import build_market_snapshot
from cockpit.models import (
    BatteryPathInput,
    CoordinatorSimulationInput,
    HorizonRequest,
    RefreshRequest,
    RegimeRequest,
)
from cockpit.decision_lifecycle_models import (
    AcceptRequest,
    DelayRequest,
    ModifyRequest,
    RejectRequest,
    ReopenRequest,
)
from cockpit.decision_orchestrator import ORCHESTRATOR
from cockpit.decision_prioritisation import HEDGE_TIMING
from cockpit.decision_service import DECISIONS, DecisionValidationError, StaleDecisionError
from cockpit.decision_state_machine import InvalidTransitionError, StagePayloadError
from cockpit.execution_models import SubmitSimulatedRequest
from cockpit.execution_service import EXECUTION, IdempotencyConflictError
from cockpit.evaluation_service import EVALUATION
from cockpit.hedge_timing_models import AssessTimingRequest
from cockpit.settlement_models import LifecycleActionRequest
from cockpit.settlement_service import (
    SETTLEMENT,
    IdempotencyConflictError as SettlementIdempotencyConflictError,
    SettlementInputError,
)
from cockpit.replay_engine import ReplayLimitExceeded
from cockpit.replay_models import ReplayCreateRequest
from cockpit.replay_service import REPLAY, ReplayIdempotencyConflictError, ReplayValidationError
from cockpit.optionality_layer import build_optionality_snapshot
from cockpit.pipeline import PIPELINE
from cockpit.position_layer import build_forecast_position
from cockpit.rolling_service import ROLLING


@asynccontextmanager
async def lifespan(_: FastAPI):
    await PIPELINE.bootstrap()
    ROLLING.initialise()
    yield


app = FastAPI(
    title="Intraday Power Optimisation Cockpit",
    version="0.8.0",
    description="Rolling non-executable UK intraday power optimisation cockpit",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rolling_snapshot_context(request, call_next):
    if ROLLING._initialised and request.url.path.startswith("/api/v1/"):
        ROLLING.ensure_published()
    return await call_next(request)


@app.get("/api/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "milestone": "rolling-optimisation-cockpit"}


@app.get("/api/v1/live-state", tags=["rolling-cockpit"])
def live_state() -> dict:
    return {"live_state": ROLLING.live_state()}


@app.post("/api/v1/live-state/refresh", tags=["rolling-cockpit"])
def refresh_live_state() -> dict:
    live = ROLLING.refresh()
    return {"live_state": live, "optimisation": ROLLING.current_optimisation()}


@app.post("/api/v1/live-state/advance", tags=["rolling-cockpit"])
def advance_live_state() -> dict:
    live, run = ROLLING.advance()
    return {"live_state": live, "optimisation": run}


@app.post("/api/v1/live-state/reset", tags=["rolling-cockpit"])
def reset_live_state() -> dict:
    live, run = ROLLING.reset()
    return {"live_state": live, "optimisation": run}


@app.post("/api/v1/live-state/regime", tags=["rolling-cockpit"])
def change_live_regime(request: RegimeRequest) -> dict:
    live = ROLLING.set_regime(request.regime)
    return {"live_state": live, "optimisation": ROLLING.current_optimisation()}


@app.post("/api/v1/live-state/horizon", tags=["rolling-cockpit"])
def change_horizon(request: HorizonRequest) -> dict:
    live = ROLLING.set_horizon_mode(request.mode)
    return {"live_state": live, "optimisation": ROLLING.current_optimisation()}


@app.get("/api/v1/optimisation/current", tags=["rolling-optimisation"])
def current_optimisation() -> dict:
    return {"optimisation": ROLLING.current_optimisation()}


@app.post("/api/v1/optimisation/run", tags=["rolling-optimisation"])
def run_optimisation() -> dict:
    return {"optimisation": ROLLING.run(), "live_state": ROLLING.live_state()}


@app.get("/api/v1/optimisation/runs", tags=["rolling-optimisation"])
def optimisation_runs() -> dict:
    return {"runs": ROLLING.list_runs()}


@app.get("/api/v1/optimisation/runs/{run_id}", tags=["rolling-optimisation"])
def optimisation_run(run_id: str) -> dict:
    run = ROLLING.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown optimisation run '{run_id}'")
    return {"optimisation": run}


@app.get("/api/v1/data-sources/health", tags=["data-flow"])
def data_source_health() -> dict:
    return {"feeds": PIPELINE.all_health()}


@app.get("/api/v1/data-flow/events", tags=["data-flow"])
def data_flow_events(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return {
        "events": PIPELINE.recent_events(limit),
        "attempts": PIPELINE.recent_attempts(limit),
    }


@app.post("/api/v1/data-sources/{source}/refresh", tags=["data-flow"])
async def refresh_source(source: str, request: RefreshRequest | None = None) -> dict:
    if source not in PIPELINE.adapters:
        raise HTTPException(status_code=404, detail=f"Unknown feed '{source}'")
    payload = request or RefreshRequest()
    attempt, feed, snapshot = await PIPELINE.refresh(
        source, include_in_snapshot=payload.include_in_snapshot
    )
    return {"attempt": attempt, "feed": feed, "snapshot": snapshot}


@app.get("/api/v1/snapshots/current", tags=["snapshots"])
def current_snapshot() -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    return {"snapshot": PIPELINE.current_snapshot}


@app.get("/api/v1/snapshots/{snapshot_id}", tags=["snapshots"])
def snapshot_by_id(snapshot_id: str) -> dict:
    snapshot = PIPELINE.snapshots.get(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Unknown snapshot '{snapshot_id}'")
    return {"snapshot": snapshot}


@app.get("/api/v1/lineage/{value_id}", tags=["lineage"])
def lineage(value_id: str) -> dict:
    point = PIPELINE.lineage_index.get(value_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"Unknown value '{value_id}'")
    age_seconds = max(
        0.0,
        (PIPELINE.current_snapshot.as_of - point.lineage.retrieved_at).total_seconds()
        if PIPELINE.current_snapshot
        else 0.0,
    )
    return {"value": point, "age_seconds": age_seconds}


def _forecast_position_result(snapshot_id: str | None = None):
    snapshot = (
        PIPELINE.snapshots.get(snapshot_id)
        if snapshot_id is not None
        else PIPELINE.current_snapshot
    )
    if snapshot is None:
        detail = (
            f"Unknown snapshot '{snapshot_id}'"
            if snapshot_id is not None
            else "No cockpit snapshot has been built"
        )
        raise HTTPException(status_code=404 if snapshot_id else 503, detail=detail)
    result = build_forecast_position(snapshot)
    for point in result.derived_values:
        PIPELINE.lineage_index[point.value_id] = point
    return result


@app.get("/api/v1/forecast-position", tags=["forecast-position"])
def forecast_position() -> dict:
    """Return the complete descriptive Forecast & Position vertical slice."""
    return {"forecast_position": _forecast_position_result().snapshot}


@app.get("/api/v1/forecast-position/{snapshot_id}", tags=["forecast-position"])
def forecast_position_by_snapshot(snapshot_id: str) -> dict:
    return {"forecast_position": _forecast_position_result(snapshot_id).snapshot}


@app.get("/api/v1/forecasts/current", tags=["forecast-position"])
def current_forecast() -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    result = build_forecast_layer(PIPELINE.current_snapshot)
    derived = [
        value
        for point in result.points
        for value in (
            point.delta.versus_previous_value,
            point.delta.versus_day_ahead_value,
        )
        if value is not None
    ]
    for point in derived:
        PIPELINE.lineage_index[point.value_id] = point
    return {
        "as_of": PIPELINE.current_snapshot.as_of,
        "latest_vintage": result.latest_vintage,
        "previous_vintage": result.previous_vintage,
        "forecasts": result.points,
        "missing_periods": result.missing_periods,
        "warnings": result.warnings,
    }


@app.get("/api/v1/positions/current", tags=["forecast-position"])
def current_position() -> dict:
    result = _forecast_position_result().snapshot
    return {
        "as_of": result.as_of,
        "position_version": result.position_version,
        "positions": [period.position for period in result.periods],
        "exposures": [
            {
                "delivery_period": period.delivery_period,
                "risk_rank": period.risk_rank,
                "base_case_direction": period.base_case_direction,
                "scenarios": period.exposures,
            }
            for period in result.periods
        ],
        "readiness": result.readiness,
        "warnings": result.warnings,
    }


def _market_result(snapshot_id: str | None = None):
    snapshot = (
        PIPELINE.snapshots.get(snapshot_id)
        if snapshot_id is not None
        else PIPELINE.current_snapshot
    )
    if snapshot is None:
        detail = (
            f"Unknown snapshot '{snapshot_id}'"
            if snapshot_id is not None
            else "No cockpit snapshot has been built"
        )
        raise HTTPException(status_code=404 if snapshot_id else 503, detail=detail)
    live_status = (
        PIPELINE.health_for("market_intraday").source_mode
        if "market_intraday" in PIPELINE.adapters
        else "ERROR"
    )
    has_live_book = any(
        point.lineage.source_feed == "market_intraday"
        and (point.metric.startswith("market_bid_") or point.metric.startswith("market_ask_"))
        for point in snapshot.values
    )
    active_health_id = "market_intraday" if has_live_book else "market_order_book_sample"
    active_health = (
        PIPELINE.health_for(active_health_id)
        if active_health_id in PIPELINE.adapters
        else None
    )
    result = build_market_snapshot(
        snapshot,
        live_provider_status=live_status,
        active_provider_quality=active_health.quality if active_health else None,
        active_provider_mode=active_health.source_mode if active_health else None,
    )
    for point in result.derived_values:
        PIPELINE.lineage_index[point.value_id] = point
    return result


@app.get("/api/v1/market-liquidity", tags=["market-liquidity"])
def market_liquidity() -> dict:
    return {"market": _market_result().snapshot}


@app.get("/api/v1/market-liquidity/{snapshot_id}", tags=["market-liquidity"])
def market_liquidity_by_snapshot(snapshot_id: str) -> dict:
    return {"market": _market_result(snapshot_id).snapshot}


@app.get("/api/v1/markets/current", tags=["market-liquidity"])
def current_market() -> dict:
    market = _market_result().snapshot
    return {
        "as_of": market.as_of,
        "active_provider": market.active_provider,
        "live_provider_status": market.live_provider_status,
        "source_mode": market.source_mode,
        "quality": market.quality,
        "readiness": market.readiness,
        "levels_considered": market.levels_considered,
        "periods": market.periods,
        "warnings": market.warnings,
    }


def _battery_result(snapshot_id: str | None = None):
    snapshot = (
        PIPELINE.snapshots.get(snapshot_id)
        if snapshot_id is not None
        else PIPELINE.current_snapshot
    )
    if snapshot is None:
        detail = (
            f"Unknown snapshot '{snapshot_id}'"
            if snapshot_id is not None
            else "No cockpit snapshot has been built"
        )
        raise HTTPException(status_code=404 if snapshot_id else 503, detail=detail)
    result = build_battery_flexibility(snapshot)
    for point in result.derived_values:
        PIPELINE.lineage_index[point.value_id] = point
    return result


@app.get("/api/v1/battery-flexibility", tags=["battery-flexibility"])
def battery_flexibility() -> dict:
    return {"battery": _battery_result().snapshot}


@app.get("/api/v1/battery-flexibility/{snapshot_id}", tags=["battery-flexibility"])
def battery_flexibility_by_snapshot(snapshot_id: str) -> dict:
    return {"battery": _battery_result(snapshot_id).snapshot}


@app.get("/api/v1/batteries/current", tags=["battery-flexibility"])
def current_battery() -> dict:
    battery = _battery_result().snapshot
    return {
        "as_of": battery.as_of,
        "source_mode": battery.source_mode,
        "quality": battery.quality,
        "readiness": battery.readiness,
        "current_soc": battery.current_soc,
        "limits": battery.limits,
        "opportunity_cost": battery.opportunity_cost,
        "periods": battery.periods,
        "warnings": battery.warnings,
    }


def _register_path_values(points) -> None:
    for point in points:
        PIPELINE.lineage_index[point.value_id] = point


@app.get("/api/v1/battery-paths/comparison", tags=["battery-paths"])
def battery_path_comparison() -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    result = build_standard_path_comparison(PIPELINE.current_snapshot)
    _register_path_values(result.derived_values)
    return {"comparison": result.comparison}


@app.get("/api/v1/battery-paths/standard/{path_name}", tags=["battery-paths"])
def standard_battery_path(path_name: str) -> dict:
    allowed = {"NO_ACTION", "P50_COVERAGE", "PRESERVE_FLEXIBILITY"}
    normalised = path_name.upper()
    if normalised not in allowed:
        raise HTTPException(status_code=404, detail=f"Unknown standard path '{path_name}'")
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    result = simulate_battery_path(
        PIPELINE.current_snapshot, BatteryPathInput(path_name=normalised)
    )
    _register_path_values(result.derived_values)
    return {"simulation": result.simulation}


@app.post("/api/v1/battery-paths/simulate", tags=["battery-paths"])
def simulate_custom_battery_path(path: BatteryPathInput) -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    custom = path.model_copy(update={"path_name": "CUSTOM"})
    result = simulate_battery_path(PIPELINE.current_snapshot, custom)
    _register_path_values(result.derived_values)
    return {"simulation": result.simulation}


def _optionality_result(path: BatteryPathInput | None = None):
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    result = build_optionality_snapshot(PIPELINE.current_snapshot, path)
    _register_path_values(result.derived_values)
    return result


@app.get("/api/v1/optionality", tags=["optionality"])
def optionality() -> dict:
    return {"optionality": _optionality_result().snapshot}


@app.post("/api/v1/optionality/simulate", tags=["optionality"])
def simulate_optionality_path(path: BatteryPathInput) -> dict:
    custom = path.model_copy(update={"path_name": "CUSTOM"})
    return {"optionality": _optionality_result(custom).snapshot}


def _coordinator_result(
    snapshot_id: str | None = None,
    settings: CoordinatorSimulationInput | None = None,
):
    snapshot = (
        PIPELINE.snapshots.get(snapshot_id)
        if snapshot_id is not None
        else PIPELINE.current_snapshot
    )
    if snapshot is None:
        detail = (
            f"Unknown snapshot '{snapshot_id}'"
            if snapshot_id is not None
            else "No cockpit snapshot has been built"
        )
        raise HTTPException(status_code=404 if snapshot_id else 503, detail=detail)
    has_live_book = any(
        point.lineage.source_feed == "market_intraday"
        and (point.metric.startswith("market_bid_") or point.metric.startswith("market_ask_"))
        for point in snapshot.values
    )
    active_health_id = "market_intraday" if has_live_book else "market_order_book_sample"
    active_health = (
        PIPELINE.health_for(active_health_id)
        if active_health_id in PIPELINE.adapters
        else None
    )
    live_status = (
        PIPELINE.health_for("market_intraday").source_mode
        if "market_intraday" in PIPELINE.adapters
        else "ERROR"
    )
    result = build_coordinator_snapshot(
        snapshot,
        settings,
        live_provider_status=live_status,
        active_provider_quality=active_health.quality if active_health else None,
        active_provider_mode=active_health.source_mode if active_health else None,
    )
    _register_path_values(result.derived_values)
    return result


@app.get("/api/v1/coordinator", tags=["coordinator"])
def coordinator() -> dict:
    return {"coordinator": _coordinator_result().snapshot}


@app.get("/api/v1/coordinator/{snapshot_id}", tags=["coordinator"])
def coordinator_by_snapshot(snapshot_id: str) -> dict:
    return {"coordinator": _coordinator_result(snapshot_id).snapshot}


@app.post("/api/v1/coordinator/simulate", tags=["coordinator"])
def simulate_coordinator(settings: CoordinatorSimulationInput) -> dict:
    return {"coordinator": _coordinator_result(settings=settings).snapshot}


@app.get("/api/v1/decisions", tags=["decisions"])
def list_decisions() -> dict:
    return {"decisions": DECISIONS.list()}


@app.get("/api/v1/decisions/{decision_id}", tags=["decisions"])
def get_decision(decision_id: str) -> dict:
    decision = DECISIONS.get(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"Unknown decision '{decision_id}'")
    return {"decision": decision}


@app.get("/api/v1/decision-batches", tags=["decisions"])
def list_decision_batches() -> dict:
    return {"batches": DECISIONS.list_batches()}


@app.get("/api/v1/decision-batches/{batch_id}", tags=["decisions"])
def get_decision_batch(batch_id: str) -> dict:
    batch = DECISIONS.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Unknown decision batch '{batch_id}'")
    return {"batch": batch}


@app.post("/api/v1/decisions/refresh", tags=["decisions"])
def refresh_decisions() -> dict:
    """Read current rolling state, compute forecast revisions and create only new
    material single-period decisions. Non-executable and diagnostic-only."""
    result = ORCHESTRATOR.refresh()
    return {
        "refresh": result,
        "created": [DECISIONS.get(decision_id) for decision_id in result.created_decision_ids],
        "existing": [DECISIONS.get(decision_id) for decision_id in result.duplicate_decision_ids],
        "batch": DECISIONS.get_batch(result.batch_id) if result.batch_id else None,
        "diagnostic_only": True,
        "trustworthy_for_live_trading": False,
    }


@app.get("/api/v1/forecast-revisions", tags=["decisions"])
def list_forecast_revisions() -> dict:
    return {"revisions": ORCHESTRATOR.revisions()}


@app.get("/api/v1/forecast-revisions/{revision_id}", tags=["decisions"])
def get_forecast_revision(revision_id: str) -> dict:
    revision = ORCHESTRATOR.revision(revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail=f"Unknown forecast revision '{revision_id}'")
    return {"revision": revision}


@app.get("/api/v1/forecast-revision-runs", tags=["decisions"])
def list_forecast_revision_runs() -> dict:
    return {"runs": ORCHESTRATOR.runs()}


@app.post("/api/v1/decisions/assess-timing", tags=["hedge-timing"])
def assess_decision_timing(request: AssessTimingRequest | None = None) -> dict:
    """Assess hedge timing for stored decisions using current observable
    conditions. Diagnostic-only, non-executable; not a price forecast."""
    payload = request or AssessTimingRequest()
    result = HEDGE_TIMING.assess_from_rolling(payload.decision_ids)
    return {"assessment": result, "diagnostic_only": True, "trustworthy_for_live_trading": False}


@app.get("/api/v1/hedge-timing-assessments", tags=["hedge-timing"])
def list_hedge_timing_assessments() -> dict:
    return {"assessments": HEDGE_TIMING.list_assessments()}


@app.get("/api/v1/hedge-timing-assessments/{assessment_id}", tags=["hedge-timing"])
def get_hedge_timing_assessment(assessment_id: str) -> dict:
    assessment = HEDGE_TIMING.get_assessment(assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail=f"Unknown hedge-timing assessment '{assessment_id}'")
    return {"assessment": assessment}


@app.get("/api/v1/decision-batch-summaries", tags=["hedge-timing"])
def list_decision_batch_summaries() -> dict:
    return {"summaries": HEDGE_TIMING.batch_summaries()}


@app.get("/api/v1/decision-batch-summaries/{batch_id}", tags=["hedge-timing"])
def get_decision_batch_summary(batch_id: str) -> dict:
    summary = HEDGE_TIMING.batch_summary(batch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Unknown decision batch '{batch_id}'")
    return {"summary": summary}


def _mutate_decision(action) -> dict:
    """Run a lifecycle mutation and map service exceptions to HTTP status codes.

    All transition/validation logic stays in the decision service/state machine;
    this only translates outcomes. Trader records remain diagnostic-only.
    """
    try:
        decision = action()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except StaleDecisionError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_decision",
                "current_status": error.current_status.value,
                "current_sequence": error.current_sequence,
                "message": str(error),
            },
        )
    except InvalidTransitionError as error:
        raise HTTPException(status_code=409, detail={"error": "invalid_transition", "message": str(error)})
    except (DecisionValidationError, StagePayloadError, ValueError) as error:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(error)})
    return {"decision": decision, "diagnostic_only": True, "trustworthy_for_live_trading": False}


@app.post("/api/v1/decisions/{decision_id}/accept", tags=["trader-lifecycle"])
def accept_decision(decision_id: str, request: AcceptRequest | None = None) -> dict:
    """Record trader acceptance. This records a decision only; no order is submitted."""
    payload = request or AcceptRequest()
    return _mutate_decision(
        lambda: DECISIONS.accept(
            decision_id,
            rationale=payload.trader_rationale,
            actor_id=payload.actor_id,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
        )
    )


@app.post("/api/v1/decisions/{decision_id}/modify", tags=["trader-lifecycle"])
def modify_decision(decision_id: str, request: ModifyRequest) -> dict:
    return _mutate_decision(
        lambda: DECISIONS.modify(
            decision_id,
            buy_mwh=request.trader_buy_mwh,
            sell_mwh=request.trader_sell_mwh,
            limit_price=request.trader_limit_price,
            rationale=request.trader_rationale,
            actor_id=request.actor_id,
            expected_status=request.expected_status,
            expected_sequence=request.expected_sequence,
        )
    )


@app.post("/api/v1/decisions/{decision_id}/reject", tags=["trader-lifecycle"])
def reject_decision(decision_id: str, request: RejectRequest) -> dict:
    return _mutate_decision(
        lambda: DECISIONS.reject(
            decision_id,
            rationale=request.trader_rationale,
            actor_id=request.actor_id,
            expected_status=request.expected_status,
            expected_sequence=request.expected_sequence,
        )
    )


@app.post("/api/v1/decisions/{decision_id}/delay", tags=["trader-lifecycle"])
def delay_decision(decision_id: str, request: DelayRequest) -> dict:
    return _mutate_decision(
        lambda: DECISIONS.delay(
            decision_id,
            until=request.delayed_until,
            rationale=request.trader_rationale,
            actor_id=request.actor_id,
            expected_status=request.expected_status,
            expected_sequence=request.expected_sequence,
        )
    )


@app.post("/api/v1/decisions/{decision_id}/reopen", tags=["trader-lifecycle"])
def reopen_decision(decision_id: str, request: ReopenRequest | None = None) -> dict:
    """Reopen a DELAYED decision back to PROPOSED. Records a decision only."""
    payload = request or ReopenRequest()
    return _mutate_decision(
        lambda: DECISIONS.reopen(
            decision_id,
            rationale=payload.trader_rationale,
            actor_id=payload.actor_id,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
        )
    )


@app.post("/api/v1/decisions/{decision_id}/submit-simulated", tags=["execution-simulation"])
def submit_simulated(decision_id: str, request: SubmitSimulatedRequest | None = None) -> dict:
    """Submit an ACCEPTED/MODIFIED trader instruction to the internal execution
    SIMULATOR. No real order is placed; the result is diagnostic and not executable."""
    payload = request or SubmitSimulatedRequest()
    try:
        outcome = EXECUTION.submit_simulated(
            decision_id,
            mode=payload.execution_mode,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
            idempotency_key=payload.idempotency_key,
            actor_id=payload.actor_id,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail={"error": "idempotency_conflict", "message": str(error)})
    except StaleDecisionError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_decision",
                "current_status": error.current_status.value,
                "current_sequence": error.current_sequence,
                "message": str(error),
            },
        )
    except InvalidTransitionError as error:
        raise HTTPException(status_code=409, detail={"error": "invalid_transition", "message": str(error)})
    except (DecisionValidationError, StagePayloadError, ValueError) as error:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(error)})
    return {
        "outcome": outcome,
        "decision": DECISIONS.get(decision_id),
        "execution_mode": outcome.execution_mode,
        "simulator_version": outcome.simulator_version,
        "assumptions_used": list(outcome.assumptions_used),
        "diagnostic_only": True,
        "not_executable": True,
        "trustworthy_for_live_trading": False,
    }


@app.get("/api/v1/simulated-orders", tags=["execution-simulation"])
def list_simulated_orders() -> dict:
    return {"orders": EXECUTION.list_orders()}


@app.get("/api/v1/simulated-orders/{order_id}", tags=["execution-simulation"])
def get_simulated_order(order_id: str) -> dict:
    order = EXECUTION.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Unknown simulated order '{order_id}'")
    return {"order": order}


@app.get("/api/v1/execution-outcomes", tags=["execution-simulation"])
def list_execution_outcomes() -> dict:
    return {"outcomes": EXECUTION.list_outcomes()}


@app.get("/api/v1/execution-outcomes/{order_id}", tags=["execution-simulation"])
def get_execution_outcome(order_id: str) -> dict:
    outcome = EXECUTION.get_outcome(order_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"Unknown execution outcome '{order_id}'")
    return {"outcome": outcome}


@app.get("/api/v1/decisions/{decision_id}/execution", tags=["execution-simulation"])
def get_decision_execution(decision_id: str) -> dict:
    return {"outcome": EXECUTION.latest_outcome_for_decision(decision_id)}


# --- Milestone 7: delivery / settlement / evaluation -----------------------

_DIAGNOSTIC = {"diagnostic_only": True, "not_executable": True}


def _settlement_http(error: Exception) -> HTTPException:
    """Map settlement/evaluation errors to the milestone's HTTP contract:
    409 for lifecycle/concurrency conflicts, 422 for invalid/unavailable input."""
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, SettlementIdempotencyConflictError):
        return HTTPException(status_code=409, detail={"error": "idempotency_conflict", "message": str(error)})
    if isinstance(error, StaleDecisionError):
        return HTTPException(
            status_code=409,
            detail={
                "error": "stale_decision",
                "current_status": error.current_status.value,
                "current_sequence": error.current_sequence,
                "message": str(error),
            },
        )
    if isinstance(error, InvalidTransitionError):
        return HTTPException(status_code=409, detail={"error": "invalid_transition", "message": str(error)})
    if isinstance(error, SettlementInputError):
        return HTTPException(status_code=422, detail={"error": "unavailable_input", "reason": error.reason.value, "message": str(error)})
    return HTTPException(status_code=422, detail={"error": "validation_error", "message": str(error)})


@app.post("/api/v1/decisions/{decision_id}/deliver", tags=["settlement-evaluation"])
def deliver_decision(decision_id: str, request: LifecycleActionRequest | None = None) -> dict:
    """Record SAMPLE physical delivery for a completed period. Diagnostic only."""
    payload = request or LifecycleActionRequest()
    try:
        delivery = SETTLEMENT.deliver(
            decision_id,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
            idempotency_key=payload.idempotency_key,
            actor_id=payload.actor_id,
        )
    except (KeyError, SettlementIdempotencyConflictError, StaleDecisionError, InvalidTransitionError, SettlementInputError, StagePayloadError, ValueError) as error:
        raise _settlement_http(error)
    return {"delivery": delivery, "decision": DECISIONS.get(decision_id), **_DIAGNOSTIC}


@app.post("/api/v1/decisions/{decision_id}/settle", tags=["settlement-evaluation"])
def settle_decision(decision_id: str, request: LifecycleActionRequest | None = None) -> dict:
    """Compute SAMPLE realised cash flow + incremental P&L vs NO_ACTION. Diagnostic only."""
    payload = request or LifecycleActionRequest()
    try:
        settlement = SETTLEMENT.settle(
            decision_id,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
            idempotency_key=payload.idempotency_key,
            actor_id=payload.actor_id,
        )
    except (KeyError, SettlementIdempotencyConflictError, StaleDecisionError, InvalidTransitionError, SettlementInputError, StagePayloadError, ValueError) as error:
        raise _settlement_http(error)
    return {"settlement": settlement, "decision": DECISIONS.get(decision_id), **_DIAGNOSTIC}


@app.post("/api/v1/decisions/{decision_id}/evaluate", tags=["settlement-evaluation"])
def evaluate_decision(decision_id: str, request: LifecycleActionRequest | None = None) -> dict:
    """Score the decision against benchmarks (regret, cautious quality label). Diagnostic only."""
    payload = request or LifecycleActionRequest()
    try:
        evaluation = EVALUATION.evaluate(
            decision_id,
            expected_status=payload.expected_status,
            expected_sequence=payload.expected_sequence,
            idempotency_key=payload.idempotency_key,
            actor_id=payload.actor_id,
        )
    except (KeyError, SettlementIdempotencyConflictError, StaleDecisionError, InvalidTransitionError, SettlementInputError, StagePayloadError, ValueError) as error:
        raise _settlement_http(error)
    return {"evaluation": evaluation, "decision": DECISIONS.get(decision_id), **_DIAGNOSTIC}


@app.post("/api/v1/decisions/process-completed", tags=["settlement-evaluation"])
def process_completed_decisions() -> dict:
    """SAMPLE-only: deliver → settle → evaluate every eligible decision whose delivery
    period has ended. Never processes future periods. Simulated realised data only."""
    result = EVALUATION.process_completed()
    return {"result": result, **_DIAGNOSTIC}


@app.get("/api/v1/deliveries", tags=["settlement-evaluation"])
def list_deliveries() -> dict:
    return {"deliveries": SETTLEMENT.list_deliveries()}


@app.get("/api/v1/deliveries/{delivery_id}", tags=["settlement-evaluation"])
def get_delivery(delivery_id: str) -> dict:
    delivery = SETTLEMENT.get_delivery(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"Unknown delivery '{delivery_id}'")
    return {"delivery": delivery}


@app.get("/api/v1/settlements", tags=["settlement-evaluation"])
def list_settlements() -> dict:
    return {"settlements": SETTLEMENT.list_settlements()}


@app.get("/api/v1/settlements/{settlement_id}", tags=["settlement-evaluation"])
def get_settlement(settlement_id: str) -> dict:
    settlement = SETTLEMENT.get_settlement(settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail=f"Unknown settlement '{settlement_id}'")
    return {"settlement": settlement}


@app.get("/api/v1/evaluations", tags=["settlement-evaluation"])
def list_evaluations() -> dict:
    return {"evaluations": EVALUATION.list_evaluations()}


@app.get("/api/v1/evaluations/{evaluation_id}", tags=["settlement-evaluation"])
def get_evaluation(evaluation_id: str) -> dict:
    evaluation = EVALUATION.get_evaluation(evaluation_id)
    if evaluation is None:
        raise HTTPException(status_code=404, detail=f"Unknown evaluation '{evaluation_id}'")
    return {"evaluation": evaluation}


@app.get("/api/v1/decisions/{decision_id}/evaluation", tags=["settlement-evaluation"])
def get_decision_evaluation(decision_id: str) -> dict:
    """Resolve the outcome bundle for one decision (delivery + settlement + evaluation),
    each null until its stage is reached."""
    return {
        "delivery": SETTLEMENT.delivery_for_decision(decision_id),
        "settlement": SETTLEMENT.settlement_for_decision(decision_id),
        "evaluation": EVALUATION.evaluation_for_decision(decision_id),
    }


# --- Milestone 8: point-in-time replay -------------------------------------


@app.get("/api/v1/replay-datasets", tags=["replay"])
def list_replay_datasets() -> dict:
    """Datasets available to replay. SAMPLE only today; HISTORICAL requires a genuine
    historical dataset to be supplied (none bundled)."""
    return {"datasets": REPLAY.available_datasets()}


@app.post("/api/v1/replay-runs", tags=["replay"])
def create_replay_run(request: ReplayCreateRequest | None = None) -> dict:
    """Run one deterministic point-in-time replay. SAMPLE_REPLAY is diagnostic and is
    never historical performance. Point-in-time integrity is enforced by the engine."""
    payload = request or ReplayCreateRequest()
    try:
        result = REPLAY.create(payload)
    except ReplayIdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail={"error": "idempotency_conflict", "message": str(error)})
    except ReplayValidationError as error:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": str(error)})
    except ReplayLimitExceeded as error:
        raise HTTPException(status_code=422, detail={"error": "bounded_run_limit", "message": str(error)})
    return {"run": result.run, "integrity": result.integrity, "diagnostic_only": True, "not_executable": True}


@app.get("/api/v1/replay-runs", tags=["replay"])
def list_replay_runs() -> dict:
    return {"runs": REPLAY.list_runs()}


@app.get("/api/v1/replay-runs/{replay_run_id}", tags=["replay"])
def get_replay_run(replay_run_id: str) -> dict:
    result = REPLAY.get(replay_run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown replay run '{replay_run_id}'")
    return {"run": result.run, "integrity": result.integrity}


@app.get("/api/v1/replay-runs/{replay_run_id}/episodes", tags=["replay"])
def get_replay_episodes(replay_run_id: str) -> dict:
    episodes = REPLAY.episodes(replay_run_id)
    if episodes is None:
        raise HTTPException(status_code=404, detail=f"Unknown replay run '{replay_run_id}'")
    return {"episodes": episodes}


@app.get("/api/v1/replay-runs/{replay_run_id}/metrics", tags=["replay"])
def get_replay_metrics(replay_run_id: str) -> dict:
    metrics = REPLAY.metrics(replay_run_id)
    if metrics is None:
        raise HTTPException(status_code=404, detail=f"Unknown replay run '{replay_run_id}'")
    return {"metrics": metrics}


@app.get("/api/v1/replay-runs/{replay_run_id}/cumulative-pnl", tags=["replay"])
def get_replay_cumulative_pnl(replay_run_id: str) -> dict:
    points = REPLAY.cumulative_pnl(replay_run_id)
    if points is None:
        raise HTTPException(status_code=404, detail=f"Unknown replay run '{replay_run_id}'")
    return {"points": points}


@app.get("/api/v1/cockpit", tags=["snapshots"])
def cockpit() -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    return {
        "snapshot": PIPELINE.current_snapshot,
        "feeds": PIPELINE.all_health(),
        "events": PIPELINE.recent_events(30),
    }
