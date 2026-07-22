from __future__ import annotations

import pytest

from cockpit.models import SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.rolling_service import RollingService


async def service() -> RollingService:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    rolling = RollingService(pipeline)
    rolling.initialise()
    return rolling


@pytest.mark.asyncio
async def test_sample_live_state_exposes_thirty_day_history_and_windows() -> None:
    rolling = await service()
    live = rolling.live_state()
    assert len(live.history) >= 720
    assert (live.history[-1].observed_at - live.history[0].observed_at).total_seconds() >= 719 * 3600
    assert live.available_history_windows == ["today", "24h", "7d", "30d", "custom"]
    assert live.state.state_source_mode == SourceMode.SAMPLE
    assert all(point.source_mode == SourceMode.SAMPLE for point in live.forecast_vintage_history)


@pytest.mark.asyncio
async def test_forecast_vintages_price_depth_and_previous_runs_have_context() -> None:
    rolling = await service()
    live = rolling.live_state()
    assert len(live.forecast_vintage_history) >= 720
    assert live.optimisation_history
    assert len(live.chart_series["market_price"][0].points) >= 720
    assert len(live.chart_series["market_depth"][0].points) >= 720
    assert max(point.value for point in live.chart_series["market_price"][0].points) > min(point.value for point in live.chart_series["market_price"][0].points)
    assert max(point.value for point in live.chart_series["market_depth"][0].points) > min(point.value for point in live.chart_series["market_depth"][0].points)


@pytest.mark.asyncio
async def test_chart_insights_units_flat_explanations_and_context_percentiles() -> None:
    rolling = await service()
    live = rolling.live_state()
    assert all(live.chart_insights[key] for key in ("production", "demand", "forecast_vintage", "market_price", "market_depth", "portfolio", "battery"))
    assert all(series.unit for group in live.chart_series.values() for series in group)
    reserve = [series for series in live.chart_series["battery"] if series.key in {"reserve_up", "reserve_down"}]
    assert reserve and all(series.flat_explanation for series in reserve)
    percentile_keys = {measure.key for measure in live.context_risk_measures}
    assert {
        "price_percentile_30d", "spread_percentile_30d", "depth_percentile_30d",
        "residual_demand_percentile_30d", "forecast_error", "forecast_revision",
    } <= percentile_keys
    assert all(0 <= measure.value <= 100 for measure in live.context_risk_measures if measure.unit == "percentile")


@pytest.mark.asyncio
async def test_optimisation_exposes_historical_soc_context_and_backend_explanations() -> None:
    rolling = await service()
    run = rolling.current_optimisation()
    assert "soc_context" in run.chart_series
    assert {series.region for series in run.chart_series["soc_context"]} == {"historical", "current", "future"}
    assert len(next(series for series in run.chart_series["soc_context"] if series.region == "historical").points) >= 720
    assert run.chart_insights["soc_path"]
    assert run.chart_insights["reserve_path"]
    assert any(measure.key == "price_percentile_30d" for measure in run.risk_measures)

