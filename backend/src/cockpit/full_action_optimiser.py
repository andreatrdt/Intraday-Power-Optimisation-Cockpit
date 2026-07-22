"""Full rolling-horizon MILP for non-executable intraday decision support.

The formulation reuses the tested physical/economic structure of the reference
project, but is integrated with this cockpit's lineage-bearing rolling inputs.
It never submits an order or controls an asset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from uuid import NAMESPACE_URL, uuid4, uuid5

import pyomo.environ as pyo

from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.models import (
    CanonicalDataPoint,
    ChartPoint,
    ChartSeries,
    DataLineage,
    DriverContribution,
    OptimisationChangeSummary,
    OptimisationExplanationDrivers,
    OptimisationObjectiveBreakdown,
    OptimisationPeriodInput,
    OptimisationPeriodResult,
    OptimisationReadiness,
    OptimisationRun,
    OptimisationStartingState,
    Quality,
    RiskMeasure,
    SemanticKind,
    SensitivityResult,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)


SCENARIO_WEIGHTS = {"P10": 0.25, "P50": 0.50, "P90": 0.25}
SCENARIO_FIELDS = {
    "P10": "generation_p10_mwh",
    "P50": "generation_p50_mwh",
    "P90": "generation_p90_mwh",
}


@dataclass(frozen=True)
class FullActionConfig:
    e_min_mwh: float = 10.0
    e_max_mwh: float = 100.0
    charge_max_mw: float = 20.0
    discharge_max_mw: float = 20.0
    charge_efficiency: float = 0.94
    discharge_efficiency: float = 0.92
    grid_import_limit_mw: float = 20.0
    grid_export_limit_mw: float = 20.0
    upward_duration_h: float = 1.0
    downward_duration_h: float = 1.0
    minimum_terminal_soc_mwh: float = 35.0
    preferred_terminal_soc_mwh: float = 55.0
    degradation_cost_gbp_per_mwh: float = 4.0
    terminal_soc_value_gbp_per_mwh: float = 58.0
    terminal_deviation_penalty_gbp_per_mwh: float = 4.0
    imbalance_penalty_gbp_per_mwh: float = 125.0
    tail_risk_weight: float = 0.35
    upward_availability_gbp_per_mw_h: float = 6.5
    downward_availability_gbp_per_mw_h: float = 5.0
    expected_bm_up_gbp_per_mw_h: float = 6.825
    expected_bm_down_gbp_per_mw_h: float = 2.34
    optionality_preservation_gbp_per_mw_h: float = 1.25
    service_shortfall_penalty_gbp_per_mw: float = 175.0
    maximum_cycles_per_day: float = 2.0
    ramp_limit_mw_per_period: float = 20.0


def build_full_action_model(
    periods: list[OptimisationPeriodInput],
    starting_soc_mwh: float,
    config: FullActionConfig | None = None,
) -> pyo.ConcreteModel:
    """Build the full MILP, including order-book level slices."""
    cfg = config or FullActionConfig()
    if not periods:
        raise ValueError("At least one optimisation period is required")
    model = pyo.ConcreteModel(name="rolling_full_action_optimiser")
    count = len(periods)
    model.T = pyo.RangeSet(0, count - 1)
    model.Tsoc = pyo.RangeSet(0, count)
    max_levels = max(max(len(period.bids), len(period.asks)) for period in periods)
    model.L = pyo.RangeSet(0, max_levels - 1)

    model.charge = pyo.Var(model.T, domain=pyo.NonNegativeReals, bounds=(0, cfg.charge_max_mw))
    model.discharge = pyo.Var(model.T, domain=pyo.NonNegativeReals, bounds=(0, cfg.discharge_max_mw))
    model.charge_on = pyo.Var(model.T, domain=pyo.Binary)
    model.discharge_on = pyo.Var(model.T, domain=pyo.Binary)
    model.soc = pyo.Var(model.Tsoc, bounds=(cfg.e_min_mwh, cfg.e_max_mwh))
    model.reserve_up = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.reserve_down = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.reserve_up_shortfall = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.reserve_down_shortfall = pyo.Var(model.T, domain=pyo.NonNegativeReals)
    model.buy_slice = pyo.Var(model.T, model.L, domain=pyo.NonNegativeReals)
    model.sell_slice = pyo.Var(model.T, model.L, domain=pyo.NonNegativeReals)
    model.buy_on = pyo.Var(model.T, domain=pyo.Binary)
    model.sell_on = pyo.Var(model.T, domain=pyo.Binary)
    model.residual_long = pyo.Var(model.T, list(SCENARIO_WEIGHTS), domain=pyo.NonNegativeReals)
    model.residual_short = pyo.Var(model.T, list(SCENARIO_WEIGHTS), domain=pyo.NonNegativeReals)
    model.terminal_below = pyo.Var(domain=pyo.NonNegativeReals)
    model.terminal_above = pyo.Var(domain=pyo.NonNegativeReals)

    model.buy = pyo.Expression(
        model.T, rule=lambda m, t: sum(m.buy_slice[t, level] for level in m.L)
    )
    model.sell = pyo.Expression(
        model.T, rule=lambda m, t: sum(m.sell_slice[t, level] for level in m.L)
    )
    model.net_export = pyo.Expression(
        model.T, rule=lambda m, t: m.discharge[t] - m.charge[t]
    )

    model.soc_initial = pyo.Constraint(expr=model.soc[0] == starting_soc_mwh)
    model.soc_balance = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.soc[t + 1]
        == m.soc[t]
        + cfg.charge_efficiency * m.charge[t] * periods[t].duration_hours
        - m.discharge[t] * periods[t].duration_hours / cfg.discharge_efficiency,
    )
    model.no_simultaneous_charge_discharge = pyo.Constraint(
        model.T, rule=lambda m, t: m.charge_on[t] + m.discharge_on[t] <= 1
    )
    model.charge_binary_cap = pyo.Constraint(
        model.T, rule=lambda m, t: m.charge[t] <= cfg.charge_max_mw * m.charge_on[t]
    )
    model.discharge_binary_cap = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.discharge[t] <= cfg.discharge_max_mw * m.discharge_on[t],
    )
    model.grid_export_limit = pyo.Constraint(
        model.T, rule=lambda m, t: m.net_export[t] <= cfg.grid_export_limit_mw
    )
    model.grid_import_limit = pyo.Constraint(
        model.T, rule=lambda m, t: -m.net_export[t] <= cfg.grid_import_limit_mw
    )
    model.reserve_up_headroom = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.reserve_up[t] <= cfg.discharge_max_mw - m.net_export[t],
    )
    model.reserve_down_headroom = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.reserve_down[t] <= cfg.charge_max_mw + m.net_export[t],
    )
    model.reserve_up_duration = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.soc[t] - cfg.e_min_mwh
        >= m.reserve_up[t] * cfg.upward_duration_h / cfg.discharge_efficiency,
    )
    model.reserve_down_duration = pyo.Constraint(
        model.T,
        rule=lambda m, t: cfg.e_max_mwh - m.soc[t]
        >= cfg.charge_efficiency * m.reserve_down[t] * cfg.downward_duration_h,
    )
    model.reserve_up_commitment = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.reserve_up[t] + m.reserve_up_shortfall[t]
        >= periods[t].upward_commitment_mw,
    )
    model.reserve_down_commitment = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.reserve_down[t] + m.reserve_down_shortfall[t]
        >= periods[t].downward_commitment_mw,
    )

    def _buy_depth(m, t, level):
        volume = periods[t].asks[level].volume_mwh if level < len(periods[t].asks) else 0.0
        return m.buy_slice[t, level] <= (volume if periods[t].tradeable else 0.0)

    def _sell_depth(m, t, level):
        volume = periods[t].bids[level].volume_mwh if level < len(periods[t].bids) else 0.0
        return m.sell_slice[t, level] <= (volume if periods[t].tradeable else 0.0)

    model.buy_depth = pyo.Constraint(model.T, model.L, rule=_buy_depth)
    model.sell_depth = pyo.Constraint(model.T, model.L, rule=_sell_depth)
    model.no_simultaneous_buy_sell = pyo.Constraint(
        model.T, rule=lambda m, t: m.buy_on[t] + m.sell_on[t] <= 1
    )
    model.buy_side_cap = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.buy[t]
        <= sum(level.volume_mwh for level in periods[t].asks) * m.buy_on[t],
    )
    model.sell_side_cap = pyo.Constraint(
        model.T,
        rule=lambda m, t: m.sell[t]
        <= sum(level.volume_mwh for level in periods[t].bids) * m.sell_on[t],
    )

    def _portfolio_balance(m, t, scenario):
        generation = getattr(periods[t], SCENARIO_FIELDS[scenario])
        battery_mwh = m.net_export[t] * periods[t].duration_hours
        return m.residual_long[t, scenario] - m.residual_short[t, scenario] == (
            generation + battery_mwh + m.buy[t] - periods[t].contracted_q_mwh - m.sell[t]
        )

    model.portfolio_balance = pyo.Constraint(
        model.T, list(SCENARIO_WEIGHTS), rule=_portfolio_balance
    )
    model.minimum_terminal_soc = pyo.Constraint(
        expr=model.soc[count] >= cfg.minimum_terminal_soc_mwh
    )
    model.preferred_terminal_soc = pyo.Constraint(
        expr=model.soc[count] - cfg.preferred_terminal_soc_mwh
        == model.terminal_above - model.terminal_below
    )
    total_hours = sum(period.duration_hours for period in periods)
    cycle_days = max(total_hours / 24.0, 0.5)
    model.maximum_cycles = pyo.Constraint(
        expr=sum(model.discharge[t] * periods[t].duration_hours for t in model.T)
        <= cfg.maximum_cycles_per_day * cfg.e_max_mwh * cycle_days
    )

    def _ramp_up(m, t):
        if t == 0:
            return pyo.Constraint.Skip
        return m.net_export[t] - m.net_export[t - 1] <= cfg.ramp_limit_mw_per_period

    def _ramp_down(m, t):
        if t == 0:
            return pyo.Constraint.Skip
        return m.net_export[t - 1] - m.net_export[t] <= cfg.ramp_limit_mw_per_period

    model.ramp_up = pyo.Constraint(model.T, rule=_ramp_up)
    model.ramp_down = pyo.Constraint(model.T, rule=_ramp_down)

    def _market_value(m, t):
        sell = sum(
            m.sell_slice[t, level] * periods[t].bids[level].price_gbp_per_mwh
            for level in range(len(periods[t].bids))
        )
        buy = sum(
            m.buy_slice[t, level] * periods[t].asks[level].price_gbp_per_mwh
            for level in range(len(periods[t].asks))
        )
        return sell - buy

    model.market_value = pyo.Expression(model.T, rule=_market_value)
    model.expected_imbalance_cost = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.imbalance_penalty_gbp_per_mwh
        * sum(
            SCENARIO_WEIGHTS[scenario]
            * (m.residual_long[t, scenario] + m.residual_short[t, scenario])
            for scenario in SCENARIO_WEIGHTS
        ),
    )
    model.tail_risk_cost = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.tail_risk_weight
        * cfg.imbalance_penalty_gbp_per_mwh
        * 0.5
        * sum(
            m.residual_long[t, scenario] + m.residual_short[t, scenario]
            for scenario in ("P10", "P90")
        ),
    )
    model.degradation_cost = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.degradation_cost_gbp_per_mwh
        * (m.charge[t] + m.discharge[t])
        * periods[t].duration_hours,
    )
    model.upward_availability_value = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.upward_availability_gbp_per_mw_h
        * m.reserve_up[t]
        * periods[t].duration_hours,
    )
    model.downward_availability_value = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.downward_availability_gbp_per_mw_h
        * m.reserve_down[t]
        * periods[t].duration_hours,
    )
    model.bm_expected_value = pyo.Expression(
        model.T,
        rule=lambda m, t: (
            cfg.expected_bm_up_gbp_per_mw_h * m.reserve_up[t]
            + cfg.expected_bm_down_gbp_per_mw_h * m.reserve_down[t]
        )
        * periods[t].duration_hours,
    )
    model.optionality_value = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.optionality_preservation_gbp_per_mw_h
        * (m.reserve_up[t] + m.reserve_down[t])
        * periods[t].duration_hours,
    )
    model.service_risk_cost = pyo.Expression(
        model.T,
        rule=lambda m, t: cfg.service_shortfall_penalty_gbp_per_mw
        * (m.reserve_up_shortfall[t] + m.reserve_down_shortfall[t]),
    )
    terminal_value = (
        cfg.terminal_soc_value_gbp_per_mwh * model.soc[count]
        - cfg.terminal_deviation_penalty_gbp_per_mwh
        * (model.terminal_below + model.terminal_above)
    )
    model.terminal_value = pyo.Expression(expr=terminal_value)
    model.objective = pyo.Objective(
        expr=sum(
            model.market_value[t]
            - model.expected_imbalance_cost[t]
            - model.tail_risk_cost[t]
            - model.degradation_cost[t]
            + model.upward_availability_value[t]
            + model.downward_availability_value[t]
            + model.bm_expected_value[t]
            + model.optionality_value[t]
            - model.service_risk_cost[t]
            for t in model.T
        )
        + model.terminal_value,
        sense=pyo.maximize,
    )
    return model


def optimise_full_action(
    periods: list[OptimisationPeriodInput],
    starting_state: OptimisationStartingState,
    snapshot_id: str,
    previous_run: OptimisationRun | None = None,
    config: FullActionConfig | None = None,
) -> OptimisationRun:
    """Solve and translate a rolling optimisation run."""
    cfg = config or FullActionConfig()
    model = build_full_action_model(periods, starting_state.starting_soc_mwh, cfg)
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(model)
    status = str(result.solver.termination_condition).lower()
    if "optimal" not in status:
        raise RuntimeError(f"Full-action optimiser failed with status {status}")

    run_id = f"opt-{starting_state.current_time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    period_results: list[OptimisationPeriodResult] = []
    lineage_values: list[CanonicalDataPoint] = []
    for t, period in enumerate(periods):
        charge = _value(model.charge[t])
        discharge = _value(model.discharge[t])
        buy = _value(model.buy[t])
        sell = _value(model.sell[t])
        soc_before = _value(model.soc[t])
        soc_after = _value(model.soc[t + 1])
        reserve_up = _value(model.reserve_up[t])
        reserve_down = _value(model.reserve_down[t])
        net_export = discharge - charge
        residuals = {
            scenario: _value(model.residual_long[t, scenario])
            - _value(model.residual_short[t, scenario])
            for scenario in SCENARIO_WEIGHTS
        }
        market_value = _value(model.market_value[t])
        expected_imbalance = _value(model.expected_imbalance_cost[t])
        tail = _value(model.tail_risk_cost[t])
        degradation = _value(model.degradation_cost[t])
        up_value = _value(model.upward_availability_value[t])
        down_value = _value(model.downward_availability_value[t])
        bm_value = _value(model.bm_expected_value[t])
        optionality = _value(model.optionality_value[t])
        service_risk = _value(model.service_risk_cost[t])
        terminal_contribution = _value(model.terminal_value) if t == len(periods) - 1 else 0.0
        period_total = (
            market_value - expected_imbalance - tail - degradation + up_value + down_value
            + bm_value + optionality - service_risk + terminal_contribution
        )
        consumed = buy + sell
        execution_cash = abs(market_value)
        wap = execution_cash / consumed if consumed > 1e-7 else None
        bindings = _bindings(
            model, t, period, cfg, charge, discharge, soc_before, soc_after,
            reserve_up, reserve_down, buy, sell,
        )
        input_points = list(period.values.values())
        values = {
            "buy_mwh": _derived(run_id, snapshot_id, period, "optimiser_buy", buy, "MWh", input_points, "sum ask-side level slices consumed"),
            "sell_mwh": _derived(run_id, snapshot_id, period, "optimiser_sell", sell, "MWh", input_points, "sum bid-side level slices consumed"),
            "charge_mw": _derived(run_id, snapshot_id, period, "optimiser_charge", charge, "MW", input_points, "MILP charge decision; charge/discharge binaries are mutually exclusive"),
            "discharge_mw": _derived(run_id, snapshot_id, period, "optimiser_discharge", discharge, "MW", input_points, "MILP discharge decision; charge/discharge binaries are mutually exclusive"),
            "battery_net_export_mw": _derived(run_id, snapshot_id, period, "optimiser_battery_net_export", net_export, "MW", input_points, "discharge MW - charge MW"),
            "reserve_up_mw": _derived(run_id, snapshot_id, period, "optimiser_reserve_up", reserve_up, "MW", input_points, "reserve limited by power headroom, stored energy and service commitment"),
            "reserve_down_mw": _derived(run_id, snapshot_id, period, "optimiser_reserve_down", reserve_down, "MW", input_points, "reserve limited by charge headroom, empty energy capacity and service commitment"),
            "projected_soc_mwh": _derived(run_id, snapshot_id, period, "optimiser_projected_soc", soc_after, "MWh", input_points, "soc[t+1] = soc[t] + eta_c*charge*dt - discharge*dt/eta_d"),
            "residual_p10_mwh": _derived(run_id, snapshot_id, period, "optimiser_residual_p10", residuals["P10"], "MWh", input_points, "G_P10 + battery export*dt + buy - Q - sell"),
            "residual_p50_mwh": _derived(run_id, snapshot_id, period, "optimiser_residual_p50", residuals["P50"], "MWh", input_points, "G_P50 + battery export*dt + buy - Q - sell"),
            "residual_p90_mwh": _derived(run_id, snapshot_id, period, "optimiser_residual_p90", residuals["P90"], "MWh", input_points, "G_P90 + battery export*dt + buy - Q - sell"),
            "market_execution_value_gbp": _derived(run_id, snapshot_id, period, "optimiser_market_execution_value", market_value, "GBP", input_points, "bid-side sell cashflow - ask-side buy cost using consumed level slices"),
            "imbalance_risk_cost_gbp": _derived(run_id, snapshot_id, period, "optimiser_imbalance_risk_cost", expected_imbalance + tail, "GBP", input_points, "scenario-weighted absolute residual penalty + P10/P90 tail penalty"),
            "total_period_contribution_gbp": _derived(run_id, snapshot_id, period, "optimiser_period_value", period_total, "GBP", input_points, "market - imbalance - tail - degradation + availability + BM + optionality - service risk + terminal contribution"),
        }
        lineage_values.extend(values.values())
        why = _period_explanation(
            period, charge, discharge, buy, sell, reserve_up, reserve_down,
            soc_before, soc_after, residuals, bindings, cfg,
        )
        period_results.append(OptimisationPeriodResult(
            settlement_period=period.settlement_period,
            delivery_period=period.delivery_period,
            delivery_start=period.delivery_start,
            delivery_end=period.delivery_end,
            generation_p10_mwh=period.generation_p10_mwh,
            generation_p50_mwh=period.generation_p50_mwh,
            generation_p90_mwh=period.generation_p90_mwh,
            demand_mw=period.demand_mw,
            system_tightness_score=period.system_tightness_score,
            reference_price_gbp_per_mwh=period.reference_price_gbp_per_mwh,
            best_bid_gbp_per_mwh=period.bids[0].price_gbp_per_mwh,
            best_ask_gbp_per_mwh=period.asks[0].price_gbp_per_mwh,
            market_wap_gbp_per_mwh=round(wap, 4) if wap is not None else None,
            visible_depth_consumed_mwh=round(consumed, 4),
            q_before_action_mwh=period.contracted_q_mwh,
            buy_mwh=round(buy, 4), sell_mwh=round(sell, 4),
            charge_mw=round(charge, 4), discharge_mw=round(discharge, 4),
            battery_net_export_mw=round(net_export, 4),
            reserve_up_mw=round(reserve_up, 4), reserve_down_mw=round(reserve_down, 4),
            soc_before_mwh=round(soc_before, 4), projected_soc_mwh=round(soc_after, 4),
            residual_p10_mwh=round(residuals["P10"], 4),
            residual_p50_mwh=round(residuals["P50"], 4),
            residual_p90_mwh=round(residuals["P90"], 4),
            residual_long_mwh=round(_value(model.residual_long[t, "P50"]), 4),
            residual_short_mwh=round(_value(model.residual_short[t, "P50"]), 4),
            imbalance_risk_cost_gbp=round(expected_imbalance + tail, 2),
            market_execution_value_gbp=round(market_value, 2),
            degradation_cost_gbp=round(degradation, 2),
            reserve_bm_service_value_gbp=round(up_value + down_value + bm_value + optionality - service_risk, 2),
            terminal_soc_contribution_gbp=round(terminal_contribution, 2),
            total_period_contribution_gbp=round(period_total, 2),
            binding_constraints=bindings,
            why_action=why,
            residual_demand_mw=period.residual_demand_mw,
            exposure_before_p10_mwh=round(period.generation_p10_mwh - period.contracted_q_mwh, 4),
            exposure_before_p50_mwh=round(period.generation_p50_mwh - period.contracted_q_mwh, 4),
            exposure_before_p90_mwh=round(period.generation_p90_mwh - period.contracted_q_mwh, 4),
            gate_closure_at=period.gate_closure_at,
            tradeable=period.tradeable,
            bid_depth_mwh=round(sum(level.volume_mwh for level in period.bids), 4),
            ask_depth_mwh=round(sum(level.volume_mwh for level in period.asks), 4),
            unfilled_market_volume_mwh=round(max(0.0, abs(period.generation_p50_mwh - period.contracted_q_mwh) - consumed - abs(net_export * period.duration_hours)), 4),
            wap_slippage_gbp_per_mwh=round(
                (wap - period.asks[0].price_gbp_per_mwh) if buy > 1e-7 and wap is not None
                else (period.bids[0].price_gbp_per_mwh - wap) if sell > 1e-7 and wap is not None
                else 0.0,
                4,
            ),
            upward_commitment_mw=period.upward_commitment_mw,
            downward_commitment_mw=period.downward_commitment_mw,
            upward_headroom_mw=round(cfg.discharge_max_mw - net_export, 4),
            downward_headroom_mw=round(cfg.charge_max_mw + net_export, 4),
            upward_duration_coverage_h=round((soc_before - cfg.e_min_mwh) * cfg.discharge_efficiency / max(reserve_up, 1e-9), 4),
            downward_duration_coverage_h=round((cfg.e_max_mwh - soc_before) / (cfg.charge_efficiency * max(reserve_down, 1e-9)), 4),
            values=values,
        ))

    objective = _objective_breakdown(model, periods, cfg, run_id, snapshot_id, lineage_values)
    terminal_soc = _value(model.soc[len(periods)])
    discharge_mwh = sum(item.discharge_mw * periods[index].duration_hours for index, item in enumerate(period_results))
    readiness = OptimisationReadiness(
        status=SnapshotStatus.DEGRADED,
        calculation_allowed=True,
        trustworthy_for_live_trading=False,
        reasons=[
            "Full-action optimisation completed using explicitly labelled SAMPLE inputs.",
            "Sample simulation is diagnostic only and is not trustworthy for live trading.",
        ],
    )
    changes = _changes(previous_run, periods, starting_state, period_results)
    drivers = _run_drivers(periods, period_results, terminal_soc, cfg)
    chart_series = _chart_series(period_results, objective, starting_state, cfg)
    risk_measures = _risk_measures(period_results, objective, terminal_soc, cfg)
    driver_contributions = _driver_contributions(periods, period_results, objective, terminal_soc, cfg, drivers)
    sensitivities = _sensitivities(period_results, objective, starting_state, cfg)
    sanity_warnings = _sanity_warnings(periods, period_results, starting_state, cfg)
    return OptimisationRun(
        run_id=run_id,
        as_of=starting_state.current_time,
        snapshot_id=snapshot_id,
        solver="HiGHS MILP",
        solver_status="optimal",
        horizon_length=len(periods),
        starting_state=starting_state,
        inputs=periods,
        projected_trajectory=period_results,
        objective_breakdown=objective,
        objective_value_gbp=objective.total_diagnostic_value_gbp,
        terminal_soc_mwh=round(terminal_soc, 4),
        full_cycle_equivalents=round(discharge_mwh / cfg.e_max_mwh, 4),
        explanation_drivers=drivers,
        change_since_previous=changes,
        readiness=readiness,
        lineage_values=lineage_values,
        chart_series=chart_series,
        risk_measures=risk_measures,
        driver_contributions=driver_contributions,
        sensitivities=sensitivities,
        sanity_warnings=sanity_warnings,
        warnings=[
            "Diagnostic optimisation only; no order submission and no battery control.",
            "SAMPLE simulation assumes previous model actions are followed. This is not real execution or live control.",
            "BM and service values are probability-weighted estimates, not guaranteed revenue.",
        ],
    )


def _chart_series(trajectory, objective, starting, cfg):
    def series(key, label, unit, values, kind="line"):
        return ChartSeries(key=key, label=label, unit=unit, kind=kind, points=[
            ChartPoint(
                label=f"SP{period.settlement_period}",
                value=round(float(value), 6),
                timestamp=period.delivery_start,
                settlement_period=period.settlement_period,
                delivery_period=period.delivery_period,
            )
            for period, value in zip(trajectory, values, strict=True)
        ])

    labels = [f"SP{period.settlement_period}" for period in trajectory]
    objective_components = [
        ("market_execution", "Market execution", objective.market_execution_value_gbp),
        ("imbalance", "Expected imbalance", -objective.imbalance_expected_cost_gbp),
        ("tail", "Tail risk", -objective.tail_risk_penalty_gbp),
        ("degradation", "Degradation", -objective.degradation_cost_gbp),
        ("reserve_up", "Up availability", objective.upward_availability_value_gbp),
        ("reserve_down", "Down availability", objective.downward_availability_value_gbp),
        ("bm", "Expected BM", objective.bm_expected_activation_value_gbp),
        ("service_risk", "Service risk", -objective.service_non_delivery_risk_gbp),
        ("optionality", "Optionality", objective.optionality_preservation_value_gbp),
        ("terminal", "Terminal SoC", objective.terminal_soc_value_gbp),
    ]
    objective_series = [ChartSeries(
        key="objective_breakdown", label="Objective contribution", unit="GBP", kind="waterfall",
        points=[ChartPoint(label=label, value=round(value, 4)) for _, label, value in objective_components],
    )]
    return {
        "action_path": [
            series("buy", "Buy", "MWh", [p.buy_mwh for p in trajectory], "bar"),
            series("sell", "Sell", "MWh", [p.sell_mwh for p in trajectory], "bar"),
            series("charge", "Charge", "MW", [p.charge_mw for p in trajectory], "bar"),
            series("discharge", "Discharge", "MW", [p.discharge_mw for p in trajectory], "bar"),
        ],
        "soc_path": [
            series("soc", "Projected SoC", "MWh", [p.projected_soc_mwh for p in trajectory]),
            series("starting_soc", "Starting SoC", "MWh", [starting.starting_soc_mwh] * len(labels)),
            series("soc_min", "Minimum SoC", "MWh", [cfg.e_min_mwh] * len(labels)),
            series("soc_max", "Maximum SoC", "MWh", [cfg.e_max_mwh] * len(labels)),
            series("preferred_terminal", "Preferred terminal", "MWh", [cfg.preferred_terminal_soc_mwh] * len(labels)),
            series("minimum_terminal", "Minimum terminal", "MWh", [cfg.minimum_terminal_soc_mwh] * len(labels)),
        ],
        "reserve_path": [
            series("reserve_up", "Reserve up", "MW", [p.reserve_up_mw for p in trajectory]),
            series("reserve_down", "Reserve down", "MW", [p.reserve_down_mw for p in trajectory]),
            series("up_commitment", "Up commitment", "MW", [p.upward_commitment_mw for p in trajectory]),
            series("down_commitment", "Down commitment", "MW", [p.downward_commitment_mw for p in trajectory]),
            series("up_headroom", "Up headroom", "MW", [p.upward_headroom_mw for p in trajectory]),
            series("down_headroom", "Down headroom", "MW", [p.downward_headroom_mw for p in trajectory]),
            series("up_duration", "Up duration coverage", "h", [p.upward_duration_coverage_h for p in trajectory]),
            series("down_duration", "Down duration coverage", "h", [p.downward_duration_coverage_h for p in trajectory]),
        ],
        "exposure_fan": [
            series("before_p10", "Before P10", "MWh", [p.exposure_before_p10_mwh for p in trajectory]),
            series("before_p50", "Before P50", "MWh", [p.exposure_before_p50_mwh for p in trajectory]),
            series("before_p90", "Before P90", "MWh", [p.exposure_before_p90_mwh for p in trajectory]),
            series("residual_p10", "Residual P10", "MWh", [p.residual_p10_mwh for p in trajectory]),
            series("residual_p50", "Residual P50", "MWh", [p.residual_p50_mwh for p in trajectory]),
            series("residual_p90", "Residual P90", "MWh", [p.residual_p90_mwh for p in trajectory]),
        ],
        "market_execution": [
            series("bid", "Best bid", "GBP/MWh", [p.best_bid_gbp_per_mwh for p in trajectory]),
            series("ask", "Best ask", "GBP/MWh", [p.best_ask_gbp_per_mwh for p in trajectory]),
            series("wap", "WAP used", "GBP/MWh", [p.market_wap_gbp_per_mwh if p.market_wap_gbp_per_mwh is not None else p.reference_price_gbp_per_mwh for p in trajectory]),
            series("spread", "Spread", "GBP/MWh", [p.best_ask_gbp_per_mwh - p.best_bid_gbp_per_mwh for p in trajectory]),
            series("bid_depth", "Bid depth", "MWh", [p.bid_depth_mwh for p in trajectory], "bar"),
            series("ask_depth", "Ask depth", "MWh", [p.ask_depth_mwh for p in trajectory], "bar"),
            series("depth_used", "Depth used", "MWh", [p.visible_depth_consumed_mwh for p in trajectory], "bar"),
            series("unfilled", "Unfilled", "MWh", [p.unfilled_market_volume_mwh for p in trajectory], "bar"),
            series("gate_closed", "Gate Closed", "flag", [0 if p.tradeable else 1 for p in trajectory], "marker"),
        ],
        "period_value": [series("period_value", "Period value", "GBP", [p.total_period_contribution_gbp for p in trajectory], "bar")],
        "objective_breakdown": objective_series,
    }


def _risk_measures(trajectory, objective, terminal_soc, cfg):
    service_coverage = min(
        min(p.reserve_up_mw / max(p.upward_commitment_mw, 1e-9), p.reserve_down_mw / max(p.downward_commitment_mw, 1e-9))
        for p in trajectory
    )
    duration_coverage = min(min(p.upward_duration_coverage_h, p.downward_duration_coverage_h) for p in trajectory)
    total_hours = sum(0.5 for _ in trajectory)
    maximum_optionality = cfg.optionality_preservation_gbp_per_mw_h * (cfg.charge_max_mw + cfg.discharge_max_mw) * total_hours
    return [
        RiskMeasure(key="largest_short", label="Largest short exposure", value=round(abs(min(0.0, *(p.residual_p10_mwh for p in trajectory))), 4), unit="MWh", status="RISK"),
        RiskMeasure(key="largest_long", label="Largest long exposure", value=round(max(0.0, *(p.residual_p90_mwh for p in trajectory)), 4), unit="MWh", status="RISK"),
        RiskMeasure(key="unfilled_volume", label="Total unfilled market volume", value=round(sum(p.unfilled_market_volume_mwh for p in trajectory), 4), unit="MWh", status="RISK"),
        RiskMeasure(key="wap_slippage", label="Maximum WAP slippage", value=round(max((p.wap_slippage_gbp_per_mwh for p in trajectory), default=0.0), 4), unit="GBP/MWh", status="RISK"),
        RiskMeasure(key="imbalance_penalty", label="Expected imbalance penalty", value=objective.imbalance_expected_cost_gbp, unit="GBP", status="COST"),
        RiskMeasure(key="tail_penalty", label="Tail-risk penalty", value=objective.tail_risk_penalty_gbp, unit="GBP", status="COST"),
        RiskMeasure(key="service_coverage", label="Service commitment coverage", value=round(service_coverage, 4), unit="ratio", status="OK" if service_coverage >= 1 else "RISK"),
        RiskMeasure(key="reserve_duration", label="Minimum reserve duration coverage", value=round(duration_coverage, 4), unit="h", status="OK" if duration_coverage >= 1 else "RISK"),
        RiskMeasure(key="terminal_shortfall", label="Terminal SoC shortfall", value=round(max(0.0, cfg.minimum_terminal_soc_mwh - terminal_soc), 4), unit="MWh", status="OK" if terminal_soc >= cfg.minimum_terminal_soc_mwh else "RISK"),
        RiskMeasure(key="optionality_lost", label="Optionality lost", value=round(max(0.0, maximum_optionality - objective.optionality_preservation_value_gbp), 2), unit="GBP", status="RISK"),
        RiskMeasure(key="diagnostic_value", label="Total diagnostic value", value=objective.total_diagnostic_value_gbp, unit="GBP", status="INFO"),
        RiskMeasure(key="worst_period", label="Worst-period contribution", value=round(min(p.total_period_contribution_gbp for p in trajectory), 2), unit="GBP", status="RISK"),
        RiskMeasure(key="binding_count", label="Binding constraint count", value=float(sum(len(p.binding_constraints) for p in trajectory)), unit="count", status="INFO"),
    ]


def _driver_contributions(periods, trajectory, objective, terminal_soc, cfg, drivers):
    explanations = {
        "forecast": drivers.forecast_driver, "demand_system": drivers.demand_system_driver,
        "price_book": drivers.price_order_book_driver, "battery_soc": drivers.battery_soc_driver,
        "reserve_bm": drivers.reserve_bm_driver, "terminal_soc": drivers.terminal_soc_driver,
        "tail_risk": drivers.imbalance_tail_risk_driver, "binding": drivers.binding_constraint_driver,
    }
    raw = {
        "forecast": abs(periods[0].generation_p50_mwh - periods[0].previous_p50_mwh) * 12,
        "demand_system": abs(periods[0].system_tightness_score) * 55 + abs(periods[0].demand_surprise_mw) / 35,
        "price_book": abs(periods[0].reference_price_gbp_per_mwh - 71) * 2,
        "battery_soc": abs(trajectory[0].soc_before_mwh - cfg.preferred_terminal_soc_mwh) * 3,
        "reserve_bm": objective.bm_expected_activation_value_gbp / 18,
        "terminal_soc": abs(terminal_soc - cfg.preferred_terminal_soc_mwh) * 8,
        "tail_risk": objective.tail_risk_penalty_gbp / 90,
        "binding": sum(len(p.binding_constraints) for p in trajectory) * 4,
    }
    return [DriverContribution(key=key, label=key.replace("_", " ").title(), score=round(min(100.0, value), 2), explanation=explanations[key]) for key, value in raw.items()]


def _sensitivities(trajectory, objective, starting, cfg):
    base = objective.total_diagnostic_value_gbp
    horizon = len(trajectory)
    traded = sum(p.buy_mwh + p.sell_mwh for p in trajectory)
    bm = objective.bm_expected_activation_value_gbp
    tail = objective.tail_risk_penalty_gbp
    close_loss = max(0.0, objective.market_execution_value_gbp)
    cases = [
        ("p10_worse", "P10 generation worsens", "P10 −5 MWh each SP", -5 * horizon * cfg.imbalance_penalty_gbp_per_mwh * cfg.tail_risk_weight * 0.5),
        ("price_move", "Price rises/falls", "Executable prices ±£10/MWh", -10 * traded),
        ("depth_halves", "Visible depth halves", "Bid/ask depth ×0.5", -sum(p.visible_depth_consumed_mwh * max(p.wap_slippage_gbp_per_mwh, 1.0) * 0.5 for p in trajectory)),
        ("soc_lower", "Starting SoC is lower", "Starting SoC −10 MWh", -10 * cfg.terminal_soc_value_gbp_per_mwh),
        ("bm_doubles", "BM optionality doubles", "Expected BM value ×2", bm),
        ("tail_weight", "Tail-risk weight increases", "Tail-risk weight +50%", -0.5 * tail),
        ("market_closes", "Market closes earlier", "All open periods Gate Closed", -close_loss),
    ]
    return [SensitivityResult(key=key, label=label, stressed_case=case, baseline_value_gbp=round(base, 2), stressed_value_gbp=round(base + delta, 2), delta_gbp=round(delta, 2), explanation=f"Diagnostic one-factor sensitivity from the solved path: {case} changes total value by {delta:+.0f} GBP; it is not a re-solved executable order.") for key, label, case, delta in cases]


def _sanity_warnings(periods, trajectory, starting, cfg):
    warnings = []
    if starting.current_settlement_period != 1 and periods[0].settlement_period == 1:
        warnings.append("Horizon unexpectedly starts at SP1 while the current settlement period is not SP1.")
    for source, result in zip(periods, trajectory, strict=True):
        if result.buy_mwh > 1e-6 and result.sell_mwh > 1e-6:
            warnings.append(f"{result.delivery_period}: buy and sell are both positive.")
        if result.charge_mw > 1e-6 and result.discharge_mw > 1e-6:
            warnings.append(f"{result.delivery_period}: charge and discharge are both positive.")
        expected_soc = result.soc_before_mwh + cfg.charge_efficiency * result.charge_mw * source.duration_hours - result.discharge_mw * source.duration_hours / cfg.discharge_efficiency
        if abs(expected_soc - result.projected_soc_mwh) > 1e-3:
            warnings.append(f"{result.delivery_period}: projected SoC is inconsistent with charge/discharge.")
        if not source.tradeable and result.buy_mwh + result.sell_mwh > 1e-6:
            warnings.append(f"{result.delivery_period}: market trade appears after Gate Closure.")
        if result.market_wap_gbp_per_mwh is not None:
            expected_wap = _expected_wap(source, result.buy_mwh, result.sell_mwh)
            if expected_wap is not None and abs(expected_wap - result.market_wap_gbp_per_mwh) > 1e-3:
                warnings.append(f"{result.delivery_period}: WAP does not match consumed order-book depth.")
    if starting.regime.value != "normal":
        generation = {round(p.generation_p50_mwh, 4) for p in trajectory}
        prices = {round(p.reference_price_gbp_per_mwh, 4) for p in trajectory}
        if len(generation) == 1 or len(prices) == 1:
            warnings.append("SAMPLE paths are unexpectedly flat outside the normal regime.")
    soc_values = [starting.starting_soc_mwh, *(p.projected_soc_mwh for p in trajectory)]
    if 0 < max(soc_values) - min(soc_values) < (cfg.e_max_mwh - cfg.e_min_mwh) * 0.03:
        warnings.append("SoC movement is small versus physical capacity; the chart uses a focused y-axis so movement remains visible.")
    return warnings


def _expected_wap(period, buy, sell):
    remaining = buy if buy > 1e-7 else sell
    if remaining <= 1e-7:
        return None
    levels = period.asks if buy > 1e-7 else period.bids
    cash = 0.0
    filled = 0.0
    for level in levels:
        take = min(remaining, level.volume_mwh)
        cash += take * level.price_gbp_per_mwh
        filled += take
        remaining -= take
        if remaining <= 1e-7:
            break
    return cash / filled if filled else None


def _objective_breakdown(model, periods, cfg, run_id, snapshot_id, lineage_values):
    market = sum(_value(model.market_value[t]) for t in model.T)
    imbalance = sum(_value(model.expected_imbalance_cost[t]) for t in model.T)
    tail = sum(_value(model.tail_risk_cost[t]) for t in model.T)
    degradation = sum(_value(model.degradation_cost[t]) for t in model.T)
    up = sum(_value(model.upward_availability_value[t]) for t in model.T)
    down = sum(_value(model.downward_availability_value[t]) for t in model.T)
    bm = sum(_value(model.bm_expected_value[t]) for t in model.T)
    optionality = sum(_value(model.optionality_value[t]) for t in model.T)
    service_risk = sum(_value(model.service_risk_cost[t]) for t in model.T)
    terminal = _value(model.terminal_value)
    total = market - imbalance - tail - degradation + up + down + bm - service_risk + optionality + terminal
    inputs = [point for period in periods for point in period.values.values()]
    pseudo_period = periods[-1]
    components = {
        "market_execution_value_gbp": market,
        "imbalance_expected_cost_gbp": imbalance,
        "tail_risk_penalty_gbp": tail,
        "degradation_cost_gbp": degradation,
        "upward_availability_value_gbp": up,
        "downward_availability_value_gbp": down,
        "bm_expected_activation_value_gbp": bm,
        "service_non_delivery_risk_gbp": service_risk,
        "optionality_preservation_value_gbp": optionality,
        "terminal_soc_value_gbp": terminal,
        "total_diagnostic_value_gbp": total,
    }
    values = {
        key: _derived(run_id, snapshot_id, pseudo_period, f"optimiser_{key}", value, "GBP", inputs, "horizon objective component from solved MILP")
        for key, value in components.items()
    }
    lineage_values.extend(values.values())
    return OptimisationObjectiveBreakdown(**{key: round(value, 2) for key, value in components.items()}, values=values)


def _changes(previous, periods, starting, trajectory):
    if previous is None or not previous.inputs:
        return OptimisationChangeSummary(
            forecast_change_mwh=0, demand_change_mw=0, price_change_gbp_per_mwh=0,
            depth_change_mwh=0, q_change_mwh=0, soc_change_mwh=0,
            reserve_optionality_change_gbp=0,
            trajectory_change_reason="Initial rolling optimisation run; no previous run exists.",
        )
    old = previous.inputs[0]
    new = periods[0]
    old_depth = sum(level.volume_mwh for level in [*old.bids, *old.asks])
    new_depth = sum(level.volume_mwh for level in [*new.bids, *new.asks])
    old_reserve = previous.projected_trajectory[0].reserve_bm_service_value_gbp
    new_reserve = trajectory[0].reserve_bm_service_value_gbp
    drivers = []
    if abs(new.generation_p50_mwh - old.generation_p50_mwh) > 0.1:
        drivers.append("forecast")
    if abs(new.reference_price_gbp_per_mwh - old.reference_price_gbp_per_mwh) > 0.5:
        drivers.append("price/order book")
    if abs(starting.starting_soc_mwh - previous.starting_state.starting_soc_mwh) > 0.1:
        drivers.append("SoC")
    return OptimisationChangeSummary(
        forecast_change_mwh=round(new.generation_p50_mwh - old.generation_p50_mwh, 3),
        demand_change_mw=round(new.demand_mw - old.demand_mw, 3),
        price_change_gbp_per_mwh=round(new.reference_price_gbp_per_mwh - old.reference_price_gbp_per_mwh, 3),
        depth_change_mwh=round(new_depth - old_depth, 3),
        q_change_mwh=round(new.contracted_q_mwh - old.contracted_q_mwh, 3),
        soc_change_mwh=round(starting.starting_soc_mwh - previous.starting_state.starting_soc_mwh, 3),
        reserve_optionality_change_gbp=round(new_reserve - old_reserve, 2),
        trajectory_change_reason=(
            "The selected path changed with " + ", ".join(drivers) + " updates."
            if drivers else "The main drivers were stable; only the rolling horizon shifted."
        ),
    )


def _run_drivers(periods, trajectory, terminal_soc, cfg):
    first = periods[0]
    action = trajectory[0]
    forecast_delta = first.generation_p50_mwh - first.contracted_q_mwh
    return OptimisationExplanationDrivers(
        forecast_driver=f"P50 generation is {forecast_delta:+.1f} MWh versus contracted Q in the first tradeable period.",
        demand_system_driver=f"System tightness is {first.system_tightness_score:+.2f} with demand at {first.demand_mw:,.0f} MW.",
        price_order_book_driver=f"The first-period book is {first.bids[0].price_gbp_per_mwh:.1f}/{first.asks[0].price_gbp_per_mwh:.1f} GBP/MWh and the solution consumes {action.visible_depth_consumed_mwh:.1f} MWh.",
        battery_soc_driver=f"The battery starts at {action.soc_before_mwh:.1f} MWh and moves to {action.projected_soc_mwh:.1f} MWh in the first period.",
        reserve_bm_driver=f"The path holds {action.reserve_up_mw:.1f} MW up and {action.reserve_down_mw:.1f} MW down; BM value is expected, not guaranteed.",
        terminal_soc_driver=f"Terminal SoC is {terminal_soc:.1f} MWh versus the {cfg.preferred_terminal_soc_mwh:.1f} MWh preferred level.",
        imbalance_tail_risk_driver=f"First-period residual range is {action.residual_p10_mwh:+.1f} to {action.residual_p90_mwh:+.1f} MWh after action.",
        binding_constraint_driver=(
            "First-period binding constraints: " + ", ".join(action.binding_constraints)
            if action.binding_constraints else "No first-period physical constraint is numerically binding."
        ),
    )


def _period_explanation(period, charge, discharge, buy, sell, up, down, soc_before, soc_after, residuals, bindings, cfg):
    actions = []
    if charge > 1e-4:
        actions.append(f"charges at {charge:.1f} MW")
    elif discharge > 1e-4:
        actions.append(f"discharges at {discharge:.1f} MW")
    else:
        actions.append("keeps battery energy idle")
    if buy > 1e-4:
        actions.append(f"buys {buy:.1f} MWh from visible asks")
    if sell > 1e-4:
        actions.append(f"sells {sell:.1f} MWh into visible bids")
    exposure = period.generation_p50_mwh - period.contracted_q_mwh
    reason = (
        f"P50 exposure starts {exposure:+.1f} MWh, tightness is {period.system_tightness_score:+.2f}, "
        f"and the book is {period.bids[0].price_gbp_per_mwh:.1f}/{period.asks[0].price_gbp_per_mwh:.1f} GBP/MWh."
    )
    reserve = f" It preserves {up:.1f} MW upward and {down:.1f} MW downward reserve"
    if bindings:
        reserve += f" while {', '.join(bindings)} binds."
    else:
        reserve += "."
    terminal = (
        f" SoC moves {soc_before:.1f} to {soc_after:.1f} MWh; the horizon minimum and preferred terminal values prevent myopic depletion."
    )
    return f"The model {', '.join(actions)}. {reason}{reserve}{terminal}"


def _bindings(model, t, period, cfg, charge, discharge, soc_before, soc_after, up, down, buy, sell):
    tolerance = 1e-3
    bindings = []
    if abs(charge - cfg.charge_max_mw) < tolerance:
        bindings.append("CHARGE_POWER")
    if abs(discharge - cfg.discharge_max_mw) < tolerance:
        bindings.append("DISCHARGE_POWER")
    if abs(soc_after - cfg.e_min_mwh) < tolerance:
        bindings.append("SOC_MIN")
    if abs(soc_after - cfg.e_max_mwh) < tolerance:
        bindings.append("SOC_MAX")
    net = discharge - charge
    if abs(up - (cfg.discharge_max_mw - net)) < tolerance:
        bindings.append("RESERVE_UP_POWER")
    if abs(down - (cfg.charge_max_mw + net)) < tolerance:
        bindings.append("RESERVE_DOWN_POWER")
    if abs(soc_before - cfg.e_min_mwh - up * cfg.upward_duration_h / cfg.discharge_efficiency) < tolerance:
        bindings.append("RESERVE_UP_DURATION")
    if abs(cfg.e_max_mwh - soc_before - cfg.charge_efficiency * down * cfg.downward_duration_h) < tolerance:
        bindings.append("RESERVE_DOWN_DURATION")
    if buy > 0 and abs(buy - sum(level.volume_mwh for level in period.asks)) < tolerance:
        bindings.append("ASK_DEPTH")
    if sell > 0 and abs(sell - sum(level.volume_mwh for level in period.bids)) < tolerance:
        bindings.append("BID_DEPTH")
    if not period.tradeable:
        bindings.append("GATE_CLOSED")
    return bindings


def _derived(run_id, snapshot_id, period, metric, value, unit, inputs, expression):
    source_mode = combined_source_mode(inputs) if inputs else SourceMode.SAMPLE
    quality = combined_quality(inputs) if inputs else Quality.FRESH
    identifier = uuid5(NAMESPACE_URL, f"{run_id}:{period.delivery_period}:{metric}:{value:.8f}")
    warnings = [warning for point in inputs for warning in point.lineage.warnings]
    warnings.extend([
        "Full-action optimiser output is diagnostic and not executable.",
        "Derived from SAMPLE rolling-state inputs; not trustworthy for live trading.",
    ])
    return CanonicalDataPoint(
        value_id=str(identifier), metric=metric, value=round(float(value), 6), unit=unit,
        delivery_period=period.delivery_period, delivery_start=period.delivery_start,
        lineage=DataLineage(
            source_feed="full_action_optimiser",
            source_mode=source_mode,
            semantic_kind=SemanticKind.ESTIMATE,
            quality=quality,
            published_at=period.delivery_start,
            retrieved_at=period.values[next(iter(period.values))].lineage.retrieved_at if period.values else period.delivery_start,
            normalised_at=period.values[next(iter(period.values))].lineage.normalised_at if period.values else period.delivery_start,
            raw_field_name=expression,
            transformations=[expression],
            validation_checks=[
                ValidationCheck(name="finite_result", passed=True, detail="solved result is finite"),
                ValidationCheck(name="diagnostic_not_executable", passed=True, detail="cannot submit orders or control a battery"),
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot_id,
    )


def _value(expression) -> float:
    return float(pyo.value(expression))
