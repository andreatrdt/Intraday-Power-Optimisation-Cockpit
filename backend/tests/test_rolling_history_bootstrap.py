from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cockpit.models import SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.rolling_service import RollingService
from cockpit.simulated_environment import SimulatedEnvironment


UTC = ZoneInfo("UTC")


@pytest.mark.asyncio
async def test_sample_bootstrap_applies_only_each_past_runs_first_action() -> None:
    as_of = datetime(2026, 7, 21, 17, 10, tzinfo=UTC)
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    rolling = RollingService(pipeline, SimulatedEnvironment(clock=lambda: as_of))
    rolling.initialise()
    run = rolling.current_optimisation()
    ledger = run.rolling_run_ledger

    assert len(ledger) == 6
    fresh_steps = [item for item in ledger if item.fresh_decision_step]
    assert len(fresh_steps) == 2
    assert all(item.decision_cadence_minutes == 120 for item in ledger)
    assert all(item.phase == "HISTORICAL_SIMULATED" for item in ledger)
    for index, item in enumerate(ledger):
        historical_run = rolling.get_run(item.optimisation_run_id)
        assert historical_run is not None
        if item.fresh_decision_step:
            first = historical_run.projected_trajectory[0]
            assert first.delivery_period == item.delivery_period
            assert item.buy_mwh == pytest.approx(first.buy_mwh)
            assert item.sell_mwh == pytest.approx(first.sell_mwh)
            assert item.charge_mw == pytest.approx(first.charge_mw)
            assert item.discharge_mw == pytest.approx(first.discharge_mw)
        else:
            assert item.buy_mwh == item.sell_mwh == 0
            assert item.charge_mw == item.discharge_mw == 0
            assert "configured 120-minute historical cadence" in item.explanation
        assert item.decision_time <= item.delivery_start
        assert all(point.lineage.retrieved_at <= item.decision_time for point in historical_run.inputs[0].values.values())
        if index:
            assert item.soc_start_mwh == pytest.approx(ledger[index - 1].soc_end_mwh)
            assert item.q_before_mwh == pytest.approx(ledger[index - 1].q_after_mwh)

    assert any(
        abs(item.buy_mwh) + abs(item.sell_mwh) + abs(item.charge_mw) + abs(item.discharge_mw) > 1e-6
        for item in ledger
    )
    assert any(abs(item.buy_mwh) + abs(item.sell_mwh) > 1e-6 for item in fresh_steps)
    assert any(abs(item.charge_mw) + abs(item.discharge_mw) > 1e-6 for item in fresh_steps)
    assert run.starting_state.starting_soc_mwh == pytest.approx(ledger[-1].soc_end_mwh)
    assert run.starting_state.starting_q_mwh == pytest.approx(ledger[-1].q_after_mwh)
    assert run.historical_soc_reconciled
    assert run.historical_q_reconciled
    assert any(point.phase == "historical_simulated" for point in run.battery_path_series)
    assert any(point.phase == "optimised_future" for point in run.battery_path_series)
    assert any(point.phase == "historical_simulated" for point in run.position_path_series)
    assert any(point.phase == "optimised_future" for point in run.position_path_series)
    assert all(
        point.tooltip_payload.get("historical_run_id")
        for point in run.interaction_points
        if point.delivery_period in {item.delivery_period for item in ledger}
    )


@pytest.mark.asyncio
async def test_reconciliation_warning_and_live_history_non_fabrication() -> None:
    as_of = datetime(2026, 7, 21, 17, 10, tzinfo=UTC)
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    environment = SimulatedEnvironment(clock=lambda: as_of)
    rolling = RollingService(pipeline, environment)
    rolling.initialise()
    run = rolling.current_optimisation()

    environment.rolling_run_ledger[-1].soc_end_mwh += 1.0
    mismatch = environment.populate_auction_window(run.model_copy(deep=True))
    assert "Historical SoC reconciliation mismatch." in mismatch.reconciliation_warnings

    live_run = run.model_copy(deep=True)
    live_run.starting_state.source_mode = SourceMode.LIVE
    unavailable = environment.populate_auction_window(live_run)
    assert not any(point.phase.startswith("historical_") for point in unavailable.position_path_series)
    assert not unavailable.historical_history_available
    assert unavailable.historical_history_message == (
        "No prior optimisation run history available before this session."
    )
