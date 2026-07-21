"""Transparent non-optimising opportunity-cost heuristics for battery flexibility."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpportunityCostResult:
    discharge_cost_gbp_per_mwh: float
    charge_cost_gbp_per_mwh: float
    discharge_terminal_component: float
    charge_terminal_component: float
    discharge_future_flex_component: float
    charge_future_flex_component: float


def calculate_opportunity_cost(
    *,
    soc_mwh: float,
    terminal_target_mwh: float,
    degradation_cost_gbp_per_mwh: float,
    terminal_penalty_gbp_per_mwh: float,
    future_flex_penalty_gbp_per_mwh: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    upward_reserved_mw: float,
    downward_reserved_mw: float,
    charge_power_max_mw: float,
    discharge_power_max_mw: float,
) -> OpportunityCostResult:
    """Price one MWh of grid-side charge or discharge using labelled assumptions."""
    initial_shortfall = max(0.0, terminal_target_mwh - soc_mwh)
    discharge_shortfall = max(0.0, terminal_target_mwh - (soc_mwh - 1 / discharge_efficiency))
    charge_shortfall = max(0.0, terminal_target_mwh - (soc_mwh + charge_efficiency))
    discharge_terminal = (discharge_shortfall - initial_shortfall) * terminal_penalty_gbp_per_mwh
    charge_terminal = max(0.0, charge_shortfall - initial_shortfall) * terminal_penalty_gbp_per_mwh
    discharge_future = future_flex_penalty_gbp_per_mwh * (
        1 + upward_reserved_mw / discharge_power_max_mw if discharge_power_max_mw else 1
    )
    charge_future = future_flex_penalty_gbp_per_mwh * (
        1 + downward_reserved_mw / charge_power_max_mw if charge_power_max_mw else 1
    )
    return OpportunityCostResult(
        discharge_cost_gbp_per_mwh=degradation_cost_gbp_per_mwh + discharge_terminal + discharge_future,
        charge_cost_gbp_per_mwh=degradation_cost_gbp_per_mwh + charge_terminal + charge_future,
        discharge_terminal_component=discharge_terminal,
        charge_terminal_component=charge_terminal,
        discharge_future_flex_component=discharge_future,
        charge_future_flex_component=charge_future,
    )
