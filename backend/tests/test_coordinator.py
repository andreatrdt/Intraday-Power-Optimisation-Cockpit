from __future__ import annotations

import pytest

from cockpit.coordinator_layer import build_coordinator_snapshot
from cockpit.models import CoordinatorAction, CoordinatorSimulationInput, Quality, SourceMode
from cockpit.pipeline import DataFlowPipeline


async def sample_snapshot():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    return pipeline.current_snapshot.model_copy(deep=True)


async def sample_result(settings: CoordinatorSimulationInput | None = None):
    return build_coordinator_snapshot(
        await sample_snapshot(), settings, live_provider_status=SourceMode.ERROR
    )


def candidate(snapshot, action: CoordinatorAction):
    return next(item for item in snapshot.candidates if item.action == action)


@pytest.mark.asyncio
async def test_all_six_candidate_actions_are_generated_and_ranked() -> None:
    result = (await sample_result()).snapshot
    assert {item.action for item in result.candidates} == set(CoordinatorAction)
    assert sorted(item.rank for item in result.candidates) == list(range(1, 7))
    assert result.recommendation is not None
    assert result.recommendation.selected_action == result.candidates[0].action
    assert result.recommendation.not_executable is True


@pytest.mark.asyncio
async def test_no_action_candidate_has_no_market_or_battery_action() -> None:
    item = candidate((await sample_result()).snapshot, CoordinatorAction.NO_ACTION)
    assert item.market_trade_volume_mwh == 0
    assert item.battery_charge_mwh == 0
    assert item.battery_discharge_mwh == 0
    for period in item.periods:
        before = {value.scenario: value.residual_position_mwh for value in period.exposure_before}
        assert all(residual.residual_exposure_mwh == pytest.approx(before[residual.scenario]) for residual in period.residuals)


@pytest.mark.asyncio
async def test_market_only_uses_wap_and_reports_capped_unfilled_volume() -> None:
    settings = CoordinatorSimulationInput(maximum_market_hedge_volume_mwh=1)
    item = candidate((await sample_result(settings)).snapshot, CoordinatorAction.MARKET_ONLY)
    assert item.market_trade_volume_mwh > 0
    assert item.market_wap_gbp_per_mwh is not None
    assert item.market_unfilled_mwh >= 0
    assert all(period.market_trade_volume_mwh <= 1 + 1e-9 for period in item.periods)


@pytest.mark.asyncio
async def test_battery_only_and_hybrid_integrate_sequential_path() -> None:
    snapshot = (await sample_result()).snapshot
    battery = candidate(snapshot, CoordinatorAction.BATTERY_ONLY_P50)
    hybrid = candidate(snapshot, CoordinatorAction.MARKET_BATTERY_HYBRID)
    assert battery.market_trade_volume_mwh == 0
    assert battery.battery_charge_mwh + battery.battery_discharge_mwh > 0
    assert hybrid.market_trade_volume_mwh > 0
    assert hybrid.battery_charge_mwh + hybrid.battery_discharge_mwh > 0
    assert hybrid.periods[1].soc_before_mwh == pytest.approx(hybrid.periods[0].soc_after_mwh)


@pytest.mark.asyncio
async def test_residual_equation_is_explicit_for_every_scenario() -> None:
    item = candidate((await sample_result()).snapshot, CoordinatorAction.MARKET_BATTERY_HYBRID)
    for period in item.periods:
        for residual in period.residuals:
            assert residual.residual_exposure_mwh == pytest.approx(
                residual.exposure_before_mwh
                + residual.battery_net_export_mwh
                - residual.signed_market_trade_mwh
            )


@pytest.mark.asyncio
async def test_opportunity_optionality_service_and_total_cost_are_integrated() -> None:
    snapshot = await sample_snapshot()
    duration = next(point for point in snapshot.values if point.metric == "service_required_duration")
    duration.value = 4.0
    result = build_coordinator_snapshot(snapshot, live_provider_status=SourceMode.ERROR).snapshot
    item = candidate(result, CoordinatorAction.BATTERY_ONLY_P50)
    assert item.cost.battery_opportunity_cost_gbp > 0
    assert item.cost.optionality_lost_gbp != 0
    assert item.cost.service_risk_penalty_gbp > 0
    assert item.cost.total_diagnostic_cost_gbp == pytest.approx(
        item.cost.market_execution_cost_gbp
        + item.cost.expected_imbalance_cost_gbp
        + item.cost.tail_risk_penalty_gbp
        + item.cost.battery_opportunity_cost_gbp
        + item.cost.optionality_lost_gbp
        + item.cost.service_risk_penalty_gbp
    )


@pytest.mark.asyncio
async def test_sample_data_remains_degraded_and_explanation_is_non_live() -> None:
    result = (await sample_result()).snapshot
    assert result.source_mode == SourceMode.SAMPLE
    assert result.readiness.status == "DEGRADED"
    assert result.readiness.calculation_allowed is True
    assert result.readiness.trustworthy_for_live_trading is False
    assert "not live-trading trustworthy" in result.recommendation.explanation.lower()
    assert "not executable" in result.recommendation.explanation.lower()


@pytest.mark.asyncio
async def test_sample_market_requires_explicit_selection() -> None:
    settings = CoordinatorSimulationInput(explicit_sample_market=False)
    result = (await sample_result(settings)).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.readiness.calculation_allowed is False
    assert any("explicit sample mode" in blocker.lower() for blocker in result.readiness.critical_blockers)


@pytest.mark.asyncio
async def test_blocked_snapshot_keeps_underlying_stale_quality() -> None:
    snapshot = await sample_snapshot()
    for point in snapshot.values:
        if point.metric.startswith("market_bid_") or point.metric.startswith("market_ask_"):
            point.lineage.quality = Quality.STALE
    settings = CoordinatorSimulationInput(explicit_sample_market=False)
    result = build_coordinator_snapshot(
        snapshot, settings, live_provider_status=SourceMode.ERROR
    ).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.quality == Quality.STALE


@pytest.mark.asyncio
async def test_missing_market_data_blocks_and_elexon_is_not_executable() -> None:
    snapshot = await sample_snapshot()
    snapshot.values = [
        point for point in snapshot.values
        if not (point.metric.startswith("market_bid_") or point.metric.startswith("market_ask_"))
    ]
    result = build_coordinator_snapshot(snapshot, live_provider_status=SourceMode.ERROR).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.candidates == []
    assert any("Elexon MID" in blocker for blocker in result.readiness.critical_blockers)


@pytest.mark.asyncio
async def test_scores_cost_components_and_residuals_have_lineage() -> None:
    result = await sample_result()
    item = result.snapshot.candidates[0]
    points = (
        item.cost.total_diagnostic_cost_value,
        item.cost.expected_imbalance_cost_value,
        item.periods[0].residuals[1].residual_value,
        item.periods[0].market_trade_value,
    )
    ids = {point.value_id for point in result.derived_values}
    for point in points:
        assert point.lineage.source_feed == "integrated_coordinator"
        assert point.lineage.transformations
        assert point.value_id in ids


@pytest.mark.asyncio
async def test_sensitivities_cover_requested_counterfactuals() -> None:
    sensitivities = (await sample_result()).snapshot.sensitivities
    assert {item.sensitivity_id for item in sensitivities} == {
        "ask-price", "bid-depth", "lower-soc", "optionality-double", "p10-weight", "market-missing"
    }
    assert all(item.explanation for item in sensitivities)
