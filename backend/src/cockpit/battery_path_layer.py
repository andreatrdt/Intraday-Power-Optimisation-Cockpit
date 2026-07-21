"""Sequential, diagnostic battery-path simulation across settlement periods."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from cockpit.battery_layer import CONFIG_METRICS, build_battery_flexibility
from cockpit.battery_physics import calculate_feasibility, next_soc, power_to_energy
from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.models import (
    BatteryPathComparison,
    BatteryPathInput,
    BatteryPathPeriodAction,
    BatteryPathPeriodResult,
    BatteryPathReadiness,
    BatteryPathSimulation,
    BatteryPathViolation,
    CanonicalDataPoint,
    CockpitSnapshot,
    DataLineage,
    Quality,
    ScenarioExposure,
    SemanticKind,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.position_layer import build_forecast_position, direction


PRESERVE_FRACTION = 0.25
PATH_LABELS = {
    "NO_ACTION": "No battery action",
    "P50_COVERAGE": "Cover P50 exposure",
    "PRESERVE_FLEXIBILITY": "Preserve flexibility",
    "CUSTOM": "Custom path",
}


@dataclass
class BatteryPathLayerResult:
    simulation: BatteryPathSimulation
    derived_values: list[CanonicalDataPoint]


@dataclass
class BatteryPathComparisonResult:
    comparison: BatteryPathComparison
    derived_values: list[CanonicalDataPoint]


def simulate_battery_path(
    snapshot: CockpitSnapshot,
    path_input: BatteryPathInput,
) -> BatteryPathLayerResult:
    battery = build_battery_flexibility(snapshot).snapshot
    positions = build_forecast_position(snapshot).snapshot
    readiness = _readiness(battery, positions)
    physical = {point.metric: point for point in snapshot.values if point.delivery_period is None}
    physical_inputs = [
        physical.get("battery_soc"),
        physical.get("upward_service_commitment"),
        physical.get("downward_service_commitment"),
        *(physical.get(metric) for metric in CONFIG_METRICS),
    ]
    valid_physical = [point for point in physical_inputs if point is not None]
    mode = combined_source_mode(valid_physical) if valid_physical else SourceMode.ERROR
    quality = combined_quality(valid_physical) if valid_physical else Quality.MISSING
    path_kind = path_input.path_name.upper()
    path_label = PATH_LABELS.get(path_kind, path_input.path_name.replace("_", " ").title())
    if not readiness.calculation_allowed:
        return BatteryPathLayerResult(
            simulation=_empty_simulation(snapshot, path_kind, path_label, mode, quality, readiness),
            derived_values=[],
        )

    soc_input = physical["battery_soc"]
    e_min = physical["battery_e_min"]
    e_max = physical["battery_e_max"]
    charge_limit = physical["battery_charge_power_max"]
    discharge_limit = physical["battery_discharge_power_max"]
    eta_c = physical["battery_charge_efficiency"]
    eta_d = physical["battery_discharge_efficiency"]
    reserve_duration = physical["battery_reserve_duration"]
    terminal_target = physical["battery_terminal_soc_target"]
    upward_reserved = physical["upward_service_commitment"]
    downward_reserved = physical["downward_service_commitment"]
    base_inputs = [
        soc_input, e_min, e_max, charge_limit, discharge_limit, eta_c, eta_d,
        reserve_duration, terminal_target, upward_reserved, downward_reserved,
    ]
    if path_kind in ("NO_ACTION", "P50_COVERAGE", "PRESERVE_FLEXIBILITY"):
        actions = _standard_actions(
            path_kind, positions.periods, float(soc_input.value),
            e_min=float(e_min.value), e_max=float(e_max.value),
            charge_limit=float(charge_limit.value), discharge_limit=float(discharge_limit.value),
            eta_c=float(eta_c.value), eta_d=float(eta_d.value),
            upward_reserved=float(upward_reserved.value), downward_reserved=float(downward_reserved.value),
            reserve_duration=float(reserve_duration.value),
        )
    else:
        actions = list(path_input.actions)

    action_map: dict[str, BatteryPathPeriodAction] = {}
    duplicate_periods: set[str] = set()
    for action in actions:
        if action.delivery_period in action_map:
            duplicate_periods.add(action.delivery_period)
        action_map[action.delivery_period] = action
    known_periods = {period.delivery_period for period in positions.periods}
    unknown_periods = sorted(set(action_map) - known_periods)
    path_violations: list[BatteryPathViolation] = []
    for period in sorted(duplicate_periods):
        path_violations.append(BatteryPathViolation(
            code="DUPLICATE_PERIOD_ACTION", message=f"Multiple actions were supplied for {period}",
            delivery_period=period,
        ))
    for period in unknown_periods:
        path_violations.append(BatteryPathViolation(
            code="UNKNOWN_DELIVERY_PERIOD", message=f"Action references unknown period {period}",
            delivery_period=period,
        ))

    current_soc = float(soc_input.value)
    prior_soc_value: CanonicalDataPoint | None = None
    results: list[BatteryPathPeriodResult] = []
    derived: list[CanonicalDataPoint] = []
    for period in positions.periods:
        action = action_map.get(period.delivery_period, BatteryPathPeriodAction(delivery_period=period.delivery_period))
        duration = period.forecast.duration_hours
        start_inputs = [prior_soc_value] if prior_soc_value is not None else [soc_input]
        starting_soc_value = _derived(
            snapshot, path_kind, "path_starting_soc", period.delivery_period, period.delivery_start,
            current_soc, "MWh", start_inputs, "previous period ending SoC, or current telemetry for the first period",
        )
        action_inputs = [starting_soc_value, charge_limit, discharge_limit]
        charge_power_value = _derived(
            snapshot, path_kind, "path_charge_power", period.delivery_period, period.delivery_start,
            action.charge_mw, "MW", action_inputs, "candidate path charge power input",
            semantic_kind=SemanticKind.ASSUMPTION,
        )
        discharge_power_value = _derived(
            snapshot, path_kind, "path_discharge_power", period.delivery_period, period.delivery_start,
            action.discharge_mw, "MW", action_inputs, "candidate path discharge power input",
            semantic_kind=SemanticKind.ASSUMPTION,
        )
        charge_mwh = power_to_energy(action.charge_mw, duration)
        discharge_mwh = power_to_energy(action.discharge_mw, duration)
        charge_energy_value = _derived(
            snapshot, path_kind, "path_charge_energy", period.delivery_period, period.delivery_start,
            charge_mwh, "MWh", [charge_power_value], "charge MW x settlement-period duration",
        )
        discharge_energy_value = _derived(
            snapshot, path_kind, "path_discharge_energy", period.delivery_period, period.delivery_start,
            discharge_mwh, "MWh", [discharge_power_value], "discharge MW x settlement-period duration",
        )
        y_mw = action.discharge_mw - action.charge_mw
        net_export_value = _derived(
            snapshot, path_kind, "path_net_export", period.delivery_period, period.delivery_start,
            y_mw, "MW", [charge_power_value, discharge_power_value], "y_t = discharge MW - charge MW",
        )
        ending_soc = next_soc(
            current_soc, action.charge_mw, action.discharge_mw, duration,
            float(eta_c.value), float(eta_d.value),
        )
        ending_soc_value = _derived(
            snapshot, path_kind, "path_ending_soc", period.delivery_period, period.delivery_start,
            ending_soc, "MWh", [starting_soc_value, charge_energy_value, discharge_energy_value, eta_c, eta_d],
            "E[t+1] = E[t] + eta_c * charge MWh - discharge MWh / eta_d",
        )
        upward_headroom = float(discharge_limit.value) - y_mw - float(upward_reserved.value)
        downward_headroom = float(charge_limit.value) + y_mw - float(downward_reserved.value)
        up_headroom_value = _derived(
            snapshot, path_kind, "path_upward_power_headroom", period.delivery_period, period.delivery_start,
            upward_headroom, "MW", [net_export_value, discharge_limit, upward_reserved],
            "P_discharge_max - y_t - U_t",
        )
        down_headroom_value = _derived(
            snapshot, path_kind, "path_downward_power_headroom", period.delivery_period, period.delivery_start,
            downward_headroom, "MW", [net_export_value, charge_limit, downward_reserved],
            "P_charge_max + y_t - D_t",
        )
        upward_duration = _upward_duration(
            ending_soc, float(e_min.value), float(eta_d.value),
            float(upward_reserved.value), max(0.0, float(discharge_limit.value) - y_mw),
        )
        downward_duration = _downward_duration(
            ending_soc, float(e_max.value), float(eta_c.value),
            float(downward_reserved.value), max(0.0, float(charge_limit.value) + y_mw),
        )
        up_duration_value = _derived(
            snapshot, path_kind, "path_upward_energy_duration", period.delivery_period, period.delivery_start,
            upward_duration, "h", [ending_soc_value, e_min, eta_d, upward_reserved],
            "deliverable energy above E_min / reserved upward MW",
        )
        down_duration_value = _derived(
            snapshot, path_kind, "path_downward_energy_duration", period.delivery_period, period.delivery_start,
            downward_duration, "h", [ending_soc_value, e_max, eta_c, downward_reserved],
            "grid-side empty energy capacity / reserved downward MW",
        )
        max_charge, max_discharge, feasibility_bindings = _available_flexibility(
            current_soc, float(e_min.value), float(e_max.value), float(charge_limit.value),
            float(discharge_limit.value), float(eta_c.value), float(eta_d.value), duration,
            float(upward_reserved.value), float(downward_reserved.value), float(reserve_duration.value),
        )
        max_charge_value = _derived(
            snapshot, path_kind, "path_max_feasible_charge", period.delivery_period, period.delivery_start,
            max_charge, "MWh", [starting_soc_value, *base_inputs[1:]],
            "maximum feasible charge from sequential starting SoC",
        )
        max_discharge_value = _derived(
            snapshot, path_kind, "path_max_feasible_discharge", period.delivery_period, period.delivery_start,
            max_discharge, "MWh", [starting_soc_value, *base_inputs[1:]],
            "maximum feasible discharge from sequential starting SoC",
        )
        violations = _period_violations(
            period.delivery_period, action, ending_soc, y_mw,
            charge_power_value, discharge_power_value, ending_soc_value,
            up_headroom_value, down_headroom_value,
            e_min, e_max, charge_limit, discharge_limit, eta_c, eta_d,
            upward_reserved, downward_reserved, reserve_duration,
        )
        bindings = _actual_bindings(
            charge_mwh, discharge_mwh, max_charge, max_discharge,
            feasibility_bindings, ending_soc, float(e_min.value), float(e_max.value),
            float(eta_c.value), float(eta_d.value), float(upward_reserved.value),
            float(downward_reserved.value), float(reserve_duration.value),
        )
        residuals: list[ScenarioExposure] = []
        battery_export_mwh = discharge_mwh - charge_mwh
        for exposure in period.exposures:
            residual = exposure.residual_position_mwh + battery_export_mwh
            residual_value = _derived(
                snapshot, path_kind, f"path_residual_{exposure.scenario.lower()}",
                period.delivery_period, period.delivery_start, residual, "MWh",
                [exposure.exposure_value, charge_energy_value, discharge_energy_value],
                f"I_t^{exposure.scenario} + discharge MWh - charge MWh",
            )
            residuals.append(ScenarioExposure(
                scenario=exposure.scenario,
                generation_mwh=exposure.generation_mwh,
                contracted_position_mwh=exposure.contracted_position_mwh,
                residual_position_mwh=residual,
                direction=direction(residual),
                generation_value=exposure.generation_value,
                exposure_value=residual_value,
            ))
            derived.append(residual_value)
        result = BatteryPathPeriodResult(
            settlement_period=period.settlement_period, delivery_period=period.delivery_period,
            delivery_start=period.delivery_start, delivery_end=period.delivery_end,
            duration_hours=duration, starting_soc_mwh=current_soc,
            charge_mw=action.charge_mw, charge_mwh=charge_mwh,
            discharge_mw=action.discharge_mw, discharge_mwh=discharge_mwh,
            net_export_mw=y_mw, ending_soc_mwh=ending_soc,
            upward_power_headroom_mw=upward_headroom, downward_power_headroom_mw=downward_headroom,
            upward_energy_duration_hours=upward_duration,
            downward_energy_duration_hours=downward_duration,
            max_feasible_charge_mwh=max_charge, max_feasible_discharge_mwh=max_discharge,
            starting_soc_value=starting_soc_value, charge_power_value=charge_power_value,
            charge_energy_value=charge_energy_value, discharge_power_value=discharge_power_value,
            discharge_energy_value=discharge_energy_value, net_export_value=net_export_value,
            ending_soc_value=ending_soc_value, upward_power_headroom_value=up_headroom_value,
            downward_power_headroom_value=down_headroom_value,
            upward_energy_duration_value=up_duration_value,
            downward_energy_duration_value=down_duration_value,
            max_feasible_charge_value=max_charge_value, max_feasible_discharge_value=max_discharge_value,
            exposure_before=period.exposures, residual_exposure=residuals,
            binding_constraints=bindings, violations=violations,
        )
        results.append(result)
        path_violations.extend(violations)
        derived.extend([
            starting_soc_value, charge_power_value, charge_energy_value, discharge_power_value,
            discharge_energy_value, net_export_value, ending_soc_value, up_headroom_value,
            down_headroom_value, up_duration_value, down_duration_value,
            max_charge_value, max_discharge_value,
        ])
        prior_soc_value = ending_soc_value
        current_soc = ending_soc

    terminal_soc_value = prior_soc_value
    terminal_shortfall = max(0.0, float(terminal_target.value) - current_soc)
    terminal_shortfall_value = _derived(
        snapshot, path_kind, "path_terminal_soc_shortfall", None, None,
        terminal_shortfall, "MWh", [terminal_soc_value or soc_input, terminal_target],
        "max(0, terminal SoC target - simulated terminal SoC)",
    )
    p50_residual_inputs = [
        next(item for item in period.residual_exposure if item.scenario == "P50").exposure_value
        for period in results
    ]
    total_p50_residual = sum(abs(float(point.value)) for point in p50_residual_inputs)
    total_p50_value = _derived(
        snapshot, path_kind, "path_total_absolute_p50_residual", None, None,
        total_p50_residual, "MWh", p50_residual_inputs,
        "sum of absolute P50 residual exposure across simulated periods",
    )
    derived.extend([terminal_shortfall_value, total_p50_value])
    first_binding = next((item for period in results for item in period.binding_constraints), None)
    explanation = _explanation(path_label, results, terminal_shortfall, bool(path_violations))
    warnings = list(dict.fromkeys(
        warning for point in valid_physical for warning in point.lineage.warnings
    ))
    simulation_hash = hashlib.sha256(
        f"{snapshot.input_hash}:{path_kind}:{[(a.delivery_period, a.charge_mw, a.discharge_mw) for a in actions]}".encode()
    ).hexdigest()
    simulation = BatteryPathSimulation(
        simulation_id=f"path-{simulation_hash[:16]}", cockpit_snapshot_id=snapshot.snapshot_id,
        path_name=path_kind, path_label=path_label, path_kind=path_kind,
        as_of=snapshot.as_of, source_mode=mode, quality=quality, readiness=readiness,
        valid=not path_violations, periods=results, terminal_soc_mwh=current_soc,
        e_min_mwh=float(e_min.value), e_max_mwh=float(e_max.value),
        e_min_value=e_min, e_max_value=e_max,
        terminal_soc_value=terminal_soc_value, terminal_target_mwh=float(terminal_target.value),
        terminal_target_value=terminal_target, terminal_shortfall_mwh=terminal_shortfall,
        terminal_shortfall_value=terminal_shortfall_value,
        total_absolute_p50_residual_mwh=total_p50_residual,
        total_absolute_p50_residual_value=total_p50_value,
        first_binding_constraint=first_binding, violations=path_violations,
        explanation=explanation, warnings=warnings,
    )
    return BatteryPathLayerResult(simulation=simulation, derived_values=derived)


def build_standard_path_comparison(snapshot: CockpitSnapshot) -> BatteryPathComparisonResult:
    no_action = simulate_battery_path(snapshot, BatteryPathInput(path_name="NO_ACTION"))
    p50 = simulate_battery_path(snapshot, BatteryPathInput(path_name="P50_COVERAGE"))
    preserve = simulate_battery_path(snapshot, BatteryPathInput(path_name="PRESERVE_FLEXIBILITY"))
    no_terminal = no_action.simulation.terminal_soc_mwh or 0.0
    no_residual = no_action.simulation.total_absolute_p50_residual_mwh or 0.0
    comparison_hash = hashlib.sha256(f"{snapshot.input_hash}:battery-path-comparison-v1".encode()).hexdigest()
    comparison = BatteryPathComparison(
        comparison_id=f"path-comparison-{comparison_hash[:16]}",
        cockpit_snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of,
        readiness=no_action.simulation.readiness, no_action=no_action.simulation,
        p50_coverage=p50.simulation, preserve_flexibility=preserve.simulation,
        p50_terminal_soc_delta_mwh=(p50.simulation.terminal_soc_mwh or 0.0) - no_terminal,
        preserve_terminal_soc_delta_mwh=(preserve.simulation.terminal_soc_mwh or 0.0) - no_terminal,
        p50_residual_reduction_mwh=no_residual - (p50.simulation.total_absolute_p50_residual_mwh or 0.0),
        preserve_residual_reduction_mwh=no_residual - (preserve.simulation.total_absolute_p50_residual_mwh or 0.0),
        explanation=(
            "Standard paths are diagnostic comparisons. Cover P50 consumes feasible flexibility period by "
            "period; preserve flexibility uses 25% of that directional energy; neither is a recommendation."
        ),
    )
    return BatteryPathComparisonResult(
        comparison=comparison,
        derived_values=list({
            point.value_id: point
            for result in (no_action, p50, preserve)
            for point in result.derived_values
        }.values()),
    )


def _standard_actions(path_kind, periods, initial_soc, **parameters):
    actions: list[BatteryPathPeriodAction] = []
    soc = initial_soc
    for period in periods:
        duration = period.forecast.duration_hours
        if path_kind == "NO_ACTION":
            action = BatteryPathPeriodAction(delivery_period=period.delivery_period)
        else:
            max_charge, max_discharge, _ = _available_flexibility(
                soc, parameters["e_min"], parameters["e_max"], parameters["charge_limit"],
                parameters["discharge_limit"], parameters["eta_c"], parameters["eta_d"], duration,
                parameters["upward_reserved"], parameters["downward_reserved"], parameters["reserve_duration"],
            )
            p50 = next(item for item in period.exposures if item.scenario == "P50").residual_position_mwh
            fraction = PRESERVE_FRACTION if path_kind == "PRESERVE_FLEXIBILITY" else 1.0
            if p50 > 0.05:
                charge_mwh = min(p50, max_charge) * fraction
                action = BatteryPathPeriodAction(
                    delivery_period=period.delivery_period, charge_mw=charge_mwh / duration,
                )
            elif p50 < -0.05:
                discharge_mwh = min(abs(p50), max_discharge) * fraction
                action = BatteryPathPeriodAction(
                    delivery_period=period.delivery_period, discharge_mw=discharge_mwh / duration,
                )
            else:
                action = BatteryPathPeriodAction(delivery_period=period.delivery_period)
        actions.append(action)
        soc = next_soc(
            soc, action.charge_mw, action.discharge_mw, duration,
            parameters["eta_c"], parameters["eta_d"],
        )
    return actions


def _available_flexibility(soc, e_min, e_max, charge_limit, discharge_limit, eta_c, eta_d, duration, upward_reserved, downward_reserved, reserve_duration):
    try:
        result = calculate_feasibility(
            soc_mwh=soc, e_min_mwh=e_min, e_max_mwh=e_max,
            charge_power_max_mw=charge_limit, discharge_power_max_mw=discharge_limit,
            charge_efficiency=eta_c, discharge_efficiency=eta_d, duration_hours=duration,
            upward_reserved_mw=upward_reserved, downward_reserved_mw=downward_reserved,
            reserve_duration_hours=reserve_duration,
        )
        return result.max_charge_mwh, result.max_discharge_mwh, result.binding_constraints
    except ValueError:
        return 0.0, 0.0, ["STATE_OUTSIDE_RESERVE_ENVELOPE"]


def _period_violations(period, action, ending_soc, y_mw, charge_value, discharge_value, soc_value, up_value, down_value, e_min, e_max, charge_limit, discharge_limit, eta_c, eta_d, upward, downward, reserve_duration):
    violations: list[BatteryPathViolation] = []
    def add(code, message, observed, limit=None):
        violations.append(BatteryPathViolation(
            code=code, message=message, delivery_period=period,
            observed_value=observed, limit_value=limit,
        ))
    if action.charge_mw < 0:
        add("NEGATIVE_CHARGE", "Charge power cannot be negative", charge_value)
    if action.discharge_mw < 0:
        add("NEGATIVE_DISCHARGE", "Discharge power cannot be negative", discharge_value)
    if action.charge_mw > 0.000001 and action.discharge_mw > 0.000001:
        add("SIMULTANEOUS_CHARGE_DISCHARGE", "Simultaneous charge and discharge is not a valid candidate action", charge_value, discharge_value)
    if action.charge_mw > float(charge_limit.value) + 0.000001:
        add("CHARGE_POWER_LIMIT", "Charge power exceeds P_charge_max", charge_value, charge_limit)
    if action.discharge_mw > float(discharge_limit.value) + 0.000001:
        add("DISCHARGE_POWER_LIMIT", "Discharge power exceeds P_discharge_max", discharge_value, discharge_limit)
    if ending_soc < float(e_min.value) - 0.000001:
        add("SOC_BELOW_MINIMUM", "Ending SoC is below E_min", soc_value, e_min)
    if ending_soc > float(e_max.value) + 0.000001:
        add("SOC_ABOVE_MAXIMUM", "Ending SoC is above E_max", soc_value, e_max)
    if float(up_value.value) < -0.000001:
        add("UPWARD_POWER_RESERVATION", "Action leaves insufficient power for reserved upward capability", up_value, upward)
    if float(down_value.value) < -0.000001:
        add("DOWNWARD_POWER_RESERVATION", "Action leaves insufficient power for reserved downward capability", down_value, downward)
    reserve_floor = float(e_min.value) + float(upward.value) * float(reserve_duration.value) / float(eta_d.value)
    reserve_ceiling = float(e_max.value) - float(eta_c.value) * float(downward.value) * float(reserve_duration.value)
    if ending_soc < reserve_floor - 0.000001:
        add("UPWARD_ENERGY_DURATION", "Insufficient stored energy for reserved upward-service duration", soc_value, e_min)
    if ending_soc > reserve_ceiling + 0.000001:
        add("DOWNWARD_ENERGY_DURATION", "Insufficient empty capacity for reserved downward-service duration", soc_value, e_max)
    return violations


def _actual_bindings(charge_mwh, discharge_mwh, max_charge, max_discharge, feasibility_bindings, ending_soc, e_min, e_max, eta_c, eta_d, upward, downward, reserve_duration):
    bindings: list[str] = []
    tolerance = 0.00001
    if charge_mwh > tolerance and abs(charge_mwh - max_charge) <= tolerance:
        bindings.extend(item for item in feasibility_bindings if "CHARGE" in item or "DOWNWARD" in item)
    if discharge_mwh > tolerance and abs(discharge_mwh - max_discharge) <= tolerance:
        bindings.extend(item for item in feasibility_bindings if "DISCHARGE" in item or "UPWARD" in item)
    reserve_floor = e_min + upward * reserve_duration / eta_d
    reserve_ceiling = e_max - eta_c * downward * reserve_duration
    if abs(ending_soc - reserve_floor) <= tolerance:
        bindings.append("UPWARD_ENERGY_DURATION")
    if abs(ending_soc - reserve_ceiling) <= tolerance:
        bindings.append("DOWNWARD_ENERGY_DURATION")
    return list(dict.fromkeys(bindings))


def _upward_duration(soc, e_min, eta_d, reserved_mw, available_mw):
    denominator = reserved_mw if reserved_mw > 0 else available_mw
    return max(0.0, (soc - e_min) * eta_d) / denominator if denominator > 0 else 0.0


def _downward_duration(soc, e_max, eta_c, reserved_mw, available_mw):
    denominator = reserved_mw if reserved_mw > 0 else available_mw
    grid_side_space = max(0.0, e_max - soc) / eta_c
    return grid_side_space / denominator if denominator > 0 else 0.0


def _readiness(battery, positions):
    reasons = list(battery.readiness.reasons)
    if not battery.readiness.calculation_allowed:
        return BatteryPathReadiness(
            status=SnapshotStatus.BLOCKED, calculation_allowed=False,
            trustworthy_for_live_trading=False, reasons=reasons,
        )
    if not positions.readiness.calculation_allowed or not positions.periods:
        return BatteryPathReadiness(
            status=SnapshotStatus.BLOCKED, calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=reasons + ["Settlement periods or P10/P50/P90 exposure data are missing or invalid"],
        )
    if any(period.forecast.duration_hours <= 0 for period in positions.periods):
        return BatteryPathReadiness(
            status=SnapshotStatus.BLOCKED, calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=reasons + ["Settlement-period duration is missing or invalid"],
        )
    if battery.readiness.status == SnapshotStatus.DEGRADED or positions.readiness.status == SnapshotStatus.DEGRADED:
        return BatteryPathReadiness(
            status=SnapshotStatus.DEGRADED, calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=list(dict.fromkeys(reasons + positions.readiness.reasons)),
        )
    return BatteryPathReadiness(
        status=SnapshotStatus.READY, calculation_allowed=True,
        trustworthy_for_live_trading=True,
        reasons=["Telemetry, limits, periods and exposure data are fresh, live and valid"],
    )


def _explanation(label, periods, terminal_shortfall, has_violations):
    active = next((period for period in periods if period.charge_mwh > 0.0001 or period.discharge_mwh > 0.0001), None)
    if active is None:
        action_text = "The path takes no battery action, so SoC is preserved across all displayed periods."
    elif active.charge_mwh > 0:
        action_text = (
            f"In {label}, the battery charges {active.charge_mwh:.1f} MWh in {active.delivery_period}, "
            f"moving SoC from {active.starting_soc_mwh:.1f} to {active.ending_soc_mwh:.1f} MWh after efficiency."
        )
    else:
        action_text = (
            f"In {label}, the battery discharges {active.discharge_mwh:.1f} MWh in {active.delivery_period}, "
            f"moving SoC from {active.starting_soc_mwh:.1f} to {active.ending_soc_mwh:.1f} MWh after efficiency."
        )
    constraint_text = (
        "One or more hard constraints are violated; inspect the flagged periods before treating the path as feasible."
        if has_violations else
        "No hard constraint is violated by the simulated path."
    )
    terminal_text = (
        f"The terminal SoC target is short by {terminal_shortfall:.1f} MWh."
        if terminal_shortfall > 0.05 else "The terminal SoC target is met."
    )
    return f"{action_text} {constraint_text} {terminal_text} This is a diagnostic path, not a recommendation."


def _empty_simulation(snapshot, path_kind, path_label, mode, quality, readiness):
    return BatteryPathSimulation(
        simulation_id=f"path-blocked-{snapshot.snapshot_id}",
        cockpit_snapshot_id=snapshot.snapshot_id, path_name=path_kind,
        path_label=path_label, path_kind=path_kind, as_of=snapshot.as_of,
        source_mode=mode, quality=quality, readiness=readiness, valid=False,
        explanation="Sequential path simulation is blocked because required inputs are unavailable.",
    )


def _derived(snapshot: CockpitSnapshot, path_kind: str, metric: str, delivery_period: str | None, delivery_start: datetime | None, value: float, unit: str, inputs: list[CanonicalDataPoint], expression: str, semantic_kind: SemanticKind = SemanticKind.ESTIMATE):
    identifier = uuid5(
        NAMESPACE_URL,
        f"{snapshot.snapshot_id}:{path_kind}:{metric}:{delivery_period}:{value:.8f}:{','.join(point.value_id for point in inputs)}",
    )
    warnings = list(dict.fromkeys(warning for point in inputs for warning in point.lineage.warnings))
    mode = combined_source_mode(inputs)
    if mode in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
        warnings.append(f"Sequential path is derived from {mode.value} inputs; not live control data.")
    warnings = list(dict.fromkeys(warnings))
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    return CanonicalDataPoint(
        value_id=str(identifier), metric=metric, value=round(value, 6), unit=unit,
        delivery_period=delivery_period, delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="battery_path_simulation" if semantic_kind == SemanticKind.ESTIMATE else "battery_path_candidate_input",
            source_mode=mode, semantic_kind=semantic_kind, quality=combined_quality(inputs),
            published_at=max(published) if published else None,
            retrieved_at=max(point.lineage.retrieved_at for point in inputs),
            normalised_at=snapshot.as_of, raw_field_name=expression,
            transformations=[expression],
            validation_checks=[
                ValidationCheck(name="finite_result", passed=value == value and abs(value) != float("inf"), detail="value is finite"),
                ValidationCheck(name="traceable_inputs", passed=bool(inputs), detail=f"derived from {len(inputs)} canonical values"),
            ], warnings=warnings,
        ), included_in_current_snapshot=True, snapshot_id=snapshot.snapshot_id,
    )
