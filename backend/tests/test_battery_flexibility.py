from __future__ import annotations

import pytest

from cockpit.battery_layer import CONFIG_METRICS, build_battery_flexibility
from cockpit.battery_physics import calculate_feasibility, next_soc, power_to_energy
from cockpit.models import Quality, SourceMode
from cockpit.opportunity_cost import calculate_opportunity_cost
from cockpit.pipeline import DataFlowPipeline


async def sample_snapshot():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    return pipeline.current_snapshot.model_copy(deep=True)


def test_soc_update_applies_charge_and_discharge_efficiency() -> None:
    assert next_soc(50, 10, 0, 0.5, 0.9, 0.8) == pytest.approx(54.5)
    assert next_soc(50, 0, 8, 0.5, 0.9, 0.8) == pytest.approx(45.0)


def test_power_to_energy_uses_settlement_period_duration() -> None:
    assert power_to_energy(20, 0.5) == pytest.approx(10)


def test_maximum_feasible_actions_respect_power_energy_and_reservations() -> None:
    result = calculate_feasibility(
        soc_mwh=54.2, e_min_mwh=10, e_max_mwh=100,
        charge_power_max_mw=20, discharge_power_max_mw=20,
        charge_efficiency=0.94, discharge_efficiency=0.92, duration_hours=0.5,
        upward_reserved_mw=8, downward_reserved_mw=5, reserve_duration_hours=1,
    )
    assert result.upward_power_headroom_mw == pytest.approx(12)
    assert result.downward_power_headroom_mw == pytest.approx(15)
    assert result.max_discharge_mwh == pytest.approx(6)
    assert result.max_charge_mwh == pytest.approx(7.5)
    assert result.projected_soc_after_max_discharge_mwh >= 10 + 8 / 0.92
    assert result.projected_soc_after_max_charge_mwh <= 100 - 0.94 * 5
    assert "DISCHARGE_POWER_HEADROOM" in result.binding_constraints
    assert "CHARGE_POWER_HEADROOM" in result.binding_constraints


def test_energy_duration_can_bind_before_power() -> None:
    result = calculate_feasibility(
        soc_mwh=20, e_min_mwh=10, e_max_mwh=100,
        charge_power_max_mw=20, discharge_power_max_mw=20,
        charge_efficiency=0.9, discharge_efficiency=0.9, duration_hours=0.5,
        upward_reserved_mw=8, downward_reserved_mw=0, reserve_duration_hours=1,
    )
    assert result.max_discharge_mwh == pytest.approx((20 - (10 + 8 / 0.9)) * 0.9)
    assert "UPWARD_ENERGY_DURATION" in result.binding_constraints


def test_invalid_soc_or_reservation_blocks_physics() -> None:
    with pytest.raises(ValueError, match="outside"):
        calculate_feasibility(
            soc_mwh=101, e_min_mwh=10, e_max_mwh=100,
            charge_power_max_mw=20, discharge_power_max_mw=20,
            charge_efficiency=0.9, discharge_efficiency=0.9, duration_hours=0.5,
        )


@pytest.mark.asyncio
async def test_sample_inputs_remain_sample_and_degraded() -> None:
    result = build_battery_flexibility(await sample_snapshot()).snapshot
    assert result.source_mode == SourceMode.SAMPLE
    assert result.readiness.status == "DEGRADED"
    assert result.readiness.calculation_allowed is True
    assert result.readiness.trustworthy_for_live_trading is False
    assert result.periods
    assert all(period.feasibility.max_charge_value.lineage.source_mode == SourceMode.SAMPLE for period in result.periods)


@pytest.mark.asyncio
async def test_stale_telemetry_degrades_battery_readiness() -> None:
    snapshot = await sample_snapshot()
    next(point for point in snapshot.values if point.metric == "battery_soc").lineage.quality = Quality.STALE
    result = build_battery_flexibility(snapshot).snapshot
    assert result.readiness.status == "DEGRADED"
    assert any("stale" in reason.lower() for reason in result.readiness.reasons)


@pytest.mark.asyncio
async def test_missing_battery_limit_blocks_calculation_without_fallback() -> None:
    snapshot = await sample_snapshot()
    snapshot.values = [point for point in snapshot.values if point.metric != "battery_e_max"]
    result = build_battery_flexibility(snapshot).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.readiness.calculation_allowed is False
    assert result.periods == []
    assert result.source_mode != SourceMode.SYNTHETIC


@pytest.mark.asyncio
async def test_max_support_reduces_long_and_short_exposure_without_changing_sign_convention() -> None:
    result = build_battery_flexibility(await sample_snapshot()).snapshot
    coverage = [item for period in result.periods for item in period.coverage]
    long_item = next(item for item in coverage if item.exposure_mwh > 0.05)
    short_item = next(item for item in coverage if item.exposure_mwh < -0.05)
    assert long_item.support_direction == "CHARGE"
    assert long_item.residual_after_support_mwh == pytest.approx(long_item.exposure_mwh - long_item.covered_mwh)
    assert short_item.support_direction == "DISCHARGE"
    assert short_item.residual_after_support_mwh == pytest.approx(short_item.exposure_mwh + short_item.covered_mwh)


@pytest.mark.asyncio
async def test_calculated_feasibility_and_coverage_have_lineage() -> None:
    result = build_battery_flexibility(await sample_snapshot())
    period = result.snapshot.periods[0]
    assert period.feasibility.max_discharge_value.lineage.source_feed == "battery_flexibility_calculation"
    assert period.coverage[0].residual_value.lineage.source_feed == "battery_flexibility_calculation"
    value_ids = {point.value_id for point in result.derived_values}
    assert period.coverage[0].residual_value.value_id in value_ids


def test_opportunity_cost_components_are_transparent() -> None:
    result = calculate_opportunity_cost(
        soc_mwh=54.2, terminal_target_mwh=55,
        degradation_cost_gbp_per_mwh=4, terminal_penalty_gbp_per_mwh=1.5,
        future_flex_penalty_gbp_per_mwh=2.5,
        charge_efficiency=0.94, discharge_efficiency=0.92,
        upward_reserved_mw=8, downward_reserved_mw=5,
        charge_power_max_mw=20, discharge_power_max_mw=20,
    )
    assert result.discharge_cost_gbp_per_mwh == pytest.approx(4 + 1.5 / 0.92 + 3.5)
    assert result.charge_cost_gbp_per_mwh == pytest.approx(4 + 3.125)


@pytest.mark.asyncio
async def test_fresh_live_valid_inputs_make_battery_ready() -> None:
    snapshot = await sample_snapshot()
    physical_metrics = {"battery_soc", "upward_service_commitment", "downward_service_commitment", *CONFIG_METRICS}
    for point in snapshot.values:
        if point.metric in physical_metrics:
            point.lineage.source_mode = SourceMode.LIVE
            point.lineage.quality = Quality.FRESH
    result = build_battery_flexibility(snapshot).snapshot
    assert result.readiness.status == "READY"
    assert result.readiness.trustworthy_for_live_trading is True
