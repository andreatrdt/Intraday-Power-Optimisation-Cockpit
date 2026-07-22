"""Explicit SAMPLE rolling environment for the live-cockpit experience."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from typing import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from cockpit.liquidity import executable_price
from cockpit.models import (
    CanonicalDataPoint,
    BatteryPathPoint,
    CockpitSnapshot,
    DataLineage,
    ChartPoint,
    ChartSeries,
    ChartAnnotation,
    ForecastVintageChartPoint,
    ForecastVintageHistoryPoint,
    HistoricalOptimisationPoint,
    HorizonMode,
    LiveStateSnapshot,
    LiveHistoryPoint,
    OptimisationRun,
    OptimisationPeriodInput,
    OptimisationInteractionPoint,
    PositionPathPoint,
    MarketExecutionPathPoint,
    RiskValuePathPoint,
    OptimiserReadiness,
    OptimiserStatus,
    Quality,
    RollingEvent,
    RollingMarketState,
    RollingOrderBookLevel,
    RollingPortfolioBattery,
    RollingProductionDemand,
    RollingState,
    RollingTrust,
    RiskMeasure,
    SampleRegime,
    SemanticKind,
    SnapshotReadiness,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.settlement import LONDON, PERIOD, UTC, auction_window_periods, daily_auction_boundaries, settlement_period_for_instant, upcoming_periods


REGIME_BIAS = {
    SampleRegime.NORMAL: (0.0, 0.0, 0.0, 1.0),
    SampleRegime.TIGHTENING: (-5.0, 650.0, 0.55, 0.72),
    SampleRegime.OVERSUPPLY: (14.0, -750.0, -0.65, 1.35),
    SampleRegime.PRICE_SPIKE: (-2.0, 250.0, 1.35, 0.52),
    SampleRegime.WIND_FORECAST_MISS: (-18.0, 180.0, 0.75, 0.70),
    SampleRegime.DEMAND_SURPRISE: (-2.0, 1400.0, 0.90, 0.62),
}


class SimulatedEnvironment:
    """Deterministic, evolving environment that is always explicitly SAMPLE."""

    def __init__(self, horizon: int = 8, clock: Callable[[], datetime] | None = None) -> None:
        self.horizon = horizon
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.horizon_mode = HorizonMode.NEXT_AUCTION
        self.debug_time_offset = timedelta(0)
        self.initial_soc_mwh = 54.2
        self.base_time = self._now()
        self.step = 0
        self.refresh_sequence = 0
        self.regime = SampleRegime.NORMAL
        self.current_soc_mwh = self.initial_soc_mwh
        self.previous_projected_soc_mwh: float | None = None
        self.q_by_period: dict[str, float] = {}
        self.previous_run_id: str | None = None
        self.latest_run_id: str | None = None
        self.last_soc_change_mwh = 0.0
        self.last_q_change_mwh = 0.0
        self.last_applied_q_mwh: float | None = None
        self.previous_forecast_vintage_id: str | None = None
        self.previous_market_snapshot_id: str | None = None
        self.events: list[RollingEvent] = []
        self.history: list[LiveHistoryPoint] = []
        self.forecast_vintage_history: list[ForecastVintageHistoryPoint] = []
        self.optimisation_history: list[HistoricalOptimisationPoint] = []
        self.applied_delivery_periods: set[str] = set()
        self._previous_production_mw: float | None = None
        self._previous_demand_mw: float | None = None
        self._previous_inputs: list[OptimisationPeriodInput] = []
        self.live_state: LiveStateSnapshot | None = None
        self.cockpit_snapshot: CockpitSnapshot | None = None

    @property
    def current_time(self) -> datetime:
        return self._now() + self.debug_time_offset

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Backend clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)

    def reset(self) -> tuple[LiveStateSnapshot, list[OptimisationPeriodInput], CockpitSnapshot]:
        self.base_time = self._now()
        self.debug_time_offset = timedelta(0)
        self.step = 0
        self.refresh_sequence = 0
        self.regime = SampleRegime.NORMAL
        self.current_soc_mwh = self.initial_soc_mwh
        self.previous_projected_soc_mwh = None
        self.q_by_period = {}
        self.previous_run_id = None
        self.latest_run_id = None
        self.last_soc_change_mwh = 0.0
        self.last_q_change_mwh = 0.0
        self.last_applied_q_mwh = None
        self.previous_forecast_vintage_id = None
        self.previous_market_snapshot_id = None
        self.events = []
        self.history = []
        self.forecast_vintage_history = []
        self.optimisation_history = []
        self.applied_delivery_periods = set()
        self._previous_production_mw = None
        self._previous_demand_mw = None
        self._previous_inputs = []
        return self.refresh(reason="Sample rolling environment reset")

    def set_horizon_mode(self, mode: HorizonMode) -> tuple[LiveStateSnapshot, list[OptimisationPeriodInput], CockpitSnapshot]:
        self.horizon_mode = mode
        return self.refresh(reason=f"Optimisation horizon changed to {mode.value}")

    def set_regime(self, regime: SampleRegime) -> tuple[LiveStateSnapshot, list[OptimisationPeriodInput], CockpitSnapshot]:
        self.regime = regime
        return self.refresh(reason=f"Scenario regime changed to {regime.value}")

    def reconcile_previous_run(self, run: OptimisationRun | None, now: datetime | None = None) -> list[str]:
        """Apply completed SAMPLE actions as backend time naturally crosses delivery periods."""
        if run is None:
            return []
        as_of = (now or self.current_time).astimezone(UTC)
        applied: list[str] = []
        initial_soc = self.current_soc_mwh
        total_q_change = 0.0
        for action in run.projected_trajectory:
            if action.delivery_end > as_of or action.delivery_period in self.applied_delivery_periods:
                continue
            old_q = self.q_by_period.get(action.delivery_period, action.q_before_action_mwh)
            new_q = old_q + action.sell_mwh - action.buy_mwh
            self.q_by_period[action.delivery_period] = new_q
            total_q_change += new_q - old_q
            self.last_applied_q_mwh = new_q
            self.previous_projected_soc_mwh = action.projected_soc_mwh
            self.current_soc_mwh = action.projected_soc_mwh
            self.applied_delivery_periods.add(action.delivery_period)
            applied.append(action.delivery_period)
        if applied:
            self.previous_run_id = run.run_id
            self.last_soc_change_mwh = self.current_soc_mwh - initial_soc
            self.last_q_change_mwh = total_q_change
        else:
            self.last_soc_change_mwh = 0.0
            self.last_q_change_mwh = 0.0
        return applied

    def advance_from_action(
        self,
        *,
        delivery_period: str,
        projected_soc_mwh: float,
        buy_mwh: float,
        sell_mwh: float,
        run_id: str,
    ) -> tuple[LiveStateSnapshot, list[OptimisationPeriodInput], CockpitSnapshot]:
        old_soc = self.current_soc_mwh
        old_q = self.q_by_period.get(delivery_period, self._base_q(self.step))
        new_q = old_q + sell_mwh - buy_mwh
        self.q_by_period[delivery_period] = new_q
        self.previous_projected_soc_mwh = projected_soc_mwh
        self.current_soc_mwh = projected_soc_mwh
        self.last_soc_change_mwh = projected_soc_mwh - old_soc
        self.last_q_change_mwh = new_q - old_q
        self.last_applied_q_mwh = new_q
        self.previous_run_id = run_id
        self.debug_time_offset += PERIOD
        self.step += 1
        self.refresh_sequence = 0
        return self.refresh(
            reason=(
                f"Advanced one settlement period; applied SAMPLE model action from {run_id}: "
                f"SoC {old_soc:.2f}->{projected_soc_mwh:.2f} MWh, Q {old_q:.2f}->{new_q:.2f} MWh"
            )
        )

    def refresh(self, reason: str = "Sample state refreshed") -> tuple[LiveStateSnapshot, list[OptimisationPeriodInput], CockpitSnapshot]:
        self.refresh_sequence += 1
        as_of = self.current_time
        base_period = settlement_period_for_instant(self.base_time)
        current_period_for_step = settlement_period_for_instant(as_of)
        self.step = max(0, int((current_period_for_step.start_utc - base_period.start_utc) / PERIOD))
        snapshot_id = (
            f"rolling-{as_of.strftime('%Y%m%dT%H%M%S')}-s{self.step}-r{self.refresh_sequence}-"
            f"{self.regime.value}"
        )
        forecast_vintage_id = f"fcst-s{self.step}-r{self.refresh_sequence}-{self.regime.value}"
        market_snapshot_id = f"book-s{self.step}-r{self.refresh_sequence}-{self.regime.value}"
        periods = self._build_periods(as_of, snapshot_id)
        first = periods[0]
        current_period = settlement_period_for_instant(as_of)
        next_gate_period = next((period for period in periods if period.gate_closure_at > as_of), first)
        gate_at = next_gate_period.gate_closure_at
        minutes_to_gate = max(0.0, (gate_at - as_of).total_seconds() / 60)

        wind_mw = first.generation_p50_mwh * 2 * 0.86
        solar_mw = first.generation_p50_mwh * 2 * 0.14
        production_mw = wind_mw + solar_mw
        production_delta = 0.0 if self._previous_production_mw is None else production_mw - self._previous_production_mw
        demand_delta = 0.0 if self._previous_demand_mw is None else first.demand_mw - self._previous_demand_mw
        self._previous_production_mw = production_mw
        self._previous_demand_mw = first.demand_mw
        production_values = {
            "renewable_production_mw": self._point(snapshot_id, as_of, "current_renewable_production", production_mw, "MW", SemanticKind.OBSERVATION),
            "wind_mw": self._point(snapshot_id, as_of, "current_wind_production", wind_mw, "MW", SemanticKind.OBSERVATION),
            "solar_mw": self._point(snapshot_id, as_of, "current_solar_production", solar_mw, "MW", SemanticKind.OBSERVATION),
            "demand_mw": first.values["demand_mw"],
            "residual_demand_mw": self._point(snapshot_id, as_of, "current_residual_demand", first.demand_mw - production_mw, "MW", SemanticKind.ESTIMATE),
        }
        production = RollingProductionDemand(
            renewable_production_mw=round(production_mw, 2), wind_mw=round(wind_mw, 2),
            solar_mw=round(solar_mw, 2), demand_mw=first.demand_mw,
            residual_demand_mw=round(first.demand_mw - production_mw, 2),
            production_delta_mw=round(production_delta, 2), demand_delta_mw=round(demand_delta, 2),
            values=production_values,
        )
        best_bid, best_ask = first.bids[0], first.asks[0]
        bid_depth = sum(item.volume_mwh for item in first.bids)
        ask_depth = sum(item.volume_mwh for item in first.asks)
        market_values = {
            "reference_price_gbp_per_mwh": first.values["reference_price_gbp_per_mwh"],
            "best_bid_gbp_per_mwh": best_bid.price_value,
            "best_ask_gbp_per_mwh": best_ask.price_value,
            "frequency_hz": first.values["frequency_hz"],
            "system_tightness_score": first.values["system_tightness_score"],
        }
        market = RollingMarketState(
            reference_price_gbp_per_mwh=first.reference_price_gbp_per_mwh,
            best_bid_gbp_per_mwh=best_bid.price_gbp_per_mwh,
            best_ask_gbp_per_mwh=best_ask.price_gbp_per_mwh,
            spread_gbp_per_mwh=round(best_ask.price_gbp_per_mwh - best_bid.price_gbp_per_mwh, 2),
            bid_depth_mwh=round(bid_depth, 2), ask_depth_mwh=round(ask_depth, 2),
            sell_wap_5_mwh=self._wap(first, 5, "SELL"), sell_wap_10_mwh=self._wap(first, 10, "SELL"),
            buy_wap_5_mwh=self._wap(first, 5, "BUY"), buy_wap_10_mwh=self._wap(first, 10, "BUY"),
            frequency_hz=float(first.values["frequency_hz"].value),
            system_tightness_score=first.system_tightness_score,
            market_regime=self.regime,
            bids=first.bids, asks=first.asks, values=market_values,
        )
        current_q = self.last_applied_q_mwh if self.last_applied_q_mwh is not None else first.contracted_q_mwh
        exposure = first.generation_p50_mwh - first.contracted_q_mwh
        portfolio_values = {
            "current_q_mwh": self._point(snapshot_id, as_of, "rolling_current_q", current_q, "MWh", SemanticKind.ASSUMPTION),
            "current_forecast_generation_mwh": first.values["generation_p50_mwh"],
            "exposure_before_action_mwh": self._point(snapshot_id, as_of, "rolling_current_exposure", exposure, "MWh", SemanticKind.ESTIMATE),
            "current_soc_mwh": self._point(snapshot_id, as_of, "battery_soc", self.current_soc_mwh, "MWh", SemanticKind.OBSERVATION),
        }
        portfolio = RollingPortfolioBattery(
            current_q_mwh=round(current_q, 3),
            current_forecast_generation_mwh=first.generation_p50_mwh,
            exposure_before_action_mwh=round(exposure, 3),
            current_soc_mwh=round(self.current_soc_mwh, 3),
            previous_projected_soc_mwh=self.previous_projected_soc_mwh,
            reserve_up_held_mw=first.upward_commitment_mw,
            reserve_down_held_mw=first.downward_commitment_mw,
            values=portfolio_values,
        )
        trust = RollingTrust(
            readiness=SnapshotStatus.DEGRADED,
            calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=["Explicit SAMPLE environment is fresh and internally consistent but is not live trading data."],
        )
        _, next_auction = daily_auction_boundaries(as_of)
        state = RollingState(
            current_time=as_of,
            current_settlement_period=current_period.settlement_period,
            current_settlement_label=current_period.label,
            next_settlement_period=first.settlement_period,
            next_settlement_label=first.delivery_period,
            next_gate_closure_at=gate_at,
            minutes_to_gate_closure=round(minutes_to_gate, 2),
            current_soc_mwh=round(self.current_soc_mwh, 4),
            previous_projected_soc_mwh=self.previous_projected_soc_mwh,
            current_q_mwh_by_period={period.delivery_period: period.contracted_q_mwh for period in periods},
            previous_run_id=self.previous_run_id,
            latest_optimisation_run_id=self.latest_run_id,
            current_forecast_vintage_id=forecast_vintage_id,
            previous_forecast_vintage_id=self.previous_forecast_vintage_id,
            current_market_snapshot_id=market_snapshot_id,
            previous_market_snapshot_id=self.previous_market_snapshot_id,
            current_regime=self.regime,
            current_step=self.step,
            refresh_sequence=self.refresh_sequence,
            state_source_mode=SourceMode.SAMPLE,
            quality=Quality.FRESH,
            trust=trust,
            snapshot_id=snapshot_id,
            last_soc_change_mwh=round(self.last_soc_change_mwh, 4),
            last_q_change_mwh=round(self.last_q_change_mwh, 4),
            horizon_mode=self.horizon_mode,
            effective_horizon_mode=self.horizon_mode,
            optimisation_horizon_start=as_of,
            optimisation_horizon_end=next_auction if self.horizon_mode == HorizonMode.NEXT_AUCTION else periods[-1].delivery_end,
            horizon_warning=None,
            auction_calendar_configured=True,
        )
        event_points = [*production_values.values(), *market_values.values(), *portfolio_values.values()]
        self._emit(as_of, "forecast", f"Forecast vintage updated to {forecast_vintage_id}", event_points[0])
        self._emit(as_of, "market", f"Market snapshot updated to {market_snapshot_id}", best_bid.price_value)
        self._emit(as_of, "frequency", f"Frequency updated to {market.frequency_hz:.3f} Hz", first.values["frequency_hz"])
        self._emit(as_of, "demand", f"Demand changed {demand_delta:+.1f} MW to {first.demand_mw:,.0f} MW", first.values["demand_mw"])
        self._emit(as_of, "production", f"Production changed {production_delta:+.1f} MW to {production_mw:.1f} MW", production_values["renewable_production_mw"])
        self._emit(as_of, "order_book", f"Visible depth is {bid_depth:.1f} MWh bid / {ask_depth:.1f} MWh ask", best_ask.volume_value)
        self._emit(as_of, "battery", f"Battery SoC updated to {self.current_soc_mwh:.2f} MWh", portfolio_values["current_soc_mwh"])
        self._emit(as_of, "portfolio", f"Portfolio Q updated/confirmed at {current_q:.2f} MWh", portfolio_values["current_q_mwh"])
        self._emit(as_of, "state", reason, None)

        lineage_values = list({point.value_id: point for point in [
            *event_points,
            *(point for period in periods for point in period.values.values()),
            *(level.price_value for period in periods for level in [*period.bids, *period.asks]),
            *(level.volume_value for period in periods for level in [*period.bids, *period.asks]),
        ]}.values())
        self._record_history(as_of, production, market, portfolio, first)
        forecast_series = [ForecastVintageChartPoint(
            settlement_period=period.settlement_period,
            delivery_period=period.delivery_period,
            delivery_start=period.delivery_start,
            previous_p50_mwh=period.previous_p50_mwh,
            latest_p50_mwh=period.generation_p50_mwh,
            p10_mwh=period.generation_p10_mwh,
            p90_mwh=period.generation_p90_mwh,
            delta_mwh=round(period.generation_p50_mwh - period.previous_p50_mwh, 3),
            confidence_score=period.forecast_confidence_score,
            driver=period.forecast_driver,
        ) for period in periods]
        chart_series = self._live_chart_series(forecast_series)
        chart_insights = self._chart_insights(production, market, portfolio, forecast_series)
        context_risk_measures = self.context_risk_measures(production, market, forecast_series)
        warnings = [
            "SAMPLE simulation assumes previous model actions are followed. This is not real execution or live control.",
            "All evolving values remain explicitly SAMPLE; no synthetic or live fallback is used.",
        ]
        if state.horizon_warning:
            warnings.append(state.horizon_warning)
        live = LiveStateSnapshot(
            state=state, production_demand=production, market=market,
            portfolio_battery=portfolio, events=list(reversed(self.events[-80:])),
            lineage_values=lineage_values,
            history=self.history[-721:],
            forecast_vintage_series=forecast_series,
            forecast_vintage_history=self.forecast_vintage_history[-1500:],
            optimisation_history=self.optimisation_history[-1500:],
            chart_series=chart_series,
            chart_insights=chart_insights,
            context_risk_measures=context_risk_measures,
            warnings=warnings,
        )
        cockpit = self._cockpit_snapshot(snapshot_id, as_of, periods, lineage_values)
        self.previous_forecast_vintage_id = forecast_vintage_id
        self.previous_market_snapshot_id = market_snapshot_id
        self._previous_inputs = [period.model_copy(deep=True) for period in periods]
        self.live_state = live
        self.cockpit_snapshot = cockpit
        return live, periods, cockpit

    def _record_history(self, as_of, production, market, portfolio, first) -> None:
        if not self.history:
            for offset in range(-719, 0):
                observed_at = as_of + timedelta(hours=offset)
                daily = 2 * math.pi * (observed_at.hour + observed_at.minute / 60) / 24
                weekly = 2 * math.pi * ((observed_at.weekday() * 24 + observed_at.hour) / (7 * 24))
                slow = 2 * math.pi * offset / (24 * 16)
                solar = max(0.0, 38 * math.sin(math.pi * (observed_at.hour - 6) / 12))
                wind = max(35.0, production.wind_mw * (0.88 + 0.16 * math.sin(slow) + 0.09 * math.cos(weekly * 1.7)))
                historical_production = wind + solar
                peak = 1150 * max(0.0, math.sin(daily - math.pi / 2))
                historical_demand = 27400 + peak + 520 * math.sin(weekly) + 180 * math.cos(daily * 2)
                forecast_error = 14 * math.sin(offset / 17) + 5 * math.cos(offset / 7)
                forecast_mw = historical_production - forecast_error
                residual = historical_demand - historical_production
                tightening = residual > 28750
                oversupply = historical_production > 215 and historical_demand < 27900
                spike = observed_at.hour in {17, 18} and observed_at.day % 9 == 0
                regime = "price_spike" if spike else "tightening" if tightening else "oversupply" if oversupply else "normal"
                reference = 55 + (residual - 27000) / 170 + 5 * math.sin(daily) + (65 if spike else 0)
                spread = 1.4 + abs(reference - 65) / 35 + (2.2 if spike else 0)
                bid = reference - spread / 2
                ask = reference + spread / 2
                depth_factor = max(0.35, 1.15 - spread / 8)
                bid_depth = 37 * depth_factor * (1 + 0.11 * math.sin(offset / 5))
                ask_depth = 35 * depth_factor * (1 - 0.09 * math.cos(offset / 6))
                tightness_score = max(-1.5, min(1.8, (residual - 28000) / 1400))
                q = portfolio.current_q_mwh + 4.5 * math.sin(offset / 31)
                exposure = historical_production / 2 - q
                soc = max(32.0, min(78.0, 54 + 7 * math.sin(offset / 43) + 2 * math.cos(offset / 11)))
                self.history.append(LiveHistoryPoint(
                    observed_at=observed_at,
                    renewable_production_mw=round(historical_production, 3),
                    wind_mw=round(wind, 3), solar_mw=round(solar, 3),
                    demand_mw=round(historical_demand, 3),
                    residual_demand_mw=round(residual, 3),
                    forecast_p50_mw=round(forecast_mw, 3),
                    forecast_error_mw=round(forecast_error, 3),
                    frequency_hz=round(50.0 - tightness_score * 0.026 + 0.005 * math.sin(offset), 6),
                    reference_price_gbp_per_mwh=round(reference, 3),
                    best_bid_gbp_per_mwh=round(bid, 3), best_ask_gbp_per_mwh=round(ask, 3),
                    bid_depth_mwh=round(bid_depth, 3), ask_depth_mwh=round(ask_depth, 3),
                    q_mwh=round(q, 3), exposure_mwh=round(exposure, 3), soc_mwh=round(soc, 3),
                    previous_projected_soc_mwh=round(soc + 0.8 * math.sin(offset / 9), 3),
                    reserve_up_mw=portfolio.reserve_up_held_mw,
                    reserve_down_mw=portfolio.reserve_down_held_mw,
                    system_tightness_score=round(tightness_score, 4),
                    demand_surprise_mw=round(160 * math.sin(offset / 13), 3),
                    production_surprise_mw=round(forecast_error, 3), regime=regime,
                ))
                self.forecast_vintage_history.append(ForecastVintageHistoryPoint(
                    observed_at=observed_at,
                    vintage_id=f"hist-fcst-{observed_at.strftime('%Y%m%dT%H')}",
                    delivery_period=settlement_period_for_instant(observed_at + PERIOD).label,
                    p50_mwh=round(forecast_mw / 2, 3),
                    previous_p50_mwh=round((forecast_mw - 3.5 * math.sin(offset / 8)) / 2, 3),
                    actual_mwh=round(historical_production / 2, 3),
                    error_mwh=round(forecast_error / 2, 3),
                ))
        self.history.append(LiveHistoryPoint(
            observed_at=as_of,
            renewable_production_mw=production.renewable_production_mw,
            wind_mw=production.wind_mw, solar_mw=production.solar_mw,
            demand_mw=production.demand_mw, residual_demand_mw=production.residual_demand_mw,
            forecast_p50_mw=first.generation_p50_mwh * 2,
            forecast_error_mw=round(production.renewable_production_mw - first.generation_p50_mwh * 2, 3),
            frequency_hz=market.frequency_hz,
            reference_price_gbp_per_mwh=market.reference_price_gbp_per_mwh,
            best_bid_gbp_per_mwh=market.best_bid_gbp_per_mwh,
            best_ask_gbp_per_mwh=market.best_ask_gbp_per_mwh,
            bid_depth_mwh=market.bid_depth_mwh, ask_depth_mwh=market.ask_depth_mwh,
            q_mwh=portfolio.current_q_mwh, exposure_mwh=portfolio.exposure_before_action_mwh,
            soc_mwh=portfolio.current_soc_mwh,
            previous_projected_soc_mwh=portfolio.previous_projected_soc_mwh,
            reserve_up_mw=portfolio.reserve_up_held_mw, reserve_down_mw=portfolio.reserve_down_held_mw,
            system_tightness_score=market.system_tightness_score,
            demand_surprise_mw=first.demand_surprise_mw,
            production_surprise_mw=first.production_surprise_mw,
            regime=self.regime.value,
        ))
        self.forecast_vintage_history.append(ForecastVintageHistoryPoint(
            observed_at=as_of, vintage_id=self.previous_forecast_vintage_id or f"fcst-current-{as_of.strftime('%Y%m%dT%H%M')}",
            delivery_period=first.delivery_period, p50_mwh=first.generation_p50_mwh,
            previous_p50_mwh=first.previous_p50_mwh,
            actual_mwh=round(production.renewable_production_mw / 2, 3),
            error_mwh=round(production.renewable_production_mw / 2 - first.generation_p50_mwh, 3),
        ))
        self.history = self.history[-721:]
        self.forecast_vintage_history = self.forecast_vintage_history[-1500:]

    def _live_chart_series(self, forecast: list[ForecastVintageChartPoint]) -> dict[str, list[ChartSeries]]:
        history = self.history[-721:]
        regime_annotations = self._history_annotations(history)

        def historical(
            key: str,
            label: str,
            unit: str,
            attr: str,
            kind: str = "line",
            flat_explanation: str | None = None,
            annotations: list[ChartAnnotation] | None = None,
        ) -> ChartSeries:
            values = [float(getattr(item, attr)) for item in history]
            is_flat = len(values) > 1 and max(values) - min(values) < 1e-8
            return ChartSeries(
                key=key,
                label=label,
                unit=unit,
                kind=kind,
                region="historical",
                flat_explanation=flat_explanation if is_flat else None,
                annotations=annotations or [],
                points=[
                    ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=value)
                    for item, value in zip(history, values, strict=True)
                ],
            )

        def vintage(key: str, label: str, attr: str, kind: str = "line") -> ChartSeries:
            values = [float(getattr(item, attr)) for item in forecast]
            is_flat = len(values) > 1 and max(values) - min(values) < 1e-8
            return ChartSeries(
                key=key,
                label=label,
                unit="MWh",
                kind=kind,
                region="future",
                flat_explanation=(
                    "Forecast is constant across the selected horizon; the SAMPLE path is insufficiently dynamic."
                    if is_flat else None
                ),
                points=[
                    ChartPoint(label=f"SP{item.settlement_period}", timestamp=item.delivery_start, settlement_period=item.settlement_period, delivery_period=item.delivery_period, value=value)
                    for item, value in zip(forecast, values, strict=True)
                ],
            )

        vintage_history = self.forecast_vintage_history[-721:]
        historical_forecast = [
            ChartSeries(
                key=key,
                label=label,
                unit="MWh",
                region="historical",
                points=[ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=float(getattr(item, attr))) for item in vintage_history],
            )
            for key, label, attr in (
                ("vintage_p50", "Vintage P50", "p50_mwh"),
                ("vintage_previous", "Previous vintage", "previous_p50_mwh"),
                ("vintage_actual", "Simulated actual", "actual_mwh"),
                ("vintage_error", "Forecast error", "error_mwh"),
            )
        ]
        return {
            "production": [historical("production", "Production", "MW", "renewable_production_mw", annotations=regime_annotations), historical("wind", "Wind", "MW", "wind_mw"), historical("solar", "Solar", "MW", "solar_mw"), historical("forecast_actual", "Forecast P50", "MW", "forecast_p50_mw")],
            "demand": [historical("demand", "Demand", "MW", "demand_mw"), historical("residual_demand", "Residual demand", "MW", "residual_demand_mw")],
            "forecast_vintage": [vintage("previous_p50", "Previous P50", "previous_p50_mwh"), vintage("latest_p50", "Latest P50", "latest_p50_mwh"), vintage("p10", "P10", "p10_mwh"), vintage("p90", "P90", "p90_mwh"), vintage("delta", "Vintage delta", "delta_mwh", "bar")],
            "forecast_history": historical_forecast,
            "market_price": [
                historical("reference", "Reference", "GBP/MWh", "reference_price_gbp_per_mwh", annotations=regime_annotations),
                historical("best_bid", "Best bid", "GBP/MWh", "best_bid_gbp_per_mwh"),
                historical("best_ask", "Best ask", "GBP/MWh", "best_ask_gbp_per_mwh"),
                ChartSeries(key="sell_wap_10", label="Sell WAP 10", unit="GBP/MWh", region="historical", points=[ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=round(item.best_bid_gbp_per_mwh - 0.35, 4)) for item in history]),
                ChartSeries(key="buy_wap_10", label="Buy WAP 10", unit="GBP/MWh", region="historical", points=[ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=round(item.best_ask_gbp_per_mwh + 0.42, 4)) for item in history]),
            ],
            "market_depth": [historical("bid_depth", "Bid depth", "MWh", "bid_depth_mwh", "bar"), historical("ask_depth", "Ask depth", "MWh", "ask_depth_mwh", "bar")],
            "frequency": [historical("frequency", "Frequency", "Hz", "frequency_hz")],
            "system": [historical("tightness", "Tightness", "score", "system_tightness_score", annotations=regime_annotations), historical("demand_surprise", "Demand surprise", "MW", "demand_surprise_mw"), historical("production_surprise", "Production surprise", "MW", "production_surprise_mw")],
            "portfolio": [historical("q", "Q", "MWh", "q_mwh"), historical("exposure", "Exposure", "MWh", "exposure_mwh")],
            "battery": [
                historical("soc", "Current SoC", "MWh", "soc_mwh"),
                ChartSeries(key="previous_projected_soc", label="Previous projected SoC", unit="MWh", region="historical", points=[
                    ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=float(item.previous_projected_soc_mwh if item.previous_projected_soc_mwh is not None else item.soc_mwh))
                    for item in history
                ]),
                historical("reserve_up", "Reserve up", "MW", "reserve_up_mw", flat_explanation="Reserve fixed by SAMPLE commitment: 8 MW up / 5 MW down."),
                historical("reserve_down", "Reserve down", "MW", "reserve_down_mw", flat_explanation="Reserve fixed by SAMPLE commitment: 8 MW up / 5 MW down."),
            ],
        }

    @staticmethod
    def _history_annotations(history: list[LiveHistoryPoint]) -> list[ChartAnnotation]:
        annotations: list[ChartAnnotation] = []
        previous = None
        for item in history:
            if item.regime != previous and item.regime != "normal":
                annotations.append(ChartAnnotation(
                    timestamp=item.observed_at,
                    label=item.regime.replace("_", " ").title(),
                    kind="regime",
                ))
            previous = item.regime
        return annotations[-8:]

    @staticmethod
    def _percentile(value: float, values: list[float]) -> float:
        if not values:
            return 0.0
        return round(100.0 * sum(item <= value for item in values) / len(values), 1)

    def _chart_insights(self, production, market, portfolio, forecast: list[ForecastVintageChartPoint]) -> dict[str, str]:
        history = self.history[-721:]
        comparison = history[-25] if len(history) >= 25 else history[0]
        residual_change = production.residual_demand_mw - comparison.residual_demand_mw
        forecast_error = production.renewable_production_mw - forecast[0].latest_p50_mwh * 2
        largest = max(forecast, key=lambda item: abs(item.delta_mwh))
        price_pct = self._percentile(market.reference_price_gbp_per_mwh, [item.reference_price_gbp_per_mwh for item in history])
        depth_pct = self._percentile(market.bid_depth_mwh + market.ask_depth_mwh, [item.bid_depth_mwh + item.ask_depth_mwh for item in history])
        return {
            "production": f"Wind-led renewable production is {abs(forecast_error):.1f} MW {'below' if forecast_error < 0 else 'above'} the current P50 forecast.",
            "demand": f"Residual demand has {'risen' if residual_change >= 0 else 'fallen'} {abs(residual_change):.0f} MW over the last 24 hours.",
            "forecast_vintage": f"SP{largest.settlement_period} has the largest forecast revision at {largest.delta_mwh:+.1f} MWh versus the previous vintage.",
            "forecast_history": f"Current simulated forecast error is {forecast_error / 2:+.1f} MWh; every historical vintage remains explicitly SAMPLE.",
            "market_price": f"The reference price is at the {price_pct:.0f}th percentile of the 30-day SAMPLE history; bid and ask remain the executable references.",
            "market_depth": f"Combined visible depth is at the {depth_pct:.0f}th percentile of the 30-day SAMPLE history.",
            "frequency": f"Frequency is {market.frequency_hz:.3f} Hz; deviations from 50 Hz provide system-balance context, not an execution signal.",
            "system": f"The current {market.market_regime.value.replace('_', ' ')} regime has a tightness score of {market.system_tightness_score:+.2f}.",
            "portfolio": f"The portfolio is {abs(portfolio.exposure_before_action_mwh):.1f} MWh {'long' if portfolio.exposure_before_action_mwh >= 0 else 'short'} before action against Q of {portfolio.current_q_mwh:.1f} MWh.",
            "battery": f"Current SoC is {portfolio.current_soc_mwh:.1f} MWh; projected and realised SAMPLE state are shown separately when a prior run exists.",
        }

    def context_risk_measures(self, production, market, forecast: list[ForecastVintageChartPoint]) -> list[RiskMeasure]:
        history = self.history[-721:]
        spread = market.best_ask_gbp_per_mwh - market.best_bid_gbp_per_mwh
        depth = market.bid_depth_mwh + market.ask_depth_mwh
        residual = production.residual_demand_mw
        forecast_error_mwh = production.renewable_production_mw / 2 - forecast[0].latest_p50_mwh
        largest_revision = max(abs(item.delta_mwh) for item in forecast)
        return [
            RiskMeasure(key="forecast_error", label="Forecast error versus simulated actual", value=round(forecast_error_mwh, 2), unit="MWh", status="RISK" if abs(forecast_error_mwh) > 10 else "INFO"),
            RiskMeasure(key="forecast_revision", label="Largest forecast revision", value=round(largest_revision, 2), unit="MWh", status="RISK" if largest_revision > 8 else "INFO"),
            RiskMeasure(key="price_percentile_30d", label="Price percentile versus 30 days", value=self._percentile(market.reference_price_gbp_per_mwh, [item.reference_price_gbp_per_mwh for item in history]), unit="percentile", status="INFO"),
            RiskMeasure(key="spread_percentile_30d", label="Spread percentile versus 30 days", value=self._percentile(spread, [item.best_ask_gbp_per_mwh - item.best_bid_gbp_per_mwh for item in history]), unit="percentile", status="INFO"),
            RiskMeasure(key="depth_percentile_30d", label="Depth percentile versus 30 days", value=self._percentile(depth, [item.bid_depth_mwh + item.ask_depth_mwh for item in history]), unit="percentile", status="INFO"),
            RiskMeasure(key="residual_demand_percentile_30d", label="Residual demand percentile versus 30 days", value=self._percentile(residual, [item.residual_demand_mw for item in history]), unit="percentile", status="INFO"),
        ]

    def optimisation_context_series(self, run: OptimisationRun) -> dict[str, list[ChartSeries]]:
        history = self.history[-721:]
        historical_soc = ChartSeries(
            key="historical_soc", label="Historical SoC", unit="MWh", region="historical",
            points=[ChartPoint(label=item.observed_at.strftime("%d %b %H:%M"), timestamp=item.observed_at, value=item.soc_mwh) for item in history],
        )
        future_soc = ChartSeries(
            key="projected_soc", label="Projected SoC", unit="MWh", region="future",
            flat_explanation=(
                "SoC flat because the optimiser did not use the battery in this run."
                if all(abs(item.charge_mw) < 1e-7 and abs(item.discharge_mw) < 1e-7 for item in run.projected_trajectory)
                else None
            ),
            points=[ChartPoint(label=f"SP{item.settlement_period}", timestamp=item.delivery_start, settlement_period=item.settlement_period, delivery_period=item.delivery_period, value=item.projected_soc_mwh) for item in run.projected_trajectory],
        )
        current_soc = ChartSeries(
            key="current_soc", label="Current SoC", unit="MWh", kind="marker", region="current",
            points=[ChartPoint(label="Current", timestamp=run.as_of, value=run.starting_state.starting_soc_mwh)],
        )
        return {"soc_context": [historical_soc, current_soc, future_soc]}

    def populate_auction_window(self, run: OptimisationRun) -> OptimisationRun:
        """Attach chart-ready 15:00 UK auction-window paths to a solved future run."""
        previous_auction, next_auction = daily_auction_boundaries(run.as_of)
        visual_periods = auction_window_periods(run.as_of)
        solved = {item.delivery_period: item for item in run.projected_trajectory}
        history = self.history[-721:]
        battery: list[BatteryPathPoint] = []
        position: list[PositionPathPoint] = []
        market: list[MarketExecutionPathPoint] = []
        risk: list[RiskValuePathPoint] = []
        eta_c = eta_d = 0.95
        charge_max = discharge_max = 50.0
        e_min, e_max = 10.0, 100.0
        terminal_target, terminal_minimum = 55.0, 35.0

        def nearest(instant: datetime) -> LiveHistoryPoint:
            return min(history, key=lambda item: abs((item.observed_at - instant).total_seconds()))

        for period in visual_periods:
            result = solved.get(period.label)
            if result is not None:
                phase = "current" if period.start_utc <= run.as_of < period.end_utc else "optimised_future"
                q_after = result.q_before_action_mwh + result.sell_mwh - result.buy_mwh
                battery.append(BatteryPathPoint(
                    settlement_period=period.settlement_period, delivery_period=period.label,
                    timestamp=period.start_utc, delivery_end=period.end_utc, phase=phase,
                    charge_mw=result.charge_mw, charge_mwh=round(result.charge_mw * period.duration_hours, 4),
                    discharge_mw=result.discharge_mw, discharge_mwh=round(result.discharge_mw * period.duration_hours, 4),
                    soc_start_mwh=result.soc_before_mwh, soc_end_mwh=result.projected_soc_mwh,
                    reserve_up_mw=result.reserve_up_mw, reserve_down_mw=result.reserve_down_mw,
                    upward_headroom_mw=result.upward_headroom_mw, downward_headroom_mw=result.downward_headroom_mw,
                    upward_duration_coverage_h=result.upward_duration_coverage_h,
                    downward_duration_coverage_h=result.downward_duration_coverage_h,
                    soc_min_mwh=e_min, soc_max_mwh=e_max,
                    terminal_soc_target_mwh=terminal_target, terminal_soc_minimum_mwh=terminal_minimum,
                    binding_constraints=result.binding_constraints,
                ))
                position.append(PositionPathPoint(
                    settlement_period=period.settlement_period, delivery_period=period.label,
                    timestamp=period.start_utc, delivery_end=period.end_utc, phase=phase,
                    generation_p10_mwh=result.generation_p10_mwh, generation_p50_mwh=result.generation_p50_mwh,
                    generation_p90_mwh=result.generation_p90_mwh, demand_mw=result.demand_mw,
                    residual_demand_mw=result.residual_demand_mw,
                    q_before_mwh=result.q_before_action_mwh, buy_mwh=result.buy_mwh, sell_mwh=result.sell_mwh,
                    q_after_mwh=round(q_after, 4),
                    exposure_before_p10_mwh=result.exposure_before_p10_mwh,
                    exposure_before_p50_mwh=result.exposure_before_p50_mwh,
                    exposure_before_p90_mwh=result.exposure_before_p90_mwh,
                    residual_p10_mwh=result.residual_p10_mwh, residual_p50_mwh=result.residual_p50_mwh,
                    residual_p90_mwh=result.residual_p90_mwh, market_action_allowed=result.tradeable,
                    gate_closure_at=result.gate_closure_at,
                    gate_closure_status="OPEN" if result.tradeable else "GATE_CLOSED",
                    binding_constraints=result.binding_constraints, one_line_reason=result.why_action,
                ))
                market.append(MarketExecutionPathPoint(
                    settlement_period=period.settlement_period, delivery_period=period.label,
                    timestamp=period.start_utc, phase=phase,
                    bid_price_gbp_per_mwh=result.best_bid_gbp_per_mwh,
                    ask_price_gbp_per_mwh=result.best_ask_gbp_per_mwh,
                    wap_used_gbp_per_mwh=result.market_wap_gbp_per_mwh,
                    spread_gbp_per_mwh=round(result.best_ask_gbp_per_mwh - result.best_bid_gbp_per_mwh, 4),
                    bid_depth_mwh=result.bid_depth_mwh, ask_depth_mwh=result.ask_depth_mwh,
                    consumed_bid_depth_mwh=result.sell_mwh, consumed_ask_depth_mwh=result.buy_mwh,
                    unfilled_volume_mwh=result.unfilled_market_volume_mwh,
                    executable_data_mode=SourceMode.SAMPLE,
                    reference_price_gbp_per_mwh=result.reference_price_gbp_per_mwh,
                    reference_price_mode=SourceMode.SAMPLE,
                    gate_closure_at=result.gate_closure_at, market_action_allowed=result.tradeable,
                ))
                maximum_optionality = 0.55 * (charge_max + discharge_max) * period.duration_hours
                risk.append(RiskValuePathPoint(
                    settlement_period=period.settlement_period, delivery_period=period.label,
                    timestamp=period.start_utc, phase=phase,
                    market_value_or_cost_gbp=result.market_execution_value_gbp,
                    imbalance_cost_gbp=result.imbalance_expected_cost_gbp,
                    tail_risk_penalty_gbp=result.tail_risk_penalty_gbp,
                    degradation_cost_gbp=result.degradation_cost_gbp,
                    terminal_soc_value_gbp=result.terminal_soc_contribution_gbp,
                    reserve_bm_service_value_gbp=result.reserve_bm_service_value_gbp,
                    optionality_lost_gbp=round(max(0.0, maximum_optionality - result.optionality_preservation_value_gbp), 2),
                    total_period_contribution_gbp=result.total_period_contribution_gbp,
                    worst_case_residual_mwh=round(max(abs(result.residual_p10_mwh), abs(result.residual_p90_mwh)), 4),
                    binding_constraint_count=len(result.binding_constraints),
                ))
                continue

            sample = nearest(min(period.end_utc, run.as_of))
            previous_sample = nearest(period.start_utc - PERIOD)
            phase = "current" if period.start_utc <= run.as_of < period.end_utc else "historical"
            soc_start = previous_sample.soc_mwh
            soc_end = run.starting_state.starting_soc_mwh if phase == "current" else sample.soc_mwh
            soc_delta = soc_end - soc_start
            charge = min(charge_max, max(0.0, soc_delta / (eta_c * period.duration_hours)))
            discharge = min(discharge_max, max(0.0, -soc_delta * eta_d / period.duration_hours))
            net_export = discharge - charge
            reserve_up, reserve_down = sample.reserve_up_mw, sample.reserve_down_mw
            up_headroom = discharge_max - net_export
            down_headroom = charge_max + net_export
            up_duration = (soc_start - e_min) * eta_d / max(reserve_up, 1e-9)
            down_duration = (e_max - soc_start) / (eta_c * max(reserve_down, 1e-9))
            p50 = sample.forecast_p50_mw * period.duration_hours
            p10, p90 = max(0.0, p50 - 8.0), p50 + 8.0
            q_before = sample.q_mwh
            residual_p10, residual_p50, residual_p90 = p10 - q_before, p50 - q_before, p90 - q_before
            historical_reason = (
                "Current SAMPLE state at NOW; market actions are not back-filled."
                if phase == "current" else "Historical SAMPLE state; no optimiser action is attributed retrospectively."
            )
            battery.append(BatteryPathPoint(
                settlement_period=period.settlement_period, delivery_period=period.label,
                timestamp=period.start_utc, delivery_end=period.end_utc, phase=phase,
                charge_mw=round(charge, 4), charge_mwh=round(charge * period.duration_hours, 4),
                discharge_mw=round(discharge, 4), discharge_mwh=round(discharge * period.duration_hours, 4),
                soc_start_mwh=round(soc_start, 4), soc_end_mwh=round(soc_end, 4),
                reserve_up_mw=reserve_up, reserve_down_mw=reserve_down,
                upward_headroom_mw=round(up_headroom, 4), downward_headroom_mw=round(down_headroom, 4),
                upward_duration_coverage_h=round(up_duration, 4), downward_duration_coverage_h=round(down_duration, 4),
                soc_min_mwh=e_min, soc_max_mwh=e_max,
                terminal_soc_target_mwh=terminal_target, terminal_soc_minimum_mwh=terminal_minimum,
            ))
            gate_at = period.start_utc - timedelta(minutes=5)
            position.append(PositionPathPoint(
                settlement_period=period.settlement_period, delivery_period=period.label,
                timestamp=period.start_utc, delivery_end=period.end_utc, phase=phase,
                generation_p10_mwh=round(p10, 4), generation_p50_mwh=round(p50, 4), generation_p90_mwh=round(p90, 4),
                demand_mw=sample.demand_mw, residual_demand_mw=sample.residual_demand_mw,
                q_before_mwh=q_before, buy_mwh=0.0, sell_mwh=0.0, q_after_mwh=q_before,
                exposure_before_p10_mwh=round(residual_p10, 4), exposure_before_p50_mwh=round(residual_p50, 4),
                exposure_before_p90_mwh=round(residual_p90, 4), residual_p10_mwh=round(residual_p10, 4),
                residual_p50_mwh=round(residual_p50, 4), residual_p90_mwh=round(residual_p90, 4),
                market_action_allowed=False, gate_closure_at=gate_at,
                gate_closure_status="CURRENT_STATE" if phase == "current" else "HISTORICAL",
                one_line_reason=historical_reason,
            ))
            market.append(MarketExecutionPathPoint(
                settlement_period=period.settlement_period, delivery_period=period.label,
                timestamp=period.start_utc, phase=phase,
                bid_price_gbp_per_mwh=sample.best_bid_gbp_per_mwh,
                ask_price_gbp_per_mwh=sample.best_ask_gbp_per_mwh,
                spread_gbp_per_mwh=round(sample.best_ask_gbp_per_mwh - sample.best_bid_gbp_per_mwh, 4),
                bid_depth_mwh=sample.bid_depth_mwh, ask_depth_mwh=sample.ask_depth_mwh,
                consumed_bid_depth_mwh=0.0, consumed_ask_depth_mwh=0.0, unfilled_volume_mwh=0.0,
                executable_data_mode=SourceMode.SAMPLE,
                reference_price_gbp_per_mwh=sample.reference_price_gbp_per_mwh,
                reference_price_mode=SourceMode.SAMPLE,
                gate_closure_at=gate_at, market_action_allowed=False,
            ))
            imbalance = abs(residual_p50) * 4.0
            tail = max(abs(residual_p10), abs(residual_p90)) * 1.5
            risk.append(RiskValuePathPoint(
                settlement_period=period.settlement_period, delivery_period=period.label,
                timestamp=period.start_utc, phase=phase, market_value_or_cost_gbp=0.0,
                imbalance_cost_gbp=round(imbalance, 2), tail_risk_penalty_gbp=round(tail, 2),
                degradation_cost_gbp=round(4.0 * (charge + discharge) * period.duration_hours, 2),
                terminal_soc_value_gbp=0.0, reserve_bm_service_value_gbp=0.0,
                optionality_lost_gbp=0.0, total_period_contribution_gbp=round(-imbalance - tail, 2),
                worst_case_residual_mwh=round(max(abs(residual_p10), abs(residual_p90)), 4),
                binding_constraint_count=0,
            ))

        future_battery = [item for item in battery if item.phase == "optimised_future"]
        if future_battery:
            soc_move = future_battery[-1].soc_end_mwh - future_battery[0].soc_start_mwh
            dispatch = sum(item.charge_mwh + item.discharge_mwh for item in future_battery)
            reserve_span = max(item.reserve_up_mw + item.reserve_down_mw for item in future_battery) - min(item.reserve_up_mw + item.reserve_down_mw for item in future_battery)
            headroom_span = max(item.upward_headroom_mw + item.downward_headroom_mw for item in future_battery) - min(item.upward_headroom_mw + item.downward_headroom_mw for item in future_battery)
            explanation_parts = []
            if dispatch < 1e-6:
                explanation_parts.append("SoC flat because optimiser chose not to dispatch the battery; reserve/BM value, headroom preservation or tail-risk penalty dominated.")
            elif abs(soc_move) < 2:
                explanation_parts.append(f"SoC movement is small: {soc_move:+.1f} MWh across the optimised window.")
            if reserve_span < 1e-6:
                explanation_parts.append("Reserve is flat because its marginal availability value and commitment requirement do not change by period.")
            if headroom_span < 1e-6:
                explanation_parts.append("Headroom is flat because battery dispatch and reserve allocation leave the same physical margin in every future period.")
            explanation = " ".join(explanation_parts)
            for item in future_battery:
                item.flat_path_explanation = explanation or None

        run.auction_boundary_time = "15:00 UK time"
        run.previous_auction_time = previous_auction
        run.next_auction_time = next_auction
        run.now_marker_time = run.as_of
        run.current_sp = settlement_period_for_instant(run.as_of).settlement_period
        run.visual_window_start = previous_auction
        run.visual_window_end = next_auction
        run.optimisation_window_start = run.as_of
        run.optimisation_window_end = next_auction
        run.number_of_sps_shown = len(visual_periods)
        run.number_of_sps_optimised = len(run.projected_trajectory)
        run.battery_path_series = battery
        run.position_path_series = position
        run.market_execution_series = market
        run.risk_value_series = risk
        run.interaction_points = [
            OptimisationInteractionPoint(
                stable_sp_id=point.delivery_period,
                delivery_period=point.delivery_period,
                settlement_period=point.settlement_period,
                display_label=f"SP{point.settlement_period}",
                uk_delivery_time=(
                    f"{point.timestamp.astimezone(LONDON).strftime('%d %b %H:%M')}–"
                    f"{point.delivery_end.astimezone(LONDON).strftime('%H:%M')} UK time"
                ),
                phase=point.phase,
                linked_trajectory_row_id=(
                    f"trajectory-{point.delivery_period.lower().replace(' ', '-')}"
                ),
                tooltip_payload={
                    "battery_reason": battery[index].flat_path_explanation or point.one_line_reason,
                    "position_reason": point.one_line_reason,
                    "gate_closure_status": point.gate_closure_status,
                    "trust_statement": "Explicit SAMPLE state; calculable but not trustworthy for live trading.",
                },
                annotation_payload=[
                    *point.binding_constraints,
                    *(["GATE_CLOSED"] if not point.market_action_allowed else []),
                ],
                source_mode=SourceMode.SAMPLE,
                source_provenance=[
                    "rolling_sample_environment",
                    *(["full_action_optimiser"] if point.phase in {"current", "optimised_future"} else []),
                ],
                explanation_text=point.one_line_reason,
            )
            for index, point in enumerate(position)
        ]
        run.whole_path_explanation = self._whole_path_explanation(run)
        return run

    @staticmethod
    def _whole_path_explanation(run: OptimisationRun) -> str:
        trajectory = run.projected_trajectory
        if not trajectory:
            return "No future settlement period remains before the next 15:00 UK auction boundary."
        buys = [item for item in trajectory if item.buy_mwh > 1e-5]
        sells = [item for item in trajectory if item.sell_mwh > 1e-5]
        battery_used = [item for item in trajectory if item.charge_mw > 1e-5 or item.discharge_mw > 1e-5]
        largest_tail = max(trajectory, key=lambda item: max(abs(item.residual_p10_mwh), abs(item.residual_p90_mwh)))
        bindings = sorted({binding for item in trajectory for binding in item.binding_constraints})
        trade_text = []
        if sells:
            trade_text.append(f"sells {sum(item.sell_mwh for item in sells):.1f} MWh across {', '.join(f'SP{item.settlement_period}' for item in sells[:6])} because P50 surplus can consume visible bid depth")
        if buys:
            trade_text.append(f"buys {sum(item.buy_mwh for item in buys):.1f} MWh across {', '.join(f'SP{item.settlement_period}' for item in buys[:6])} to reduce short exposure through visible ask depth")
        if not trade_text:
            trade_text.append("makes no intraday trade because execution cost exceeds the diagnostic value of reducing exposure")
        if battery_used:
            battery_text = f"Battery dispatch is used in {len(battery_used)} periods while minimum combined headroom remains {min(item.upward_headroom_mw + item.downward_headroom_mw for item in trajectory):.1f} MW."
        else:
            battery_text = "The battery is preserved because reserve/BM value, headroom and terminal SoC value dominate incremental dispatch value."
        binding_text = f" Binding constraints include {', '.join(bindings[:4])}." if bindings else " No physical constraint is binding materially."
        return (
            f"The optimiser {' and '.join(trade_text)}. {battery_text} "
            f"The largest remaining tail risk is SP{largest_tail.settlement_period} at "
            f"{max(abs(largest_tail.residual_p10_mwh), abs(largest_tail.residual_p90_mwh)):.1f} MWh.{binding_text}"
        )

    def mark_optimisation(self, run_id: str, occurred_at: datetime, run: OptimisationRun | None = None) -> None:
        self.latest_run_id = run_id
        if run and run.projected_trajectory:
            first = run.projected_trajectory[0]
            actions = []
            if first.buy_mwh > 1e-5: actions.append(f"buy {first.buy_mwh:.1f} MWh")
            if first.sell_mwh > 1e-5: actions.append(f"sell {first.sell_mwh:.1f} MWh")
            if first.charge_mw > 1e-5: actions.append(f"charge {first.charge_mw:.1f} MW")
            if first.discharge_mw > 1e-5: actions.append(f"discharge {first.discharge_mw:.1f} MW")
            self.optimisation_history.append(HistoricalOptimisationPoint(
                as_of=occurred_at, run_id=run_id, first_action=", ".join(actions) or "hold",
                starting_soc_mwh=run.starting_state.starting_soc_mwh,
                projected_soc_mwh=first.projected_soc_mwh,
                starting_q_mwh=run.starting_state.starting_q_mwh,
                buy_mwh=first.buy_mwh, sell_mwh=first.sell_mwh,
                diagnostic_value_gbp=run.objective_value_gbp,
            ))
            if self.live_state:
                self.live_state.optimisation_history = list(self.optimisation_history[-1500:])
        if self.live_state:
            self.live_state.state.latest_optimisation_run_id = run_id
            self.live_state.events.insert(0, RollingEvent(
                event_id=str(uuid4()), occurred_at=occurred_at, event_type="optimisation",
                message=f"Rolling optimisation run {run_id} completed",
                source_mode=SourceMode.SAMPLE, quality=Quality.FRESH, step=self.step,
            ))

    def _build_periods(self, as_of, snapshot_id):
        forecast_shift, demand_shift, regime_tightness, depth_factor = REGIME_BIAS[self.regime]
        recent_history = self.history[-12:]
        recent_forecast_error_mwh = (
            sum(point.forecast_error_mw for point in recent_history) / len(recent_history) / 2
            if recent_history else 0.0
        )
        recent_demand_surprise_mw = (
            sum(point.demand_surprise_mw for point in recent_history) / len(recent_history)
            if recent_history else 0.0
        )
        periods = []
        future = self._horizon_periods(as_of)
        for index, period in enumerate(future):
            phase = self.step + index + self.refresh_sequence * 0.19
            base_generation = 71 + 10 * math.sin((phase + 1) / 2.25) + 3 * math.cos(phase / 3.4)
            miss_ramp = -3.0 * index if self.regime == SampleRegime.WIND_FORECAST_MISS else 0.0
            history_decay = math.exp(-index / 5.0)
            p50 = max(12.0, base_generation + forecast_shift + miss_ramp + 0.35 * recent_forecast_error_mwh * history_decay)
            uncertainty = 12.0 + 0.8 * index + (6.0 if self.regime in {SampleRegime.WIND_FORECAST_MISS, SampleRegime.TIGHTENING} else 0.0)
            p10, p90 = max(0.0, p50 - uncertainty), p50 + uncertainty
            previous_p50 = p50 - (2.4 * math.sin(phase + 0.6) + forecast_shift * 0.12)
            demand = 27800 + 750 * math.sin((phase + 2) / 3.1) + demand_shift + 0.25 * recent_demand_surprise_mw * history_decay
            demand_surprise = demand_shift + 90 * math.sin(phase / 1.3)
            production_surprise = forecast_shift * 2 + 12 * math.cos(phase / 1.5)
            residual_demand = demand - p50 * 2
            confidence = max(0.35, min(0.96, 0.92 - uncertainty / 100 - abs(production_surprise) / 220))
            driver = self.regime.value if self.regime != SampleRegime.NORMAL else (
                "recent_forecast_error" if abs(recent_forecast_error_mwh) > 1.0 else ("normal" if index < 2 else "diurnal_shape")
            )
            tightness = max(-1.5, min(1.8, regime_tightness + 0.22 * math.sin(phase / 1.7) + (demand - 27800) / 4200 - (p50 - 71) / 70))
            gate_at = period.start_utc - timedelta(minutes=5)
            minutes_to_gate = max(0, (gate_at - as_of).total_seconds() / 60)
            urgency = max(0.0, 1.0 - minutes_to_gate / 180)
            price_spike = 55.0 * math.exp(-index / 1.6) if self.regime == SampleRegime.PRICE_SPIKE else 0.0
            reference = 71 + index * 0.9 + 19 * tightness + 3.5 * urgency + price_spike
            spread = 1.2 + 0.5 * abs(tightness) + 0.7 * urgency
            bids, asks = [], []
            for level in range(1, 6):
                base_volume = (3.0 + 0.9 * level + 0.7 * ((index + level + self.step) % 3)) * depth_factor
                bid_volume = max(0.4, base_volume * (1.0 + 0.08 * max(-tightness, 0)))
                ask_volume = max(0.4, base_volume * 0.92 * (1.0 + 0.08 * max(tightness, 0)))
                bid_price = reference - spread / 2 - 0.65 * (level - 1)
                ask_price = reference + spread / 2 + 0.72 * (level - 1)
                bids.append(self._book_level(snapshot_id, as_of, period.label, period.start_utc, "BID", level, bid_price, bid_volume))
                asks.append(self._book_level(snapshot_id, as_of, period.label, period.start_utc, "ASK", level, ask_price, ask_volume))
            q = self.q_by_period.setdefault(period.label, self._base_q(self.step + index))
            frequency = 50.0 - 0.028 * tightness + 0.006 * math.sin(phase * 1.9)
            values = {
                "generation_p10_mwh": self._point(snapshot_id, as_of, "wind_p10", p10, "MWh", SemanticKind.FORECAST, period.label, period.start_utc, previous_value=max(0, previous_p50 - uncertainty)),
                "generation_p50_mwh": self._point(snapshot_id, as_of, "wind_p50", p50, "MWh", SemanticKind.FORECAST, period.label, period.start_utc, previous_value=previous_p50),
                "generation_p90_mwh": self._point(snapshot_id, as_of, "wind_p90", p90, "MWh", SemanticKind.FORECAST, period.label, period.start_utc, previous_value=previous_p50 + uncertainty),
                "previous_p50_mwh": self._point(snapshot_id, as_of, "wind_previous_p50", previous_p50, "MWh", SemanticKind.FORECAST, period.label, period.start_utc),
                "demand_mw": self._point(snapshot_id, as_of, "system_demand", demand, "MW", SemanticKind.ESTIMATE, period.label, period.start_utc),
                "system_tightness_score": self._point(snapshot_id, as_of, "system_tightness", tightness, "score", SemanticKind.ESTIMATE, period.label, period.start_utc),
                "reference_price_gbp_per_mwh": self._point(snapshot_id, as_of, "market_reference_price", reference, "GBP/MWh", SemanticKind.ESTIMATE, period.label, period.start_utc),
                "contracted_q_mwh": self._point(snapshot_id, as_of, "contracted_position_q", q, "MWh", SemanticKind.ASSUMPTION, period.label, period.start_utc),
                "frequency_hz": self._point(snapshot_id, as_of, "gb_system_frequency", frequency, "Hz", SemanticKind.OBSERVATION, period.label, period.start_utc),
            }
            periods.append(OptimisationPeriodInput(
                settlement_period=period.settlement_period, delivery_period=period.label,
                delivery_start=period.start_utc, delivery_end=period.end_utc,
                duration_hours=period.duration_hours,
                generation_p10_mwh=round(p10, 3), generation_p50_mwh=round(p50, 3), generation_p90_mwh=round(p90, 3),
                demand_mw=round(demand, 2), system_tightness_score=round(tightness, 4),
                reference_price_gbp_per_mwh=round(reference, 3), contracted_q_mwh=round(q, 3),
                bids=bids, asks=asks, gate_closure_at=gate_at, tradeable=as_of < gate_at,
                upward_commitment_mw=8.0, downward_commitment_mw=5.0, values=values,
                residual_demand_mw=round(residual_demand, 2),
                previous_p50_mwh=round(previous_p50, 3),
                forecast_confidence_score=round(confidence, 4),
                forecast_driver=driver,
                demand_surprise_mw=round(demand_surprise, 2),
                production_surprise_mw=round(production_surprise, 2),
            ))
        return periods

    def _horizon_periods(self, as_of: datetime):
        """Choose actionable/current periods; daily 15:00 UK is the primary horizon."""
        if self.horizon_mode == HorizonMode.NEXT_AUCTION:
            _, next_auction = daily_auction_boundaries(as_of)
            return [period for period in auction_window_periods(as_of) if period.end_utc > as_of and period.start_utc < next_auction]
        first_candidates = upcoming_periods(as_of + PERIOD, 52)
        if self.horizon_mode == HorizonMode.END_OF_DAY:
            delivery_day = first_candidates[0].settlement_date
            selected = [period for period in first_candidates if period.settlement_date == delivery_day]
            return selected or first_candidates[: self.horizon]
        return first_candidates[: self.horizon]

    def _cockpit_snapshot(self, snapshot_id, as_of, periods, rolling_values):
        values = []
        for period in periods:
            values.extend(period.values.values())
            values.extend(point for level in [*period.bids, *period.asks] for point in (level.price_value, level.volume_value))
            for metric, value, unit in (
                ("wind_day_ahead_p50", period.generation_p50_mwh + 4.0, "MWh"),
                ("wind_model_disagreement", abs(period.generation_p90_mwh - period.generation_p10_mwh) / 4, "MWh"),
                ("wind_reliability_score", max(0.55, 0.9 - abs(period.system_tightness_score) * 0.08), "score"),
            ):
                values.append(self._point(snapshot_id, as_of, metric, value, unit, SemanticKind.FORECAST, period.delivery_period, period.delivery_start))
        values.extend(self._config_points(snapshot_id, as_of))
        unique = list({point.value_id: point for point in [*values, *rolling_values]}.values())
        digest = hashlib.sha256("|".join(sorted(point.value_id for point in unique)).encode()).hexdigest()
        return CockpitSnapshot(
            snapshot_id=snapshot_id, as_of=as_of, input_hash=digest,
            status=SnapshotStatus.DEGRADED,
            readiness=SnapshotReadiness(status=SnapshotStatus.DEGRADED, reasons=["Current rolling state is explicit SAMPLE data."]),
            optimiser_readiness=OptimiserReadiness(status=OptimiserStatus.DEGRADED, allowed=True, reasons=["SAMPLE inputs are calculable but not live-trading trustworthy."]),
            feeds_included=["rolling_sample_environment"], feeds_excluded=["market_intraday"],
            stale_feeds=[], missing_feeds=["market_intraday"], values=unique,
        )

    def _config_points(self, snapshot_id, as_of):
        definitions = (
            ("battery_e_min", 10.0, "MWh"), ("battery_e_max", 100.0, "MWh"),
            ("battery_charge_power_max", 20.0, "MW"), ("battery_discharge_power_max", 20.0, "MW"),
            ("battery_charge_efficiency", 0.94, "ratio"), ("battery_discharge_efficiency", 0.92, "ratio"),
            ("battery_reserve_duration", 1.0, "h"), ("battery_terminal_soc_target", 55.0, "MWh"),
            ("battery_degradation_cost", 4.0, "GBP/MWh"), ("battery_terminal_soc_penalty", 1.5, "GBP/MWh"),
            ("battery_future_flexibility_penalty", 2.5, "GBP/MWh"),
            ("upward_service_commitment", 8.0, "MW"), ("downward_service_commitment", 5.0, "MW"),
            ("service_required_duration", 1.0, "h"),
            ("bm_acceptance_probability", 0.35, "probability"), ("bm_expected_activation_duration", 0.25, "h"),
            ("bm_expected_margin", 78.0, "GBP/MWh"), ("bm_non_delivery_penalty", 140.0, "GBP/MWh"),
            ("service_availability_fee", 6.5, "GBP/MW/h"), ("service_activation_probability", 0.18, "probability"),
            ("service_expected_activation_duration", 0.25, "h"), ("service_expected_margin", 52.0, "GBP/MWh"),
            ("service_non_delivery_penalty", 175.0, "GBP/MWh"),
        )
        return [self._point(snapshot_id, as_of, metric, value, unit, SemanticKind.ASSUMPTION) for metric, value, unit in definitions]

    def _point(self, snapshot_id, as_of, metric, value, unit, kind, delivery_period=None, delivery_start=None, previous_value=None):
        identifier = uuid5(NAMESPACE_URL, f"{snapshot_id}:{metric}:{delivery_period}:{float(value):.8f}")
        delta = float(value) - float(previous_value) if previous_value is not None else None
        return CanonicalDataPoint(
            value_id=str(identifier), metric=metric, value=round(float(value), 6), unit=unit,
            delivery_period=delivery_period, delivery_start=delivery_start,
            lineage=DataLineage(
                source_feed="rolling_sample_environment", source_mode=SourceMode.SAMPLE,
                semantic_kind=kind, quality=Quality.FRESH, published_at=as_of,
                retrieved_at=as_of, normalised_at=as_of, raw_field_name=metric,
                transformations=["deterministic rolling SAMPLE regime model", "value evolves with time step, refresh sequence and selected regime"],
                validation_checks=[ValidationCheck(name="finite", passed=True, detail="finite SAMPLE value")],
                warnings=["Explicit SAMPLE simulation value; not live trading data."],
            ),
            previous_value=round(float(previous_value), 6) if previous_value is not None else None,
            delta_vs_previous=round(delta, 6) if delta is not None else None,
            included_in_current_snapshot=True, snapshot_id=snapshot_id,
        )

    def _book_level(self, snapshot_id, as_of, delivery_period, delivery_start, side, level, price, volume):
        prefix = side.lower()
        return RollingOrderBookLevel(
            side=side, level=level, price_gbp_per_mwh=round(price, 3), volume_mwh=round(volume, 3),
            price_value=self._point(snapshot_id, as_of, f"market_{prefix}_price_l{level}", price, "GBP/MWh", SemanticKind.OBSERVATION, delivery_period, delivery_start),
            volume_value=self._point(snapshot_id, as_of, f"market_{prefix}_volume_l{level}", volume, "MWh", SemanticKind.OBSERVATION, delivery_period, delivery_start),
        )

    @staticmethod
    def _wap(period, volume, side):
        levels = period.bids if side == "SELL" else period.asks
        result = executable_price(levels, volume, side, len(levels))
        return round(result.wap_gbp_per_mwh, 3) if result.wap_gbp_per_mwh is not None else None

    @staticmethod
    def _base_q(index):
        return round(68 + 2.5 * math.cos(index / 1.8), 3)

    def _emit(self, occurred_at, event_type, message, point):
        self.events.append(RollingEvent(
            event_id=str(uuid4()), occurred_at=occurred_at, event_type=event_type,
            message=message, source_mode=SourceMode.SAMPLE, quality=Quality.FRESH,
            step=self.step, value_id=point.value_id if point else None,
        ))
