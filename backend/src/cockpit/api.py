"""FastAPI surface for data-flow and forecast-position diagnostics."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from cockpit.battery_layer import build_battery_flexibility
from cockpit.battery_path_layer import build_standard_path_comparison, simulate_battery_path
from cockpit.forecast_layer import build_forecast_layer
from cockpit.market_layer import build_market_snapshot
from cockpit.models import BatteryPathInput, RefreshRequest
from cockpit.pipeline import PIPELINE
from cockpit.position_layer import build_forecast_position


@asynccontextmanager
async def lifespan(_: FastAPI):
    await PIPELINE.bootstrap()
    yield


app = FastAPI(
    title="Intraday Power Optimisation Cockpit",
    version="0.5.0",
    description="Observable data flow, position, liquidity and sequential battery-path diagnostics",
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


@app.get("/api/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "milestone": "1E-sequential-battery-path-what-if"}


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


@app.get("/api/v1/cockpit", tags=["snapshots"])
def cockpit() -> dict:
    if PIPELINE.current_snapshot is None:
        raise HTTPException(status_code=503, detail="No cockpit snapshot has been built")
    return {
        "snapshot": PIPELINE.current_snapshot,
        "feeds": PIPELINE.all_health(),
        "events": PIPELINE.recent_events(30),
    }
