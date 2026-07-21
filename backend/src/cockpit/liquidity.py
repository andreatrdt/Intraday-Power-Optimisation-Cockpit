"""Pure order-book execution and liquidity calculations."""

from __future__ import annotations

from dataclasses import dataclass

from cockpit.models import OrderBookLevel


@dataclass(frozen=True)
class ExecutionResult:
    side: str
    required_volume_mwh: float
    executable_volume_mwh: float
    unfilled_volume_mwh: float
    wap_gbp_per_mwh: float | None
    levels_considered: int
    levels_used: int


def ordered_levels(levels: list[OrderBookLevel], side: str) -> list[OrderBookLevel]:
    side = side.upper()
    if side == "BUY":
        eligible = [level for level in levels if level.side == "ASK"]
        return sorted(eligible, key=lambda level: (level.price_gbp_per_mwh, level.level))
    if side == "SELL":
        eligible = [level for level in levels if level.side == "BID"]
        return sorted(eligible, key=lambda level: (-level.price_gbp_per_mwh, level.level))
    if side == "NONE":
        return []
    raise ValueError(f"Unsupported hedge side '{side}'")


def executable_price(
    levels: list[OrderBookLevel], required_volume_mwh: float, side: str, max_levels: int = 3
) -> ExecutionResult:
    """Sweep the economically best levels, capped at ``max_levels``."""
    required = max(0.0, float(required_volume_mwh))
    side = side.upper()
    if side == "NONE" or required == 0:
        return ExecutionResult(side, required, 0.0, 0.0, None, max_levels, 0)

    book = ordered_levels(levels, side)[:max_levels]
    remaining = required
    executed = 0.0
    notional = 0.0
    used = 0
    for level in book:
        if remaining <= 0:
            break
        fill = min(remaining, level.volume_mwh)
        if fill > 0:
            executed += fill
            notional += fill * level.price_gbp_per_mwh
            remaining -= fill
            used += 1
    wap = notional / executed if executed else None
    return ExecutionResult(
        side=side,
        required_volume_mwh=required,
        executable_volume_mwh=executed,
        unfilled_volume_mwh=max(0.0, remaining),
        wap_gbp_per_mwh=wap,
        levels_considered=max_levels,
        levels_used=used,
    )


def liquidity_score(bid_depth_mwh: float, ask_depth_mwh: float, spread: float) -> float:
    """Transparent 0-1 diagnostic score; it is not a predictive model."""
    depth_component = min(1.0, (bid_depth_mwh + ask_depth_mwh) / 30.0)
    spread_component = max(0.0, 1.0 - spread / 8.0)
    return round(0.65 * depth_component + 0.35 * spread_component, 4)


def hedge_side(exposure_mwh: float, tolerance_mwh: float = 0.05) -> str:
    if exposure_mwh < -tolerance_mwh:
        return "BUY"
    if exposure_mwh > tolerance_mwh:
        return "SELL"
    return "NONE"
