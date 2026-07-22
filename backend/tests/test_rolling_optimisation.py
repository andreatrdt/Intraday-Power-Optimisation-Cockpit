from __future__ import annotations

import pytest

from cockpit.forecast_layer import build_forecast_layer
from cockpit.full_action_optimiser import FullActionConfig, build_full_action_model
from cockpit.market_layer import build_market_snapshot
from cockpit.models import SampleRegime, SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.position_layer import build_forecast_position
from cockpit.rolling_service import RollingService


async def service() -> RollingService:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    rolling = RollingService(pipeline)
    rolling.initialise()
    return rolling


@pytest.mark.asyncio
async def test_rolling_state_initialises_as_calculable_sample() -> None:
    rolling = await service()
    live = rolling.live_state()
    assert live.state.current_step == 0
    assert live.state.state_source_mode == SourceMode.SAMPLE
    assert live.state.trust.calculation_allowed is True
    assert live.state.trust.trustworthy_for_live_trading is False
    assert live.state.trust.readiness == "DEGRADED"
    assert live.state.latest_optimisation_run_id
    assert live.events


@pytest.mark.asyncio
async def test_refresh_and_regime_change_create_new_vintages_and_books() -> None:
    rolling = await service()
    before = rolling.live_state().model_copy(deep=True)
    refreshed = rolling.refresh()
    assert refreshed.state.current_forecast_vintage_id != before.state.current_forecast_vintage_id
    assert refreshed.state.current_market_snapshot_id != before.state.current_market_snapshot_id
    assert refreshed.market.reference_price_gbp_per_mwh != before.market.reference_price_gbp_per_mwh
    changed = rolling.set_regime(SampleRegime.TIGHTENING)
    assert changed.state.current_regime == SampleRegime.TIGHTENING
    assert changed.production_demand.demand_mw != refreshed.production_demand.demand_mw
    assert changed.market.bid_depth_mwh != refreshed.market.bid_depth_mwh


@pytest.mark.asyncio
async def test_advance_applies_first_action_updates_soc_q_and_keeps_old_run_immutable() -> None:
    rolling = await service()
    rolling.set_regime(SampleRegime.OVERSUPPLY)
    selected = rolling.run()
    old_dump = selected.model_dump(mode="json")
    action = selected.projected_trajectory[0]
    old_q = action.q_before_action_mwh
    live, next_run = rolling.advance()
    assert live.state.current_step == 1
    assert live.portfolio_battery.current_soc_mwh == pytest.approx(action.projected_soc_mwh)
    assert live.state.last_soc_change_mwh == pytest.approx(action.projected_soc_mwh - action.soc_before_mwh)
    assert live.portfolio_battery.current_q_mwh == pytest.approx(old_q + action.sell_mwh - action.buy_mwh, abs=1e-3)
    assert live.state.last_q_change_mwh == pytest.approx(action.sell_mwh - action.buy_mwh, abs=1e-3)
    assert next_run.run_id != selected.run_id
    assert rolling.get_run(selected.run_id).model_dump(mode="json") == old_dump


@pytest.mark.asyncio
async def test_full_model_exposes_requested_action_variables() -> None:
    rolling = await service()
    model = build_full_action_model(
        rolling.period_inputs,
        rolling.live_state().state.current_soc_mwh,
    )
    for name in (
        "buy", "sell", "charge", "discharge", "soc", "reserve_up", "reserve_down",
        "residual_long", "residual_short", "charge_on", "discharge_on",
        "portfolio_balance", "buy_depth", "sell_depth",
    ):
        assert hasattr(model, name)


@pytest.mark.asyncio
async def test_solution_respects_battery_reserve_and_portfolio_constraints() -> None:
    rolling = await service()
    run = rolling.current_optimisation()
    cfg = FullActionConfig()
    previous_net = None
    total_discharge = 0.0
    for period in run.projected_trajectory:
        assert not (period.charge_mw > 1e-5 and period.discharge_mw > 1e-5)
        expected_soc = (
            period.soc_before_mwh
            + cfg.charge_efficiency * period.charge_mw * 0.5
            - period.discharge_mw * 0.5 / cfg.discharge_efficiency
        )
        assert period.projected_soc_mwh == pytest.approx(expected_soc, abs=1e-3)
        assert cfg.e_min_mwh - 1e-5 <= period.projected_soc_mwh <= cfg.e_max_mwh + 1e-5
        assert period.reserve_up_mw <= cfg.discharge_max_mw - period.battery_net_export_mw + 1e-4
        assert period.reserve_down_mw <= cfg.charge_max_mw + period.battery_net_export_mw + 1e-4
        assert period.soc_before_mwh - cfg.e_min_mwh + 1e-4 >= period.reserve_up_mw * cfg.upward_duration_h / cfg.discharge_efficiency
        assert cfg.e_max_mwh - period.soc_before_mwh + 1e-4 >= cfg.charge_efficiency * period.reserve_down_mw * cfg.downward_duration_h
        for generation, residual in (
            (period.generation_p10_mwh, period.residual_p10_mwh),
            (period.generation_p50_mwh, period.residual_p50_mwh),
            (period.generation_p90_mwh, period.residual_p90_mwh),
        ):
            expected = generation + period.battery_net_export_mw * 0.5 + period.buy_mwh - period.q_before_action_mwh - period.sell_mwh
            assert residual == pytest.approx(expected, abs=1e-3)
        if previous_net is not None:
            assert abs(period.battery_net_export_mw - previous_net) <= cfg.ramp_limit_mw_per_period + 1e-4
        previous_net = period.battery_net_export_mw
        total_discharge += period.discharge_mw * 0.5
    assert run.terminal_soc_mwh >= cfg.minimum_terminal_soc_mwh - 1e-5
    assert total_discharge <= cfg.maximum_cycles_per_day * cfg.e_max_mwh * 0.5 + 1e-4


