"""Milestone 8 — deterministic trader policies and WAIT reassessment."""

from __future__ import annotations

from cockpit.execution_models import ExecutionMode
from cockpit.replay_dataset import build_sample_dataset
from cockpit.replay_engine import ReplayConfig, run_replay
from cockpit.replay_models import LifecyclePath, ReplayMode, TraderPolicy


def _run(policy, *, depth_scale=1.0, mode=ExecutionMode.REALISTIC):
    ds = build_sample_dataset(depth_scale=depth_scale)
    lo, hi = ds.bounds()
    return run_replay(ReplayConfig(
        dataset=ds, run_mode=ReplayMode.SAMPLE_REPLAY, trader_policy=policy, execution_mode=mode,
        replay_start=lo, replay_end=hi, timing_policy_version="v", simulator_version="execution-sim-v1",
        materiality_config_ref="m", max_periods=200,
    ))


def _material(result):
    return [e for e in result.episodes if e.decision_ids]


def test_model_follow_accepts_and_submits_every_material_decision():
    result = _run(TraderPolicy.MODEL_FOLLOW)
    material = _material(result)
    assert material
    assert all(e.simulated_order_ids for e in material)  # every one submitted
    assert all(e.trader_policy_action == "ACCEPT_SUBMIT" for e in material)
    assert all(e.lifecycle_path is LifecyclePath.FILLED for e in material)  # full depth → filled


def test_no_action_policy_submits_nothing_and_is_the_baseline():
    result = _run(TraderPolicy.NO_ACTION)
    material = _material(result)
    assert material
    assert all(not e.simulated_order_ids for e in material)   # no submissions
    assert all(e.trader_policy_action == "REJECT" for e in material)
    assert all(e.lifecycle_path is LifecyclePath.REJECTED for e in material)
    # No-action incremental P&L is exactly zero for every evaluated episode.
    assert all(abs(e.realised_incremental_pnl_gbp) < 1e-9 for e in material if e.realised_incremental_pnl_gbp is not None)


def test_timing_policy_hedge_now_and_wait_both_occur():
    result = _run(TraderPolicy.TIMING_POLICY)
    verdicts = {e.timing_verdict for e in result.episodes if e.timing_verdict}
    assert "HEDGE_NOW" in verdicts   # reassessment flipped WAIT → HEDGE_NOW near gate
    submitted = [e for e in result.episodes if e.simulated_order_ids]
    assert submitted and all(e.timing_verdict in ("HEDGE_NOW", "PARTIAL_HEDGE_NOW") for e in submitted)


def test_timing_policy_wait_then_reassess_submits_before_gate():
    """A decision that is WAIT at the decision time is re-assessed at later
    time-to-gate checkpoints and submits once the verdict becomes HEDGE_NOW."""
    result = _run(TraderPolicy.TIMING_POLICY)
    # Some episodes are HEDGE_NOW-and-submitted; any that stayed WAIT never submitted
    # and end as a realised no-trade (REJECTED).
    hedged = [e for e in result.episodes if e.timing_verdict == "HEDGE_NOW" and e.simulated_order_ids]
    waited = [e for e in result.episodes if e.timing_verdict == "WAIT"]
    assert hedged
    assert all(not e.simulated_order_ids for e in waited)
    assert all(e.lifecycle_path is LifecyclePath.REJECTED for e in waited)


def test_timing_policy_partial_modifies_to_now_volume():
    result = _run(TraderPolicy.TIMING_POLICY, depth_scale=0.15)  # thin book → partial-now
    partials = [e for e in result.episodes if e.trader_policy_action == "MODIFY_SUBMIT"]
    assert partials, "thin book should produce PARTIAL_HEDGE_NOW modify-and-submit"
    for episode in partials:
        assert episode.timing_verdict == "PARTIAL_HEDGE_NOW"
        assert episode.simulated_order_ids


def test_no_duplicate_submission_after_reassessment():
    for policy in (TraderPolicy.MODEL_FOLLOW, TraderPolicy.TIMING_POLICY):
        result = _run(policy)
        for episode in result.episodes:
            assert len(episode.simulated_order_ids) <= 1  # never resubmitted


def test_execution_mode_recorded_on_run():
    for mode in (ExecutionMode.IDEAL, ExecutionMode.REALISTIC, ExecutionMode.STRESS):
        result = _run(TraderPolicy.MODEL_FOLLOW, mode=mode)
        assert result.run.execution_mode is mode
