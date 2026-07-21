"""Diagnostic BM and ancillary-service optionality impacts for candidate battery paths."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from cockpit.battery_layer import build_battery_flexibility
from cockpit.battery_path_layer import build_standard_path_comparison, simulate_battery_path
from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.models import (
    AncillaryServiceEstimate,
    BatteryPathInput,
    BatteryPathPeriodResult,
    BatteryPathSimulation,
    BMOptionalityEstimate,
    CanonicalDataPoint,
    CockpitSnapshot,
    DataLineage,
    OptionalityAssumption,
    OptionalityPathImpact,
    OptionalityPeriodDiagnostic,
    OptionalityReadiness,
    OptionalitySnapshot,
    OptionalityViolation,
    Quality,
    SemanticKind,
    ServiceCommitment,
    ServiceProduct,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)


ASSUMPTION_DEFINITIONS = {
    "bm_acceptance_probability": (
        "BM acceptance probability", "Probability that optional BM volume is accepted."
    ),
    "bm_expected_activation_duration": (
        "BM activation duration", "Expected grid-side activation duration if accepted."
    ),
    "bm_expected_margin": (
        "BM expected margin", "Heuristic activation margin before opportunity cost."
    ),
    "bm_non_delivery_penalty": (
        "BM non-delivery penalty", "Penalty assumption applied to unavailable accepted energy."
    ),
    "service_availability_fee": (
        "Service availability fee", "Availability fee for committed capacity."
    ),
    "service_activation_probability": (
        "Service activation probability", "Probability of committed service activation."
    ),
    "service_expected_activation_duration": (
        "Service activation duration", "Expected duration of an ancillary-service activation."
    ),
    "service_expected_margin": (
        "Service activation margin", "Expected margin on activated committed energy."
    ),
    "service_non_delivery_penalty": (
        "Service non-delivery penalty", "Penalty assumption for undeliverable committed energy."
    ),
}

REQUIRED_METRICS = (
    "upward_service_commitment",
    "downward_service_commitment",
    "service_required_duration",
    *ASSUMPTION_DEFINITIONS,
)


@dataclass
class OptionalityLayerResult:
    snapshot: OptionalitySnapshot
    derived_values: list[CanonicalDataPoint]


def build_optionality_snapshot(
    snapshot: CockpitSnapshot,
    custom_path: BatteryPathInput | None = None,
) -> OptionalityLayerResult:
    points = {point.metric: point for point in snapshot.values if point.delivery_period is None}
    required = [points.get(metric) for metric in REQUIRED_METRICS]
    available = [point for point in required if point is not None]
    source_mode = combined_source_mode(available) if available else SourceMode.ERROR
    quality = combined_quality(available) if available else Quality.MISSING

    comparison_result = build_standard_path_comparison(snapshot)
    comparison = comparison_result.comparison
    source_mode = _combined_mode(source_mode, comparison.no_action.source_mode)
    quality = _combined_quality(quality, comparison.no_action.quality)
    readiness = _readiness(required, comparison.no_action)
    commitments = _commitments(points)
    assumptions = _assumptions(points)
    derived = list(comparison_result.derived_values)
    battery_result = build_battery_flexibility(snapshot)
    derived.extend(battery_result.derived_values)

    path_results = [
        comparison.no_action,
        comparison.p50_coverage,
        comparison.preserve_flexibility,
    ]
    if custom_path is not None:
        custom_result = simulate_battery_path(
            snapshot, custom_path.model_copy(update={"path_name": "CUSTOM"})
        )
        path_results.append(custom_result.simulation)
        derived.extend(custom_result.derived_values)

    impacts: list[OptionalityPathImpact] = []
    if readiness.calculation_allowed and battery_result.snapshot.opportunity_cost is not None:
        context = _context(points, battery_result.snapshot.opportunity_cost)
        for path in path_results:
            impact, impact_values = _path_impact(
                snapshot, comparison.no_action, path, context
            )
            impacts.append(impact)
            derived.extend(impact_values)

    digest = hashlib.sha256(
        f"{snapshot.input_hash}:optionality-v1:{custom_path.model_dump() if custom_path else 'standard'}".encode()
    ).hexdigest()
    warnings = list(dict.fromkeys([
        warning for point in available for warning in point.lineage.warnings
    ] + [
        "BM and ancillary-service values are probability-weighted optional estimates, not guaranteed revenue.",
        "No optionality diagnostic is a trade or battery dispatch recommendation.",
    ]))
    result = OptionalitySnapshot(
        optionality_snapshot_id=f"optionality-{digest[:16]}",
        cockpit_snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        source_mode=source_mode,
        quality=quality,
        readiness=readiness,
        commitments=commitments,
        assumptions=assumptions,
        path_impacts=impacts,
        warnings=warnings,
    )
    return OptionalityLayerResult(
        snapshot=result,
        derived_values=list({point.value_id: point for point in derived}.values()),
    )


def _commitments(points: dict[str, CanonicalDataPoint]) -> list[ServiceCommitment]:
    duration = points.get("service_required_duration")
    if duration is None:
        return []
    products = (
        (
            "UPWARD",
            "Committed upward response",
            "upward_service_commitment",
            "Reserved discharge capability that must remain deliverable.",
        ),
        (
            "DOWNWARD",
            "Committed downward response",
            "downward_service_commitment",
            "Reserved charging capability and empty energy space that must remain deliverable.",
        ),
    )
    commitments: list[ServiceCommitment] = []
    for direction, name, metric, description in products:
        reserved = points.get(metric)
        if reserved is None:
            continue
        product = ServiceProduct(
            product_id=f"sample-{direction.lower()}-response",
            name=name,
            direction=direction,
            product_kind="COMMITTED",
            description=description,
        )
        commitments.append(ServiceCommitment(
            commitment_id=f"commitment-{direction.lower()}-{reserved.value_id[:8]}",
            product=product,
            delivery_period="All displayed settlement periods",
            reserved_mw=float(reserved.value),
            required_duration_hours=float(duration.value),
            obligation_status="COMMITTED · MUST REMAIN DELIVERABLE",
            reserved_value=reserved,
            duration_value=duration,
        ))
    return commitments


def _assumptions(points: dict[str, CanonicalDataPoint]) -> list[OptionalityAssumption]:
    assumptions: list[OptionalityAssumption] = []
    for key, (label, description) in ASSUMPTION_DEFINITIONS.items():
        point = points.get(key)
        if point is not None:
            assumptions.append(OptionalityAssumption(
                key=key,
                label=label,
                value=float(point.value),
                unit=point.unit,
                description=description,
                value_point=point,
            ))
    return assumptions


def _context(points, opportunity_cost):
    return {
        **{metric: points[metric] for metric in REQUIRED_METRICS},
        "e_min": points["battery_e_min"],
        "e_max": points["battery_e_max"],
        "charge_limit": points["battery_charge_power_max"],
        "discharge_limit": points["battery_discharge_power_max"],
        "eta_c": points["battery_charge_efficiency"],
        "eta_d": points["battery_discharge_efficiency"],
        "charge_opportunity_cost": opportunity_cost.charge_cost_value,
        "discharge_opportunity_cost": opportunity_cost.discharge_cost_value,
    }


def _path_impact(snapshot, baseline, selected, context):
    baseline_by_period = {period.delivery_period: period for period in baseline.periods}
    diagnostics: list[OptionalityPeriodDiagnostic] = []
    derived: list[CanonicalDataPoint] = []
    violations: list[OptionalityViolation] = []
    for period in selected.periods:
        before_period = baseline_by_period[period.delivery_period]
        before, before_values = _value_components(
            snapshot, f"{selected.path_name}_before", before_period, context
        )
        after, after_values = _value_components(
            snapshot, f"{selected.path_name}_after", period, context
        )
        derived.extend([*before_values, *after_values])
        lost = before["total"] - after["total"]
        lost_value = _derived(
            snapshot, selected.path_name, "optionality_lost", period.delivery_period,
            period.delivery_start, lost, "GBP",
            [before["total_value"], after["total_value"]],
            "optionality before no-action baseline - optionality after selected path",
        )
        derived.append(lost_value)
        period_violations = _commitment_violations(period, after, context)
        violations.extend(period_violations)
        diagnostics.append(OptionalityPeriodDiagnostic(
            settlement_period=period.settlement_period,
            delivery_period=period.delivery_period,
            delivery_start=period.delivery_start,
            delivery_end=period.delivery_end,
            starting_soc_mwh=period.starting_soc_mwh,
            ending_soc_mwh=period.ending_soc_mwh,
            starting_soc_value=period.starting_soc_value,
            ending_soc_value=period.ending_soc_value,
            upward_power_available_before_mw=before["up_power"],
            downward_power_available_before_mw=before["down_power"],
            upward_power_available_after_mw=after["up_power"],
            downward_power_available_after_mw=after["down_power"],
            upward_power_available_before_value=before["up_power_value"],
            downward_power_available_before_value=before["down_power_value"],
            upward_power_available_after_value=after["up_power_value"],
            downward_power_available_after_value=after["down_power_value"],
            upward_duration_available_hours=after["up_duration"],
            downward_duration_available_hours=after["down_duration"],
            upward_duration_available_value=after["up_duration_value"],
            downward_duration_available_value=after["down_duration_value"],
            committed_upward_mw=float(context["upward_service_commitment"].value),
            committed_downward_mw=float(context["downward_service_commitment"].value),
            optional_upward_before_mw=before["optional_up"],
            optional_downward_before_mw=before["optional_down"],
            optional_upward_after_mw=after["optional_up"],
            optional_downward_after_mw=after["optional_down"],
            optional_upward_after_value=after["optional_up_value"],
            optional_downward_after_value=after["optional_down_value"],
            commitment_coverage_ratio=after["coverage"],
            commitment_coverage_value=after["coverage_value"],
            bm_estimate=after["bm"],
            service_estimate=after["service"],
            optionality_value_before_gbp=before["total"],
            optionality_value_after_gbp=after["total"],
            optionality_lost_gbp=lost,
            optionality_value_before_value=before["total_value"],
            optionality_value_after_value=after["total_value"],
            optionality_lost_value=lost_value,
            commitment_at_risk=bool(period_violations),
            violations=period_violations,
            warnings=[
                "Optionality value is heuristic, probability-weighted and not guaranteed revenue."
            ],
        ))

    ranked = sorted(
        diagnostics,
        key=lambda item: (item.commitment_at_risk, item.optionality_lost_gbp),
        reverse=True,
    )
    for rank, item in enumerate(ranked, start=1):
        item.risk_rank = rank

    before_total = sum(period.optionality_value_before_gbp for period in diagnostics)
    after_total = sum(period.optionality_value_after_gbp for period in diagnostics)
    lost_total = before_total - after_total
    before_inputs = [period.optionality_value_before_value for period in diagnostics]
    after_inputs = [period.optionality_value_after_value for period in diagnostics]
    lost_inputs = [period.optionality_lost_value for period in diagnostics]
    before_value = _summary_value(snapshot, selected.path_name, "path_optionality_before", before_total, before_inputs)
    after_value = _summary_value(snapshot, selected.path_name, "path_optionality_after", after_total, after_inputs)
    lost_value = _summary_value(snapshot, selected.path_name, "path_optionality_lost", lost_total, lost_inputs)
    derived.extend([before_value, after_value, lost_value])
    affected = [
        item for item in diagnostics
        if item.commitment_at_risk or abs(item.optionality_lost_gbp) > 0.005
    ]
    worst = max(
        affected,
        key=lambda item: (item.commitment_at_risk, item.optionality_lost_gbp),
        default=None,
    )
    explanation = _explanation(selected, diagnostics, lost_total, violations)
    return OptionalityPathImpact(
        path_name=selected.path_name,
        path_label=selected.path_label,
        optionality_value_before_gbp=before_total,
        optionality_value_after_gbp=after_total,
        optionality_lost_gbp=lost_total,
        optionality_value_before_value=before_value,
        optionality_value_after_value=after_value,
        optionality_lost_value=lost_value,
        commitments_at_risk=sum(item.commitment_at_risk for item in diagnostics),
        worst_affected_period=worst.delivery_period if worst else None,
        periods=diagnostics,
        violations=violations,
        explanation=explanation,
    ), derived


def _value_components(snapshot, key, period: BatteryPathPeriodResult, context):
    value = lambda name: float(context[name].value)
    soc = period.ending_soc_mwh
    y_mw = period.net_export_mw
    required_duration = value("service_required_duration")
    upward_committed = value("upward_service_commitment")
    downward_committed = value("downward_service_commitment")
    up_power = value("discharge_limit") - y_mw
    down_power = value("charge_limit") + y_mw
    up_duration = max(0.0, (soc - value("e_min")) * value("eta_d")) / max(upward_committed, 1e-9)
    down_duration = max(0.0, value("e_max") - soc) / (
        value("eta_c") * max(downward_committed, 1e-9)
    )
    up_energy_mw = max(0.0, (soc - value("e_min")) * value("eta_d")) / required_duration
    down_energy_mw = max(0.0, value("e_max") - soc) / (value("eta_c") * required_duration)
    deliverable_up = max(0.0, min(up_power, up_energy_mw))
    deliverable_down = max(0.0, min(down_power, down_energy_mw))
    optional_up = max(0.0, deliverable_up - upward_committed)
    optional_down = max(0.0, deliverable_down - downward_committed)
    up_coverage = min(1.0, deliverable_up / max(upward_committed, 1e-9))
    down_coverage = min(1.0, deliverable_down / max(downward_committed, 1e-9))
    coverage = min(up_coverage, down_coverage)
    shortfall_up = max(0.0, upward_committed - deliverable_up)
    shortfall_down = max(0.0, downward_committed - deliverable_down)
    shortfall = shortfall_up + shortfall_down

    common_inputs = [
        period.ending_soc_value, period.net_export_value,
        context["e_min"], context["e_max"], context["eta_c"], context["eta_d"],
        context["upward_service_commitment"], context["downward_service_commitment"],
        context["service_required_duration"],
    ]
    up_power_value = _derived(snapshot, key, "upward_power_available", period.delivery_period, period.delivery_start, up_power, "MW", [period.net_export_value, context["discharge_limit"]], "P_discharge_max - y_t")
    down_power_value = _derived(snapshot, key, "downward_power_available", period.delivery_period, period.delivery_start, down_power, "MW", [period.net_export_value, context["charge_limit"]], "P_charge_max + y_t")
    up_duration_value = _derived(snapshot, key, "upward_duration_available", period.delivery_period, period.delivery_start, up_duration, "h", common_inputs, "(E_t - E_min) * eta_d / committed upward MW")
    down_duration_value = _derived(snapshot, key, "downward_duration_available", period.delivery_period, period.delivery_start, down_duration, "h", common_inputs, "(E_max - E_t) / (eta_c * committed downward MW)")
    optional_up_value = _derived(snapshot, key, "optional_upward_mw", period.delivery_period, period.delivery_start, optional_up, "MW", [up_power_value, up_duration_value, context["upward_service_commitment"], context["service_required_duration"]], "max(0, energy-and-power deliverable upward MW - committed upward MW)")
    optional_down_value = _derived(snapshot, key, "optional_downward_mw", period.delivery_period, period.delivery_start, optional_down, "MW", [down_power_value, down_duration_value, context["downward_service_commitment"], context["service_required_duration"]], "max(0, energy-and-power deliverable downward MW - committed downward MW)")
    coverage_value = _derived(snapshot, key, "commitment_coverage_ratio", period.delivery_period, period.delivery_start, coverage, "ratio", [up_power_value, down_power_value, up_duration_value, down_duration_value, context["upward_service_commitment"], context["downward_service_commitment"]], "minimum power-and-energy coverage ratio across committed upward and downward obligations")

    bm_duration = value("bm_expected_activation_duration")
    acceptance = value("bm_acceptance_probability")
    activation_mwh = (optional_up + optional_down) * bm_duration
    gross_bm = acceptance * activation_mwh * value("bm_expected_margin")
    bm_risk = acceptance * shortfall * bm_duration * value("bm_non_delivery_penalty")
    opportunity_cost = acceptance * bm_duration * (
        optional_up * value("discharge_opportunity_cost")
        + optional_down * value("charge_opportunity_cost")
    )
    bm_expected = gross_bm - bm_risk - opportunity_cost
    activation_value = _derived(snapshot, key, "bm_expected_activation_energy", period.delivery_period, period.delivery_start, activation_mwh, "MWh", [optional_up_value, optional_down_value, context["bm_expected_activation_duration"]], "optional upward and downward MW * expected activation duration")
    gross_bm_value = _derived(snapshot, key, "bm_gross_expected_value", period.delivery_period, period.delivery_start, gross_bm, "GBP", [activation_value, context["bm_acceptance_probability"], context["bm_expected_margin"]], "acceptance probability * expected activation MWh * expected margin")
    bm_risk_value = _derived(snapshot, key, "bm_non_delivery_risk_penalty", period.delivery_period, period.delivery_start, bm_risk, "GBP", [coverage_value, context["bm_acceptance_probability"], context["bm_non_delivery_penalty"]], "probability-weighted BM non-delivery energy shortfall penalty")
    opportunity_value = _derived(snapshot, key, "bm_activation_opportunity_cost", period.delivery_period, period.delivery_start, opportunity_cost, "GBP", [activation_value, context["charge_opportunity_cost"], context["discharge_opportunity_cost"]], "probability-weighted activation energy * battery opportunity cost")
    bm_expected_value = _derived(snapshot, key, "bm_expected_optionality_value", period.delivery_period, period.delivery_start, bm_expected, "GBP", [gross_bm_value, bm_risk_value, opportunity_value], "gross expected BM value - non-delivery risk penalty - activation opportunity cost")
    bm = BMOptionalityEstimate(
        acceptance_probability=acceptance,
        expected_activation_mwh=activation_mwh,
        expected_margin_gbp_per_mwh=value("bm_expected_margin"),
        gross_expected_value_gbp=gross_bm,
        non_delivery_risk_penalty_gbp=bm_risk,
        activation_opportunity_cost_gbp=opportunity_cost,
        expected_value_gbp=bm_expected,
        expected_activation_value=activation_value,
        gross_expected_value=gross_bm_value,
        non_delivery_risk_penalty_value=bm_risk_value,
        activation_opportunity_cost_value=opportunity_value,
        expected_value=bm_expected_value,
    )

    availability = value("service_availability_fee") * (upward_committed + downward_committed) * period.duration_hours
    service_activation = value("service_activation_probability") * (deliverable_up + deliverable_down) * value("service_expected_activation_duration") * value("service_expected_margin")
    service_risk = shortfall * required_duration * value("service_non_delivery_penalty")
    service_expected = availability + service_activation - service_risk
    availability_value = _derived(snapshot, key, "service_availability_value", period.delivery_period, period.delivery_start, availability, "GBP", [context["service_availability_fee"], context["upward_service_commitment"], context["downward_service_commitment"], period.ending_soc_value], "availability fee * committed MW * settlement-period duration")
    service_activation_value = _derived(snapshot, key, "service_expected_activation_value", period.delivery_period, period.delivery_start, service_activation, "GBP", [coverage_value, context["service_activation_probability"], context["service_expected_activation_duration"], context["service_expected_margin"]], "activation probability * deliverable committed MW * activation duration * margin")
    service_risk_value = _derived(snapshot, key, "service_non_delivery_risk_penalty", period.delivery_period, period.delivery_start, service_risk, "GBP", [coverage_value, context["service_required_duration"], context["service_non_delivery_penalty"]], "committed energy shortfall * required duration * non-delivery penalty")
    service_expected_value = _derived(snapshot, key, "service_expected_value", period.delivery_period, period.delivery_start, service_expected, "GBP", [availability_value, service_activation_value, service_risk_value], "availability value + expected activation value - non-delivery risk penalty")
    service = AncillaryServiceEstimate(
        availability_value_gbp=availability,
        expected_activation_value_gbp=service_activation,
        non_delivery_risk_penalty_gbp=service_risk,
        expected_service_value_gbp=service_expected,
        availability_value=availability_value,
        expected_activation_value=service_activation_value,
        non_delivery_risk_penalty_value=service_risk_value,
        expected_service_value=service_expected_value,
    )
    total = bm_expected + service_expected
    total_value = _derived(snapshot, key, "period_optionality_value", period.delivery_period, period.delivery_start, total, "GBP", [bm_expected_value, service_expected_value], "BM optionality estimate + ancillary-service expected value")
    values = [
        up_power_value, down_power_value, up_duration_value, down_duration_value,
        optional_up_value, optional_down_value, coverage_value, activation_value,
        gross_bm_value, bm_risk_value, opportunity_value, bm_expected_value,
        availability_value, service_activation_value, service_risk_value,
        service_expected_value, total_value,
    ]
    return {
        "up_power": up_power, "down_power": down_power,
        "up_power_value": up_power_value, "down_power_value": down_power_value,
        "up_duration": up_duration, "down_duration": down_duration,
        "up_duration_value": up_duration_value, "down_duration_value": down_duration_value,
        "deliverable_up": deliverable_up, "deliverable_down": deliverable_down,
        "optional_up": optional_up, "optional_down": optional_down,
        "optional_up_value": optional_up_value, "optional_down_value": optional_down_value,
        "up_coverage": up_coverage, "down_coverage": down_coverage,
        "coverage": coverage, "coverage_value": coverage_value,
        "bm": bm, "service": service, "total": total, "total_value": total_value,
    }, values


def _commitment_violations(period, values, context):
    violations: list[OptionalityViolation] = []
    directions = (
        ("UPWARD", values["up_coverage"], values["up_duration_value"], context["upward_service_commitment"]),
        ("DOWNWARD", values["down_coverage"], values["down_duration_value"], context["downward_service_commitment"]),
    )
    for direction, coverage, observed, required in directions:
        if coverage < 1 - 1e-8:
            violations.append(OptionalityViolation(
                code=f"{direction}_COMMITMENT_AT_RISK",
                message=f"{direction.title()} committed service is not fully power-and-energy deliverable after the candidate action.",
                severity="ERROR",
                delivery_period=period.delivery_period,
                direction=direction,
                observed_value=observed,
                required_value=required,
            ))
    return violations


def _readiness(required, path):
    names = [*REQUIRED_METRICS]
    missing = [name for name, point in zip(names, required) if point is None]
    if missing:
        return OptionalityReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=["Missing required commitments or optionality assumptions: " + ", ".join(missing)],
        )
    points = [point for point in required if point is not None]
    invalid = [point.metric for point in points if point.lineage.quality in (Quality.MISSING, Quality.INVALID)]
    error_modes = [point.metric for point in points if point.lineage.source_mode == SourceMode.ERROR]
    if invalid or error_modes or not path.readiness.calculation_allowed:
        return OptionalityReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=list(dict.fromkeys([
                *( ["Missing/invalid optionality inputs: " + ", ".join(invalid)] if invalid else [] ),
                *( ["Required optionality feeds are in ERROR: " + ", ".join(error_modes)] if error_modes else [] ),
                *path.readiness.reasons,
            ])),
        )
    reasons: list[str] = []
    if any(point.lineage.quality == Quality.STALE for point in points):
        reasons.append("One or more service commitments or optionality assumptions are stale but calculable")
    modes = {point.lineage.source_mode for point in points}
    if modes != {SourceMode.LIVE}:
        reasons.append("Calculation uses non-live optionality inputs: " + ", ".join(sorted(mode.value for mode in modes)))
    if path.readiness.status == SnapshotStatus.DEGRADED:
        reasons.extend(path.readiness.reasons)
    if reasons:
        reasons.append("Optionality is calculable for labelled inputs but not trustworthy for live trading")
        return OptionalityReadiness(
            status=SnapshotStatus.DEGRADED,
            calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=list(dict.fromkeys(reasons)),
        )
    return OptionalityReadiness(
        status=SnapshotStatus.READY,
        calculation_allowed=True,
        trustworthy_for_live_trading=True,
        reasons=["Commitments, assumptions, SoC, limits and path data are live, fresh and valid"],
    )


def _combined_mode(*modes: SourceMode) -> SourceMode:
    precedence = (
        SourceMode.ERROR,
        SourceMode.SYNTHETIC,
        SourceMode.SAMPLE,
        SourceMode.LATEST_AVAILABLE,
        SourceMode.LIVE,
    )
    return next(mode for mode in precedence if mode in modes)


def _combined_quality(*qualities: Quality) -> Quality:
    precedence = (
        Quality.INVALID,
        Quality.MISSING,
        Quality.STALE,
        Quality.PARTIAL,
        Quality.REVISED,
        Quality.FRESH,
    )
    return next(quality for quality in precedence if quality in qualities)


def _explanation(path, periods, lost, violations):
    active = next((period for period in path.periods if abs(period.net_export_mw) > 1e-8), None)
    if active is None:
        action = "The path takes no battery action, so it is the optionality baseline."
    elif active.net_export_mw < 0:
        action = f"The path charges in {active.delivery_period}, increasing SoC but consuming downward power and empty-space optionality."
    else:
        action = f"The path discharges in {active.delivery_period}, consuming upward power and stored-energy optionality."
    impact = (
        f"Estimated optionality lost versus no action is £{lost:.2f}."
        if lost >= 0 else f"Estimated optionality increases by £{abs(lost):.2f} versus no action."
    )
    risk = (
        f"{len(violations)} commitment-deliverability warning(s) are present."
        if violations else "Committed upward and downward service remains covered in the displayed horizon."
    )
    affected = [
        item for item in periods
        if item.commitment_at_risk or abs(item.optionality_lost_gbp) > 0.005
    ]
    worst = max(affected, key=lambda item: item.optionality_lost_gbp, default=None)
    period_text = f" The largest value impact is in {worst.delivery_period}." if worst else ""
    return (
        f"{action} {risk} {impact}{period_text} BM and service values are probability-weighted, "
        "not guaranteed revenue, and this diagnostic is not an action recommendation."
    )


def _summary_value(snapshot, path_name, metric, value, inputs):
    return _derived(
        snapshot, path_name, metric, None, None, value, "GBP", inputs,
        "sum of period-level optionality diagnostic values",
    )


def _derived(snapshot: CockpitSnapshot, path_name: str, metric: str, delivery_period: str | None, delivery_start: datetime | None, value: float, unit: str, inputs: list[CanonicalDataPoint], expression: str):
    identifier = uuid5(
        NAMESPACE_URL,
        f"{snapshot.snapshot_id}:{path_name}:{metric}:{delivery_period}:{value:.8f}:{','.join(point.value_id for point in inputs)}",
    )
    mode = combined_source_mode(inputs)
    quality = combined_quality(inputs)
    warnings = list(dict.fromkeys([
        warning for point in inputs for warning in point.lineage.warnings
    ] + [
        "BM and service value is a probability-weighted optional estimate, not guaranteed revenue."
    ]))
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    return CanonicalDataPoint(
        value_id=str(identifier),
        metric=metric,
        value=round(value, 6),
        unit=unit,
        delivery_period=delivery_period,
        delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="optionality_diagnostic",
            source_mode=mode,
            semantic_kind=SemanticKind.ESTIMATE,
            quality=quality,
            published_at=max(published) if published else None,
            retrieved_at=max(point.lineage.retrieved_at for point in inputs),
            normalised_at=snapshot.as_of,
            raw_field_name=expression,
            transformations=[expression],
            validation_checks=[
                ValidationCheck(name="finite_result", passed=value == value and abs(value) != float("inf"), detail="value is finite"),
                ValidationCheck(name="traceable_inputs", passed=bool(inputs), detail=f"derived from {len(inputs)} canonical values"),
                ValidationCheck(name="optional_not_guaranteed", passed=True, detail="value is explicitly labelled optional and non-guaranteed"),
            ],
            warnings=warnings,
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot.snapshot_id,
    )
