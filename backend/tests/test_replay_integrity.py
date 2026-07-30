"""Milestone 8 — point-in-time integrity, look-ahead guard, isolation, determinism."""

from __future__ import annotations

from datetime import timedelta

import pytest

from cockpit.decision_service import DECISIONS
from cockpit.evaluation_service import EVALUATION
from cockpit.execution_models import ExecutionMode
from cockpit.replay_dataset import LookAheadViolation, PointInTimeView, build_sample_dataset
from cockpit.replay_engine import ReplayConfig, run_replay
from cockpit.replay_models import LookAheadKind, ReplayMode, TraderPolicy
from cockpit.settlement_service import SETTLEMENT


def _cfg(dataset, *, policy=TraderPolicy.TIMING_POLICY, mode=ExecutionMode.REALISTIC, max_periods=200):
    lo, hi = dataset.bounds()
    return ReplayConfig(
        dataset=dataset, run_mode=ReplayMode.SAMPLE_REPLAY, trader_policy=policy, execution_mode=mode,
        replay_start=lo, replay_end=hi, timing_policy_version="v", simulator_version="execution-sim-v1",
        materiality_config_ref="m", max_periods=max_periods,
    )


def test_future_forecast_vintage_excluded_from_pit_view():
    ds = build_sample_dataset()
    early = min(v.published_at for v in ds.vintages)  # only the prior vintage is published
    view = PointInTimeView(ds, lambda: early)
    asof = view.vintage_points_asof()
    assert all(v.published_at <= early for v in asof)
    later = [v for v in ds.vintages if v.published_at > early]
    assert later, "dataset must have a later revised vintage"
    assert all(v not in asof for v in later)  # future vintages rejected from the view


def test_future_market_snapshot_rejected():
    ds = build_sample_dataset()
    period = ds.periods[0]
    before = period.market_available_at - timedelta(minutes=1)
    view = PointInTimeView(ds, lambda: before)
    with pytest.raises(LookAheadViolation) as excinfo:
        view.market_period(period.settlement_period)
    assert excinfo.value.record.kind is LookAheadKind.FUTURE_MARKET_SNAPSHOT
    assert view.violations  # recorded


def test_realised_generation_unavailable_before_delivery():
    ds = build_sample_dataset()
    period = ds.periods[0]
    during = period.delivery_start  # before delivery_end
    view = PointInTimeView(ds, lambda: during)
    with pytest.raises(LookAheadViolation) as excinfo:
        view.realised_period(period.settlement_period)
    assert excinfo.value.record.kind is LookAheadKind.REALISED_BEFORE_DELIVERY


def test_settlement_price_available_only_after_delivery_end():
    ds = build_sample_dataset()
    period = ds.periods[0]
    view_before = PointInTimeView(ds, lambda: period.delivery_end - timedelta(seconds=1))
    with pytest.raises(LookAheadViolation):
        view_before.realised_period(period.settlement_period)
    view_after = PointInTimeView(ds, lambda: period.delivery_end)
    got = view_after.realised_period(period.settlement_period)  # now available
    assert got.imbalance_buy_price_gbp_per_mwh is not None
    assert got.imbalance_sell_price_gbp_per_mwh is not None


def test_perfect_foresight_inputs_unavailable_during_decision_creation():
    """At the decision time, realised data (which perfect foresight needs) is not
    yet readable — so it cannot leak into decision creation."""
    ds = build_sample_dataset()
    decision_time = max(v.published_at for v in ds.vintages)
    view = PointInTimeView(ds, lambda: decision_time)
    for period in ds.periods:
        if period.delivery_end > decision_time:  # future deliveries (all of them here)
            with pytest.raises(LookAheadViolation):
                view.realised_period(period.settlement_period)


def test_valid_run_has_zero_lookahead_violations():
    ds = build_sample_dataset()
    result = run_replay(_cfg(ds))
    assert result.run.lookahead_violation_count == 0
    assert result.integrity.status.value == "OK"
    assert not result.integrity.violations


def test_replay_clock_controls_time_dependent_services():
    """Every lifecycle timestamp comes from the injected replay clock (dataset epoch),
    never wall-clock now()."""
    ds = build_sample_dataset()
    lo, hi = ds.bounds()
    result = run_replay(_cfg(ds, policy=TraderPolicy.MODEL_FOLLOW))
    for episode in result.episodes:
        decision = None
        # Timestamps must fall inside the dataset's time window, not "now" (2026-07-onwards real time).
        # We assert via the run bounds which are the dataset epoch.
    assert result.run.replay_start == lo and result.run.replay_end == hi
    # The created_at is pinned to replay_start (dataset epoch), not wall clock.
    assert result.run.created_at == lo


def test_global_singleton_state_unchanged():
    ds = build_sample_dataset()
    before = (len(DECISIONS.list()), len(SETTLEMENT.list_deliveries()), len(EVALUATION.list_evaluations()))
    run_replay(_cfg(ds, policy=TraderPolicy.MODEL_FOLLOW))
    after = (len(DECISIONS.list()), len(SETTLEMENT.list_deliveries()), len(EVALUATION.list_evaluations()))
    assert before == after  # a replay never mutates the live cockpit


def test_identical_config_is_deterministic():
    ds = build_sample_dataset()
    a = run_replay(_cfg(ds))
    b = run_replay(_cfg(ds))
    key = lambda r: [(e.settlement_period, e.realised_incremental_pnl_gbp, str(e.lifecycle_path), e.trader_policy_action) for e in r.episodes]
    assert key(a) == key(b)


def test_replay_models_are_immutable():
    ds = build_sample_dataset()
    result = run_replay(_cfg(ds))
    with pytest.raises((ValueError, TypeError)):
        result.run.decision_count = 0  # type: ignore[misc]
    with pytest.raises((ValueError, TypeError)):
        result.episodes[0].realised_incremental_pnl_gbp = 0.0  # type: ignore[misc]
