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
    CockpitSnapshot,
    DataLineage,
    ChartPoint,
    ChartSeries,
    ForecastVintageChartPoint,
    HorizonMode,
    LiveStateSnapshot,
    LiveHistoryPoint,
    OptimisationRun,
    OptimisationPeriodInput,
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
    SampleRegime,
    SemanticKind,
    SnapshotReadiness,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.settlement import PERIOD, UTC, settlement_period_for_instant, upcoming_periods


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
        self.horizon_mode = HorizonMode.NEXT_8_PERIODS
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
            effective_horizon_mode=(HorizonMode.NEXT_8_PERIODS if self.horizon_mode == HorizonMode.NEXT_AUCTION else self.horizon_mode),
            optimisation_horizon_start=periods[0].delivery_start,
            optimisation_horizon_end=periods[-1].delivery_end,
            horizon_warning=(
                "No intraday-auction calendar is configured. Explicit next-auction selection is using the next 8 settlement periods."
                if self.horizon_mode == HorizonMode.NEXT_AUCTION else None
            ),
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
            history=self.history[-96:],
            forecast_vintage_series=forecast_series,
            chart_series=chart_series,
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
            for offset in range(-23, 0):
                phase = offset / 3.0
                production_factor = 1 + 0.055 * math.sin(phase)
                demand_delta = 135 * math.cos(phase / 1.7)
                price_delta = 2.8 * math.sin(phase / 1.4)
                tightness_delta = 0.12 * math.cos(phase)
                historical_production = production.renewable_production_mw * production_factor
                historical_demand = production.demand_mw + demand_delta
                forecast_mw = historical_production - 5.5 * math.sin(phase * 1.3)
                bid = market.best_bid_gbp_per_mwh + price_delta
                ask = market.best_ask_gbp_per_mwh + price_delta
                self.history.append(LiveHistoryPoint(
                    observed_at=as_of + timedelta(minutes=offset * 5),
                    renewable_production_mw=round(historical_production, 3),
                    wind_mw=round(historical_production * 0.86, 3),
                    solar_mw=round(historical_production * 0.14, 3),
                    demand_mw=round(historical_demand, 3),
                    residual_demand_mw=round(historical_demand - historical_production, 3),
                    forecast_p50_mw=round(forecast_mw, 3),
                    forecast_error_mw=round(historical_production - forecast_mw, 3),
                    frequency_hz=round(market.frequency_hz - tightness_delta * 0.025, 6),
                    reference_price_gbp_per_mwh=round(market.reference_price_gbp_per_mwh + price_delta, 3),
                    best_bid_gbp_per_mwh=round(bid, 3), best_ask_gbp_per_mwh=round(ask, 3),
                    bid_depth_mwh=round(market.bid_depth_mwh * (1 + 0.08 * math.cos(phase)), 3),
                    ask_depth_mwh=round(market.ask_depth_mwh * (1 - 0.07 * math.sin(phase)), 3),
                    q_mwh=round(portfolio.current_q_mwh + 0.4 * math.sin(phase), 3),
                    exposure_mwh=round(first.generation_p50_mwh - portfolio.current_q_mwh + 0.7 * math.cos(phase), 3),
                    soc_mwh=round(self.current_soc_mwh + 0.2 * math.sin(phase / 2), 3),
                    previous_projected_soc_mwh=self.previous_projected_soc_mwh,
                    reserve_up_mw=portfolio.reserve_up_held_mw,
                    reserve_down_mw=portfolio.reserve_down_held_mw,
                    system_tightness_score=round(market.system_tightness_score + tightness_delta, 4),
                    demand_surprise_mw=round(demand_delta, 3),
                    production_surprise_mw=round(historical_production - forecast_mw, 3),
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
        ))
        self.history = self.history[-96:]

    def _live_chart_series(self, forecast: list[ForecastVintageChartPoint]) -> dict[str, list[ChartSeries]]:
        history = self.history[-48:]
        def historical(key: str, label: str, unit: str, attr: str, kind: str = "line") -> ChartSeries:
            return ChartSeries(key=key, label=label, unit=unit, kind=kind, points=[
                ChartPoint(label=item.observed_at.strftime("%H:%M"), timestamp=item.observed_at, value=float(getattr(item, attr)))
                for item in history
            ])
        def vintage(key: str, label: str, attr: str, kind: str = "line") -> ChartSeries:
            return ChartSeries(key=key, label=label, unit="MWh", kind=kind, points=[
                ChartPoint(label=f"SP{item.settlement_period}", timestamp=item.delivery_start, settlement_period=item.settlement_period, delivery_period=item.delivery_period, value=float(getattr(item, attr)))
                for item in forecast
            ])
        return {
            "production": [historical("production", "Production", "MW", "renewable_production_mw"), historical("wind", "Wind", "MW", "wind_mw"), historical("solar", "Solar", "MW", "solar_mw"), historical("forecast_actual", "Forecast P50", "MW", "forecast_p50_mw")],
            "demand": [historical("demand", "Demand", "MW", "demand_mw"), historical("residual_demand", "Residual demand", "MW", "residual_demand_mw")],
            "forecast_vintage": [vintage("previous_p50", "Previous P50", "previous_p50_mwh"), vintage("latest_p50", "Latest P50", "latest_p50_mwh"), vintage("p10", "P10", "p10_mwh"), vintage("p90", "P90", "p90_mwh"), vintage("delta", "Vintage delta", "delta_mwh", "bar")],
            "market_price": [
                historical("reference", "Reference", "GBP/MWh", "reference_price_gbp_per_mwh"),
                historical("best_bid", "Best bid", "GBP/MWh", "best_bid_gbp_per_mwh"),
                historical("best_ask", "Best ask", "GBP/MWh", "best_ask_gbp_per_mwh"),
                ChartSeries(key="sell_wap_10", label="Sell WAP 10", unit="GBP/MWh", points=[ChartPoint(label=item.observed_at.strftime("%H:%M"), timestamp=item.observed_at, value=round(item.best_bid_gbp_per_mwh - 0.35, 4)) for item in history]),
                ChartSeries(key="buy_wap_10", label="Buy WAP 10", unit="GBP/MWh", points=[ChartPoint(label=item.observed_at.strftime("%H:%M"), timestamp=item.observed_at, value=round(item.best_ask_gbp_per_mwh + 0.42, 4)) for item in history]),
            ],
            "market_depth": [historical("bid_depth", "Bid depth", "MWh", "bid_depth_mwh", "bar"), historical("ask_depth", "Ask depth", "MWh", "ask_depth_mwh", "bar")],
            "frequency": [historical("frequency", "Frequency", "Hz", "frequency_hz")],
            "system": [historical("tightness", "Tightness", "score", "system_tightness_score"), historical("demand_surprise", "Demand surprise", "MW", "demand_surprise_mw"), historical("production_surprise", "Production surprise", "MW", "production_surprise_mw")],
            "portfolio": [historical("q", "Q", "MWh", "q_mwh"), historical("exposure", "Exposure", "MWh", "exposure_mwh")],
            "battery": [
                historical("soc", "Current SoC", "MWh", "soc_mwh"),
                ChartSeries(key="previous_projected_soc", label="Previous projected SoC", unit="MWh", points=[
                    ChartPoint(label=item.observed_at.strftime("%H:%M"), timestamp=item.observed_at, value=float(item.previous_projected_soc_mwh if item.previous_projected_soc_mwh is not None else item.soc_mwh))
                    for item in history
                ]),
                historical("reserve_up", "Reserve up", "MW", "reserve_up_mw"),
                historical("reserve_down", "Reserve down", "MW", "reserve_down_mw"),
            ],
        }

    def mark_optimisation(self, run_id: str, occurred_at: datetime) -> None:
        self.latest_run_id = run_id
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
        """Start at the next deliverable SP and choose a backend-owned horizon."""
        first_candidates = upcoming_periods(as_of + PERIOD, 52)
        effective = HorizonMode.NEXT_8_PERIODS if self.horizon_mode == HorizonMode.NEXT_AUCTION else self.horizon_mode
        if effective == HorizonMode.END_OF_DAY:
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
