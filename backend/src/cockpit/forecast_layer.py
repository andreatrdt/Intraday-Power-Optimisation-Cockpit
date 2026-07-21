"""Forecast-vintage diagnostics built from a traceable cockpit snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from cockpit.models import (
    CanonicalDataPoint,
    CockpitSnapshot,
    DataLineage,
    ForecastDelta,
    ForecastPoint,
    ForecastReliability,
    ForecastVintage,
    Quality,
    SemanticKind,
    SourceMode,
    ValidationCheck,
)


@dataclass
class ForecastLayerResult:
    points: list[ForecastPoint]
    latest_vintage: ForecastVintage | None
    previous_vintage: ForecastVintage | None
    warnings: list[str]
    missing_periods: list[str]


def energy_from_power(value: float, unit: str, duration_hours: float) -> float:
    """Return MWh for an energy or average-power forecast input."""
    normalised = unit.strip().lower()
    if normalised == "mwh":
        return value
    if normalised == "mw":
        return value * duration_hours
    raise ValueError(f"Unsupported forecast unit '{unit}'; expected MW or MWh")


def build_forecast_layer(snapshot: CockpitSnapshot) -> ForecastLayerResult:
    grouped: dict[str, dict[str, CanonicalDataPoint]] = {}
    for point in snapshot.values:
        if point.delivery_period and point.metric.startswith("wind_"):
            grouped.setdefault(point.delivery_period, {})[point.metric] = point

    forecast_points: list[ForecastPoint] = []
    missing_periods: list[str] = []
    warnings: list[str] = []
    latest_issued = []
    previous_issued = []

    for delivery_period, metrics in sorted(
        grouped.items(), key=lambda item: _delivery_sort_key(item[1])
    ):
        required = {
            "p10": _find_metric(metrics, "wind_p10", "wind_p10_mw"),
            "p50": _find_metric(metrics, "wind_p50", "wind_p50_mw"),
            "p90": _find_metric(metrics, "wind_p90", "wind_p90_mw"),
        }
        if any(point is None for point in required.values()):
            missing_periods.append(delivery_period)
            warnings.append(f"{delivery_period}: missing one or more P10/P50/P90 values")
            continue

        duration_hours = 0.5
        p10 = _as_mwh(required["p10"], duration_hours, snapshot)
        p50 = _as_mwh(required["p50"], duration_hours, snapshot)
        p90 = _as_mwh(required["p90"], duration_hours, snapshot)
        assert p10 and p50 and p90
        if any(
            point.lineage.quality in (Quality.MISSING, Quality.INVALID)
            for point in (p10, p50, p90)
        ) or not float(p10.value) <= float(p50.value) <= float(p90.value):
            missing_periods.append(delivery_period)
            warnings.append(
                f"{delivery_period}: P10/P50/P90 is invalid or internally inconsistent"
            )
            continue
        delivery_start = p50.delivery_start
        if delivery_start is None:
            missing_periods.append(delivery_period)
            warnings.append(f"{delivery_period}: delivery start is missing")
            continue
        previous = _find_metric(metrics, "wind_previous_p50", "wind_previous_p50_mw")
        day_ahead = _find_metric(metrics, "wind_day_ahead_p50", "wind_day_ahead_p50_mw")
        previous_mwh = _as_mwh(previous, duration_hours, snapshot) if previous else None
        day_ahead_mwh = _as_mwh(day_ahead, duration_hours, snapshot) if day_ahead else None

        delta_previous = None
        if previous_mwh:
            delta_previous = derived_value(
                snapshot,
                metric="forecast_delta_previous",
                delivery_period=delivery_period,
                delivery_start=delivery_start,
                value=float(p50.value) - float(previous_mwh.value),
                unit="MWh",
                inputs=[p50, previous_mwh],
                expression="latest P50 - previous-vintage P50",
            )
        delta_day_ahead = None
        if day_ahead_mwh:
            delta_day_ahead = derived_value(
                snapshot,
                metric="forecast_delta_day_ahead",
                delivery_period=delivery_period,
                delivery_start=delivery_start,
                value=float(p50.value) - float(day_ahead_mwh.value),
                unit="MWh",
                inputs=[p50, day_ahead_mwh],
                expression="latest P50 - day-ahead baseline P50",
            )

        disagreement = metrics.get("wind_model_disagreement")
        disagreement_mwh = (
            energy_from_power(float(disagreement.value), disagreement.unit, duration_hours)
            if disagreement
            else None
        )
        score_point = metrics.get("wind_reliability_score")
        score = float(score_point.value) if score_point else None
        reliability_flags: list[str] = []
        period_warnings = _input_warnings([p10, p50, p90, previous_mwh, day_ahead_mwh])
        if score is None:
            reliability_flags.append("Reliability score is unavailable")
        if p50.lineage.source_mode in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
            reliability_flags.append(f"Forecast source is {p50.lineage.source_mode.value}")
        if any(point.lineage.quality == Quality.STALE for point in (p10, p50, p90)):
            reliability_flags.append("Forecast is stale")
        interval_width = float(p90.value) - float(p10.value)
        if float(p50.value) and interval_width / float(p50.value) > 0.5:
            reliability_flags.append("P10-P90 interval is wide relative to P50")
        if disagreement_mwh is not None and float(p50.value):
            if disagreement_mwh / float(p50.value) > 0.1:
                reliability_flags.append("Model disagreement exceeds 10% of P50")
        period_warnings.extend(flag for flag in reliability_flags if flag not in period_warnings)

        latest_stamp = p50.lineage.published_at or p50.lineage.retrieved_at
        latest_issued.append(latest_stamp)
        if previous_mwh:
            previous_issued.append(
                previous_mwh.lineage.published_at or previous_mwh.lineage.retrieved_at
            )
        forecast_points.append(
            ForecastPoint(
                settlement_period=_settlement_period(delivery_period),
                delivery_period=delivery_period,
                delivery_start=delivery_start,
                delivery_end=delivery_start + timedelta(minutes=30),
                duration_hours=duration_hours,
                p10=p10,
                p50=p50,
                p90=p90,
                previous_p50=previous_mwh,
                day_ahead_p50=day_ahead_mwh,
                delta=ForecastDelta(
                    versus_previous_mwh=(float(delta_previous.value) if delta_previous else None),
                    versus_day_ahead_mwh=(float(delta_day_ahead.value) if delta_day_ahead else None),
                    versus_previous_value=delta_previous,
                    versus_day_ahead_value=delta_day_ahead,
                ),
                reliability=ForecastReliability(
                    score=score,
                    label=_reliability_label(score),
                    flags=reliability_flags,
                    model_disagreement_mwh=disagreement_mwh,
                    score_value=score_point,
                    disagreement_value=disagreement,
                ),
                warnings=period_warnings,
            )
        )

    latest_vintage = None
    previous_vintage = None
    if forecast_points and latest_issued:
        exemplar = forecast_points[0].p50
        issued_at = max(latest_issued)
        latest_vintage = ForecastVintage(
            vintage_id=f"forecast-{issued_at.strftime('%Y%m%dT%H%M%S')}-latest",
            issued_at=issued_at,
            source_feed=exemplar.lineage.source_feed,
            source_mode=exemplar.lineage.source_mode,
            quality=exemplar.lineage.quality,
            model_name=(
                "sample-renewable-ensemble-v1"
                if exemplar.lineage.source_mode == SourceMode.SAMPLE
                else exemplar.lineage.source_feed
            ),
        )
    if forecast_points and previous_issued:
        exemplar = forecast_points[0].previous_p50 or forecast_points[0].p50
        issued_at = max(previous_issued)
        previous_vintage = ForecastVintage(
            vintage_id=f"forecast-{issued_at.strftime('%Y%m%dT%H%M%S')}-previous",
            issued_at=issued_at,
            source_feed=exemplar.lineage.source_feed,
            source_mode=exemplar.lineage.source_mode,
            quality=exemplar.lineage.quality,
            model_name=(
                "sample-renewable-ensemble-v1"
                if exemplar.lineage.source_mode == SourceMode.SAMPLE
                else exemplar.lineage.source_feed
            ),
        )
    if not forecast_points:
        warnings.append("No internally consistent forecast periods are available")
    return ForecastLayerResult(
        points=forecast_points,
        latest_vintage=latest_vintage,
        previous_vintage=previous_vintage,
        warnings=list(dict.fromkeys(warnings)),
        missing_periods=missing_periods,
    )


def derived_value(
    snapshot: CockpitSnapshot,
    *,
    metric: str,
    delivery_period: str,
    delivery_start,
    value: float,
    unit: str,
    inputs: list[CanonicalDataPoint],
    expression: str,
) -> CanonicalDataPoint:
    source_mode = combined_source_mode(inputs)
    quality = combined_quality(inputs)
    warnings = _input_warnings(inputs)
    if source_mode in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
        warnings.append(f"Derived from {source_mode.value} inputs; not live trading data.")
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    retrieved = max(point.lineage.retrieved_at for point in inputs)
    identifier = uuid5(
        NAMESPACE_URL,
        f"{snapshot.snapshot_id}:{metric}:{delivery_period}:{','.join(p.value_id for p in inputs)}",
    )
    return CanonicalDataPoint(
        value_id=str(identifier),
        metric=metric,
        value=round(value, 6),
        unit=unit,
        delivery_period=delivery_period,
        delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="forecast_position_calculation",
            source_mode=source_mode,
            semantic_kind=SemanticKind.ESTIMATE,
            quality=quality,
            published_at=max(published) if published else None,
            retrieved_at=retrieved,
            normalised_at=snapshot.as_of,
            raw_field_name=expression,
            transformations=[expression, "preserved positive-long / negative-short convention"],
            validation_checks=[
                ValidationCheck(
                    name="finite_result",
                    passed=value == value and abs(value) != float("inf"),
                    detail="calculated value is finite",
                ),
                ValidationCheck(
                    name="unit_alignment",
                    passed=all(point.unit in ("MW", "MWh") for point in inputs),
                    detail="inputs resolve to settlement-period MWh",
                ),
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot.snapshot_id,
    )


def combined_source_mode(points: list[CanonicalDataPoint]) -> SourceMode:
    modes = {point.lineage.source_mode for point in points}
    for mode in (
        SourceMode.ERROR,
        SourceMode.SYNTHETIC,
        SourceMode.SAMPLE,
        SourceMode.LATEST_AVAILABLE,
        SourceMode.LIVE,
    ):
        if mode in modes:
            return mode
    return SourceMode.ERROR


def combined_quality(points: list[CanonicalDataPoint]) -> Quality:
    qualities = {point.lineage.quality for point in points}
    for quality in (
        Quality.INVALID,
        Quality.MISSING,
        Quality.STALE,
        Quality.PARTIAL,
        Quality.REVISED,
        Quality.FRESH,
    ):
        if quality in qualities:
            return quality
    return Quality.INVALID


def _as_mwh(
    point: CanonicalDataPoint | None,
    duration_hours: float,
    snapshot: CockpitSnapshot,
) -> CanonicalDataPoint | None:
    if point is None or point.unit == "MWh":
        return point
    if point.unit != "MW":
        return point
    converted = point.model_copy(deep=True)
    converted.value_id = str(
        uuid5(NAMESPACE_URL, f"{snapshot.snapshot_id}:mw-to-mwh:{point.value_id}")
    )
    converted.metric = point.metric.removesuffix("_mw")
    converted.value = round(float(point.value) * duration_hours, 6)
    converted.unit = "MWh"
    converted.lineage.semantic_kind = SemanticKind.ESTIMATE
    converted.lineage.transformations.append(
        f"converted average MW to MWh using {duration_hours:g} h settlement duration"
    )
    converted.lineage.validation_checks.append(
        ValidationCheck(
            name="mw_to_mwh_duration",
            passed=duration_hours > 0,
            detail=f"duration_hours={duration_hours:g}",
        )
    )
    return converted


def _find_metric(
    metrics: dict[str, CanonicalDataPoint], *names: str
) -> CanonicalDataPoint | None:
    for name in names:
        if name in metrics:
            return metrics[name]
    return None


def _delivery_sort_key(metrics: dict[str, CanonicalDataPoint]):
    starts = [point.delivery_start for point in metrics.values() if point.delivery_start]
    return (0, min(starts).timestamp()) if starts else (1, float("inf"))


def _settlement_period(delivery_period: str) -> int:
    match = re.search(r"SP(\d+)$", delivery_period)
    if not match:
        raise ValueError(f"Cannot parse settlement period from '{delivery_period}'")
    return int(match.group(1))


def _reliability_label(score: float | None) -> str:
    if score is None:
        return "UNKNOWN"
    if score >= 0.8:
        return "HIGH"
    if score >= 0.6:
        return "MEDIUM"
    return "LOW"


def _input_warnings(points: list[CanonicalDataPoint | None]) -> list[str]:
    warnings: list[str] = []
    for point in points:
        if point:
            warnings.extend(point.lineage.warnings)
    return list(dict.fromkeys(warnings))
