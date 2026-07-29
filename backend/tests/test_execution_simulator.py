"""Milestone 6B — pure execution simulator (deterministic SAMPLE)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cockpit.decision_models import ExecutionStatus
from cockpit.execution_models import BookLevel, ExecutionConfig, ExecutionMode, SimulatedOrder
from cockpit.execution_simulator import simulate

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
CFG = ExecutionConfig()


def _lvl(price, volume):
    return BookLevel(price_gbp_per_mwh=price, volume_mwh=volume)


ASKS = (_lvl(72.0, 10), _lvl(73.0, 10), _lvl(74.0, 10))   # best-first for BUY
BIDS = (_lvl(70.0, 10), _lvl(69.0, 10), _lvl(68.0, 10))   # best-first for SELL


def _order(side, volume, *, limit=None, mode=ExecutionMode.REALISTIC):
    return SimulatedOrder(
        order_id="simord-x", decision_id="dec-x", submitted_at=AT, side=side,
        requested_volume_mwh=volume, limit_price=limit, execution_mode=mode,
        settlement_period=24, delivery_start=AT,
    )


def test_ideal_full_buy_fill():
    outcome = simulate(_order("BUY", 30.0, mode=ExecutionMode.IDEAL), ASKS, CFG, at=AT)
    assert outcome.execution_status is ExecutionStatus.FILLED
    assert outcome.total_filled_volume_mwh == 30.0
    assert outcome.average_fill_price_gbp_per_mwh == 72.0
    assert outcome.total_slippage_gbp == 0.0
    assert outcome.levels_consumed == 1


def test_ideal_full_sell_fill():
    outcome = simulate(_order("SELL", 30.0, mode=ExecutionMode.IDEAL), BIDS, CFG, at=AT)
    assert outcome.execution_status is ExecutionStatus.FILLED
    assert outcome.average_fill_price_gbp_per_mwh == 70.0
    assert outcome.total_slippage_gbp == 0.0


def test_realistic_multi_level_buy_fill():
    outcome = simulate(_order("BUY", 25.0), ASKS, CFG, at=AT)  # 10% haircut -> 9 per level
    assert outcome.execution_status is ExecutionStatus.FILLED
    assert outcome.levels_consumed == 3
    assert outcome.total_filled_volume_mwh == 25.0
    assert outcome.average_fill_price_gbp_per_mwh == pytest.approx((9 * 72 + 9 * 73 + 7 * 74) / 25, abs=1e-3)


def test_realistic_multi_level_sell_fill():
    outcome = simulate(_order("SELL", 25.0), BIDS, CFG, at=AT)
    assert outcome.levels_consumed == 3
    assert outcome.average_fill_price_gbp_per_mwh == pytest.approx((9 * 70 + 9 * 69 + 7 * 68) / 25, abs=1e-3)


def test_partial_fill_due_to_depth():
    outcome = simulate(_order("BUY", 40.0), ASKS, CFG, at=AT)  # only 27 available after haircut
    assert outcome.execution_status is ExecutionStatus.PARTIALLY_FILLED
    assert outcome.total_filled_volume_mwh == 27.0
    assert outcome.unfilled_volume_mwh == 13.0
    assert any("Partial" in w for w in outcome.warnings)


def test_zero_fill_due_to_limit_price():
    outcome = simulate(_order("BUY", 30.0, limit=70.0), ASKS, CFG, at=AT)  # best 72 > 70
    assert outcome.execution_status is ExecutionStatus.EXPIRED
    assert outcome.total_filled_volume_mwh == 0.0
    assert outcome.fills == ()


def test_buy_limit_price_enforcement():
    outcome = simulate(_order("BUY", 30.0, limit=72.5), ASKS, CFG, at=AT)  # only level 1 (72) qualifies
    assert outcome.execution_status is ExecutionStatus.PARTIALLY_FILLED
    assert outcome.total_filled_volume_mwh == 9.0
    assert all(fill.fill_price_gbp_per_mwh <= 72.5 for fill in outcome.fills)


def test_sell_limit_price_enforcement():
    outcome = simulate(_order("SELL", 30.0, limit=69.5), BIDS, CFG, at=AT)  # only level 1 (70) qualifies
    assert outcome.total_filled_volume_mwh == 9.0
    assert all(fill.fill_price_gbp_per_mwh >= 69.5 for fill in outcome.fills)


def test_stress_depth_haircut():
    outcome = simulate(_order("BUY", 30.0, mode=ExecutionMode.STRESS), ASKS, CFG, at=AT)  # 40% haircut -> 6 per level
    assert outcome.total_filled_volume_mwh == 18.0
    assert outcome.execution_status is ExecutionStatus.PARTIALLY_FILLED


def test_stress_adverse_price_adjustment():
    outcome = simulate(_order("BUY", 6.0, mode=ExecutionMode.STRESS), ASKS, CFG, at=AT)
    # level 1 price 72 + 8 adverse = 80.
    assert outcome.fills[0].fill_price_gbp_per_mwh == 80.0
    assert outcome.average_fill_price_gbp_per_mwh == 80.0


def test_fee_calculation():
    outcome = simulate(_order("BUY", 20.0, mode=ExecutionMode.IDEAL), ASKS, CFG, at=AT)
    assert outcome.total_fees_gbp == pytest.approx(20.0 * CFG.fee_gbp_per_mwh, abs=1e-6)


def test_slippage_calculation():
    outcome = simulate(_order("BUY", 25.0), ASKS, CFG, at=AT)
    # slippage per level vs best 72: 0, 1, 2 ; weighted by 9,9,7.
    assert outcome.total_slippage_gbp == pytest.approx(9 * 0 + 9 * 1 + 7 * 2, abs=1e-3)


def test_reproducibility():
    a = simulate(_order("BUY", 25.0, mode=ExecutionMode.STRESS), ASKS, CFG, at=AT)
    b = simulate(_order("BUY", 25.0, mode=ExecutionMode.STRESS), ASKS, CFG, at=AT)
    assert a == b


def test_outcome_and_fills_are_immutable():
    outcome = simulate(_order("BUY", 25.0), ASKS, CFG, at=AT)
    assert isinstance(outcome.fills, tuple)
    with pytest.raises(ValidationError):
        outcome.total_filled_volume_mwh = 0.0
    with pytest.raises(ValidationError):
        outcome.fills[0].filled_volume_mwh = 0.0
    assert outcome.diagnostic_only and outcome.not_executable and outcome.trustworthy_for_live_trading is False


def test_no_depth_expires():
    outcome = simulate(_order("BUY", 10.0), (), CFG, at=AT)
    assert outcome.execution_status is ExecutionStatus.EXPIRED
    assert any("No visible depth" in w for w in outcome.warnings)
