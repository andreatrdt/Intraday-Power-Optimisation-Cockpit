from __future__ import annotations

import pytest

from cockpit.forecast_layer import build_forecast_layer, energy_from_power
from cockpit.models import Quality, SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.position_layer import build_forecast_position, direction


async def sample_snapshot():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    return pipeline.current_snapshot.model_copy(deep=True)


@pytest.mark.asyncio
async def test_forecast_delta_is_latest_minus_previous_vintage() -> None:
    snapshot = await sample_snapshot()
    forecasts = build_forecast_layer(snapshot)
    assert forecasts.points
    for point in forecasts.points:
        assert point.previous_p50 is not None
        assert point.delta.versus_previous_mwh == pytest.approx(
            float(point.p50.value) - float(point.previous_p50.value)
        )
    assert -8.0 in [point.delta.versus_previous_mwh for point in forecasts.points]


def test_mw_forecast_is_converted_to_settlement_period_mwh() -> None:
    assert energy_from_power(50.0, "MW", 0.5) == pytest.approx(25.0)
    assert energy_from_power(25.0, "MWh", 0.5) == pytest.approx(25.0)
    with pytest.raises(ValueError, match="expected MW or MWh"):
        energy_from_power(50.0, "kW", 0.5)


@pytest.mark.asyncio
async def test_p10_p50_p90_exposure_uses_generation_minus_q() -> None:
    result = build_forecast_position(await sample_snapshot()).snapshot
    period = result.periods[0]
    q = float(period.position.contracted_position.value)
    assert {item.scenario for item in period.exposures} == {"P10", "P50", "P90"}
    for item in period.exposures:
        assert item.residual_position_mwh == pytest.approx(item.generation_mwh - q)


def test_positive_is_long_negative_is_short_and_small_is_flat() -> None:
    assert direction(4.0) == "LONG"
    assert direction(-4.0) == "SHORT"
    assert direction(0.04) == "FLAT"


@pytest.mark.asyncio
async def test_stale_forecast_degrades_but_does_not_block_calculation() -> None:
    snapshot = await sample_snapshot()
    for point in snapshot.values:
        if point.metric in {"wind_p10", "wind_p50", "wind_p90"}:
            point.lineage.quality = Quality.STALE
    result = build_forecast_position(snapshot).snapshot
    assert result.readiness.status == "DEGRADED"
    assert result.readiness.calculation_allowed is True
    assert result.readiness.trustworthy_for_live_trading is False
    assert any("stale" in reason.lower() for reason in result.readiness.reasons)


@pytest.mark.asyncio
async def test_missing_q_blocks_position_calculation() -> None:
    snapshot = await sample_snapshot()
    snapshot.values = [
        point for point in snapshot.values if point.metric != "contracted_position_q"
    ]
    result = build_forecast_position(snapshot).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.readiness.calculation_allowed is False
    assert result.periods == []
    assert any("Q_t" in reason for reason in result.readiness.reasons)


@pytest.mark.asyncio
async def test_sample_inputs_and_calculated_exposures_remain_sample() -> None:
    result = build_forecast_position(await sample_snapshot()).snapshot
    assert result.readiness.status == "DEGRADED"
    assert result.latest_vintage is not None
    assert result.latest_vintage.source_mode == SourceMode.SAMPLE
    assert all(
        exposure.exposure_value.lineage.source_mode == SourceMode.SAMPLE
        for period in result.periods
        for exposure in period.exposures
    )


@pytest.mark.asyncio
async def test_forecast_position_never_silently_falls_back_to_synthetic() -> None:
    result = build_forecast_position(await sample_snapshot()).snapshot
    modes = {
        point.lineage.source_mode
        for period in result.periods
        for point in (
            period.forecast.p10,
            period.forecast.p50,
            period.forecast.p90,
            period.position.contracted_position,
            *(exposure.exposure_value for exposure in period.exposures),
        )
    }
    assert SourceMode.SYNTHETIC not in modes
    assert SourceMode.SAMPLE in modes


@pytest.mark.asyncio
async def test_calculated_exposure_has_resolvable_input_lineage() -> None:
    snapshot = await sample_snapshot()
    result = build_forecast_position(snapshot)
    exposure = result.snapshot.periods[0].exposures[1].exposure_value
    assert exposure.snapshot_id == snapshot.snapshot_id
    assert exposure.included_in_current_snapshot is True
    assert exposure.lineage.source_feed == "forecast_position_calculation"
    assert exposure.lineage.raw_field_name == "I_t^P50 = G_t^P50 - Q_t"
    assert exposure.lineage.transformations
    assert all(check.passed for check in exposure.lineage.validation_checks)
    assert exposure.value_id in {point.value_id for point in result.derived_values}


@pytest.mark.asyncio
async def test_position_readiness_ready_degraded_and_blocked_states() -> None:
    sample = await sample_snapshot()
    assert build_forecast_position(sample).snapshot.readiness.status == "DEGRADED"

    live = sample.model_copy(deep=True)
    for point in live.values:
        if point.metric in {"wind_p10", "wind_p50", "wind_p90", "contracted_position_q"}:
            point.lineage.source_mode = SourceMode.LIVE
            point.lineage.quality = Quality.FRESH
    ready = build_forecast_position(live).snapshot.readiness
    assert ready.status == "READY"
    assert ready.trustworthy_for_live_trading is True

    blocked = sample.model_copy(deep=True)
    blocked.values = [point for point in blocked.values if point.metric != "wind_p50"]
    blocked_readiness = build_forecast_position(blocked).snapshot.readiness
    assert blocked_readiness.status == "BLOCKED"
    assert blocked_readiness.calculation_allowed is False
