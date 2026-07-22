"""Transparent diagnostic coordination of exposure, market, battery and optionality layers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from math import inf
from uuid import NAMESPACE_URL, uuid5

from cockpit.battery_layer import build_battery_flexibility
from cockpit.battery_path_layer import build_standard_path_comparison
from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.liquidity import executable_price, hedge_side
from cockpit.market_layer import build_market_snapshot
from cockpit.models import (
    BatteryPathSimulation,
    CanonicalDataPoint,
    CockpitSnapshot,
    CoordinatorAction,
    CoordinatorCandidate,
    CoordinatorCostBreakdown,
    CoordinatorPeriodResult,
    CoordinatorReadiness,
    CoordinatorRecommendation,
    CoordinatorScenarioResidual,
    CoordinatorSensitivity,
    CoordinatorSimulationInput,
    CoordinatorSnapshot,
    DataLineage,
    MarketPeriodSnapshot,
    OptionalityPathImpact,
    Quality,
    SemanticKind,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.optionality_layer import build_optionality_snapshot
from cockpit.position_layer import build_forecast_position, direction


SCENARIO_WEIGHTS = {"P10": 0.25, "P50": 0.50, "P90": 0.25}
ACTION_NAMES = {
    CoordinatorAction.NO_ACTION: "No action",
    CoordinatorAction.MARKET_ONLY: "Market-only hedge",
    CoordinatorAction.BATTERY_ONLY_P50: "Battery-only P50 coverage",
    CoordinatorAction.BATTERY_PRESERVE_FLEXIBILITY: "Battery preserve-flexibility path",
    CoordinatorAction.MARKET_BATTERY_HYBRID: "Market + battery hybrid",
    CoordinatorAction.OPTIONALITY_PRESERVING: "Optionality-preserving action",
}
PATH_ALIASES = {
    "NO_ACTION": "NO_ACTION",
    "P50": "P50_COVERAGE",
    "P50_COVERAGE": "P50_COVERAGE",
    "PRESERVE": "PRESERVE_FLEXIBILITY",
    "PRESERVE_FLEXIBILITY": "PRESERVE_FLEXIBILITY",
}


@dataclass
class CoordinatorLayerResult:
    snapshot: CoordinatorSnapshot
    derived_values: list[CanonicalDataPoint]


def build_coordinator_snapshot(
    snapshot: CockpitSnapshot,
    settings: CoordinatorSimulationInput | None = None,
    *,
    live_provider_status: SourceMode = SourceMode.ERROR,
    active_provider_quality: Quality | None = None,
    active_provider_mode: SourceMode | None = None,
) -> CoordinatorLayerResult:
    """Build all six diagnostic candidates from the immutable cockpit snapshot."""
    settings = settings or CoordinatorSimulationInput()
    confidence = settings.confidence_scenario.upper()
    if confidence not in SCENARIO_WEIGHTS:
        confidence = "P50"
    selected_path = PATH_ALIASES.get(settings.selected_battery_path.upper(), "PRESERVE_FLEXIBILITY")

    positions_result = build_forecast_position(snapshot)
    market_result = build_market_snapshot(
        snapshot,
        live_provider_status=live_provider_status,
        active_provider_quality=active_provider_quality,
        active_provider_mode=active_provider_mode,
    )
    battery_result = build_battery_flexibility(snapshot)
    path_result = build_standard_path_comparison(snapshot)
    optionality_result = build_optionality_snapshot(snapshot)
    derived = [
        *positions_result.derived_values,
        *market_result.derived_values,
        *battery_result.derived_values,
        *path_result.derived_values,
        *optionality_result.derived_values,
    ]
    assumptions = _assumption_points(snapshot, settings, confidence, selected_path)
    derived.extend(assumptions)
    layer_inputs = [
        *assumptions,
        *(period.best_bid for period in market_result.snapshot.periods),
        *(period.best_ask for period in market_result.snapshot.periods),
        *(exposure.exposure_value for period in positions_result.snapshot.periods for exposure in period.exposures),
        *(period.ending_soc_value for period in path_result.comparison.no_action.periods),
        *(
            impact.optionality_lost_value
            for impact in optionality_result.snapshot.path_impacts
            if impact.optionality_lost_value
        ),
    ]
    readiness = _readiness(
        positions_result.snapshot.readiness,
        market_result.snapshot,
        path_result.comparison.readiness,
        optionality_result.snapshot.readiness,
        settings,
        assumptions,
    )

    if not readiness.calculation_allowed:
        digest = _snapshot_digest(snapshot, settings)
        result = CoordinatorSnapshot(
            coordinator_snapshot_id=f"coordinator-{digest[:16]}",
            cockpit_snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            source_mode=combined_source_mode(layer_inputs),
            quality=combined_quality(layer_inputs),
            readiness=readiness,
            assumptions=assumptions,
            warnings=[
                "Diagnostic recommendation is blocked because critical coordinator inputs are unavailable.",
                "No order or battery control action can be produced by this endpoint.",
            ],
        )
        return CoordinatorLayerResult(snapshot=result, derived_values=_dedupe(derived))

    paths = {
        "NO_ACTION": path_result.comparison.no_action,
        "P50_COVERAGE": path_result.comparison.p50_coverage,
        "PRESERVE_FLEXIBILITY": path_result.comparison.preserve_flexibility,
    }
    impacts = {item.path_name: item for item in optionality_result.snapshot.path_impacts}
    market_by_period = {item.delivery_period: item for item in market_result.snapshot.periods}
    specs = (
        (CoordinatorAction.NO_ACTION, "NO_ACTION", 0.0),
        (CoordinatorAction.MARKET_ONLY, "NO_ACTION", 1.0),
        (CoordinatorAction.BATTERY_ONLY_P50, "P50_COVERAGE", 0.0),
        (CoordinatorAction.BATTERY_PRESERVE_FLEXIBILITY, "PRESERVE_FLEXIBILITY", 0.0),
        (CoordinatorAction.MARKET_BATTERY_HYBRID, selected_path, 1.0),
        (CoordinatorAction.OPTIONALITY_PRESERVING, "PRESERVE_FLEXIBILITY", 0.5),
    )
    candidates: list[CoordinatorCandidate] = []
    for action, path_name, market_fraction in specs:
        candidate, values = _candidate(
            snapshot,
            action,
            paths[path_name],
            impacts[path_name],
            market_by_period,
            battery_result.snapshot.opportunity_cost,
            assumptions,
            settings,
            confidence,
            market_fraction,
            readiness,
        )
        candidates.append(candidate)
        derived.extend(values)

    ranked = sorted(candidates, key=lambda item: item.cost.total_diagnostic_cost_gbp)
    for rank, candidate in enumerate(ranked, start=1):
        candidate.rank = rank
    selected = ranked[0]
    explanation = _recommendation_explanation(selected, market_result.snapshot, readiness, confidence)
    recommendation = CoordinatorRecommendation(
        selected_candidate_id=selected.candidate_id,
        selected_action=selected.action,
        selected_action_name=selected.action_name,
        diagnostic_score_gbp=selected.cost.total_diagnostic_cost_gbp,
        diagnostic_score_value=selected.cost.total_diagnostic_cost_value,
        trustworthy_for_live_trading=readiness.trustworthy_for_live_trading,
        explanation=explanation,
        what_would_change=_what_would_change(settings),
        warnings=[
            "Diagnostic recommendation · Not executable.",
            "Not trustworthy for live trading unless all required inputs are LIVE/FRESH/VALID.",
        ],
    )
    sensitivities = _sensitivities(candidates, selected, settings)
    digest = _snapshot_digest(snapshot, settings)
    warnings = list(dict.fromkeys([
        *market_result.snapshot.warnings,
        *optionality_result.snapshot.warnings,
        "Diagnostic recommendation only; no orders are submitted and no battery is controlled.",
        "Market sells are positive cashflow and therefore negative diagnostic cost; market buys are positive diagnostic cost.",
    ]))
    result = CoordinatorSnapshot(
        coordinator_snapshot_id=f"coordinator-{digest[:16]}",
        cockpit_snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        source_mode=combined_source_mode(layer_inputs),
        quality=combined_quality(layer_inputs),
        readiness=readiness,
        assumptions=assumptions,
        candidates=sorted(candidates, key=lambda item: item.rank),
        recommendation=recommendation,
        sensitivities=sensitivities,
        warnings=warnings,
    )
    return CoordinatorLayerResult(snapshot=result, derived_values=_dedupe(derived))


def _candidate(
    snapshot,
    action,
    path: BatteryPathSimulation,
    impact: OptionalityPathImpact,
    markets: dict[str, MarketPeriodSnapshot],
    opportunity_cost,
    assumptions,
    settings,
    confidence,
    market_fraction,
    readiness,
):
    assumption_map = {point.metric: point for point in assumptions}
    impact_by_period = {period.delivery_period: period for period in impact.periods}
    periods: list[CoordinatorPeriodResult] = []
    derived: list[CanonicalDataPoint] = []
    for battery_period in path.periods:
        market = markets[battery_period.delivery_period]
        optionality = impact_by_period[battery_period.delivery_period]
        before = {item.scenario: item for item in battery_period.exposure_before}
        after_battery = {item.scenario: item for item in battery_period.residual_exposure}
        target = after_battery[confidence].residual_position_mwh
        side = hedge_side(target) if market_fraction > 0 else "NONE"
        requested = abs(target) * market_fraction if side != "NONE" else 0.0
        if settings.maximum_market_hedge_volume_mwh is not None:
            requested = min(requested, settings.maximum_market_hedge_volume_mwh)
        execution = executable_price(
            [*market.bids, *market.asks], requested, side, market.levels_considered if hasattr(market, "levels_considered") else 3
        ) if side != "NONE" else None
        executed = execution.executable_volume_mwh if execution else 0.0
        unfilled = max(0.0, requested - executed) if side != "NONE" else 0.0
        signed_trade = executed if side == "SELL" else -executed if side == "BUY" else 0.0
        trade_inputs = [after_battery[confidence].exposure_value]
        relevant_levels = market.bids if side == "SELL" else market.asks if side == "BUY" else []
        trade_inputs.extend(value for level in relevant_levels[:3] for value in (level.price_value, level.volume_value))
        trade_value = _derived(
            snapshot, action, "coordinator_market_trade", battery_period.delivery_period,
            battery_period.delivery_start, executed, "MWh", trade_inputs,
            "executable absolute hedge volume after battery action and configured market cap",
        )
        unfilled_value = _derived(
            snapshot, action, "coordinator_market_unfilled", battery_period.delivery_period,
            battery_period.delivery_start, unfilled, "MWh", [after_battery[confidence].exposure_value, trade_value],
            "absolute selected-scenario exposure after battery - executable market volume",
        )
        wap_value = None
        wap = execution.wap_gbp_per_mwh if execution else None
        if wap is not None:
            wap_value = _derived(
                snapshot, action, "coordinator_market_wap", battery_period.delivery_period,
                battery_period.delivery_start, wap, "GBP/MWh", trade_inputs,
                "volume-weighted executable price from selected order-book levels",
            )
        battery_net_mwh = battery_period.discharge_mwh - battery_period.charge_mwh
        battery_action_value = _derived(
            snapshot, action, "coordinator_battery_net_export", battery_period.delivery_period,
            battery_period.delivery_start, battery_net_mwh, "MWh",
            [battery_period.charge_energy_value, battery_period.discharge_energy_value],
            "battery discharge MWh - battery charge MWh",
        )
        residuals: list[CoordinatorScenarioResidual] = []
        for scenario, exposure in before.items():
            residual = exposure.residual_position_mwh + battery_net_mwh - signed_trade
            residual_value = _derived(
                snapshot, action, f"coordinator_residual_{scenario.lower()}",
                battery_period.delivery_period, battery_period.delivery_start, residual, "MWh",
                [exposure.exposure_value, battery_action_value, trade_value],
                "pre-action exposure + battery net export - signed market trade",
            )
            residuals.append(CoordinatorScenarioResidual(
                scenario=scenario,
                exposure_before_mwh=exposure.residual_position_mwh,
                battery_net_export_mwh=battery_net_mwh,
                signed_market_trade_mwh=signed_trade,
                residual_exposure_mwh=residual,
                direction=direction(residual),
                residual_value=residual_value,
            ))
            derived.append(residual_value)
        residual_map = {item.scenario: item for item in residuals}
        market_cost = -(signed_trade * (wap or 0.0))
        expected_imbalance = sum(
            SCENARIO_WEIGHTS[item.scenario] * abs(item.residual_exposure_mwh)
            for item in residuals
        ) * settings.imbalance_price_gbp_per_mwh
        tail = settings.tail_risk_weight * max(
            abs(residual_map["P10"].residual_exposure_mwh),
            abs(residual_map["P90"].residual_exposure_mwh),
        ) * settings.imbalance_price_gbp_per_mwh
        battery_cost = (
            battery_period.charge_mwh * opportunity_cost.charge_cost_gbp_per_mwh
            + battery_period.discharge_mwh * opportunity_cost.discharge_cost_gbp_per_mwh
        )
        weighted_optionality = optionality.optionality_lost_gbp * settings.optionality_loss_weight
        service_risk = optionality.service_estimate.non_delivery_risk_penalty_gbp
        component_values = _cost_values(
            snapshot, action, battery_period, residuals, trade_value, wap_value,
            optionality, opportunity_cost, assumption_map,
            market_cost, expected_imbalance, tail, battery_cost, weighted_optionality, service_risk,
        )
        derived.extend([trade_value, unfilled_value, battery_action_value, *component_values])
        if wap_value:
            derived.append(wap_value)
        cost = CoordinatorCostBreakdown(
            market_execution_cost_gbp=market_cost,
            expected_imbalance_cost_gbp=expected_imbalance,
            tail_risk_penalty_gbp=tail,
            battery_opportunity_cost_gbp=battery_cost,
            optionality_lost_gbp=weighted_optionality,
            service_risk_penalty_gbp=service_risk,
            total_diagnostic_cost_gbp=sum((market_cost, expected_imbalance, tail, battery_cost, weighted_optionality, service_risk)),
            market_execution_cost_value=component_values[0],
            expected_imbalance_cost_value=component_values[1],
            tail_risk_penalty_value=component_values[2],
            battery_opportunity_cost_value=component_values[3],
            optionality_lost_value=component_values[4],
            service_risk_penalty_value=component_values[5],
            total_diagnostic_cost_value=component_values[6],
        )
        bindings = list(dict.fromkeys([
            *battery_period.binding_constraints,
            *(violation.code for violation in battery_period.violations),
            *(violation.code for violation in optionality.violations),
            *( ["MARKET_DEPTH"] if unfilled > 0.001 and market_fraction > 0 else []),
        ]))
        periods.append(CoordinatorPeriodResult(
            settlement_period=battery_period.settlement_period,
            delivery_period=battery_period.delivery_period,
            delivery_start=battery_period.delivery_start,
            delivery_end=battery_period.delivery_end,
            exposure_before=list(before.values()),
            market_hedge_side=side,
            signed_market_trade_mwh=signed_trade,
            market_trade_volume_mwh=executed,
            market_trade_value=trade_value,
            market_wap_gbp_per_mwh=wap,
            market_wap_value=wap_value,
            market_unfilled_mwh=unfilled,
            market_unfilled_value=unfilled_value,
            battery_charge_mwh=battery_period.charge_mwh,
            battery_discharge_mwh=battery_period.discharge_mwh,
            battery_net_export_mwh=battery_net_mwh,
            battery_action_value=battery_action_value,
            soc_before_mwh=battery_period.starting_soc_mwh,
            soc_after_mwh=battery_period.ending_soc_mwh,
            soc_before_value=battery_period.starting_soc_value,
            soc_after_value=battery_period.ending_soc_value,
            residuals=residuals,
            optionality_lost_gbp=optionality.optionality_lost_gbp,
            optionality_lost_value=optionality.optionality_lost_value,
            service_commitment_at_risk=optionality.commitment_at_risk,
            service_coverage_ratio=optionality.commitment_coverage_ratio,
            service_risk_value=optionality.service_estimate.non_delivery_risk_penalty_value,
            binding_constraints=bindings,
            cost=cost,
            warnings=list(dict.fromkeys([*market.warnings, *optionality.warnings])),
        ))

    candidate_cost, summary_values = _summarise_cost(snapshot, action, periods)
    derived.extend(summary_values)
    trades = [period for period in periods if period.market_trade_volume_mwh > 0]
    sides = {period.market_hedge_side for period in trades}
    total_volume = sum(period.market_trade_volume_mwh for period in periods)
    aggregate_wap = (
        sum(period.market_trade_volume_mwh * (period.market_wap_gbp_per_mwh or 0) for period in trades) / total_volume
        if total_volume else None
    )
    summary_metrics = {
        "market_trade_volume_value": _derived(snapshot, action, "coordinator_horizon_market_volume", None, None, total_volume, "MWh", [period.market_trade_value for period in periods], "sum executable market volume across horizon"),
        "market_unfilled_value": _derived(snapshot, action, "coordinator_horizon_market_unfilled", None, None, sum(period.market_unfilled_mwh for period in periods if market_fraction > 0), "MWh", [period.market_unfilled_value for period in periods], "sum requested but unfilled market hedge volume across horizon"),
        "battery_charge_value": _derived(snapshot, action, "coordinator_horizon_battery_charge", None, None, sum(period.battery_charge_mwh for period in periods), "MWh", [period.battery_action_value for period in periods], "sum battery charging energy across horizon"),
        "battery_discharge_value": _derived(snapshot, action, "coordinator_horizon_battery_discharge", None, None, sum(period.battery_discharge_mwh for period in periods), "MWh", [period.battery_action_value for period in periods], "sum battery discharging energy across horizon"),
        "residual_p10_value": _derived(snapshot, action, "coordinator_horizon_residual_p10", None, None, sum(_residual(period, "P10") for period in periods), "MWh", [next(item.residual_value for item in period.residuals if item.scenario == "P10") for period in periods], "sum signed P10 residual exposure across horizon"),
        "residual_p50_value": _derived(snapshot, action, "coordinator_horizon_residual_p50", None, None, sum(_residual(period, "P50") for period in periods), "MWh", [next(item.residual_value for item in period.residuals if item.scenario == "P50") for period in periods], "sum signed P50 residual exposure across horizon"),
        "residual_p90_value": _derived(snapshot, action, "coordinator_horizon_residual_p90", None, None, sum(_residual(period, "P90") for period in periods), "MWh", [next(item.residual_value for item in period.residuals if item.scenario == "P90") for period in periods], "sum signed P90 residual exposure across horizon"),
        "optionality_lost_value": _derived(snapshot, action, "coordinator_horizon_raw_optionality_lost", None, None, sum(period.optionality_lost_gbp for period in periods), "GBP", [period.optionality_lost_value for period in periods], "sum unweighted optionality loss across horizon"),
    }
    summary_wap_value = None
    if aggregate_wap is not None:
        summary_wap_value = _derived(snapshot, action, "coordinator_horizon_market_wap", None, None, aggregate_wap, "GBP/MWh", [point for period in trades for point in (period.market_trade_value, period.market_wap_value) if point], "market-volume-weighted WAP across horizon")
    derived.extend([*summary_metrics.values(), *( [summary_wap_value] if summary_wap_value else [])])
    warning_badges = [readiness.status.value, "DIAGNOSTIC", "NOT EXECUTABLE"]
    if not readiness.trustworthy_for_live_trading:
        warning_badges.append("NOT LIVE-TRUSTWORTHY")
    if any(period.service_commitment_at_risk for period in periods):
        warning_badges.append("SERVICE RISK")
    candidate_id = str(uuid5(NAMESPACE_URL, f"{snapshot.snapshot_id}:{action}:{path.simulation_id}:{total_volume:.6f}"))
    candidate = CoordinatorCandidate(
        candidate_id=candidate_id,
        action=action,
        action_name=ACTION_NAMES[action],
        market_trade_volume_mwh=total_volume,
        market_trade_volume_value=summary_metrics["market_trade_volume_value"],
        market_hedge_side=next(iter(sides)) if len(sides) == 1 else "MIXED" if sides else "NONE",
        market_wap_gbp_per_mwh=aggregate_wap,
        market_wap_value=summary_wap_value,
        market_unfilled_mwh=sum(period.market_unfilled_mwh for period in periods if market_fraction > 0),
        market_unfilled_value=summary_metrics["market_unfilled_value"],
        battery_path=path.path_name,
        battery_charge_mwh=sum(period.battery_charge_mwh for period in periods),
        battery_charge_value=summary_metrics["battery_charge_value"],
        battery_discharge_mwh=sum(period.battery_discharge_mwh for period in periods),
        battery_discharge_value=summary_metrics["battery_discharge_value"],
        residual_p10_mwh=sum(_residual(period, "P10") for period in periods),
        residual_p10_value=summary_metrics["residual_p10_value"],
        residual_p50_mwh=sum(_residual(period, "P50") for period in periods),
        residual_p50_value=summary_metrics["residual_p50_value"],
        residual_p90_mwh=sum(_residual(period, "P90") for period in periods),
        residual_p90_value=summary_metrics["residual_p90_value"],
        optionality_lost_gbp=sum(period.optionality_lost_gbp for period in periods),
        optionality_lost_value=summary_metrics["optionality_lost_value"],
        service_commitments_at_risk=sum(period.service_commitment_at_risk for period in periods),
        cost=candidate_cost,
        readiness=readiness,
        periods=periods,
        explanation=_candidate_explanation(action, path, total_volume, candidate_cost, readiness),
        warning_badges=warning_badges,
    )
    return candidate, derived


def _cost_values(snapshot, action, period, residuals, trade, wap, optionality, opportunity, assumptions, market_cost, imbalance, tail, battery, optionality_cost, service):
    residual_inputs = [item.residual_value for item in residuals]
    market_inputs = [trade, *( [wap] if wap else [])]
    values = [
        _derived(snapshot, action, "coordinator_market_execution_cost", period.delivery_period, period.delivery_start, market_cost, "GBP", market_inputs, "negative signed market cashflow; sells reduce cost and buys increase cost"),
        _derived(snapshot, action, "coordinator_expected_imbalance_cost", period.delivery_period, period.delivery_start, imbalance, "GBP", [*residual_inputs, assumptions["coordinator_imbalance_price"]], "scenario-weighted absolute residual MWh x imbalance price assumption"),
        _derived(snapshot, action, "coordinator_tail_risk_penalty", period.delivery_period, period.delivery_start, tail, "GBP", [residuals[0].residual_value, residuals[2].residual_value, assumptions["coordinator_tail_risk_weight"], assumptions["coordinator_imbalance_price"]], "tail weight x max absolute P10/P90 residual x imbalance price"),
        _derived(snapshot, action, "coordinator_battery_opportunity_cost", period.delivery_period, period.delivery_start, battery, "GBP", [period.charge_energy_value, period.discharge_energy_value, opportunity.charge_cost_value, opportunity.discharge_cost_value], "charge and discharge energy x directional battery opportunity cost"),
        _derived(snapshot, action, "coordinator_weighted_optionality_loss", period.delivery_period, period.delivery_start, optionality_cost, "GBP", [optionality.optionality_lost_value, assumptions["coordinator_optionality_loss_weight"]], "path optionality lost x coordinator optionality-loss weight"),
        _derived(snapshot, action, "coordinator_service_risk_penalty", period.delivery_period, period.delivery_start, service, "GBP", [optionality.service_estimate.non_delivery_risk_penalty_value], "committed-service non-delivery risk penalty from optionality diagnostics"),
    ]
    values.append(_derived(snapshot, action, "coordinator_total_diagnostic_cost", period.delivery_period, period.delivery_start, sum((market_cost, imbalance, tail, battery, optionality_cost, service)), "GBP", values, "market execution cost + expected imbalance cost + tail risk + battery opportunity cost + weighted optionality loss + service risk"))
    return values


def _summarise_cost(snapshot, action, periods):
    attrs = (
        ("market_execution_cost_gbp", "market_execution_cost_value"),
        ("expected_imbalance_cost_gbp", "expected_imbalance_cost_value"),
        ("tail_risk_penalty_gbp", "tail_risk_penalty_value"),
        ("battery_opportunity_cost_gbp", "battery_opportunity_cost_value"),
        ("optionality_lost_gbp", "optionality_lost_value"),
        ("service_risk_penalty_gbp", "service_risk_penalty_value"),
    )
    totals = {name: sum(getattr(period.cost, name) for period in periods) for name, _ in attrs}
    values = [
        _derived(snapshot, action, f"coordinator_horizon_{name}", None, None, totals[name], "GBP", [getattr(period.cost, value_name) for period in periods], "sum period-level coordinator cost component across horizon")
        for name, value_name in attrs
    ]
    total = sum(totals.values())
    total_value = _derived(snapshot, action, "coordinator_horizon_total_diagnostic_cost", None, None, total, "GBP", values, "sum horizon coordinator cost components; lower is better")
    values.append(total_value)
    return CoordinatorCostBreakdown(
        **totals,
        total_diagnostic_cost_gbp=total,
        market_execution_cost_value=values[0],
        expected_imbalance_cost_value=values[1],
        tail_risk_penalty_value=values[2],
        battery_opportunity_cost_value=values[3],
        optionality_lost_value=values[4],
        service_risk_penalty_value=values[5],
        total_diagnostic_cost_value=values[6],
    ), values


def _assumption_points(snapshot, settings, confidence, selected_path):
    definitions = (
        ("coordinator_imbalance_price", settings.imbalance_price_gbp_per_mwh, "GBP/MWh", "Explicit imbalance price assumption"),
        ("coordinator_tail_risk_weight", settings.tail_risk_weight, "ratio", "Explicit tail-risk multiplier"),
        ("coordinator_optionality_loss_weight", settings.optionality_loss_weight, "ratio", "Explicit optionality-loss multiplier"),
        ("coordinator_market_hedge_cap", settings.maximum_market_hedge_volume_mwh if settings.maximum_market_hedge_volume_mwh is not None else 1_000_000.0, "MWh", "Per-period maximum market hedge volume; large value means uncapped"),
        ("coordinator_confidence_scenario", confidence, "scenario", "Scenario targeted by market hedge candidates"),
        ("coordinator_selected_battery_path", selected_path, "path", "Battery path used by hybrid candidate"),
    )
    return [_assumption(snapshot, metric, value, unit, description, settings.assumption_source_mode) for metric, value, unit, description in definitions]


def _assumption(snapshot, metric, value, unit, description, mode):
    identifier = uuid5(NAMESPACE_URL, f"{snapshot.snapshot_id}:{metric}:{value}:{mode}")
    warnings = [] if mode == SourceMode.LIVE else [f"Coordinator assumption is {mode.value}; it is not a live calibrated input."]
    return CanonicalDataPoint(
        value_id=str(identifier), metric=metric, value=value, unit=unit,
        lineage=DataLineage(
            source_feed="coordinator_simulation_input",
            source_mode=mode,
            semantic_kind=SemanticKind.ASSUMPTION,
            quality=Quality.FRESH,
            published_at=snapshot.as_of,
            retrieved_at=snapshot.as_of,
            normalised_at=snapshot.as_of,
            raw_field_name=metric,
            transformations=[description],
            validation_checks=[ValidationCheck(name="explicit_assumption", passed=True, detail=description)],
            warnings=warnings,
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot.snapshot_id,
    )


def _readiness(position, market, battery_path, optionality, settings, assumptions):
    blockers: list[str] = []
    reasons: list[str] = []
    layers = {
        "Forecast & Position": position,
        "Market & Liquidity": market.readiness,
        "Sequential Battery Path": battery_path,
        "BM / ancillary Optionality": optionality,
    }
    for name, layer in layers.items():
        if not layer.calculation_allowed or layer.status == SnapshotStatus.BLOCKED:
            blockers.append(f"{name} is BLOCKED: {'; '.join(layer.reasons)}")
        elif layer.status == SnapshotStatus.DEGRADED:
            reasons.append(f"{name} is DEGRADED: {'; '.join(layer.reasons)}")
    executable_provider = market.active_provider in {
        "market_intraday", "market_order_book_sample", "rolling_sample_environment"
    }
    if not executable_provider or not market.periods:
        blockers.append("Executable bid/ask order-book data is unavailable; Elexon MID/reference data is not executable.")
    if market.source_mode == SourceMode.SAMPLE and not settings.explicit_sample_market:
        blockers.append("Sample market data exists but explicit sample mode was not selected.")
    if any(point.lineage.source_mode != SourceMode.LIVE for point in assumptions):
        reasons.append("Coordinator assumptions are not all LIVE calibrated inputs.")
    if settings.confidence_scenario.upper() not in SCENARIO_WEIGHTS:
        blockers.append("Confidence scenario must be P10, P50 or P90.")
    if settings.selected_battery_path.upper() not in PATH_ALIASES:
        blockers.append("Selected battery path is not a supported standard path.")
    if blockers:
        return CoordinatorReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=list(dict.fromkeys([*reasons, *blockers])),
            critical_blockers=list(dict.fromkeys(blockers)),
        )
    all_ready = all(layer.status == SnapshotStatus.READY for layer in layers.values())
    all_live_fresh = (
        market.source_mode == SourceMode.LIVE
        and market.quality == Quality.FRESH
        and all(point.lineage.source_mode == SourceMode.LIVE and point.lineage.quality == Quality.FRESH for point in assumptions)
    )
    trustworthy = all_ready and all_live_fresh
    if not trustworthy:
        reasons.append("Not trustworthy for live trading unless all required inputs are LIVE/FRESH/VALID.")
    return CoordinatorReadiness(
        status=SnapshotStatus.READY if trustworthy else SnapshotStatus.DEGRADED,
        calculation_allowed=True,
        trustworthy_for_live_trading=trustworthy,
        reasons=list(dict.fromkeys(reasons)),
    )


def _derived(snapshot, action, metric, delivery_period, delivery_start, value, unit, inputs, expression):
    inputs = [point for point in inputs if point is not None]
    identifier = uuid5(NAMESPACE_URL, f"{snapshot.snapshot_id}:{action}:{metric}:{delivery_period}:{float(value):.8f}:{','.join(point.value_id for point in inputs)}")
    mode = combined_source_mode(inputs)
    quality = combined_quality(inputs)
    warnings = list(dict.fromkeys([warning for point in inputs for warning in point.lineage.warnings]))
    if mode != SourceMode.LIVE:
        warnings.append(f"Coordinator value is derived from {mode.value} inputs; not live-trading trustworthy.")
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    return CanonicalDataPoint(
        value_id=str(identifier), metric=metric, value=round(float(value), 6), unit=unit,
        delivery_period=delivery_period, delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="integrated_coordinator",
            source_mode=mode,
            semantic_kind=SemanticKind.ESTIMATE,
            quality=quality,
            published_at=max(published) if published else snapshot.as_of,
            retrieved_at=max((point.lineage.retrieved_at for point in inputs), default=snapshot.as_of),
            normalised_at=snapshot.as_of,
            raw_field_name=expression,
            transformations=[expression],
            validation_checks=[
                ValidationCheck(name="finite_result", passed=float(value) == float(value) and abs(float(value)) != inf, detail="value is finite"),
                ValidationCheck(name="traceable_inputs", passed=bool(inputs), detail=f"derived from {len(inputs)} canonical values"),
                ValidationCheck(name="diagnostic_not_executable", passed=True, detail="coordinator output cannot submit orders or control a battery"),
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot.snapshot_id,
    )


def _sensitivities(candidates, selected, settings):
    baseline = selected.cost.total_diagnostic_cost_gbp
    cases = (
        ("ask-price", "Ask price rises", "Ask price +£20/MWh", lambda c: c.cost.total_diagnostic_cost_gbp + (20 * c.market_trade_volume_mwh if c.market_hedge_side in {"BUY", "MIXED"} else 0)),
        ("bid-depth", "Bid depth falls", "Executable bid depth -50%", lambda c: c.cost.total_diagnostic_cost_gbp + (0.5 * c.market_trade_volume_mwh * settings.imbalance_price_gbp_per_mwh if c.market_hedge_side in {"SELL", "MIXED"} else 0)),
        ("lower-soc", "Battery SoC is lower", "Usable discharge energy -25%", lambda c: c.cost.total_diagnostic_cost_gbp + 0.25 * c.battery_discharge_mwh * settings.imbalance_price_gbp_per_mwh),
        ("optionality-double", "Optionality value doubles", "Optionality-loss weight x2", lambda c: c.cost.total_diagnostic_cost_gbp + c.optionality_lost_gbp * settings.optionality_loss_weight),
        ("p10-weight", "P10 receives more weight", "Add 25% P10 tail emphasis", lambda c: c.cost.total_diagnostic_cost_gbp + abs(c.residual_p10_mwh) * settings.imbalance_price_gbp_per_mwh * 0.25),
        ("market-missing", "Market feed is missing", "Market candidates unavailable", lambda c: inf if c.market_trade_volume_mwh > 0 else c.cost.total_diagnostic_cost_gbp),
    )
    output = []
    for key, label, change, scorer in cases:
        scored = [(candidate, scorer(candidate)) for candidate in candidates]
        winner, cost = min(scored, key=lambda item: item[1])
        output.append(CoordinatorSensitivity(
            sensitivity_id=key,
            label=label,
            change=change,
            baseline_preferred_action=selected.action,
            counterfactual_preferred_action=winner.action,
            baseline_cost_gbp=baseline,
            counterfactual_cost_gbp=cost,
            changed_preference=winner.action != selected.action,
            explanation=(
                f"Under '{change}', {winner.action_name} has the lowest approximate diagnostic cost. "
                "This is a one-factor screening sensitivity, not a full re-optimisation."
            ),
        ))
    return output


def _candidate_explanation(action, path, volume, cost, readiness):
    return (
        f"{ACTION_NAMES[action]} combines {volume:.1f} MWh of executable diagnostic market volume "
        f"with the {path.path_label} battery path. Its score is £{cost.total_diagnostic_cost_gbp:,.0f}; "
        "lower scores are preferred. "
        + ("All required inputs pass live trust checks." if readiness.trustworthy_for_live_trading else "Inputs do not pass live-trading trust checks.")
    )


def _recommendation_explanation(selected, market, readiness, confidence):
    trust = (
        "Required inputs are LIVE/FRESH/VALID."
        if readiness.trustworthy_for_live_trading
        else f"This is not live-trading trustworthy because the coordinator is {readiness.status.value} and the market source is {market.source_mode.value}."
    )
    return (
        f"The coordinator prefers the {selected.action_name} diagnostic path under the {confidence} targeting assumption. "
        f"It leaves horizon residuals of {selected.residual_p10_mwh:.1f} MWh (P10), "
        f"{selected.residual_p50_mwh:.1f} MWh (P50) and {selected.residual_p90_mwh:.1f} MWh (P90), "
        f"with £{selected.optionality_lost_gbp:,.0f} of diagnostic optionality loss and "
        f"{selected.service_commitments_at_risk} period(s) of service risk. {trust} "
        "Diagnostic recommendation · Not executable."
    )


def _what_would_change(settings):
    return [
        "A materially different executable bid/ask WAP or displayed market depth.",
        "A lower confirmed SoC or a tighter charge/discharge/service reserve envelope.",
        f"A different imbalance price than £{settings.imbalance_price_gbp_per_mwh:.0f}/MWh or greater P10/tail emphasis.",
        "Higher optionality value, a new service commitment, or a larger non-delivery penalty.",
        "A missing, stale, invalid, or newly LIVE/FRESH input feed.",
    ]


def _residual(period, scenario):
    return next(item.residual_exposure_mwh for item in period.residuals if item.scenario == scenario)


def _snapshot_digest(snapshot, settings):
    return hashlib.sha256(f"{snapshot.input_hash}:coordinator-v1:{settings.model_dump(mode='json')}".encode()).hexdigest()


def _dedupe(points):
    return list({point.value_id: point for point in points}.values())
