"""Pure benchmark calculations (Milestone 7).

Each benchmark is a hypothetical hedge, valued at the **same** realised generation
and imbalance prices as the decision, so its ``incremental_pnl_vs_no_action_gbp`` is
directly comparable to the realised decision. No I/O, no lifecycle, no storage.

Benchmarks implemented:

* ``NO_ACTION`` — no hedge beyond the original contracted position (the primary
  baseline; incremental P&L is 0 by definition).
* ``MODEL_RECOMMENDATION`` — the model-recommended volume executed under an
  explicitly-labelled **IDEAL** convention (at the reference market price). Never a
  silent perfect-execution assumption.
* ``TRADER_INSTRUCTION`` — the actual trader instruction and actual simulated
  execution result (this reproduces the realised decision outcome).
* ``PERFECT_FORESIGHT`` — the best feasible hedge given realised generation, driving
  the imbalance to zero. Labelled hindsight-only / unattainable / upper-bound.
"""

from __future__ import annotations

from cockpit.pnl_attribution import (
    execution_cashflow,
    imbalance_cashflow,
    imbalance_direction,
    incremental_pnl_vs_no_action,
    no_action_total_cashflow,
    realised_imbalance,
    reconstruct_final_position,
    total_realised_cashflow,
)
from cockpit.settlement_models import BenchmarkName, BenchmarkResult, RealisedInputs

# Matches the execution simulator's ExecutionConfig.fee_gbp_per_mwh default; the
# service passes the live config value so the two never silently diverge.
DEFAULT_FEE_GBP_PER_MWH = 0.15


def _evaluate_hedge(
    inputs: RealisedInputs,
    *,
    name: BenchmarkName,
    description: str,
    attainable: bool,
    hindsight_only: bool,
    assumed_execution_mode: str | None,
    hedge_buy_mwh: float,
    hedge_sell_mwh: float,
    execution_price: float | None,
    fees_gbp: float,
    warnings: tuple[str, ...] = (),
    assumptions: tuple[str, ...] = (),
) -> BenchmarkResult:
    """Value a hypothetical hedge at the realised generation and imbalance prices."""
    buy_price = inputs.imbalance_buy_price_gbp_per_mwh
    sell_price = inputs.imbalance_sell_price_gbp_per_mwh

    final_q = reconstruct_final_position(inputs.initial_contracted_position_mwh, hedge_buy_mwh, hedge_sell_mwh)
    imb = realised_imbalance(inputs.realised_generation_mwh, final_q)
    exec_cf = execution_cashflow(hedge_buy_mwh, hedge_sell_mwh, execution_price)
    imb_cf = imbalance_cashflow(imb, buy_price, sell_price)
    total_cf = total_realised_cashflow(exec_cf, imb_cf, fees_gbp)
    no_action_cf = no_action_total_cashflow(
        inputs.realised_generation_mwh, inputs.initial_contracted_position_mwh, buy_price, sell_price
    )
    incremental = incremental_pnl_vs_no_action(total_cf, no_action_cf)
    return BenchmarkResult(
        benchmark_name=name,
        description=description,
        attainable=attainable,
        hindsight_only=hindsight_only,
        assumed_execution_mode=assumed_execution_mode,
        hedge_buy_mwh=hedge_buy_mwh,
        hedge_sell_mwh=hedge_sell_mwh,
        execution_price_gbp_per_mwh=execution_price,
        execution_fees_gbp=fees_gbp,
        final_position_mwh=final_q,
        realised_imbalance_mwh=imb,
        imbalance_direction=imbalance_direction(imb),
        total_cashflow_gbp=total_cf,
        incremental_pnl_vs_no_action_gbp=incremental,
        warnings=warnings,
        assumptions=assumptions,
    )


def no_action_benchmark(inputs: RealisedInputs) -> BenchmarkResult:
    return _evaluate_hedge(
        inputs,
        name=BenchmarkName.NO_ACTION,
        description="No hedge beyond the original contracted position; the whole imbalance settles at imbalance prices.",
        attainable=True,
        hindsight_only=False,
        assumed_execution_mode=None,
        hedge_buy_mwh=0.0,
        hedge_sell_mwh=0.0,
        execution_price=None,
        fees_gbp=0.0,
        assumptions=("Primary baseline: pre-decision contracted position retained.",),
    )


