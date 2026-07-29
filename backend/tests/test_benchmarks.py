"""Milestone 7 — pure benchmark calculations, labelling and regret sign."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cockpit import benchmarks as bm
from cockpit.settlement_models import BenchmarkName, ImbalanceDirection, RealisedInputs

T = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)


def _inputs(**overrides) -> RealisedInputs:
    base = dict(
        decision_id="d1", settlement_period=17, delivery_start=T, delivery_end=T, delivered_at=T,
        realised_generation_mwh=45.0, initial_contracted_position_mwh=60.0,
        executed_buy_mwh=10.0, executed_sell_mwh=0.0, average_execution_price_gbp_per_mwh=55.0,
        execution_fees_gbp=1.5, imbalance_buy_price_gbp_per_mwh=80.0, imbalance_sell_price_gbp_per_mwh=60.0,
        reference_market_price_gbp_per_mwh=70.0,
    )
    base.update(overrides)
    return RealisedInputs(**base)


def test_benchmark_set_names_and_order():
    results = bm.compute_benchmarks(_inputs(), model_buy_mwh=10.0, model_sell_mwh=0.0, fee_gbp_per_mwh=0.15)
    assert [b.benchmark_name for b in results] == [
        BenchmarkName.NO_ACTION,
        BenchmarkName.MODEL_RECOMMENDATION,
        BenchmarkName.TRADER_INSTRUCTION,
        BenchmarkName.PERFECT_FORESIGHT,
    ]


def test_no_action_incremental_is_zero_and_baseline():
    result = bm.no_action_benchmark(_inputs())
    assert result.incremental_pnl_vs_no_action_gbp == 0.0
    assert result.hedge_buy_mwh == 0.0 and result.hedge_sell_mwh == 0.0
    assert result.attainable is True and result.hindsight_only is False


def test_trader_instruction_reproduces_actual_execution():
    inp = _inputs()
    result = bm.trader_instruction_benchmark(inp)
    assert result.hedge_buy_mwh == inp.executed_buy_mwh
    assert result.hedge_sell_mwh == inp.executed_sell_mwh
    assert result.execution_price_gbp_per_mwh == inp.average_execution_price_gbp_per_mwh
    assert result.assumed_execution_mode == "ACTUAL_SIMULATION"


def test_model_recommendation_labelled_ideal_not_silent():
    result = bm.model_recommendation_benchmark(_inputs(), model_buy_mwh=12.0, model_sell_mwh=0.0, fee_gbp_per_mwh=0.15)
    assert result.assumed_execution_mode == "IDEAL"
    assert result.attainable is True and result.hindsight_only is False
    assert any("IDEAL" in a for a in result.assumptions)


def test_perfect_foresight_labelled_unattainable_hindsight():
    result = bm.perfect_foresight_benchmark(_inputs(), fee_gbp_per_mwh=0.15)
    assert result.attainable is False
    assert result.hindsight_only is True
    assert result.assumed_execution_mode == "HINDSIGHT_IDEAL"
    assert any("not attainable" in w.lower() for w in result.warnings)


def test_perfect_foresight_drives_imbalance_to_zero():
    inp = _inputs()
    result = bm.perfect_foresight_benchmark(inp, fee_gbp_per_mwh=0.15)
    assert result.realised_imbalance_mwh == pytest.approx(0.0)
    assert result.imbalance_direction is ImbalanceDirection.FLAT
    # It must be an upper bound: never worse than no-action.
    assert result.incremental_pnl_vs_no_action_gbp >= -1e-6


def test_regret_sign_convention():
    """regret = benchmark_incremental − realised_incremental; positive ⇒ benchmark better."""
    inp = _inputs()
    results = {b.benchmark_name: b for b in bm.compute_benchmarks(inp, model_buy_mwh=10.0, model_sell_mwh=0.0, fee_gbp_per_mwh=0.15)}
    realised = results[BenchmarkName.TRADER_INSTRUCTION].incremental_pnl_vs_no_action_gbp
    regret_pf = results[BenchmarkName.PERFECT_FORESIGHT].incremental_pnl_vs_no_action_gbp - realised
    # Perfect foresight is the upper bound, so its regret vs the realised decision is >= 0.
    assert regret_pf >= -1e-6
    # Regret vs the trader instruction (itself) is exactly zero.
    assert results[BenchmarkName.TRADER_INSTRUCTION].incremental_pnl_vs_no_action_gbp - realised == 0.0


def test_benchmarks_use_immutable_tuples():
    result = bm.no_action_benchmark(_inputs())
    assert isinstance(result.warnings, tuple)
    assert isinstance(result.assumptions, tuple)
    with pytest.raises((ValueError, TypeError)):
        result.total_cashflow_gbp = 0.0  # type: ignore[misc]
