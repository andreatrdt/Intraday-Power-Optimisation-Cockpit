"""Pure realised cash-flow and P&L calculations (Milestone 7).

No I/O, no lifecycle, no storage — just the signed arithmetic that turns realised
inputs into settlement figures and an incremental-P&L attribution. All sign
conventions are defined in :mod:`cockpit.settlement_models` and repeated at each
function. Realised-P&L calculations live **only** here (and its benchmark sibling
:mod:`cockpit.benchmarks`), never in the decision/execution/orchestrator/API code.

Conventions:

* ``final_Q = initial_Q + executed_buy - executed_sell`` (executed, not requested).
* ``I_t = G_t - Q_t``; ``I_t > 0`` LONG, ``I_t < 0`` SHORT, ``≈0`` FLAT.
* Cash received positive; cash paid negative.
* Execution: ``+ sell×price − buy×price`` (fees excluded, subtracted separately).
* Imbalance: ``I_t × (sell_price if I_t ≥ 0 else buy_price)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from cockpit.settlement_models import (
    IMBALANCE_FLAT_TOLERANCE_MWH,
    ImbalanceDirection,
    RealisedInputs,
    RealisedPnlAttribution,
)


# ---------------------------------------------------------------------------
# Position, imbalance, direction
# ---------------------------------------------------------------------------


def reconstruct_final_position(initial_q_mwh: float, executed_buy_mwh: float, executed_sell_mwh: float) -> float:
    """final Q = initial Q + executed BUY − executed SELL (executed volume only).

    For rejected / expired / zero-filled decisions ``executed_buy == executed_sell
    == 0`` so ``final_Q == initial_Q`` — the pre-decision contracted position is
    unchanged.
    """
    return initial_q_mwh + executed_buy_mwh - executed_sell_mwh


def realised_imbalance(realised_generation_mwh: float, final_contracted_position_mwh: float) -> float:
    """I_t = G_t − Q_t (positive = LONG generation, negative = SHORT)."""
    return realised_generation_mwh - final_contracted_position_mwh


def imbalance_direction(imbalance_mwh: float, *, tolerance_mwh: float = IMBALANCE_FLAT_TOLERANCE_MWH) -> ImbalanceDirection:
    if imbalance_mwh > tolerance_mwh:
        return ImbalanceDirection.LONG
    if imbalance_mwh < -tolerance_mwh:
        return ImbalanceDirection.SHORT
    return ImbalanceDirection.FLAT


# ---------------------------------------------------------------------------
# Cash flows (cash received positive)
# ---------------------------------------------------------------------------


def execution_cashflow(executed_buy_mwh: float, executed_sell_mwh: float, average_price_gbp_per_mwh: float | None) -> float:
    """Signed execution cash flow, fees excluded.

    BUY pays: ``− executed_buy × price``. SELL receives: ``+ executed_sell × price``.
    The simulated order is one-sided, so at most one leg is non-zero. Returns 0 when
    nothing executed (``price is None``).
    """
    if average_price_gbp_per_mwh is None:
        return 0.0
    return executed_sell_mwh * average_price_gbp_per_mwh - executed_buy_mwh * average_price_gbp_per_mwh


def imbalance_price_for(imbalance_mwh: float, imbalance_buy_price: float, imbalance_sell_price: float) -> float:
    """The applicable imbalance price: sell price when LONG (I≥0), buy price when SHORT."""
    return imbalance_sell_price if imbalance_mwh >= 0 else imbalance_buy_price


def imbalance_cashflow(imbalance_mwh: float, imbalance_buy_price: float, imbalance_sell_price: float) -> float:
    """LONG (I≥0): ``I × sell_price`` (receive for surplus).
    SHORT (I<0): ``I × buy_price`` — negative I × positive price ⇒ a payment.
    """
    return imbalance_mwh * imbalance_price_for(imbalance_mwh, imbalance_buy_price, imbalance_sell_price)


def total_realised_cashflow(execution_cf_gbp: float, imbalance_cf_gbp: float, fees_gbp: float) -> float:
    """execution + imbalance − fees. This is the raw realised trading cash flow,
    NOT labelled P&L (see :func:`incremental_pnl_vs_no_action`)."""
    return execution_cf_gbp + imbalance_cf_gbp - fees_gbp


def no_action_total_cashflow(
    realised_generation_mwh: float,
    initial_contracted_position_mwh: float,
    imbalance_buy_price: float,
    imbalance_sell_price: float,
) -> float:
    """NO_ACTION keeps the pre-decision contracted position: no execution, no fees,
    the whole ``G − initial_Q`` settles at imbalance prices."""
    imb = realised_imbalance(realised_generation_mwh, initial_contracted_position_mwh)
    return imbalance_cashflow(imb, imbalance_buy_price, imbalance_sell_price)


def incremental_pnl_vs_no_action(decision_total_cashflow_gbp: float, no_action_total_cashflow_gbp: float) -> float:
    """Incremental realised P&L = decision cash flow − no-action cash flow.

    Zero when the decision did not trade (executed == 0), because then the decision
    and no-action cash flows are identical."""
    return decision_total_cashflow_gbp - no_action_total_cashflow_gbp


# ---------------------------------------------------------------------------
# Combined settlement figures + attribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementFigures:
    """All computed settlement numbers for one period (assembled into a
    :class:`~cockpit.settlement_models.SettlementCalculation` by the service)."""

    final_contracted_position_mwh: float
    realised_imbalance_mwh: float
    imbalance_direction: ImbalanceDirection
    execution_cashflow_gbp: float
    execution_fees_gbp: float
    imbalance_cashflow_gbp: float
    total_realised_cashflow_gbp: float
    no_action_total_cashflow_gbp: float
    realised_pnl_gbp: float  # incremental vs NO_ACTION
    attribution: RealisedPnlAttribution


def compute_settlement(inputs: RealisedInputs) -> SettlementFigures:
    """Compute every settlement figure from realised inputs, purely."""
    buy = inputs.executed_buy_mwh
    sell = inputs.executed_sell_mwh
    fees = inputs.execution_fees_gbp
    buy_price = inputs.imbalance_buy_price_gbp_per_mwh
    sell_price = inputs.imbalance_sell_price_gbp_per_mwh

    final_q = reconstruct_final_position(inputs.initial_contracted_position_mwh, buy, sell)
    imb = realised_imbalance(inputs.realised_generation_mwh, final_q)
    direction = imbalance_direction(imb)

    exec_cf = execution_cashflow(buy, sell, inputs.average_execution_price_gbp_per_mwh)
    imb_cf = imbalance_cashflow(imb, buy_price, sell_price)
    total_cf = total_realised_cashflow(exec_cf, imb_cf, fees)
    no_action_cf = no_action_total_cashflow(
        inputs.realised_generation_mwh, inputs.initial_contracted_position_mwh, buy_price, sell_price
    )
    realised_pnl = incremental_pnl_vs_no_action(total_cf, no_action_cf)

    attribution = compute_attribution(inputs)
    return SettlementFigures(
        final_contracted_position_mwh=final_q,
        realised_imbalance_mwh=imb,
        imbalance_direction=direction,
        execution_cashflow_gbp=exec_cf,
        execution_fees_gbp=fees,
        imbalance_cashflow_gbp=imb_cf,
        total_realised_cashflow_gbp=total_cf,
        no_action_total_cashflow_gbp=no_action_cf,
        realised_pnl_gbp=realised_pnl,
        attribution=attribution,
    )


def compute_attribution(inputs: RealisedInputs) -> RealisedPnlAttribution:
    """Decompose incremental P&L (vs NO_ACTION) into reconciling effects.

    Let ``N = executed_buy − executed_sell`` (net executed, buy positive). Then the
    decision imbalance ``I_dec = I_na − N`` where ``I_na = G − initial_Q``. Writing
    ``p_dec`` / ``p_na`` for the applicable imbalance price of each case:

        imb_cf_dec − imb_cf_na = −N·p_dec + I_na·(p_dec − p_na)

    so the four effects sum exactly to the incremental P&L:

        execution_price_effect  = execution cash flow
        execution_fees_effect   = −fees
        imbalance_reduction      = −N·p_dec        (volume term)
        imbalance_residual       = I_na·(p_dec − p_na)   (price-regime term; 0 unless sign flip)
    """
    buy = inputs.executed_buy_mwh
    sell = inputs.executed_sell_mwh
    fees = inputs.execution_fees_gbp
    buy_price = inputs.imbalance_buy_price_gbp_per_mwh
    sell_price = inputs.imbalance_sell_price_gbp_per_mwh

    net_executed = buy - sell
    final_q = reconstruct_final_position(inputs.initial_contracted_position_mwh, buy, sell)
    i_dec = realised_imbalance(inputs.realised_generation_mwh, final_q)
    i_na = realised_imbalance(inputs.realised_generation_mwh, inputs.initial_contracted_position_mwh)
    p_dec = imbalance_price_for(i_dec, buy_price, sell_price)
    p_na = imbalance_price_for(i_na, buy_price, sell_price)

    exec_cf = execution_cashflow(buy, sell, inputs.average_execution_price_gbp_per_mwh)
    imb_cf_dec = imbalance_cashflow(i_dec, buy_price, sell_price)
    imb_cf_na = imbalance_cashflow(i_na, buy_price, sell_price)

    decision_total = exec_cf + imb_cf_dec - fees
    no_action_total = imb_cf_na
    total_incremental = decision_total - no_action_total

    execution_price_effect = exec_cf
    execution_fees_effect = -fees
    imbalance_reduction_effect = -net_executed * p_dec
    imbalance_residual_effect = i_na * (p_dec - p_na)

    reconciliation_error = total_incremental - (
        execution_price_effect + execution_fees_effect + imbalance_reduction_effect + imbalance_residual_effect
    )
    return RealisedPnlAttribution(
        execution_price_effect_gbp=execution_price_effect,
        execution_fees_effect_gbp=execution_fees_effect,
        imbalance_reduction_effect_gbp=imbalance_reduction_effect,
        imbalance_residual_effect_gbp=imbalance_residual_effect,
        total_incremental_pnl_gbp=total_incremental,
        reconciliation_error_gbp=reconciliation_error,
    )
