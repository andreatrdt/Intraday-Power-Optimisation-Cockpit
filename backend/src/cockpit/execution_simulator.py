"""Pure, deterministic execution simulator (Milestone 6B).

Given a :class:`SimulatedOrder`, the visible order-book levels for its side
(best-first) and a mode/config, it produces an :class:`ExecutionOutcome` with
per-level :class:`SimulatedFill` evidence. It is a **diagnostic SAMPLE**
simulator — no randomness, no live venue. Same inputs + config → same output.

Modes:

* ``IDEAL`` — benchmark: full requested volume at the best visible price, zero
  slippage, zero latency (fees still apply). Not liquidity-constrained.
* ``REALISTIC`` — walks visible depth with a configurable depth haircut and
  latency; assumption-driven.
* ``STRESS`` — larger haircut, extra latency and an adverse per-level price
  shift; assumption-driven.

Latency is modelled deterministically: it is recorded in the assumptions and the
depth haircut represents visible depth assumed consumed by faster participants
during that latency. There is no stochastic process.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from cockpit.decision_models import ExecutionStatus
from cockpit.execution_models import (
    BookLevel,
    ExecutionConfig,
    ExecutionMode,
    ExecutionOutcome,
    SimulatedFill,
    SimulatedOrder,
    new_fill_id,
)


def _mode_params(mode: ExecutionMode, config: ExecutionConfig) -> tuple[float, float, int]:
    """Return (depth_haircut, adverse_price_gbp_per_mwh, latency_ms)."""
    if mode is ExecutionMode.IDEAL:
        return 0.0, 0.0, 0
    if mode is ExecutionMode.REALISTIC:
        return config.realistic_depth_haircut, 0.0, config.realistic_latency_ms
    return config.stress_depth_haircut, config.stress_adverse_price_gbp_per_mwh, config.stress_latency_ms


def _assumptions(mode: ExecutionMode, config: ExecutionConfig) -> tuple[str, ...]:
    haircut, adverse, latency = _mode_params(mode, config)
    if mode is ExecutionMode.IDEAL:
        return (
            "IDEAL benchmark: full fill at best visible price; zero slippage/latency.",
            f"Fee {config.fee_gbp_per_mwh:.2f} GBP/MWh.",
        )
    label = "REALISTIC (assumption-driven SAMPLE)" if mode is ExecutionMode.REALISTIC else "STRESS (assumption-driven SAMPLE)"
    items = [
        f"{label}.",
        f"Depth haircut {haircut:.0%} of visible depth.",
        f"Deterministic latency {latency} ms (depth-haircut proxy; no stochastic process).",
        f"Fee {config.fee_gbp_per_mwh:.2f} GBP/MWh.",
    ]
    if adverse > 0:
        items.append(f"Adverse per-level price shift {adverse:.2f} GBP/MWh.")
    items.append("Not calibrated to a real exchange.")
    return tuple(items)


def _round(value: float, digits: int = 6) -> float:
    return round(value, digits)


def simulate(
    order: SimulatedOrder,
    side_levels: Sequence[BookLevel],
    config: ExecutionConfig,
    *,
    at: datetime,
) -> ExecutionOutcome:
    """Simulate a synchronous IOC-style fill against ``side_levels`` (best-first)."""
    mode = order.execution_mode
    haircut, adverse, _latency = _mode_params(mode, config)
    tol = config.tolerance_mwh
    side = order.side
    requested = order.requested_volume_mwh
    limit = order.limit_price
    best = side_levels[0].price_gbp_per_mwh if side_levels else None

    fills: list[SimulatedFill] = []
    warnings: list[str] = []

    def _slippage(price: float) -> float:
        if best is None:
            return 0.0
        return (price - best) if side == "BUY" else (best - price)

    if best is None:
        warnings.append("No visible depth; no simulated fill possible.")
    elif mode is ExecutionMode.IDEAL:
        if limit is not None and ((side == "BUY" and best > limit + tol) or (side == "SELL" and best < limit - tol)):
            warnings.append("Limit price prevented any simulated fill at the best visible price.")
        else:
            fee = requested * config.fee_gbp_per_mwh
            fills.append(SimulatedFill(
                fill_id=new_fill_id(order.order_id, 1), order_id=order.order_id, filled_at=at, side=side,
                filled_volume_mwh=_round(requested), fill_price_gbp_per_mwh=_round(best), fee_gbp=_round(fee, 4),
                slippage_gbp_per_mwh=0.0, order_book_level=1, assumption_basis="IDEAL benchmark (best visible price)",
            ))
    else:
        remaining = requested
        for index, level in enumerate(side_levels, start=1):
            if remaining <= tol:
                break
            available = level.volume_mwh * (1.0 - haircut)
            if available <= tol:
                continue
            price = level.price_gbp_per_mwh + adverse if side == "BUY" else level.price_gbp_per_mwh - adverse
            if limit is not None and ((side == "BUY" and price > limit + tol) or (side == "SELL" and price < limit - tol)):
                break  # this and all worse levels violate the limit
            take = min(remaining, available)
            fee = take * config.fee_gbp_per_mwh
            fills.append(SimulatedFill(
                fill_id=new_fill_id(order.order_id, index), order_id=order.order_id, filled_at=at, side=side,
                filled_volume_mwh=_round(take), fill_price_gbp_per_mwh=_round(price), fee_gbp=_round(fee, 4),
                slippage_gbp_per_mwh=_round(_slippage(price), 4), order_book_level=index,
                assumption_basis=f"{mode.value} SAMPLE order book",
            ))
            remaining -= take

    total_filled = sum(fill.filled_volume_mwh for fill in fills)
    unfilled = max(0.0, requested - total_filled)
    notional = sum(fill.filled_volume_mwh * fill.fill_price_gbp_per_mwh for fill in fills)
    total_fees = sum(fill.fee_gbp for fill in fills)
    total_slippage = sum(fill.filled_volume_mwh * fill.slippage_gbp_per_mwh for fill in fills)
    average_price = (notional / total_filled) if total_filled > tol else None
    # Signed portfolio cash flow: BUY pays notional; SELL receives it; fees always cost.
    execution_cost = (notional if side == "BUY" else -notional) + total_fees

    if total_filled <= tol:
        status = ExecutionStatus.EXPIRED
        if best is not None and not any("Limit price" in w for w in warnings) and not warnings:
            warnings.append("No executable volume within visible depth; simulated order expired.")
    elif unfilled > tol:
        status = ExecutionStatus.PARTIALLY_FILLED
        warnings.append(f"Partial simulated fill: {unfilled:.3f} MWh unfilled (depth or limit).")
    else:
        status = ExecutionStatus.FILLED

    return ExecutionOutcome(
        order=order,
        fills=tuple(fills),
        total_filled_volume_mwh=_round(total_filled),
        unfilled_volume_mwh=_round(unfilled),
        average_fill_price_gbp_per_mwh=_round(average_price, 4) if average_price is not None else None,
        best_price_before_execution_gbp_per_mwh=_round(best, 4) if best is not None else None,
        total_fees_gbp=_round(total_fees, 4),
        total_execution_cost_gbp=_round(execution_cost, 4),
        total_slippage_gbp=_round(total_slippage, 4),
        execution_status=status,
        execution_mode=mode,
        levels_consumed=len(fills),
        warnings=tuple(warnings),
        assumptions_used=_assumptions(mode, config),
    )
