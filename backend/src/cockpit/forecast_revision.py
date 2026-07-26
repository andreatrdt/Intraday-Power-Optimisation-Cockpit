"""Forecast revision service (Milestone 2) — selection, calculation, orchestration.

Compares the latest forecast vintage with the immediately preceding valid
vintage **for one settlement period at a time** and produces an auditable, frozen
:class:`~cockpit.forecast_revision_models.ForecastRevision`. A material revision
is a *trigger candidate* that a later milestone can turn into a single-period
``TradeDecision`` (that integration is Milestone 3 and is intentionally *not*
done here).

Module boundaries:

* frozen data contracts        -> :mod:`cockpit.forecast_revision_models`
* forecast-error providers      -> :mod:`cockpit.forecast_calibration`
* selection / calculation /     -> this module (``ForecastRevisionService``)
  significance / materiality /
  orchestration

The public names from the two sibling modules are re-exported here (see
``__all__``) so callers can ``from cockpit.forecast_revision import …`` a stable
surface.

Sign convention (shared with ``position_layer`` / README):

    I_t^s = G_t^s − Q_t

    G_t^s is forecast generation for quantile/scenario ``s``; Q_t is the
    contracted position. exposure > +tol -> LONG, < −tol -> SHORT, else FLAT.

Because Q is unchanged between two vintages of the same period, the P50 exposure
change equals the P50 generation revision.

Significance semantics: ``revision_z_score`` standardises a forecast-to-forecast
revision using a forecast-*error* dispersion proxy (error std ≠ revision std in
general); ``revision_significance_score = erf(|z|/√2)`` measures the statistical
unusualness of the revision, **not** the reliability of the latest forecast.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from uuid import uuid4

from cockpit.decision_models import RunMode
from cockpit.forecast_calibration import (
    AssumptionCalibration,
    CalibrationResult,
    ForecastErrorCalibration,
    ResidualSample,
    SampleDerivedCalibration,
    UnavailableCalibration,
    horizon_bucket,
)
from cockpit.forecast_revision_models import (
    CalibrationBasis,
    ForecastComparison,
    ForecastRevision,
    ForecastRevisionBatch,
    ForecastRevisionError,
    ForecastRevisionRun,
    InvalidQuantileOrderError,
    LookAheadError,
    MaterialityAssessment,
    MaterialityConfig,
    MissingLatestVintageError,
    MissingPreviousVintageError,
    PortfolioEffect,
    RevisionSignificance,
    SettlementPeriodMismatchError,
    SkippedPeriod,
    UnitMismatchError,
    VintageForecastPoint,
)

_SQRT2 = math.sqrt(2.0)
_ORDER_TOLERANCE = 1e-9


__all__ = [
    # models / enums / errors (re-exported)
    "CalibrationBasis",
    "ForecastComparison",
    "ForecastRevision",
    "ForecastRevisionBatch",
    "ForecastRevisionError",
    "ForecastRevisionRun",
    "InvalidQuantileOrderError",
    "LookAheadError",
    "MaterialityAssessment",
    "MaterialityConfig",
    "MissingLatestVintageError",
    "MissingPreviousVintageError",
    "PortfolioEffect",
    "RevisionSignificance",
    "SettlementPeriodMismatchError",
    "SkippedPeriod",
    "UnitMismatchError",
    "VintageForecastPoint",
    # calibration (re-exported)
    "AssumptionCalibration",
    "CalibrationResult",
    "ForecastErrorCalibration",
    "ResidualSample",
    "SampleDerivedCalibration",
    "UnavailableCalibration",
    "horizon_bucket",
    # service
    "ForecastRevisionService",
    "SERVICE",
    "direction",
    "new_revision_id",
    "new_revision_batch_id",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def direction(exposure_mwh: float, tolerance_mwh: float) -> str:
    if exposure_mwh > tolerance_mwh:
        return "LONG"
    if exposure_mwh < -tolerance_mwh:
        return "SHORT"
    return "FLAT"


def _crossed_zero(before: float, after: float, tolerance_mwh: float) -> bool:
    """True only for a strict sign flip beyond the flat tolerance."""
    return (before > tolerance_mwh and after < -tolerance_mwh) or (
        before < -tolerance_mwh and after > tolerance_mwh
    )


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _validate_point(point: VintageForecastPoint, expected_unit: str, settlement_period: int) -> None:
    if point.settlement_period != settlement_period:
        raise SettlementPeriodMismatchError(
            f"Vintage '{point.vintage_id}' is for SP{point.settlement_period}, expected SP{settlement_period}."
        )
    if point.unit != expected_unit:
        raise UnitMismatchError(
            f"Vintage '{point.vintage_id}' unit '{point.unit}' != expected '{expected_unit}'."
        )
    if point.p10_mwh > point.p50_mwh + _ORDER_TOLERANCE or point.p50_mwh > point.p90_mwh + _ORDER_TOLERANCE:
        raise InvalidQuantileOrderError(
            f"Vintage '{point.vintage_id}' has non-monotone quantiles "
            f"(P10={point.p10_mwh}, P50={point.p50_mwh}, P90={point.p90_mwh})."
        )


def new_revision_id(as_of: datetime, settlement_period: int) -> str:
    return f"rev-{as_of.strftime('%Y%m%dT%H%M%S')}-SP{settlement_period}-{uuid4().hex[:8]}"


def new_revision_batch_id(as_of: datetime) -> str:
    return f"revbatch-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"


def _assert_not_future(point: VintageForecastPoint, as_of: datetime) -> None:
    """Programmatic look-ahead guard for a selected vintage."""
    if point.published_at > as_of:
        raise LookAheadError(
            f"Vintage '{point.vintage_id}' published {point.published_at.isoformat()} is after as_of "
            f"{as_of.isoformat()}."
        )


def _latest_eligible_vintage_id(
    by_period: dict[int, list[VintageForecastPoint]], as_of: datetime
) -> str | None:
    latest: VintageForecastPoint | None = None
    for period_points in by_period.values():
        for point in period_points:
            if point.published_at <= as_of and (latest is None or point.published_at > latest.published_at):
                latest = point
    return latest.vintage_id if latest else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ForecastRevisionService:
    """Stateless service that turns vintage forecasts into forecast revisions."""

    def __init__(
        self,
        *,
        calibration: ForecastErrorCalibration | None = None,
        materiality: MaterialityConfig | None = None,
    ) -> None:
        self.calibration = calibration or UnavailableCalibration()
        self.materiality_config = materiality or MaterialityConfig()

    # -- single period ------------------------------------------------------

    def compute_revision(
        self,
        points: Sequence[VintageForecastPoint],
        *,
        as_of: datetime,
        contracted_position_q_mwh: float,
        gate_closure_at: datetime | None = None,
        run_mode: RunMode = RunMode.SAMPLE_DEMO,
        expected_unit: str = "MWh",
        now: datetime | None = None,
    ) -> ForecastRevision:
        """Compute the revision for one settlement period.

        Raises a :class:`ForecastRevisionError` subclass on any reject condition
        (look-ahead, missing/invalid vintage, unit mismatch, bad quantiles).
        """
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not points:
            raise MissingLatestVintageError("No forecast vintage points were supplied.")

        settlement_period = points[0].settlement_period
        for point in points:
            if point.settlement_period != settlement_period:
                raise SettlementPeriodMismatchError(
                    "compute_revision expects points for a single settlement period; "
                    f"found SP{settlement_period} and SP{point.settlement_period}."
                )

        # Point-in-time: only vintages published on or before as_of are eligible.
        eligible = sorted(
            (point for point in points if point.published_at <= as_of),
            key=lambda point: (point.published_at, point.vintage_id),
        )
        if not eligible:
            raise MissingLatestVintageError("No forecast vintage was published on or before as_of.")

        latest = eligible[-1]
        _assert_not_future(latest, as_of)
        _validate_point(latest, expected_unit, settlement_period)

        previous = self._select_previous(eligible[:-1], latest, expected_unit, settlement_period, as_of)

        return self._build_revision(
            latest=latest,
            previous=previous,
            as_of=as_of,
            q_mwh=contracted_position_q_mwh,
            gate_closure_at=gate_closure_at,
            run_mode=run_mode,
            now=now or _utcnow(),
        )

    def _select_previous(
        self,
        earlier: list[VintageForecastPoint],
        latest: VintageForecastPoint,
        expected_unit: str,
        settlement_period: int,
        as_of: datetime,
    ) -> VintageForecastPoint:
        for candidate in reversed(earlier):
            if candidate.vintage_id == latest.vintage_id:
                continue
            _assert_not_future(candidate, as_of)
            # The immediate predecessor must itself be valid (no silent repair).
            _validate_point(candidate, expected_unit, settlement_period)
            return candidate
        raise MissingPreviousVintageError(
            f"No valid predecessor vintage before '{latest.vintage_id}' is available at as_of."
        )

    def _build_revision(
        self,
        *,
        latest: VintageForecastPoint,
        previous: VintageForecastPoint,
        as_of: datetime,
        q_mwh: float,
        gate_closure_at: datetime | None,
        run_mode: RunMode,
        now: datetime,
    ) -> ForecastRevision:
        cfg = self.materiality_config

        delta_p10 = latest.p10_mwh - previous.p10_mwh
        delta_p50 = latest.p50_mwh - previous.p50_mwh
        delta_p90 = latest.p90_mwh - previous.p90_mwh
        previous_width = previous.p90_mwh - previous.p10_mwh
        latest_width = latest.p90_mwh - latest.p10_mwh
        horizon_minutes = round((latest.delivery_start - as_of).total_seconds() / 60.0, 2)

        percentage = (
            delta_p50 / previous.p50_mwh
            if abs(previous.p50_mwh) >= cfg.min_percentage_denominator_mwh
            else None
        )

        comparison = ForecastComparison(
            previous_p10_mwh=previous.p10_mwh,
            previous_p50_mwh=previous.p50_mwh,
            previous_p90_mwh=previous.p90_mwh,
            latest_p10_mwh=latest.p10_mwh,
            latest_p50_mwh=latest.p50_mwh,
            latest_p90_mwh=latest.p90_mwh,
            delta_p10_mwh=round(delta_p10, 6),
            delta_p50_mwh=round(delta_p50, 6),
            delta_p90_mwh=round(delta_p90, 6),
            previous_uncertainty_width_mwh=round(previous_width, 6),
            latest_uncertainty_width_mwh=round(latest_width, 6),
            uncertainty_width_change_mwh=round(latest_width - previous_width, 6),
            absolute_revision_mwh=round(abs(delta_p50), 6),
            percentage_revision=round(percentage, 6) if percentage is not None else None,
            forecast_horizon_minutes=horizon_minutes,
            unit=latest.unit,
        )

        prev_p10_exp = previous.p10_mwh - q_mwh
        prev_p50_exp = previous.p50_mwh - q_mwh
        prev_p90_exp = previous.p90_mwh - q_mwh
        latest_p10_exp = latest.p10_mwh - q_mwh
        latest_p50_exp = latest.p50_mwh - q_mwh
        latest_p90_exp = latest.p90_mwh - q_mwh
        portfolio = PortfolioEffect(
            contracted_position_q_mwh=q_mwh,
            previous_p10_exposure_mwh=round(prev_p10_exp, 6),
            previous_p50_exposure_mwh=round(prev_p50_exp, 6),
            previous_p90_exposure_mwh=round(prev_p90_exp, 6),
            latest_p10_exposure_mwh=round(latest_p10_exp, 6),
            latest_p50_exposure_mwh=round(latest_p50_exp, 6),
            latest_p90_exposure_mwh=round(latest_p90_exp, 6),
            delta_p50_exposure_mwh=round(latest_p50_exp - prev_p50_exp, 6),
            direction_before=direction(prev_p50_exp, cfg.flat_tolerance_mwh),
            direction_after=direction(latest_p50_exp, cfg.flat_tolerance_mwh),
            crossed_zero_exposure=_crossed_zero(prev_p50_exp, latest_p50_exp, cfg.flat_tolerance_mwh),
        )

        significance = self._assess_significance(
            settlement_period=latest.settlement_period,
            horizon_minutes=horizon_minutes,
            delivery_start=latest.delivery_start,
            as_of=as_of,
            delta_p50=delta_p50,
        )

        minutes_to_gate = (
            round((gate_closure_at - as_of).total_seconds() / 60.0, 2) if gate_closure_at is not None else None
        )
        materiality = self._assess_materiality(comparison, portfolio, significance, minutes_to_gate)

        lineage = tuple(dict.fromkeys([*latest.lineage_value_ids, *previous.lineage_value_ids]))

        return ForecastRevision(
            revision_id=new_revision_id(as_of, latest.settlement_period),
            calculated_at=now,
            as_of=as_of,
            settlement_period=latest.settlement_period,
            delivery_period=latest.delivery_period,
            delivery_start=latest.delivery_start,
            delivery_end=latest.delivery_end,
            latest_forecast_vintage_id=latest.vintage_id,
            previous_forecast_vintage_id=previous.vintage_id,
            latest_publication_time=latest.published_at,
            previous_publication_time=previous.published_at,
            source_mode=latest.source_mode,
            quality=latest.quality,
            run_mode=run_mode,
            lineage_value_ids=lineage,
            comparison=comparison,
            portfolio=portfolio,
            significance=significance,
            materiality=materiality,
        )

    def _assess_significance(
        self,
        *,
        settlement_period: int,
        horizon_minutes: float,
        delivery_start: datetime,
        as_of: datetime,
        delta_p50: float,
    ) -> RevisionSignificance:
        cal = self.calibration.error_std(
            settlement_period=settlement_period,
            horizon_minutes=horizon_minutes,
            delivery_start=delivery_start,
            as_of=as_of,
        )
        std = cal.error_std_mwh
        if std is not None and std > 0:
            z = delta_p50 / std
            significance_score = math.erf(abs(z) / _SQRT2)  # = P(|N(0,1)| ≤ |z|)
            z_out: float | None = round(z, 4)
            significance_out: float | None = round(significance_score, 4)
        else:
            z_out = None
            significance_out = None
        return RevisionSignificance(
            revision_z_score=z_out,
            error_std_mwh=std,
            revision_significance_score=significance_out,
            calibration_basis=cal.basis,
            calibration_sample_size=cal.sample_size,
            calibration_horizon_bucket=cal.horizon_bucket,
            note=cal.note,
        )

    def _assess_materiality(
        self,
        comparison: ForecastComparison,
        portfolio: PortfolioEffect,
        significance: RevisionSignificance,
        minutes_to_gate: float | None,
    ) -> MaterialityAssessment:
        cfg = self.materiality_config
        abs_rev = comparison.absolute_revision_mwh
        pct = comparison.percentage_revision
        z = significance.revision_z_score
        delta_exposure = abs(portfolio.delta_p50_exposure_mwh)

        pct_material = pct is not None and abs(pct) >= cfg.percentage_threshold
        absolute_volume_material = abs_rev >= cfg.absolute_mwh_threshold or pct_material
        standardised_revision_material = z is not None and abs(z) >= cfg.z_score_threshold
        exposure_change_material = delta_exposure >= cfg.p50_exposure_change_threshold_mwh
        direction_flip_material = cfg.direction_flip_is_material and portfolio.crossed_zero_exposure
        gate_closure_material = (
            minutes_to_gate is not None and 0.0 <= minutes_to_gate <= cfg.gate_closure_minutes_threshold
        )

        is_material = (
            absolute_volume_material
            or standardised_revision_material
            or exposure_change_material
            or direction_flip_material
        )

        score = cfg.weight_absolute * (abs_rev / cfg.absolute_mwh_threshold)
        if z is not None:
            score += cfg.weight_zscore * (abs(z) / cfg.z_score_threshold)
        score += cfg.weight_exposure * (delta_exposure / cfg.p50_exposure_change_threshold_mwh)
        if direction_flip_material:
            score += cfg.weight_direction_flip
        if gate_closure_material and abs_rev > 0:
            urgency = (cfg.gate_closure_minutes_threshold - minutes_to_gate) / cfg.gate_closure_minutes_threshold
            score += cfg.weight_gate_closure * max(0.0, urgency)

        reasons: list[str] = []
        if absolute_volume_material:
            detail = f"Absolute P50 revision {abs_rev:.1f} MWh ≥ {cfg.absolute_mwh_threshold:.1f} MWh threshold"
            if pct_material and pct is not None:
                detail += f" (|Δ| {abs(pct) * 100:.0f}% ≥ {cfg.percentage_threshold * 100:.0f}%)"
            reasons.append(detail)
        if standardised_revision_material and z is not None:
            reasons.append(
                f"Standardised revision |z|={abs(z):.2f} ≥ {cfg.z_score_threshold:.2f} "
                f"({significance.calibration_basis.value})"
            )
        if exposure_change_material:
            reasons.append(
                f"P50 exposure change {delta_exposure:.1f} MWh ≥ {cfg.p50_exposure_change_threshold_mwh:.1f} MWh"
            )
        if direction_flip_material:
            reasons.append(
                f"Exposure direction flipped {portfolio.direction_before}→{portfolio.direction_after} (crossed zero)"
            )
        if gate_closure_material and minutes_to_gate is not None:
            reasons.append(
                f"{minutes_to_gate:.0f} min to Gate Closure ≤ {cfg.gate_closure_minutes_threshold:.0f} min"
            )
        if not is_material and not reasons:
            reasons.append("Revision below all configured materiality thresholds.")
        elif not is_material:
            reasons.insert(0, "Not material: no forecast-revision signal crossed its threshold.")

        return MaterialityAssessment(
            absolute_volume_material=absolute_volume_material,
            standardised_revision_material=standardised_revision_material,
            exposure_change_material=exposure_change_material,
            direction_flip_material=direction_flip_material,
            gate_closure_material=gate_closure_material,
            signal_materiality_score=round(score, 4),
            is_material=is_material,
            materiality_reasons=tuple(reasons),
        )

    # -- many periods -------------------------------------------------------

    def compute_run(
        self,
        points: Iterable[VintageForecastPoint],
        *,
        as_of: datetime,
        q_by_period: dict[str, float],
        gate_closure_by_period: dict[str, datetime] | None = None,
        run_mode: RunMode = RunMode.SAMPLE_DEMO,
        expected_unit: str = "MWh",
        now: datetime | None = None,
    ) -> ForecastRevisionRun:
        """Compute revisions for every settlement period present in ``points``.

        Per-period reject conditions are captured in ``skipped`` rather than
        aborting the whole run. Q is looked up by ``delivery_period``.
        """
        calculated_at = now or _utcnow()
        gate_by_period = gate_closure_by_period or {}

        by_period: dict[int, list[VintageForecastPoint]] = {}
        for point in points:
            by_period.setdefault(point.settlement_period, []).append(point)

        latest_vintage_id = _latest_eligible_vintage_id(by_period, as_of)

        revisions: list[ForecastRevision] = []
        skipped: list[SkippedPeriod] = []
        for settlement_period in sorted(by_period):
            period_points = by_period[settlement_period]
            delivery_period = period_points[0].delivery_period
            q_mwh = q_by_period.get(delivery_period)
            if q_mwh is None:
                skipped.append(
                    SkippedPeriod(
                        settlement_period=settlement_period,
                        delivery_period=delivery_period,
                        error_code="MISSING_CONTRACTED_POSITION",
                        message=f"No contracted position Q for {delivery_period}.",
                    )
                )
                continue
            try:
                revision = self.compute_revision(
                    period_points,
                    as_of=as_of,
                    contracted_position_q_mwh=q_mwh,
                    gate_closure_at=gate_by_period.get(delivery_period),
                    run_mode=run_mode,
                    expected_unit=expected_unit,
                    now=calculated_at,
                )
            except ForecastRevisionError as error:
                skipped.append(
                    SkippedPeriod(
                        settlement_period=settlement_period,
                        delivery_period=delivery_period,
                        error_code=error.code,
                        message=str(error),
                    )
                )
                continue
            revisions.append(revision)

        batch: ForecastRevisionBatch | None = None
        if revisions:
            first = revisions[0]
            batch = ForecastRevisionBatch(
                batch_id=new_revision_batch_id(as_of),
                created_at=calculated_at,
                as_of=as_of,
                latest_vintage_id=latest_vintage_id or first.latest_forecast_vintage_id,
                run_mode=run_mode,
                source_mode=first.source_mode,
                quality=first.quality,
                revision_ids=tuple(revision.revision_id for revision in revisions),
                affected_delivery_periods=tuple(revision.delivery_period for revision in revisions),
            )

        return ForecastRevisionRun(
            run_id=f"revrun-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
            calculated_at=calculated_at,
            as_of=as_of,
            latest_vintage_id=latest_vintage_id,
            run_mode=run_mode,
            revisions=tuple(revisions),
            batch=batch,
            trigger_candidate_ids=tuple(r.revision_id for r in revisions if r.is_material),
            skipped=tuple(skipped),
        )


SERVICE = ForecastRevisionService()
"""Default service instance (unavailable calibration; default thresholds)."""