def model_recommendation_benchmark(
    inputs: RealisedInputs, *, model_buy_mwh: float, model_sell_mwh: float, fee_gbp_per_mwh: float
) -> BenchmarkResult:
    reference = inputs.reference_market_price_gbp_per_mwh
    volume = model_buy_mwh + model_sell_mwh
    warnings: tuple[str, ...] = ()
    if reference is None and volume > 0:
        warnings = ("Reference market price unavailable; model execution cash flow omitted.",)
    fees = fee_gbp_per_mwh * volume if reference is not None else 0.0
    return _evaluate_hedge(
        inputs,
        name=BenchmarkName.MODEL_RECOMMENDATION,
        description="Model-recommended volume assumed executed IDEALLY at the reference market price.",
        attainable=True,
        hindsight_only=False,
        assumed_execution_mode="IDEAL",
        hedge_buy_mwh=model_buy_mwh,
        hedge_sell_mwh=model_sell_mwh,
        execution_price=reference,
        fees_gbp=fees,
        warnings=warnings,
        assumptions=(
            "IDEAL execution: full recommended volume fills at the reference market price.",
            "Not the trader's actual simulator mode — an explicitly labelled IDEAL benchmark.",
        ),
    )


def trader_instruction_benchmark(inputs: RealisedInputs) -> BenchmarkResult:
    return _evaluate_hedge(
        inputs,
        name=BenchmarkName.TRADER_INSTRUCTION,
        description="Actual trader instruction and actual simulated execution — the realised decision outcome.",
        attainable=True,
        hindsight_only=False,
        assumed_execution_mode="ACTUAL_SIMULATION",
        hedge_buy_mwh=inputs.executed_buy_mwh,
        hedge_sell_mwh=inputs.executed_sell_mwh,
        execution_price=inputs.average_execution_price_gbp_per_mwh,
        fees_gbp=inputs.execution_fees_gbp,
        assumptions=("Uses executed (not requested) volume and the realised average execution price.",),
    )


def perfect_foresight_benchmark(inputs: RealisedInputs, *, fee_gbp_per_mwh: float) -> BenchmarkResult:
    """Drive the imbalance to zero with perfect foresight of realised generation.

    Hedge = ``G − initial_Q`` (buy if positive, sell if negative), executed at the
    reference market price. This is an UNATTAINABLE hindsight upper bound: it uses
    knowledge of realised generation that is not available at decision time, and it
    does not model finite order-book depth.
    """
    reference = inputs.reference_market_price_gbp_per_mwh
    needed = inputs.realised_generation_mwh - inputs.initial_contracted_position_mwh  # to reach final_Q = G
    hedge_buy = needed if needed > 0 else 0.0
    hedge_sell = -needed if needed < 0 else 0.0
    volume = hedge_buy + hedge_sell
    warnings = ("Hindsight upper bound — not attainable. Uses realised generation unknown at decision time.",)
    if reference is None and volume > 0:
        warnings = (*warnings, "Reference market price unavailable; execution cash flow omitted.")
    fees = fee_gbp_per_mwh * volume if reference is not None else 0.0
    return _evaluate_hedge(
        inputs,
        name=BenchmarkName.PERFECT_FORESIGHT,
        description="Best feasible hedge with perfect foresight of realised generation (imbalance driven to zero).",
        attainable=False,
        hindsight_only=True,
        assumed_execution_mode="HINDSIGHT_IDEAL",
        hedge_buy_mwh=hedge_buy,
        hedge_sell_mwh=hedge_sell,
        execution_price=reference,
        fees_gbp=fees,
        warnings=warnings,
        assumptions=(
            "Closes the imbalance exactly at the reference market price.",
            "Order-book depth not modelled; unattainable upper-bound diagnostic only.",
        ),
    )


def compute_benchmarks(
    inputs: RealisedInputs,
    *,
    model_buy_mwh: float,
    model_sell_mwh: float,
    fee_gbp_per_mwh: float = DEFAULT_FEE_GBP_PER_MWH,
) -> tuple[BenchmarkResult, ...]:
    """The full benchmark set, in a stable order (NO_ACTION first)."""
    return (
        no_action_benchmark(inputs),
        model_recommendation_benchmark(
            inputs, model_buy_mwh=model_buy_mwh, model_sell_mwh=model_sell_mwh, fee_gbp_per_mwh=fee_gbp_per_mwh
        ),
        trader_instruction_benchmark(inputs),
        perfect_foresight_benchmark(inputs, fee_gbp_per_mwh=fee_gbp_per_mwh),
    )
