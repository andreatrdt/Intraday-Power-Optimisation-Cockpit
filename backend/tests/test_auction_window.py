from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cockpit.full_action_optimiser import FullActionConfig
from cockpit.models import HorizonMode, SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.rolling_service import RollingService
from cockpit.settlement import auction_window_periods, daily_auction_boundaries
from cockpit.simulated_environment import SimulatedEnvironment

UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("as_of", "previous", "following"),
    [
        (datetime(2026, 7, 22, 9, 30, tzinfo=UTC), datetime(2026, 7, 21, 14, 0, tzinfo=UTC), datetime(2026, 7, 22, 14, 0, tzinfo=UTC)),
        (datetime(2026, 7, 22, 15, 30, tzinfo=UTC), datetime(2026, 7, 22, 14, 0, tzinfo=UTC), datetime(2026, 7, 23, 14, 0, tzinfo=UTC)),
        (datetime(2026, 1, 10, 15, 0, tzinfo=UTC), datetime(2026, 1, 10, 15, 0, tzinfo=UTC), datetime(2026, 1, 11, 15, 0, tzinfo=UTC)),
    ],
)
def test_previous_and_next_daily_1500_uk_auction(as_of: datetime, previous: datetime, following: datetime) -> None:
    assert daily_auction_boundaries(as_of) == (previous, following)


def test_auction_window_is_dst_aware() -> None:
    spring = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
    autumn = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    spring_previous, spring_next = daily_auction_boundaries(spring)
    autumn_previous, autumn_next = daily_auction_boundaries(autumn)
    assert spring_previous == datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
    assert spring_next == datetime(2026, 3, 29, 14, 0, tzinfo=UTC)
    assert autumn_previous == datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
    assert autumn_next == datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
    assert len(auction_window_periods(spring)) == 46
    assert len(auction_window_periods(autumn)) == 50


@pytest.mark.asyncio
async def test_run_exposes_complete_auction_window_and_chart_ready_paths() -> None:
    as_of = datetime(2026, 7, 21, 16, 10, tzinfo=UTC)
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    rolling = RollingService(pipeline, SimulatedEnvironment(clock=lambda: as_of))
    rolling.initialise()
    run = rolling.current_optimisation()

    assert rolling.live_state().state.horizon_mode == HorizonMode.NEXT_AUCTION
    assert run.auction_boundary_time == "15:00 UK time"
    assert run.visual_window_start == datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    assert run.visual_window_end == datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    assert run.optimisation_window_start == as_of
    assert run.optimisation_window_end == run.next_auction_time
    assert run.now_marker_time == as_of
    assert run.number_of_sps_shown == 48
    assert len(run.battery_path_series) == len(run.position_path_series) == len(run.market_execution_series) == len(run.risk_value_series) == 48
    assert len(run.interaction_points) == 48
    assert len({point.stable_sp_id for point in run.interaction_points}) == 48
    assert all(point.display_label.startswith("SP") for point in run.interaction_points)
    assert all(point.uk_delivery_time.endswith("UK time") for point in run.interaction_points)
    assert all(point.linked_trajectory_row_id.startswith("trajectory-") for point in run.interaction_points)
    assert all(point.tooltip_payload.get("position_reason") for point in run.interaction_points)
    assert all(point.tooltip_payload.get("battery_reason") for point in run.interaction_points)
    assert all(point.source_mode == SourceMode.SAMPLE for point in run.interaction_points)
    assert any(point.phase == "historical_simulated" for point in run.position_path_series)
    assert any(point.phase == "current" for point in run.position_path_series)
    assert any(point.phase == "optimised_future" for point in run.position_path_series)

    historical_ids = {point.delivery_period for point in run.position_path_series if point.phase.startswith("historical_")}
    solved_ids = {point.delivery_period for point in run.projected_trajectory}
    assert historical_ids.isdisjoint(solved_ids)
    assert all(point.timestamp >= run.visual_window_start for point in run.position_path_series)
    assert all(point.timestamp < run.visual_window_end for point in run.position_path_series)

    cfg = FullActionConfig()
    positions = {point.delivery_period: point for point in run.position_path_series}
    batteries = {point.delivery_period: point for point in run.battery_path_series}
    markets = {point.delivery_period: point for point in run.market_execution_series}
    for result in run.projected_trajectory:
        position = positions[result.delivery_period]
        battery = batteries[result.delivery_period]
        market = markets[result.delivery_period]
        assert position.q_after_mwh == pytest.approx(position.q_before_mwh + position.sell_mwh - position.buy_mwh, abs=1e-3)
        expected_soc = battery.soc_start_mwh + cfg.charge_efficiency * battery.charge_mw * 0.5 - battery.discharge_mw * 0.5 / cfg.discharge_efficiency
        assert battery.soc_end_mwh == pytest.approx(expected_soc, abs=1e-3)
        assert battery.upward_headroom_mw >= 0
        assert battery.downward_headroom_mw >= 0
        if not position.market_action_allowed:
            assert position.buy_mwh == pytest.approx(0)
            assert position.sell_mwh == pytest.approx(0)
        if position.sell_mwh > 0:
            assert market.consumed_bid_depth_mwh == pytest.approx(position.sell_mwh)
            assert market.consumed_ask_depth_mwh == pytest.approx(0)
        if position.buy_mwh > 0:
            assert market.consumed_ask_depth_mwh == pytest.approx(position.buy_mwh)
            assert market.consumed_bid_depth_mwh == pytest.approx(0)
        assert market.executable_data_mode == SourceMode.SAMPLE
        assert market.reference_price_mode == SourceMode.SAMPLE

    future_battery = [point for point in run.battery_path_series if point.phase == "optimised_future"]
    assert future_battery
    headroom_totals = {round(point.upward_headroom_mw + point.downward_headroom_mw, 6) for point in future_battery}
    if len(headroom_totals) == 1:
        assert all(point.flat_path_explanation and "Headroom is flat" in point.flat_path_explanation for point in future_battery)
    if sum(point.charge_mwh + point.discharge_mwh for point in future_battery) < 1e-6:
        assert all(point.flat_path_explanation and "SoC flat" in point.flat_path_explanation for point in future_battery)
    assert run.whole_path_explanation
    assert run.rolling_run_ledger
    assert run.historical_history_available
    assert run.historical_soc_reconciled
    assert run.historical_q_reconciled
    assert not run.reconciliation_warnings
    assert all(item.optimisation_run_id for item in run.rolling_run_ledger)
    assert all(item.lineage_value_ids for item in run.rolling_run_ledger)
