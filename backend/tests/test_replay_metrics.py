"""Milestone 8 — pure aggregate metrics (explicit numerators/denominators)."""

from __future__ import annotations

import pytest

from cockpit.replay_metrics import build_metrics, cumulative_pnl_series, segmentation_metrics
from cockpit.replay_models import (
    EpisodeSkipReason,
    LifecyclePath,
    ReplayEpisodeResult,
    ReplayMode,
)


def _ep(sp, *, incr=None, skip=None, submitted=True, path=LifecyclePath.FILLED, model=None, perfect=None,
        regret_model=None, regret_pf=None, verdict="HEDGE_NOW", priority="HIGH", fees=0.15, slippage=1.0,
        levels=2, exec_price=50.0, exec_vol=10.0, cashflow=None):
    return ReplayEpisodeResult(
        episode_id=f"e{sp}", replay_run_id="r", settlement_period=sp, delivery_period=f"D{sp}",
        decision_ids=() if skip else ("d",), revision_ids=() if skip else ("v",),
        simulated_order_ids=("o",) if submitted else (), lifecycle_path=(None if skip else path), skip_reason=skip,
        realised_incremental_pnl_gbp=incr, total_realised_cashflow_gbp=cashflow,
        model_pnl_gbp=model, perfect_foresight_pnl_gbp=perfect,
        regret_vs_model_gbp=regret_model, regret_vs_perfect_foresight_gbp=regret_pf,
        executed_buy_mwh=exec_vol, average_execution_price_gbp_per_mwh=exec_price,
        fees_gbp=fees, slippage_gbp=slippage, levels_consumed=levels,
        timing_verdict=verdict, timing_priority=priority,
    )


def _sample():
    return [
        _ep(1, incr=100.0, model=90.0, perfect=150.0, regret_model=-10.0, regret_pf=50.0, path=LifecyclePath.FILLED),
        _ep(2, incr=-50.0, model=-40.0, perfect=80.0, regret_model=10.0, regret_pf=130.0, path=LifecyclePath.PARTIALLY_FILLED),
        _ep(3, incr=0.0, model=0.0, perfect=20.0, regret_model=0.0, regret_pf=20.0, path=LifecyclePath.FILLED),
        _ep(4, skip=EpisodeSkipReason.MISSING_REALISED_GENERATION, submitted=False),  # excluded everywhere
        _ep(5, incr=200.0, model=180.0, perfect=200.0, regret_model=-20.0, regret_pf=0.0, path=LifecyclePath.FILLED),
    ]


def test_total_and_mean_and_median():
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, _sample())
    assert m.pnl.sample_size == 4  # skipped excluded
    assert m.pnl.total_incremental_pnl_gbp == 250.0
    assert m.pnl.mean_incremental_pnl_gbp == pytest.approx(62.5)
    assert m.pnl.median_incremental_pnl_gbp == pytest.approx(50.0)  # median of [-50,0,100,200]
    assert m.pnl.min_incremental_pnl_gbp == -50.0 and m.pnl.max_incremental_pnl_gbp == 200.0


def test_hit_rate():
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, _sample())
    assert m.hit_regret.pct_outperforming_no_action == pytest.approx(50.0)   # e1, e5
    assert m.hit_regret.pct_underperforming_no_action == pytest.approx(25.0)  # e2
    assert m.hit_regret.pct_in_line == pytest.approx(25.0)                    # e3


def test_drawdown_downside_percentile_lossfreq():
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, _sample())
    # cumulative [100, 50, 50, 250]; peak [100,100,100,250]; max drawdown 50.
    assert m.risk.max_drawdown_gbp == pytest.approx(50.0)
    assert m.risk.worst_single_period_loss_gbp == -50.0
    # downside deviation = sqrt(mean([0,2500,0,0])) = 25.
    assert m.risk.downside_deviation_gbp == pytest.approx(25.0)
    # 5th percentile of [-50,0,100,200] via linear interp = -42.5.
    assert m.risk.fifth_percentile_gbp == pytest.approx(-42.5)
    assert m.risk.loss_frequency == pytest.approx(25.0)  # 1 of 4


def test_fill_rate_slippage_vwap():
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, _sample())
    # submitted = e1,e2,e3,e5 (4); filled = e1,e3,e5 (3); partial = e2 (1).
    assert m.execution.fill_rate == pytest.approx(0.75)
    assert m.execution.partial_fill_rate == pytest.approx(0.25)
    assert m.execution.average_slippage_gbp == pytest.approx(1.0)
    assert m.execution.average_fee_gbp == pytest.approx(0.15)
    assert m.execution.volume_weighted_execution_price_gbp_per_mwh == pytest.approx(50.0)


def test_regret_and_perfect_foresight_capture():
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, _sample())
    assert m.hit_regret.mean_regret_vs_model_gbp == pytest.approx((-10 + 10 + 0 - 20) / 4)
    assert m.hit_regret.mean_regret_vs_perfect_foresight_gbp == pytest.approx((50 + 130 + 20 + 0) / 4)
    # capture = trader_sum / perfect_sum = 250 / (150+80+20+200=450).
    assert m.hit_regret.perfect_foresight_capture_ratio == pytest.approx(250.0 / 450.0, abs=1e-4)


def test_zero_denominator_capture_is_none_with_note():
    episodes = [
        _ep(1, incr=10.0, perfect=0.0),
        _ep(2, incr=-5.0, perfect=0.0),
    ]
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, episodes)
    assert m.hit_regret.perfect_foresight_capture_ratio is None
    assert "undefined" in m.hit_regret.capture_ratio_note.lower()


def test_skipped_episodes_excluded_from_denominators():
    episodes = [_ep(1, incr=100.0, perfect=100.0), _ep(2, skip=EpisodeSkipReason.NO_MATERIAL_REVISION, submitted=False)]
    m = build_metrics("r", ReplayMode.SAMPLE_REPLAY, episodes)
    assert m.pnl.sample_size == 1  # only the evaluated one
    assert m.coverage.skipped_count == 1
    assert m.coverage.total_eligible_periods == 2


def test_segmentation_counts_reconcile_to_total():
    episodes = _sample()
    segments = segmentation_metrics(episodes)
    # Every episode has a settlement_period, so that dimension partitions the full set.
    sp_segments = [s for s in segments if s.dimension == "settlement_period"]
    assert sum(s.episode_count for s in sp_segments) == len(episodes)
    # And its evaluated counts reconcile to the non-skipped episodes.
    assert sum(s.evaluated_count for s in sp_segments) == sum(1 for e in episodes if e.skip_reason is None)


def test_cumulative_series_orders_and_accumulates():
    points = cumulative_pnl_series(_sample())
    assert [p.settlement_period for p in points] == [1, 2, 3, 5]  # skipped e4 excluded, ordered
    assert points[-1].cumulative_trader_gbp == pytest.approx(250.0)
    assert all(p.cumulative_no_action_gbp == 0.0 for p in points)  # no-action baseline is flat 0