@pytest.mark.asyncio
async def test_market_depth_wap_and_gate_closure_are_respected() -> None:
    rolling = await service()
    run = rolling.current_optimisation()
    for input_period, result in zip(run.inputs, run.projected_trajectory, strict=True):
        assert result.buy_mwh <= sum(level.volume_mwh for level in input_period.asks) + 1e-5
        assert result.sell_mwh <= sum(level.volume_mwh for level in input_period.bids) + 1e-5
        if not input_period.tradeable:
            assert result.buy_mwh == pytest.approx(0)
            assert result.sell_mwh == pytest.approx(0)
        if result.buy_mwh > 1e-5:
            ask_prices = [level.price_gbp_per_mwh for level in input_period.asks]
            assert min(ask_prices) <= result.market_wap_gbp_per_mwh <= max(ask_prices)
        if result.sell_mwh > 1e-5:
            bid_prices = [level.price_gbp_per_mwh for level in input_period.bids]
            assert min(bid_prices) <= result.market_wap_gbp_per_mwh <= max(bid_prices)


@pytest.mark.asyncio
async def test_objective_contains_degradation_terminal_availability_bm_and_tail_terms() -> None:
    rolling = await service()
    rolling.set_regime(SampleRegime.TIGHTENING)
    run = rolling.run()
    objective = run.objective_breakdown
    expected_degradation = sum(
        4.0 * (period.charge_mw + period.discharge_mw) * 0.5
        for period in run.projected_trajectory
    )
    assert objective.degradation_cost_gbp == pytest.approx(expected_degradation, abs=0.1)
    assert objective.terminal_soc_value_gbp != 0
    assert objective.upward_availability_value_gbp > 0
    assert objective.downward_availability_value_gbp > 0
    assert objective.bm_expected_activation_value_gbp > 0
    assert objective.tail_risk_penalty_gbp > 0
    assert objective.total_diagnostic_value_gbp == pytest.approx(run.objective_value_gbp)


@pytest.mark.asyncio
async def test_explanations_are_driver_specific_and_lineage_bearing() -> None:
    rolling = await service()
    run = rolling.current_optimisation()
    assert run.projected_trajectory[0].why_action.startswith("The model")
    assert "P50 exposure" in run.projected_trajectory[0].why_action
    assert run.explanation_drivers.forecast_driver
    assert run.explanation_drivers.reserve_bm_driver
    assert all(point.lineage.source_feed == "full_action_optimiser" for point in run.lineage_values)
    assert all(point.lineage.source_mode == SourceMode.SAMPLE for point in run.lineage_values)


@pytest.mark.asyncio
async def test_diagnostics_consume_current_rolling_snapshot_without_synthetic_or_mid_fallback() -> None:
    rolling = await service()
    snapshot = rolling.pipeline.current_snapshot
    live = rolling.live_state()
    assert snapshot.snapshot_id == live.state.snapshot_id
    forecast = build_forecast_layer(snapshot)
    position = build_forecast_position(snapshot).snapshot
    market = build_market_snapshot(
        snapshot,
        live_provider_status=SourceMode.ERROR,
        active_provider_mode=SourceMode.SAMPLE,
    ).snapshot
    assert forecast.latest_vintage is not None
    assert position.cockpit_snapshot_id == snapshot.snapshot_id
    assert market.cockpit_snapshot_id == snapshot.snapshot_id
    assert market.active_provider == "rolling_sample_environment"
    assert market.active_provider != "elexon"
    assert market.source_mode == SourceMode.SAMPLE
    assert all(point.lineage.source_mode != SourceMode.SYNTHETIC for point in snapshot.values)
