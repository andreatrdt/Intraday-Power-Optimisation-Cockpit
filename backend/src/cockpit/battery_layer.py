"""Battery flexibility, exposure coverage and readiness diagnostics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from cockpit.battery_physics import calculate_feasibility
from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.models import (
    BatteryAssetLimits,
    BatteryExposureCoverage,
    BatteryFeasibilityPoint,
    BatteryFlexibilitySnapshot,
    BatteryOpportunityCost,
    BatteryPeriodSnapshot,
    BatteryReadiness,
    CanonicalDataPoint,
    CockpitSnapshot,
    DataLineage,
    Quality,
    SemanticKind,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.opportunity_cost import calculate_opportunity_cost
from cockpit.position_layer import build_forecast_position


CONFIG_METRICS = (
    "battery_e_min",
    "battery_e_max",
    "battery_charge_power_max",
    "battery_discharge_power_max",
    "battery_charge_efficiency",
    "battery_discharge_efficiency",
    "battery_reserve_duration",
    "battery_terminal_soc_target",
    "battery_degradation_cost",
    "battery_terminal_soc_penalty",
    "battery_future_flexibility_penalty",
)


@dataclass
class BatteryLayerResult:
    snapshot: BatteryFlexibilitySnapshot
    derived_values: list[CanonicalDataPoint]


def build_battery_flexibility(snapshot: CockpitSnapshot) -> BatteryLayerResult:
    values = {point.metric: point for point in snapshot.values if point.delivery_period is None}
    soc = values.get("battery_soc")
    config = {metric: values.get(metric) for metric in CONFIG_METRICS}
    upward_reserved = values.get("upward_service_commitment")
    downward_reserved = values.get("downward_service_commitment")
    required = [soc, upward_reserved, downward_reserved, *config.values()]
    valid_inputs = [point for point in required if point is not None]
    source_mode = combined_source_mode(valid_inputs) if valid_inputs else SourceMode.ERROR
    quality = combined_quality(valid_inputs) if valid_inputs else Quality.MISSING
    readiness = _readiness(required, soc, config, upward_reserved, downward_reserved)
    derived: list[CanonicalDataPoint] = []
    warnings = list(dict.fromkeys(
        warning for point in valid_inputs for warning in point.lineage.warnings
    ))

    if not readiness.calculation_allowed or soc is None or upward_reserved is None or downward_reserved is None:
        return BatteryLayerResult(
            snapshot=_snapshot(snapshot, source_mode, quality, readiness, soc, None, None, [], [], warnings),
            derived_values=[],
        )

    typed_config = {key: point for key, point in config.items() if point is not None}
    if len(typed_config) != len(CONFIG_METRICS):
        return BatteryLayerResult(
            snapshot=_snapshot(snapshot, source_mode, quality, readiness, soc, None, None, [], [], warnings),
            derived_values=[],
        )
    limits = BatteryAssetLimits(
        e_min=typed_config["battery_e_min"],
        e_max=typed_config["battery_e_max"],
        charge_power_max=typed_config["battery_charge_power_max"],
        discharge_power_max=typed_config["battery_discharge_power_max"],
        charge_efficiency=typed_config["battery_charge_efficiency"],
        discharge_efficiency=typed_config["battery_discharge_efficiency"],
        reserve_duration=typed_config["battery_reserve_duration"],
    )
    cost = _opportunity_cost(snapshot, soc, typed_config, upward_reserved, downward_reserved)
    derived.extend([cost.discharge_cost_value, cost.charge_cost_value])

    position_result = build_forecast_position(snapshot)
    derived.extend(position_result.derived_values)
    periods: list[BatteryPeriodSnapshot] = []
    usefulness: list[tuple[float, str]] = []
    if not position_result.snapshot.readiness.calculation_allowed:
        warnings.append("Forecast/position exposure is unavailable; physical feasibility remains calculable.")
    for position_period in position_result.snapshot.periods:
        try:
            feasibility = calculate_feasibility(
                soc_mwh=float(soc.value),
                e_min_mwh=float(limits.e_min.value),
                e_max_mwh=float(limits.e_max.value),
                charge_power_max_mw=float(limits.charge_power_max.value),
                discharge_power_max_mw=float(limits.discharge_power_max.value),
                charge_efficiency=float(limits.charge_efficiency.value),
                discharge_efficiency=float(limits.discharge_efficiency.value),
                duration_hours=0.5,
                upward_reserved_mw=float(upward_reserved.value),
                downward_reserved_mw=float(downward_reserved.value),
                reserve_duration_hours=float(limits.reserve_duration.value),
            )
        except ValueError as exc:
            warnings.append(f"{position_period.delivery_period}: {exc}")
            continue
        common_inputs = [soc, upward_reserved, downward_reserved, *typed_config.values()]
        f_values = {
            "max_charge": _derived(snapshot, "battery_max_charge", position_period.delivery_period, position_period.delivery_start, feasibility.max_charge_mwh, "MWh", common_inputs, "min(charge power headroom x dt, reserved energy space / eta_c)"),
            "max_discharge": _derived(snapshot, "battery_max_discharge", position_period.delivery_period, position_period.delivery_start, feasibility.max_discharge_mwh, "MWh", common_inputs, "min(discharge power headroom x dt, reserved usable energy x eta_d)"),
            "up_power": _derived(snapshot, "battery_upward_power_headroom", position_period.delivery_period, position_period.delivery_start, feasibility.upward_power_headroom_mw, "MW", common_inputs, "P_discharge_max - upward reserved capability"),
            "down_power": _derived(snapshot, "battery_downward_power_headroom", position_period.delivery_period, position_period.delivery_start, feasibility.downward_power_headroom_mw, "MW", common_inputs, "P_charge_max - downward reserved capability"),
            "up_duration": _derived(snapshot, "battery_upward_energy_duration", position_period.delivery_period, position_period.delivery_start, feasibility.upward_energy_duration_hours, "h", common_inputs, "remaining deliverable energy / upward power headroom"),
            "down_duration": _derived(snapshot, "battery_downward_space_duration", position_period.delivery_period, position_period.delivery_start, feasibility.downward_space_duration_hours, "h", common_inputs, "remaining grid-side charge space / downward power headroom"),
            "soc_charge": _derived(snapshot, "battery_soc_after_max_charge", position_period.delivery_period, position_period.delivery_start, feasibility.projected_soc_after_max_charge_mwh, "MWh", common_inputs, "E_t + eta_c * maximum charge MWh"),
            "soc_discharge": _derived(snapshot, "battery_soc_after_max_discharge", position_period.delivery_period, position_period.delivery_start, feasibility.projected_soc_after_max_discharge_mwh, "MWh", common_inputs, "E_t - maximum discharge MWh / eta_d"),
        }
        derived.extend(f_values.values())
        feasibility_point = BatteryFeasibilityPoint(
            settlement_period=position_period.settlement_period,
            delivery_period=position_period.delivery_period,
            delivery_start=position_period.delivery_start,
            delivery_end=position_period.delivery_end,
            duration_hours=0.5,
            current_soc=soc,
            upward_reserved=upward_reserved,
            downward_reserved=downward_reserved,
            max_charge_mwh=feasibility.max_charge_mwh,
            max_discharge_mwh=feasibility.max_discharge_mwh,
            upward_power_headroom_mw=feasibility.upward_power_headroom_mw,
            downward_power_headroom_mw=feasibility.downward_power_headroom_mw,
            upward_energy_duration_hours=feasibility.upward_energy_duration_hours,
            downward_space_duration_hours=feasibility.downward_space_duration_hours,
            projected_soc_after_max_charge_mwh=feasibility.projected_soc_after_max_charge_mwh,
            projected_soc_after_max_discharge_mwh=feasibility.projected_soc_after_max_discharge_mwh,
            max_charge_value=f_values["max_charge"],
            max_discharge_value=f_values["max_discharge"],
            upward_power_headroom_value=f_values["up_power"],
            downward_power_headroom_value=f_values["down_power"],
            upward_energy_duration_value=f_values["up_duration"],
            downward_space_duration_value=f_values["down_duration"],
            projected_soc_after_max_charge_value=f_values["soc_charge"],
            projected_soc_after_max_discharge_value=f_values["soc_discharge"],
            binding_constraints=feasibility.binding_constraints,
            warnings=[],
        )
        coverage: list[BatteryExposureCoverage] = []
        for exposure in position_period.exposures:
            item, coverage_values = _coverage(snapshot, position_period, exposure, f_values)
            coverage.append(item)
            derived.extend(coverage_values)
        useful = max((item.covered_mwh for item in coverage), default=0.0)
        usefulness.append((useful, position_period.delivery_period))
        p50 = next(item for item in coverage if item.scenario == "P50")
        p10 = next(item for item in coverage if item.scenario == "P10")
        explanation = (
            f"{position_period.delivery_period} can absorb up to {feasibility.max_charge_mwh:.1f} MWh "
            f"or export up to {feasibility.max_discharge_mwh:.1f} MWh while preserving labelled reservations. "
            f"Maximum support leaves P50 at {p50.residual_after_support_mwh:+.1f} MWh and P10 at "
            f"{p10.residual_after_support_mwh:+.1f} MWh. This is a feasibility diagnostic, not a dispatch instruction."
        )
        periods.append(BatteryPeriodSnapshot(
            settlement_period=position_period.settlement_period,
            delivery_period=position_period.delivery_period,
            delivery_start=position_period.delivery_start,
            delivery_end=position_period.delivery_end,
            feasibility=feasibility_point,
            coverage=coverage,
            explanation=explanation,
            warnings=[],
        ))

    if readiness.calculation_allowed and position_result.snapshot.readiness.status == SnapshotStatus.BLOCKED:
        readiness = BatteryReadiness(
            status=SnapshotStatus.DEGRADED,
            calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=readiness.reasons + ["Forecast/position exposure coverage is unavailable"],
        )
    useful_periods = [period for _, period in sorted(usefulness, reverse=True)[:3]]
    return BatteryLayerResult(
        snapshot=_snapshot(snapshot, source_mode, quality, readiness, soc, limits, cost, periods, useful_periods, warnings),
        derived_values=derived,
    )


def _coverage(snapshot, period, exposure, values):
    amount = exposure.residual_position_mwh
    if amount > 0.05:
        direction = "CHARGE"
        maximum = float(values["max_charge"].value)
        covered = min(amount, maximum)
        residual = amount - covered
        support_value = values["max_charge"]
    elif amount < -0.05:
        direction = "DISCHARGE"
        maximum = float(values["max_discharge"].value)
        covered = min(abs(amount), maximum)
        residual = amount + covered
        support_value = values["max_discharge"]
    else:
        direction = "NONE"
        maximum = covered = residual = 0.0
        support_value = values["max_charge"]
    covered_value = _derived(snapshot, f"battery_{exposure.scenario.lower()}_covered", period.delivery_period, period.delivery_start, covered, "MWh", [exposure.exposure_value, support_value], "min(abs(scenario exposure), feasible directional battery energy)")
    residual_value = _derived(snapshot, f"battery_{exposure.scenario.lower()}_residual_after_support", period.delivery_period, period.delivery_start, residual, "MWh", [exposure.exposure_value, covered_value], "scenario exposure adjusted by maximum feasible directional support")
    percent = 100.0 if abs(amount) <= 0.05 else covered / abs(amount) * 100
    return BatteryExposureCoverage(
        scenario=exposure.scenario,
        exposure_mwh=amount,
        support_direction=direction,
        maximum_support_mwh=maximum,
        covered_mwh=covered,
        residual_after_support_mwh=residual,
        coverage_percent=percent,
        exposure_value=exposure.exposure_value,
        covered_value=covered_value,
        residual_value=residual_value,
    ), [covered_value, residual_value]


def _opportunity_cost(snapshot, soc, config, upward_reserved, downward_reserved):
    result = calculate_opportunity_cost(
        soc_mwh=float(soc.value),
        terminal_target_mwh=float(config["battery_terminal_soc_target"].value),
        degradation_cost_gbp_per_mwh=float(config["battery_degradation_cost"].value),
        terminal_penalty_gbp_per_mwh=float(config["battery_terminal_soc_penalty"].value),
        future_flex_penalty_gbp_per_mwh=float(config["battery_future_flexibility_penalty"].value),
        charge_efficiency=float(config["battery_charge_efficiency"].value),
        discharge_efficiency=float(config["battery_discharge_efficiency"].value),
        upward_reserved_mw=float(upward_reserved.value),
        downward_reserved_mw=float(downward_reserved.value),
        charge_power_max_mw=float(config["battery_charge_power_max"].value),
        discharge_power_max_mw=float(config["battery_discharge_power_max"].value),
    )
    inputs = [soc, upward_reserved, downward_reserved, *config.values()]
    discharge = _derived(snapshot, "battery_discharge_opportunity_cost", None, None, result.discharge_cost_gbp_per_mwh, "GBP/MWh", inputs, "degradation + incremental terminal SoC shortfall + upward reservation-weighted flexibility penalty")
    charge = _derived(snapshot, "battery_charge_opportunity_cost", None, None, result.charge_cost_gbp_per_mwh, "GBP/MWh", inputs, "degradation + incremental terminal SoC shortfall + downward reservation-weighted flexibility penalty")
    return BatteryOpportunityCost(
        discharge_cost_gbp_per_mwh=result.discharge_cost_gbp_per_mwh,
        charge_cost_gbp_per_mwh=result.charge_cost_gbp_per_mwh,
        discharge_cost_value=discharge,
        charge_cost_value=charge,
        degradation_cost=config["battery_degradation_cost"],
        terminal_soc_penalty=config["battery_terminal_soc_penalty"],
        future_flexibility_penalty=config["battery_future_flexibility_penalty"],
        terminal_soc_target=config["battery_terminal_soc_target"],
        assumptions=[
            "Heuristic only: no market price, imbalance price, BM value or service value is included.",
            "One MWh means grid-side charge or discharge energy; conversion losses change stored energy.",
            "Future-flexibility penalty is scaled by the corresponding reserved-power fraction.",
        ],
    )


def _readiness(required, soc, config, upward, downward):
    missing = [name for name, point in zip(("SoC", "upward reservation", "downward reservation", *CONFIG_METRICS), required) if point is None]
    if missing:
        return BatteryReadiness(status=SnapshotStatus.BLOCKED, calculation_allowed=False, trustworthy_for_live_trading=False, reasons=["Missing battery inputs: " + ", ".join(missing)])
    points = [point for point in required if point is not None]
    if any(point.lineage.quality in (Quality.MISSING, Quality.INVALID) for point in points):
        return BatteryReadiness(status=SnapshotStatus.BLOCKED, calculation_allowed=False, trustworthy_for_live_trading=False, reasons=["Battery telemetry, limits or reservations contain missing/invalid values"])
    assert soc is not None and upward is not None and downward is not None
    try:
        calculate_feasibility(
            soc_mwh=float(soc.value), e_min_mwh=float(config["battery_e_min"].value), e_max_mwh=float(config["battery_e_max"].value),
            charge_power_max_mw=float(config["battery_charge_power_max"].value), discharge_power_max_mw=float(config["battery_discharge_power_max"].value),
            charge_efficiency=float(config["battery_charge_efficiency"].value), discharge_efficiency=float(config["battery_discharge_efficiency"].value),
            duration_hours=0.5, upward_reserved_mw=float(upward.value), downward_reserved_mw=float(downward.value), reserve_duration_hours=float(config["battery_reserve_duration"].value),
        )
    except (ValueError, AttributeError) as exc:
        return BatteryReadiness(status=SnapshotStatus.BLOCKED, calculation_allowed=False, trustworthy_for_live_trading=False, reasons=[str(exc)])
    reasons = []
    if any(point.lineage.quality == Quality.STALE for point in points):
        reasons.append("Battery telemetry, limits or reservations are stale but calculable")
    modes = {point.lineage.source_mode for point in points}
    if modes != {SourceMode.LIVE}:
        reasons.append("Calculation uses non-live input modes: " + ", ".join(sorted(mode.value for mode in modes)))
    if reasons:
        reasons.append("Feasibility is valid for labelled inputs, not trustworthy for live control")
        return BatteryReadiness(status=SnapshotStatus.DEGRADED, calculation_allowed=True, trustworthy_for_live_trading=False, reasons=reasons)
    return BatteryReadiness(status=SnapshotStatus.READY, calculation_allowed=True, trustworthy_for_live_trading=True, reasons=["Telemetry, limits and reservations are fresh, live and internally valid"])


def _snapshot(snapshot, source_mode, quality, readiness, soc, limits, cost, periods, useful, warnings):
    digest = hashlib.sha256(f"{snapshot.input_hash}:battery-flexibility-v1".encode()).hexdigest()
    return BatteryFlexibilitySnapshot(
        battery_snapshot_id=f"battery-{digest[:16]}", cockpit_snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of, input_hash=digest, source_mode=source_mode, quality=quality,
        readiness=readiness, current_soc=soc, limits=limits, opportunity_cost=cost,
        periods=periods, most_useful_periods=useful, warnings=list(dict.fromkeys(warnings)),
    )


def _derived(
    snapshot: CockpitSnapshot,
    metric: str,
    delivery_period: str | None,
    delivery_start: datetime | None,
    value: float,
    unit: str,
    inputs: list[CanonicalDataPoint],
    expression: str,
) -> CanonicalDataPoint:
    identifier = uuid5(NAMESPACE_URL, f"{snapshot.snapshot_id}:{metric}:{delivery_period}:{','.join(point.value_id for point in inputs)}")
    warnings = list(dict.fromkeys(warning for point in inputs for warning in point.lineage.warnings))
    mode = combined_source_mode(inputs)
    if mode in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
        warnings.append(f"Derived from {mode.value} inputs; not live control data.")
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    return CanonicalDataPoint(
        value_id=str(identifier), metric=metric, value=round(value, 6), unit=unit,
        delivery_period=delivery_period, delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="battery_flexibility_calculation", source_mode=mode,
            semantic_kind=SemanticKind.ESTIMATE, quality=combined_quality(inputs),
            published_at=max(published) if published else None,
            retrieved_at=max(point.lineage.retrieved_at for point in inputs), normalised_at=snapshot.as_of,
            raw_field_name=expression, transformations=[expression],
            validation_checks=[
                ValidationCheck(name="finite_result", passed=value == value and abs(value) != float("inf"), detail="calculated value is finite"),
                ValidationCheck(name="traceable_inputs", passed=bool(inputs), detail=f"derived from {len(inputs)} canonical input values"),
            ], warnings=warnings,
        ), included_in_current_snapshot=True, snapshot_id=snapshot.snapshot_id,
    )
