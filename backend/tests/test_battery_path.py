from __future__ import annotations

import pytest

from cockpit.battery_path_layer import build_standard_path_comparison, simulate_battery_path
from cockpit.models import BatteryPathInput, BatteryPathPeriodAction, Quality, SourceMode
from cockpit.pipeline import DataFlowPipeline


async def sample_snapshot():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    return pipeline.current_snapshot.model_copy(deep=True)


@pytest.mark.asyncio
async def test_no_action_path_preserves_soc_sequentially() -> None:
    result = simulate_battery_path(
        await sample_snapshot(), BatteryPathInput(path_name="NO_ACTION")
    ).simulation
    assert result.valid is True
    assert len(result.periods) == 8
    assert all(period.starting_soc_mwh == pytest.approx(54.2) for period in result.periods)
    assert all(period.ending_soc_mwh == pytest.approx(54.2) for period in result.periods)


@pytest.mark.asyncio
async def test_charge_efficiency_propagates_across_later_periods() -> None:
    snapshot = await sample_snapshot()
    period_ids = [period.delivery_period for period in build_standard_path_comparison(snapshot).comparison.no_action.periods]
    result = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=period_ids[0], charge_mw=10),
        BatteryPathPeriodAction(delivery_period=period_ids[1], charge_mw=4),
    ])).simulation
    assert result.periods[0].ending_soc_mwh == pytest.approx(54.2 + 0.94 * 10 * 0.5)
    assert result.periods[1].starting_soc_mwh == pytest.approx(result.periods[0].ending_soc_mwh)
    assert result.periods[1].ending_soc_mwh == pytest.approx(54.2 + 0.94 * 7)
    assert result.periods[2].starting_soc_mwh == pytest.approx(result.periods[1].ending_soc_mwh)


@pytest.mark.asyncio
async def test_discharge_efficiency_propagates_across_later_periods() -> None:
    snapshot = await sample_snapshot()
    first = build_standard_path_comparison(snapshot).comparison.no_action.periods[0].delivery_period
    result = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=first, discharge_mw=8),
    ])).simulation
    expected = 54.2 - 8 * 0.5 / 0.92
    assert result.periods[0].ending_soc_mwh == pytest.approx(expected)
    assert result.periods[1].starting_soc_mwh == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("charge", "discharge", "expected_code"),
    [(120.0, 0.0, "SOC_ABOVE_MAXIMUM"), (0.0, 100.0, "SOC_BELOW_MINIMUM")],
)
async def test_soc_limit_violations_are_reported(charge, discharge, expected_code) -> None:
    snapshot = await sample_snapshot()
    first = build_standard_path_comparison(snapshot).comparison.no_action.periods[0].delivery_period
    result = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=first, charge_mw=charge, discharge_mw=discharge),
    ])).simulation
    assert result.valid is False
    assert expected_code in {violation.code for violation in result.violations}


@pytest.mark.asyncio
async def test_power_limit_and_simultaneous_action_violations_are_reported() -> None:
    snapshot = await sample_snapshot()
    first = build_standard_path_comparison(snapshot).comparison.no_action.periods[0].delivery_period
    result = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=first, charge_mw=25, discharge_mw=3),
    ])).simulation
    codes = {violation.code for violation in result.violations}
    assert "CHARGE_POWER_LIMIT" in codes
    assert "SIMULTANEOUS_CHARGE_DISCHARGE" in codes


@pytest.mark.asyncio
async def test_residual_exposure_includes_path_net_export_energy() -> None:
    snapshot = await sample_snapshot()
    first = build_standard_path_comparison(snapshot).comparison.no_action.periods[0].delivery_period
    result = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=first, discharge_mw=6),
    ])).simulation
    period = result.periods[0]
    for before, after in zip(period.exposure_before, period.residual_exposure, strict=True):
        assert after.residual_position_mwh == pytest.approx(before.residual_position_mwh + 3.0)


@pytest.mark.asyncio
async def test_using_energy_now_reduces_future_upward_energy_headroom() -> None:
    snapshot = await sample_snapshot()
    comparison = build_standard_path_comparison(snapshot).comparison
    first = comparison.no_action.periods[0].delivery_period
    custom = simulate_battery_path(snapshot, BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period=first, discharge_mw=12),
    ])).simulation
    assert custom.periods[1].upward_energy_duration_hours < comparison.no_action.periods[1].upward_energy_duration_hours


@pytest.mark.asyncio
async def test_terminal_soc_shortfall_is_calculated() -> None:
    result = build_standard_path_comparison(await sample_snapshot()).comparison.no_action
    assert result.terminal_target_mwh == pytest.approx(55)
    assert result.terminal_soc_mwh == pytest.approx(54.2)
    assert result.terminal_shortfall_mwh == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_standard_p50_path_reduces_residual_and_is_not_a_recommendation() -> None:
    comparison = build_standard_path_comparison(await sample_snapshot()).comparison
    assert comparison.p50_coverage.valid is True
    assert comparison.p50_coverage.diagnostic_only is True
    assert comparison.p50_residual_reduction_mwh > 0
    assert comparison.p50_coverage.total_absolute_p50_residual_mwh < comparison.no_action.total_absolute_p50_residual_mwh


@pytest.mark.asyncio
async def test_preserve_path_uses_less_energy_than_full_p50_coverage() -> None:
    comparison = build_standard_path_comparison(await sample_snapshot()).comparison
    full_energy = sum(period.charge_mwh + period.discharge_mwh for period in comparison.p50_coverage.periods)
    preserve_energy = sum(period.charge_mwh + period.discharge_mwh for period in comparison.preserve_flexibility.periods)
    assert preserve_energy < full_energy
    assert preserve_energy > 0


@pytest.mark.asyncio
async def test_custom_unknown_period_is_invalid() -> None:
    result = simulate_battery_path(await sample_snapshot(), BatteryPathInput(path_name="CUSTOM", actions=[
        BatteryPathPeriodAction(delivery_period="2099-01-01 SP01", charge_mw=1),
    ])).simulation
    assert result.valid is False
    assert "UNKNOWN_DELIVERY_PERIOD" in {violation.code for violation in result.violations}


@pytest.mark.asyncio
async def test_sample_and_stale_readiness_propagate() -> None:
    snapshot = await sample_snapshot()
    sample = simulate_battery_path(snapshot, BatteryPathInput(path_name="NO_ACTION")).simulation
    assert sample.source_mode == SourceMode.SAMPLE
    assert sample.readiness.status == "DEGRADED"
    next(point for point in snapshot.values if point.metric == "battery_soc").lineage.quality = Quality.STALE
    stale = simulate_battery_path(snapshot, BatteryPathInput(path_name="NO_ACTION")).simulation
    assert stale.readiness.status == "DEGRADED"
    assert any("stale" in reason.lower() for reason in stale.readiness.reasons)


@pytest.mark.asyncio
async def test_sequential_soc_and_residual_values_have_lineage() -> None:
    result = simulate_battery_path(
        await sample_snapshot(), BatteryPathInput(path_name="P50_COVERAGE")
    )
    period = result.simulation.periods[0]
    assert period.ending_soc_value.lineage.source_feed == "battery_path_simulation"
    p50 = next(item for item in period.residual_exposure if item.scenario == "P50")
    assert p50.exposure_value.lineage.source_feed == "battery_path_simulation"
    ids = {point.value_id for point in result.derived_values}
    assert period.ending_soc_value.value_id in ids
    assert p50.exposure_value.value_id in ids
