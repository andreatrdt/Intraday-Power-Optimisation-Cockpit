from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cockpit.models import HorizonMode, SampleRegime
from cockpit.pipeline import DataFlowPipeline
from cockpit.rolling_service import RollingService
from cockpit.settlement import UTC, settlement_period_for_instant
from cockpit.simulated_environment import SimulatedEnvironment


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


async def service_at(value: datetime) -> tuple[RollingService, MutableClock]:
    clock = MutableClock(value)
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    rolling = RollingService(pipeline, SimulatedEnvironment(clock=clock))
    rolling.initialise()
    return rolling, clock


def test_backend_time_maps_to_dst_aware_gb_settlement_period() -> None:
    summer = settlement_period_for_instant(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    winter = settlement_period_for_instant(datetime(2026, 1, 22, 12, 10, tzinfo=UTC))
    assert summer.settlement_period == 27  # 13:10 UK time
    assert winter.settlement_period == 25  # 12:10 UK time


@pytest.mark.asyncio
async def test_horizon_starts_from_backend_now_and_shifts_without_manual_advance() -> None:
    rolling, clock = await service_at(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    before = rolling.current_optimisation()
    assert before.starting_state.current_settlement_period == 27
    assert before.projected_trajectory[0].settlement_period == 28
    assert before.projected_trajectory[0].settlement_period != 1

    old_dump = before.model_dump(mode="json")
    clock.advance(minutes=51)
    refreshed = rolling.refresh()
    after = rolling.current_optimisation()
    assert refreshed.state.current_time == clock.value
    assert after.run_id != before.run_id
    assert after.projected_trajectory[0].settlement_period > before.projected_trajectory[0].settlement_period
    assert rolling.get_run(before.run_id).model_dump(mode="json") == old_dump


@pytest.mark.asyncio
async def test_refresh_applies_completed_previous_soc_and_market_actions() -> None:
    rolling, clock = await service_at(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    rolling.set_regime(SampleRegime.TIGHTENING)
    selected = rolling.current_optimisation()
    action = selected.projected_trajectory[0]
    clock.value = action.delivery_end + timedelta(seconds=1)
    live = rolling.refresh()
    assert live.portfolio_battery.current_soc_mwh == pytest.approx(action.projected_soc_mwh, abs=1e-3)
    assert live.state.last_soc_change_mwh == pytest.approx(action.projected_soc_mwh - action.soc_before_mwh, abs=1e-3)
    assert live.state.last_q_change_mwh == pytest.approx(action.sell_mwh - action.buy_mwh, abs=1e-3)
    assert live.state.previous_run_id == selected.run_id


@pytest.mark.asyncio
async def test_refresh_updates_forecast_market_and_non_flat_history_series() -> None:
    rolling, _ = await service_at(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    before = rolling.live_state().model_copy(deep=True)
    after = rolling.refresh()
    assert after.state.current_forecast_vintage_id != before.state.current_forecast_vintage_id
    assert after.state.current_market_snapshot_id != before.state.current_market_snapshot_id
    assert len(after.history) >= 24
    for key in ("production", "demand", "forecast_vintage", "market_price", "market_depth", "frequency", "portfolio", "battery"):
        assert after.chart_series[key]
        assert all(series.unit for series in after.chart_series[key])
    assert len({point.value for point in after.chart_series["production"][0].points}) > 1
    assert len({point.latest_p50_mwh for point in after.forecast_vintage_series}) > 1


def test_horizon_modes_and_explicit_auction_fallback() -> None:
    clock = MutableClock(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    environment = SimulatedEnvironment(clock=clock)
    live, periods, _ = environment.reset()
    assert len(periods) == 8
    assert live.state.horizon_mode == HorizonMode.NEXT_8_PERIODS

    auction_live, auction_periods, _ = environment.set_horizon_mode(HorizonMode.NEXT_AUCTION)
    assert len(auction_periods) == 8
    assert auction_live.state.effective_horizon_mode == HorizonMode.NEXT_8_PERIODS
    assert auction_live.state.horizon_warning

    end_live, end_periods, _ = environment.set_horizon_mode(HorizonMode.END_OF_DAY)
    assert len(end_periods) > 8
    assert len({period.delivery_start.astimezone().date() for period in end_periods}) <= 2
    assert end_live.state.optimisation_horizon_end == end_periods[-1].delivery_end


@pytest.mark.asyncio
async def test_gate_closed_period_has_no_market_trade() -> None:
    # At 13:27 UK time, Gate Closure for SP28 (13:30 delivery) has passed.
    rolling, _ = await service_at(datetime(2026, 7, 22, 12, 27, tzinfo=UTC))
    run = rolling.current_optimisation()
    assert run.inputs[0].tradeable is False
    assert run.projected_trajectory[0].buy_mwh == pytest.approx(0)
    assert run.projected_trajectory[0].sell_mwh == pytest.approx(0)


@pytest.mark.asyncio
async def test_chart_series_risk_drivers_and_wap_match_solution() -> None:
    rolling, _ = await service_at(datetime(2026, 7, 22, 12, 10, tzinfo=UTC))
    rolling.set_regime(SampleRegime.OVERSUPPLY)
    run = rolling.current_optimisation()
    assert all(series.unit for group in run.chart_series.values() for series in group)
    assert run.risk_measures
    assert {item.key for item in run.risk_measures} >= {"largest_short", "largest_long", "tail_penalty", "binding_count"}
    assert len(run.driver_contributions) == 8
    assert len(run.sensitivities) == 7
    soc_series = next(series for series in run.chart_series["soc_path"] if series.key == "soc")
    assert [point.value for point in soc_series.points] == pytest.approx([period.projected_soc_mwh for period in run.projected_trajectory])
    for source, result in zip(run.inputs, run.projected_trajectory, strict=True):
        if result.sell_mwh > 1e-6:
            remaining, cash = result.sell_mwh, 0.0
            for level in source.bids:
                take = min(remaining, level.volume_mwh)
                cash += take * level.price_gbp_per_mwh
                remaining -= take
                if remaining <= 1e-7:
                    break
            assert result.market_wap_gbp_per_mwh == pytest.approx(cash / result.sell_mwh, abs=1e-3)
