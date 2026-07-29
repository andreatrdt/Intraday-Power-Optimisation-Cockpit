"""Milestone 7 — pure realised cash-flow, P&L and attribution calculations."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cockpit import pnl_attribution as pa
from cockpit.settlement_models import (
    RECONCILIATION_TOLERANCE_GBP,
    DeliveryResult,
    ImbalanceDirection,
    RealisedInputs,
)

T = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)


def _inputs(**overrides) -> RealisedInputs:
    base = dict(
        decision_id="d1",
        settlement_period=17,
        delivery_start=T,
        delivery_end=T,
        delivered_at=T,
        realised_generation_mwh=100.0,
        initial_contracted_position_mwh=60.0,
        executed_buy_mwh=0.0,
        executed_sell_mwh=0.0,
        average_execution_price_gbp_per_mwh=None,
        execution_fees_gbp=0.0,
        imbalance_buy_price_gbp_per_mwh=80.0,
        imbalance_sell_price_gbp_per_mwh=50.0,
        reference_market_price_gbp_per_mwh=70.0,
    )
    base.update(overrides)
    return RealisedInputs(**base)


# --- position reconstruction ------------------------------------------------


def test_final_q_after_buy_fill():
    assert pa.reconstruct_final_position(60.0, 10.0, 0.0) == 70.0


def test_final_q_after_sell_fill():
    assert pa.reconstruct_final_position(60.0, 0.0, 10.0) == 50.0


def test_partial_fill_uses_executed_volume_only():
    # Requested 40 but only 12 executed -> final Q reflects executed 12, not 40.
    assert pa.reconstruct_final_position(60.0, 12.0, 0.0) == 72.0


def test_rejected_or_expired_leaves_q_unchanged():
    assert pa.reconstruct_final_position(60.0, 0.0, 0.0) == 60.0


# --- imbalance + direction --------------------------------------------------


def test_long_imbalance_cashflow_positive():
    imb = pa.realised_imbalance(100.0, 60.0)  # +40 LONG
    assert imb == 40.0
    assert pa.imbalance_direction(imb) is ImbalanceDirection.LONG
    assert pa.imbalance_cashflow(imb, 80.0, 50.0) == 40.0 * 50.0  # sell price for LONG


def test_short_imbalance_cashflow_negative():
    imb = pa.realised_imbalance(40.0, 60.0)  # -20 SHORT
    assert imb == -20.0
    assert pa.imbalance_direction(imb) is ImbalanceDirection.SHORT
    assert pa.imbalance_cashflow(imb, 80.0, 50.0) == -20.0 * 80.0  # buy price for SHORT (negative cash)


def test_flat_imbalance_direction():
    assert pa.imbalance_direction(0.0) is ImbalanceDirection.FLAT


# --- execution cash flow ----------------------------------------------------


def test_execution_buy_cashflow_negative():
    assert pa.execution_cashflow(10.0, 0.0, 50.0) == -500.0


def test_execution_sell_cashflow_positive():
    assert pa.execution_cashflow(0.0, 10.0, 50.0) == 500.0


def test_execution_cashflow_zero_without_price():
    assert pa.execution_cashflow(0.0, 0.0, None) == 0.0


def test_fees_deducted_correctly():
    total = pa.total_realised_cashflow(execution_cf_gbp=-500.0, imbalance_cf_gbp=200.0, fees_gbp=1.5)
    assert total == -500.0 + 200.0 - 1.5


# --- incremental P&L --------------------------------------------------------


def test_no_action_incremental_pnl_is_zero():
    figures = pa.compute_settlement(_inputs(executed_buy_mwh=0.0, executed_sell_mwh=0.0))
    assert figures.realised_pnl_gbp == 0.0


def test_incremental_pnl_matches_definition():
    inp = _inputs(executed_buy_mwh=10.0, average_execution_price_gbp_per_mwh=55.0, execution_fees_gbp=1.5)
    figures = pa.compute_settlement(inp)
    no_action = pa.no_action_total_cashflow(inp.realised_generation_mwh, inp.initial_contracted_position_mwh, 80.0, 50.0)
    assert figures.realised_pnl_gbp == pytest.approx(figures.total_realised_cashflow_gbp - no_action)


# --- attribution reconciliation ---------------------------------------------


@pytest.mark.parametrize(
    "gen, initial_q, buy, sell, price, fees",
    [
        (100.0, 60.0, 10.0, 0.0, 55.0, 1.5),   # LONG stays LONG
        (40.0, 60.0, 0.0, 15.0, 52.0, 2.25),   # SHORT, sells
        (70.0, 60.0, 30.0, 0.0, 48.0, 4.5),    # crosses LONG -> SHORT (sign flip -> residual term)
        (60.0, 60.0, 0.0, 0.0, None, 0.0),     # no trade, exactly flat before
        (55.0, 60.0, 20.0, 0.0, 61.0, 3.0),    # SHORT -> more short
    ],
)
def test_attribution_reconciles(gen, initial_q, buy, sell, price, fees):
    inp = _inputs(
        realised_generation_mwh=gen,
        initial_contracted_position_mwh=initial_q,
        executed_buy_mwh=buy,
        executed_sell_mwh=sell,
        average_execution_price_gbp_per_mwh=price,
        execution_fees_gbp=fees,
    )
    attr = pa.compute_attribution(inp)
    components = (
        attr.execution_price_effect_gbp
        + attr.execution_fees_effect_gbp
        + attr.imbalance_reduction_effect_gbp
        + attr.imbalance_residual_effect_gbp
    )
    assert components == pytest.approx(attr.total_incremental_pnl_gbp, abs=RECONCILIATION_TOLERANCE_GBP)
    assert abs(attr.reconciliation_error_gbp) <= RECONCILIATION_TOLERANCE_GBP
    assert attr.reconciled


def test_attribution_total_matches_settlement_realised_pnl():
    inp = _inputs(executed_buy_mwh=10.0, average_execution_price_gbp_per_mwh=55.0, execution_fees_gbp=1.5)
    figures = pa.compute_settlement(inp)
    assert figures.attribution.total_incremental_pnl_gbp == pytest.approx(figures.realised_pnl_gbp)


def test_no_sign_flip_has_zero_residual():
    # SHORT stays SHORT after a buy -> imbalance_residual_effect is exactly zero.
    inp = _inputs(realised_generation_mwh=40.0, initial_contracted_position_mwh=60.0, executed_buy_mwh=5.0, average_execution_price_gbp_per_mwh=50.0)
    attr = pa.compute_attribution(inp)
    assert attr.imbalance_residual_effect_gbp == 0.0


# --- immutability -----------------------------------------------------------


def test_models_are_immutable():
    inp = _inputs()
    figures = pa.compute_settlement(inp)
    with pytest.raises((ValueError, TypeError)):
        inp.realised_generation_mwh = 1.0  # type: ignore[misc]
    with pytest.raises((ValueError, TypeError)):
        figures.attribution.total_incremental_pnl_gbp = 0.0  # type: ignore[misc]
    delivery = DeliveryResult(
        delivery_id="x", decision_id="d1", settlement_period=1, delivery_start=T, delivery_end=T, delivered_at=T,
        initial_contracted_position_mwh=60.0, executed_buy_mwh=0.0, executed_sell_mwh=0.0,
        final_contracted_position_mwh=60.0, realised_generation_mwh=100.0, realised_imbalance_mwh=40.0,
        imbalance_direction=ImbalanceDirection.LONG,
    )
    with pytest.raises((ValueError, TypeError)):
        delivery.realised_generation_mwh = 0.0  # type: ignore[misc]
