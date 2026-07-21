"""Deterministic battery state and feasibility calculations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeasibilityResult:
    max_charge_mwh: float
    max_discharge_mwh: float
    upward_power_headroom_mw: float
    downward_power_headroom_mw: float
    upward_energy_duration_hours: float
    downward_space_duration_hours: float
    projected_soc_after_max_charge_mwh: float
    projected_soc_after_max_discharge_mwh: float
    binding_constraints: list[str]


def next_soc(
    soc_mwh: float,
    charge_mw: float,
    discharge_mw: float,
    duration_hours: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> float:
    return (
        soc_mwh
        + charge_efficiency * charge_mw * duration_hours
        - discharge_mw * duration_hours / discharge_efficiency
    )


def power_to_energy(power_mw: float, duration_hours: float) -> float:
    return power_mw * duration_hours


def calculate_feasibility(
    *,
    soc_mwh: float,
    e_min_mwh: float,
    e_max_mwh: float,
    charge_power_max_mw: float,
    discharge_power_max_mw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    duration_hours: float,
    upward_reserved_mw: float = 0.0,
    downward_reserved_mw: float = 0.0,
    reserve_duration_hours: float = 0.0,
) -> FeasibilityResult:
    """Return feasible one-period actions while preserving capacity reservations."""
    if not (e_min_mwh <= soc_mwh <= e_max_mwh):
        raise ValueError("SoC is outside E_min/E_max")
    if not (0 < charge_efficiency <= 1 and 0 < discharge_efficiency <= 1):
        raise ValueError("Charge and discharge efficiency must be in (0, 1]")
    if duration_hours <= 0:
        raise ValueError("Delivery duration must be positive")
    if min(charge_power_max_mw, discharge_power_max_mw, upward_reserved_mw, downward_reserved_mw) < 0:
        raise ValueError("Power and reservations must be non-negative")
    if upward_reserved_mw > discharge_power_max_mw or downward_reserved_mw > charge_power_max_mw:
        raise ValueError("Reserved capability exceeds the corresponding power limit")

    upward_headroom = discharge_power_max_mw - upward_reserved_mw
    downward_headroom = charge_power_max_mw - downward_reserved_mw
    reserve_floor = e_min_mwh + upward_reserved_mw * reserve_duration_hours / discharge_efficiency
    reserve_ceiling = e_max_mwh - charge_efficiency * downward_reserved_mw * reserve_duration_hours
    if reserve_floor > reserve_ceiling or soc_mwh < reserve_floor or soc_mwh > reserve_ceiling:
        raise ValueError("Current SoC cannot sustain the reserved energy-duration capability")

    discharge_power_energy = power_to_energy(upward_headroom, duration_hours)
    discharge_soc_energy = max(0.0, (soc_mwh - reserve_floor) * discharge_efficiency)
    max_discharge = min(discharge_power_energy, discharge_soc_energy)

    charge_power_energy = power_to_energy(downward_headroom, duration_hours)
    charge_space_energy = max(0.0, (reserve_ceiling - soc_mwh) / charge_efficiency)
    max_charge = min(charge_power_energy, charge_space_energy)

    bindings: list[str] = []
    tolerance = 1e-8
    if abs(max_discharge - discharge_power_energy) <= tolerance:
        bindings.append("DISCHARGE_POWER_HEADROOM")
    if abs(max_discharge - discharge_soc_energy) <= tolerance:
        bindings.append("UPWARD_ENERGY_DURATION")
    if abs(max_charge - charge_power_energy) <= tolerance:
        bindings.append("CHARGE_POWER_HEADROOM")
    if abs(max_charge - charge_space_energy) <= tolerance:
        bindings.append("DOWNWARD_ENERGY_SPACE")

    projected_discharge_soc = soc_mwh - max_discharge / discharge_efficiency
    projected_charge_soc = soc_mwh + charge_efficiency * max_charge
    upward_duration = discharge_soc_energy / upward_headroom if upward_headroom > 0 else 0.0
    downward_duration = charge_space_energy / downward_headroom if downward_headroom > 0 else 0.0

    return FeasibilityResult(
        max_charge_mwh=max_charge,
        max_discharge_mwh=max_discharge,
        upward_power_headroom_mw=upward_headroom,
        downward_power_headroom_mw=downward_headroom,
        upward_energy_duration_hours=upward_duration,
        downward_space_duration_hours=downward_duration,
        projected_soc_after_max_charge_mwh=projected_charge_soc,
        projected_soc_after_max_discharge_mwh=projected_discharge_soc,
        binding_constraints=bindings,
    )
