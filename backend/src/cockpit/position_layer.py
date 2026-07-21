"""Pre-action portfolio exposure diagnostics: I[t,s] = G[t,s] - Q[t]."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cockpit.forecast_layer import ForecastLayerResult, build_forecast_layer, derived_value
from cockpit.models import (
    CanonicalDataPoint,
    CockpitSnapshot,
    ForecastPositionPeriod,
    ForecastPositionSnapshot,
    PositionPoint,
    PositionReadiness,
    PositionVersion,
    Quality,
    ScenarioExposure,
    SemanticKind,
    SnapshotStatus,
    SourceMode,
)


FLAT_TOLERANCE_MWH = 0.05


@dataclass
class PositionLayerResult:
    snapshot: ForecastPositionSnapshot
    derived_values: list[CanonicalDataPoint]


def direction(exposure_mwh: float, tolerance_mwh: float = FLAT_TOLERANCE_MWH) -> str:
    if exposure_mwh > tolerance_mwh:
        return "LONG"
    if exposure_mwh < -tolerance_mwh:
        return "SHORT"
    return "FLAT"


def build_forecast_position(snapshot: CockpitSnapshot) -> PositionLayerResult:
    forecasts = build_forecast_layer(snapshot)
    q_by_period = {
        point.delivery_period: point
        for point in snapshot.values
        if point.metric == "contracted_position_q" and point.delivery_period
    }
    periods: list[ForecastPositionPeriod] = []
    derived_values: list[CanonicalDataPoint] = []
    warnings = list(forecasts.warnings)
    missing_q: list[str] = []

    for forecast in forecasts.points:
        q_value = q_by_period.get(forecast.delivery_period)
        if q_value is None or q_value.lineage.quality in (Quality.MISSING, Quality.INVALID):
            missing_q.append(forecast.delivery_period)
            warnings.append(f"{forecast.delivery_period}: contracted position Q_t is missing or invalid")
            continue
        position_warnings = list(q_value.lineage.warnings)
        position = PositionPoint(
            settlement_period=forecast.settlement_period,
            delivery_period=forecast.delivery_period,
            delivery_start=forecast.delivery_start,
            contracted_position=q_value,
            warnings=position_warnings,
        )
        exposures: list[ScenarioExposure] = []
        for scenario, generation in (
            ("P10", forecast.p10),
            ("P50", forecast.p50),
            ("P90", forecast.p90),
        ):
            residual = float(generation.value) - float(q_value.value)
            exposure_value = derived_value(
                snapshot,
                metric=f"residual_position_{scenario.lower()}",
                delivery_period=forecast.delivery_period,
                delivery_start=forecast.delivery_start,
                value=residual,
                unit="MWh",
                inputs=[generation, q_value],
                expression=f"I_t^{scenario} = G_t^{scenario} - Q_t",
            )
            derived_values.append(exposure_value)
            exposures.append(
                ScenarioExposure(
                    scenario=scenario,
                    generation_mwh=float(generation.value),
                    contracted_position_mwh=float(q_value.value),
                    residual_position_mwh=residual,
                    direction=direction(residual),
                    generation_value=generation,
                    exposure_value=exposure_value,
                )
            )
        p10_exposure, p50_exposure, p90_exposure = exposures
        period_warnings = list(
            dict.fromkeys(forecast.warnings + position_warnings + p50_exposure.exposure_value.lineage.warnings)
        )
        periods.append(
            ForecastPositionPeriod(
                settlement_period=forecast.settlement_period,
                delivery_period=forecast.delivery_period,
                delivery_start=forecast.delivery_start,
                delivery_end=forecast.delivery_end,
                forecast=forecast,
                position=position,
                exposures=exposures,
                base_case_direction=p50_exposure.direction,
                downside_exposure_mwh=p10_exposure.residual_position_mwh,
                upside_exposure_mwh=p90_exposure.residual_position_mwh,
                risk_magnitude_mwh=max(abs(item.residual_position_mwh) for item in exposures),
                explanation=_explain_period(
                    forecast,
                    float(q_value.value),
                    p10_exposure,
                    p50_exposure,
                    period_warnings,
                ),
                warnings=period_warnings,
            )
        )
        if forecast.delta.versus_previous_value:
            derived_values.append(forecast.delta.versus_previous_value)
        if forecast.delta.versus_day_ahead_value:
            derived_values.append(forecast.delta.versus_day_ahead_value)

    ranked = sorted(periods, key=lambda period: period.risk_magnitude_mwh, reverse=True)
    for rank, period in enumerate(ranked, start=1):
        period.risk_rank = rank
    periods.sort(key=lambda period: period.delivery_start)

    readiness = _readiness(forecasts, q_by_period, periods, missing_q)
    position_version = _position_version(q_by_period)
    input_hash = hashlib.sha256(
        f"{snapshot.input_hash}:forecast-position-v1".encode()
    ).hexdigest()
    fp_snapshot = ForecastPositionSnapshot(
        forecast_position_id=f"fp-{snapshot.snapshot_id}-{input_hash[:8]}",
        cockpit_snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        input_hash=input_hash,
        readiness=readiness,
        latest_vintage=forecasts.latest_vintage,
        previous_vintage=forecasts.previous_vintage,
        position_version=position_version,
        periods=periods,
        most_exposed_periods=[period.delivery_period for period in ranked[:3]],
        warnings=list(dict.fromkeys(warnings)),
    )
    return PositionLayerResult(snapshot=fp_snapshot, derived_values=derived_values)


def _readiness(
    forecasts: ForecastLayerResult,
    q_by_period: dict[str, CanonicalDataPoint],
    periods: list[ForecastPositionPeriod],
    missing_q: list[str],
) -> PositionReadiness:
    forecast_periods = {point.delivery_period for point in forecasts.points}
    position_periods = set(q_by_period)
    missing_forecast = sorted(position_periods - forecast_periods)
    if not forecasts.points or forecasts.missing_periods or missing_forecast:
        affected = sorted(set(forecasts.missing_periods + missing_forecast))
        reasons = ["Forecast P10/P50/P90 is missing or internally invalid"]
        if affected:
            reasons.append("Affected periods: " + ", ".join(affected))
        return PositionReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=reasons,
        )
    if not q_by_period or missing_q or len(periods) != len(forecasts.points):
        reasons = ["Contracted position Q_t is missing or invalid for one or more forecast periods"]
        if missing_q:
            reasons.append("Affected periods: " + ", ".join(missing_q))
        return PositionReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=reasons,
        )

    inputs = [
        point
        for period in periods
        for point in (
            period.forecast.p10,
            period.forecast.p50,
            period.forecast.p90,
            period.position.contracted_position,
        )
    ]
    modes = {point.lineage.source_mode for point in inputs}
    qualities = {point.lineage.quality for point in inputs}
    reasons: list[str] = []
    if Quality.INVALID in qualities or Quality.MISSING in qualities:
        return PositionReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=["Forecast or contracted position contains missing or invalid values"],
        )
    if Quality.STALE in qualities:
        reasons.append("Forecast or position input is stale but remains usable for diagnosis")
    non_live = modes - {SourceMode.LIVE}
    if non_live:
        reasons.append(
            "Calculation uses non-live input modes: "
            + ", ".join(sorted(mode.value for mode in non_live))
        )
    if reasons:
        reasons.append("Exposure calculation is valid for its labelled inputs, not live trading")
        return PositionReadiness(
            status=SnapshotStatus.DEGRADED,
            calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=reasons,
        )
    return PositionReadiness(
        status=SnapshotStatus.READY,
        calculation_allowed=True,
        trustworthy_for_live_trading=True,
        reasons=["Forecast and Q_t are fresh, live, aligned, and internally consistent"],
    )


def _position_version(
    q_by_period: dict[str, CanonicalDataPoint],
) -> PositionVersion | None:
    if not q_by_period:
        return None
    points = list(q_by_period.values())
    exemplar = points[0]
    as_of = max(point.lineage.retrieved_at for point in points)
    return PositionVersion(
        version_id=f"position-{as_of.strftime('%Y%m%dT%H%M%S')}",
        as_of=as_of,
        source_feed=exemplar.lineage.source_feed,
        source_mode=exemplar.lineage.source_mode,
        semantic_kind=SemanticKind.ASSUMPTION,
        quality=exemplar.lineage.quality,
    )


def _explain_period(
    forecast,
    q_mwh: float,
    p10_exposure: ScenarioExposure,
    p50_exposure: ScenarioExposure,
    warnings: list[str],
) -> str:
    delta = forecast.delta.versus_previous_mwh
    if delta is None:
        change = "has no previous vintage for comparison"
    elif delta < 0:
        change = f"fell by {abs(delta):.1f} MWh versus the previous vintage"
    elif delta > 0:
        change = f"rose by {delta:.1f} MWh versus the previous vintage"
    else:
        change = "is unchanged versus the previous vintage"
    base_direction = p50_exposure.direction.lower()
    if p50_exposure.direction == "FLAT":
        base_sentence = "the portfolio is approximately flat"
    else:
        base_sentence = (
            f"the portfolio is {abs(p50_exposure.residual_position_mwh):.1f} MWh "
            f"{base_direction}"
        )
    tail_direction = p10_exposure.direction.lower()
    tail_sentence = (
        f"Under P10, the position is {abs(p10_exposure.residual_position_mwh):.1f} MWh "
        f"{tail_direction}"
        if p10_exposure.direction != "FLAT"
        else "Under P10, the position is approximately flat"
    )
    trust = (
        "Inputs are sample-labelled, so this is a valid sample calculation but not live trading data."
        if any("Sample" in warning or "SAMPLE" in warning for warning in warnings)
        else "Input warnings should be reviewed before relying on this calculation."
        if warnings
        else "Forecast and position inputs are fresh and live."
    )
    return (
        f"Forecast {forecast.delivery_period} {change}. The contracted position is {q_mwh:.1f} "
        f"MWh sold. Under P50, expected generation is {float(forecast.p50.value):.1f} MWh, "
        f"so {base_sentence}. {tail_sentence}. {trust}"
    )
