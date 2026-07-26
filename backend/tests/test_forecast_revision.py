"""Milestone 2 — forecast revision service.

Additive and independent of the existing suites. Covers vintage selection,
point-in-time integrity, the revision/exposure formulae, the three honest
calibration bases, materiality logic, rejection of malformed inputs, frozen
result models and lightweight batch grouping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.decision_models import RunMode
from cockpit.forecast_revision import (
    AssumptionCalibration,
    CalibrationBasis,
    CalibrationResult,
    ForecastErrorCalibration,
    ForecastRevision,
    ForecastRevisionBatch,
    ForecastRevisionService,
    InvalidQuantileOrderError,
    MaterialityConfig,
    MissingLatestVintageError,
    MissingPreviousVintageError,
    ResidualSample,
    SampleDerivedCalibration,
    SettlementPeriodMismatchError,
    UnitMismatchError,
    VintageForecastPoint,
    horizon_bucket,
)

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DSTART = AT0 + timedelta(minutes=90)  # horizon 90 min -> bucket "1-2h"


def _pt(
    vintage_id: str,
    minutes_before: float,
    *,
    sp: int = 24,
    p10: float,
    p50: float,
    p90: float,
    unit: str = "MWh",
    delivery_start: datetime = DSTART,
    lineage: tuple[str, ...] = (),
) -> VintageForecastPoint:
    return VintageForecastPoint(
        vintage_id=vintage_id,
        published_at=AT0 - timedelta(minutes=minutes_before),
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        delivery_start=delivery_start,
        delivery_end=delivery_start + timedelta(minutes=30),
        p10_mwh=p10,
        p50_mwh=p50,
        p90_mwh=p90,
        unit=unit,
        lineage_value_ids=lineage,
    )


class _FakeCalibrated(ForecastErrorCalibration):
    """Stands in for a genuine historical-residual provider (not shipped)."""

    def __init__(self, std: float = 20.0) -> None:
        self._std = std

    def error_std(self, *, settlement_period, horizon_minutes, delivery_start, as_of) -> CalibrationResult:
        return CalibrationResult(
            error_std_mwh=self._std,
            sample_size=500,
            horizon_bucket=horizon_bucket(horizon_minutes),
            basis=CalibrationBasis.CALIBRATED,
            note="test calibrated provider",
        )


def _service(**kwargs) -> ForecastRevisionService:
    return ForecastRevisionService(**kwargs)


# ---------------------------------------------------------------------------
# Vintage selection & point-in-time integrity
# ---------------------------------------------------------------------------


def test_selects_latest_and_immediate_previous():
    points = [
        _pt("v1", 120, p10=170, p50=190, p90=210),
        _pt("v2", 60, p10=175, p50=195, p90=215),
        _pt("v3", 10, p10=150, p50=164, p90=196),
    ]
    revision = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=200.0)
    assert revision.latest_forecast_vintage_id == "v3"
    assert revision.previous_forecast_vintage_id == "v2"
    assert revision.latest_publication_time == AT0 - timedelta(minutes=10)


def test_no_look_ahead_excludes_future_vintages():
    points = [
        _pt("v1", 60, p10=170, p50=190, p90=210),
        _pt("v2", 10, p10=160, p50=180, p90=200),
        _pt("v_future", -30, p10=100, p50=120, p90=140),  # published after as_of
    ]
    revision = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=150.0)
    assert revision.latest_forecast_vintage_id == "v2"
    assert revision.previous_forecast_vintage_id == "v1"
    assert revision.latest_publication_time <= AT0


def test_all_future_vintages_is_missing_latest():
    points = [_pt("v_future", -30, p10=1, p50=2, p90=3)]
    with pytest.raises(MissingLatestVintageError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


def test_missing_previous_vintage():
    points = [_pt("only", 10, p10=170, p50=190, p90=210)]
    with pytest.raises(MissingPreviousVintageError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


def test_same_vintage_twice_has_no_valid_predecessor():
    points = [
        _pt("v1", 60, p10=170, p50=190, p90=210),
        _pt("v1", 10, p10=170, p50=190, p90=210),
    ]
    with pytest.raises(MissingPreviousVintageError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


def test_settlement_period_mismatch_in_single_call():
    points = [
        _pt("v1", 60, sp=24, p10=170, p50=190, p90=210),
        _pt("v2", 10, sp=25, p10=160, p50=180, p90=200),
    ]
    with pytest.raises(SettlementPeriodMismatchError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


# ---------------------------------------------------------------------------
# Formulae
# ---------------------------------------------------------------------------


def test_delta_and_uncertainty_width():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=224),  # width 44
        _pt("v2", 10, p10=150, p50=164, p90=196),  # width 46
    ]
    c = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0).comparison
    assert c.delta_p10_mwh == -30.0
    assert c.delta_p50_mwh == -36.0
    assert c.delta_p90_mwh == -28.0
    assert c.absolute_revision_mwh == 36.0
    assert c.previous_uncertainty_width_mwh == 44.0
    assert c.latest_uncertainty_width_mwh == 46.0
    assert c.uncertainty_width_change_mwh == 2.0
    assert c.forecast_horizon_minutes == 90.0


def test_exposure_change_relative_to_q():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=220),
        _pt("v2", 10, p10=150, p50=164, p90=196),
    ]
    p = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=210.0).portfolio
    assert p.contracted_position_q_mwh == 210.0
    assert p.previous_p50_exposure_mwh == -10.0  # 200 - 210
    assert p.latest_p50_exposure_mwh == -46.0  # 164 - 210
    assert p.delta_p50_exposure_mwh == -36.0
    assert p.direction_before == "SHORT"
    assert p.direction_after == "SHORT"
    assert p.crossed_zero_exposure is False


def test_zero_crossing_detection():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=220),  # exp +30 LONG at Q=170
        _pt("v2", 10, p10=120, p50=140, p90=160),  # exp -30 SHORT at Q=170
    ]
    p = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=170.0).portfolio
    assert p.direction_before == "LONG"
    assert p.direction_after == "SHORT"
    assert p.crossed_zero_exposure is True


def test_percentage_revision_normal_and_near_zero_denominator():
    normal = _service().compute_revision(
        [_pt("v1", 60, p10=180, p50=200, p90=220), _pt("v2", 10, p10=150, p50=164, p90=196)],
        as_of=AT0,
        contracted_position_q_mwh=0.0,
    )
    assert normal.comparison.percentage_revision == pytest.approx(-0.18)

    near_zero = _service().compute_revision(
        [_pt("v1", 60, p10=-5, p50=0.3, p90=5), _pt("v2", 10, p10=-4, p50=1.0, p90=6)],
        as_of=AT0,
        contracted_position_q_mwh=0.0,
    )
    assert near_zero.comparison.percentage_revision is None


# ---------------------------------------------------------------------------
# Calibration bases
# ---------------------------------------------------------------------------


def _revision_delta36():
    return [
        _pt("v1", 60, p10=180, p50=200, p90=220),
        _pt("v2", 10, p10=150, p50=164, p90=196),
    ]


def test_unavailable_significance_is_default():
    revision = _service().compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0)
    sig = revision.significance
    assert sig.calibration_basis is CalibrationBasis.UNAVAILABLE
    assert sig.revision_z_score is None
    assert sig.revision_significance_score is None
    assert sig.error_std_mwh is None


def test_calibrated_significance():
    service = _service(calibration=_FakeCalibrated(std=20.0))
    revision = service.compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0)
    sig = revision.significance
    assert sig.calibration_basis is CalibrationBasis.CALIBRATED
    assert sig.revision_z_score == pytest.approx(-1.8)
    assert sig.revision_significance_score == pytest.approx(0.9281, abs=1e-3)
    assert sig.error_std_mwh == 20.0
    assert sig.calibration_sample_size == 500


def test_sample_derived_significance_and_insufficient_bucket():
    residuals = [ResidualSample(settlement_period=24, horizon_minutes=90, error_mwh=e) for e in (-20, -10, 0, 10, 20, 5, -5)]
    service = _service(calibration=SampleDerivedCalibration(residuals, min_samples=5))
    revision = service.compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0)
    sig = revision.significance
    assert sig.calibration_basis is CalibrationBasis.SAMPLE_DERIVED
    assert sig.revision_z_score is not None
    assert sig.error_std_mwh is not None
    assert "not historically calibrated" in (sig.note or "")

    thin = _service(calibration=SampleDerivedCalibration(residuals[:3], min_samples=5))
    thin_sig = thin.compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0).significance
    assert thin_sig.calibration_basis is CalibrationBasis.UNAVAILABLE
    assert thin_sig.revision_z_score is None


def test_assumption_calibration_is_labelled_honestly():
    service = _service(calibration=AssumptionCalibration(18.0))
    sig = service.compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0).significance
    assert sig.calibration_basis is CalibrationBasis.ASSUMPTION_BASED
    assert sig.revision_z_score == pytest.approx(-2.0)
    assert sig.error_std_mwh == 18.0


def test_assumption_calibration_rejects_nonpositive_std():
    with pytest.raises(ValueError):
        AssumptionCalibration(0.0)


# ---------------------------------------------------------------------------
# Materiality
# ---------------------------------------------------------------------------


def test_material_revision_with_reasons():
    revision = _service().compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=210.0)
    m = revision.materiality
    assert m.is_material is True
    assert revision.is_trigger_candidate is True
    assert m.absolute_volume_material is True
    assert m.exposure_change_material is True
    assert any("Absolute P50 revision" in r for r in m.materiality_reasons)
    assert m.signal_materiality_score > 0


def test_non_material_revision():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=220),
        _pt("v2", 10, p10=178, p50=198, p90=218),  # delta_p50 = -2
    ]
    m = _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0).materiality
    assert m.is_material is False
    assert m.absolute_volume_material is False
    assert any("Not material" in r or "below all configured" in r for r in m.materiality_reasons)


def test_direction_flip_alone_is_material():
    # Raise the volume/exposure/z thresholds high so only the direction flip qualifies.
    cfg = MaterialityConfig(absolute_mwh_threshold=100.0, p50_exposure_change_threshold_mwh=100.0, z_score_threshold=100.0)
    service = _service(materiality=cfg)
    flip = service.compute_revision(
        [
            _pt("v1", 60, p10=180, p50=200, p90=220),  # exp +3 LONG at Q=197
            _pt("v2", 10, p10=172, p50=194, p90=214),  # exp -3 SHORT at Q=197
        ],
        as_of=AT0,
        contracted_position_q_mwh=197.0,
    ).materiality
    assert flip.direction_flip_material is True
    assert flip.is_material is True
    assert any("flipped" in r for r in flip.materiality_reasons)


def test_gate_closure_is_reported_but_not_sufficient_alone():
    cfg = MaterialityConfig(absolute_mwh_threshold=100.0, p50_exposure_change_threshold_mwh=100.0, z_score_threshold=100.0)
    service = _service(materiality=cfg)
    revision = service.compute_revision(
        [
            _pt("v1", 60, p10=180, p50=200, p90=220),
            _pt("v2", 10, p10=178, p50=198, p90=218),  # tiny -2 revision
        ],
        as_of=AT0,
        contracted_position_q_mwh=0.0,
        gate_closure_at=AT0 + timedelta(minutes=30),  # inside 90-min window
    )
    m = revision.materiality
    assert m.gate_closure_material is True
    assert m.is_material is False  # near gate but no signal crossed threshold


# ---------------------------------------------------------------------------
# Rejection of malformed inputs
# ---------------------------------------------------------------------------


def test_invalid_quantile_ordering_is_rejected():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=220),
        _pt("v2", 10, p10=210, p50=200, p90=220),  # p10 > p50
    ]
    with pytest.raises(InvalidQuantileOrderError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


def test_unit_mismatch_is_rejected():
    points = [
        _pt("v1", 60, p10=180, p50=200, p90=220),
        _pt("v2", 10, p10=150, p50=164, p90=196, unit="MW"),
    ]
    with pytest.raises(UnitMismatchError):
        _service().compute_revision(points, as_of=AT0, contracted_position_q_mwh=0.0)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_result_models_are_frozen():
    revision = _service().compute_revision(_revision_delta36(), as_of=AT0, contracted_position_q_mwh=0.0)
    with pytest.raises(ValidationError):
        revision.comparison.delta_p50_mwh = 0.0
    with pytest.raises(ValidationError):
        revision.materiality.is_material = False
    assert isinstance(revision.materiality.materiality_reasons, tuple)
    with pytest.raises(AttributeError):
        revision.materiality.materiality_reasons.append("x")


# ---------------------------------------------------------------------------
# Multi-period run & batch
# ---------------------------------------------------------------------------


def test_per_period_isolation_and_batch_grouping():
    points = [
        _pt("v1", 60, sp=24, p10=180, p50=200, p90=220),
        _pt("v2", 10, sp=24, p10=150, p50=164, p90=196),  # delta -36 (material)
        _pt("v1", 60, sp=25, p10=90, p50=100, p90=110),
        _pt("v2", 10, sp=25, p10=89, p50=99, p90=109),  # delta -1 (not material)
    ]
    run = _service().compute_run(
        points,
        as_of=AT0,
        q_by_period={"SP24": 210.0, "SP25": 50.0},
    )
    assert len(run.revisions) == 2
    by_sp = {r.settlement_period: r for r in run.revisions}
    assert by_sp[24].comparison.delta_p50_mwh == -36.0
    assert by_sp[25].comparison.delta_p50_mwh == -1.0
    assert by_sp[24].is_material and not by_sp[25].is_material
    # Batch is a pure index.
    assert run.batch is not None
    assert run.batch.revision_ids == tuple(r.revision_id for r in run.revisions)
    assert run.batch.affected_delivery_periods == ("SP24", "SP25")
    assert run.trigger_candidate_ids == (by_sp[24].revision_id,)
    for banned in ("exposure", "pnl", "executed", "position", "delta", "p50"):
        assert not any(banned in name for name in ForecastRevisionBatch.model_fields)


def test_run_skips_periods_with_errors_without_aborting():
    points = [
        _pt("v1", 60, sp=24, p10=180, p50=200, p90=220),
        _pt("v2", 10, sp=24, p10=150, p50=164, p90=196),  # good
        _pt("v1", 60, sp=25, p10=90, p50=100, p90=110),
        _pt("v2", 10, sp=25, p10=200, p50=100, p90=110),  # bad ordering
        _pt("v1", 10, sp=26, p10=90, p50=100, p90=110),  # only one vintage
    ]
    run = _service().compute_run(points, as_of=AT0, q_by_period={"SP24": 0.0, "SP25": 0.0, "SP26": 0.0})
    assert {r.settlement_period for r in run.revisions} == {24}
    codes = {s.error_code for s in run.skipped}
    assert "INVALID_QUANTILE_ORDER" in codes
    assert "MISSING_PREVIOUS_VINTAGE" in codes


def test_run_records_missing_contracted_position():
    points = [
        _pt("v1", 60, sp=24, p10=180, p50=200, p90=220),
        _pt("v2", 10, sp=24, p10=150, p50=164, p90=196),
    ]
    run = _service().compute_run(points, as_of=AT0, q_by_period={})
    assert run.revisions == ()
    assert run.batch is None
    assert run.skipped[0].error_code == "MISSING_CONTRACTED_POSITION"


def test_run_mode_is_carried_through():
    run = _service().compute_run(
        _revision_delta36(),
        as_of=AT0,
        q_by_period={"SP24": 0.0},
        run_mode=RunMode.HISTORICAL_REPLAY,
    )
    assert run.run_mode is RunMode.HISTORICAL_REPLAY
    assert run.revisions[0].run_mode is RunMode.HISTORICAL_REPLAY


# ---------------------------------------------------------------------------
# Module boundaries (split verification)
# ---------------------------------------------------------------------------


def test_models_live_in_models_module():
    from cockpit import forecast_revision_models as models

    for cls in (
        models.VintageForecastPoint,
        models.ForecastComparison,
        models.PortfolioEffect,
        models.RevisionSignificance,
        models.MaterialityAssessment,
        models.ForecastRevision,
        models.ForecastRevisionBatch,
        models.SkippedPeriod,
        models.ForecastRevisionRun,
        models.MaterialityConfig,
        models.CalibrationBasis,
        models.ForecastRevisionError,
    ):
        assert cls.__module__ == "cockpit.forecast_revision_models"


def test_calibration_lives_in_calibration_module():
    from cockpit import forecast_calibration as calib

    for member in (
        calib.horizon_bucket,
        calib.CalibrationResult,
        calib.ResidualSample,
        calib.ForecastErrorCalibration,
        calib.UnavailableCalibration,
        calib.AssumptionCalibration,
        calib.SampleDerivedCalibration,
    ):
        assert member.__module__ == "cockpit.forecast_calibration"


def test_service_lives_in_service_module_and_reexports_surface():
    from cockpit import forecast_revision as svc

    assert svc.ForecastRevisionService.__module__ == "cockpit.forecast_revision"
    # Backward-compatible re-export surface for callers importing from the service module.
    for name in (
        "VintageForecastPoint",
        "ForecastRevision",
        "RevisionSignificance",
        "MaterialityAssessment",
        "CalibrationBasis",
        "AssumptionCalibration",
        "SampleDerivedCalibration",
        "UnavailableCalibration",
        "ForecastErrorCalibration",
        "CalibrationResult",
        "ResidualSample",
        "horizon_bucket",
        "ForecastRevisionService",
        "SERVICE",
    ):
        assert name in svc.__all__
        assert hasattr(svc, name)


def test_models_module_does_not_depend_on_siblings():
    from cockpit import forecast_revision_models as models

    # No class visible in the models namespace originates from a sibling module,
    # i.e. models depends on neither calibration nor the service (no cycle).
    referenced = {value.__module__ for value in vars(models).values() if isinstance(value, type)}
    assert "cockpit.forecast_calibration" not in referenced
    assert "cockpit.forecast_revision" not in referenced


def test_calibration_module_depends_on_models_only():
    from cockpit import forecast_calibration as calib

    referenced = {value.__module__ for value in vars(calib).values() if isinstance(value, type)}
    assert "cockpit.forecast_revision" not in referenced  # calibration must not import the service
