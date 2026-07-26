"""Frozen data contracts for the forecast revision service (Milestone 2).

Pure models only — no selection, calculation, calibration or orchestration
logic. The service lives in :mod:`cockpit.forecast_revision`; forecast-error
providers live in :mod:`cockpit.forecast_calibration`.

Sign convention (shared with ``position_layer`` and the README):

    I_t^s = G_t^s − Q_t

    where G_t^s is forecast generation for quantile/scenario ``s`` and Q_t is the
    contracted position. Positive exposure is LONG, negative is SHORT, and
    |exposure| within the flat tolerance is FLAT.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cockpit.decision_models import RunMode
from cockpit.models import Quality, SourceMode


class _Frozen(BaseModel):
    """Immutable base for every forecast-revision record."""

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CalibrationBasis(StrEnum):
    """Provenance of the forecast-error statistic behind a z-score/significance.

    ``CALIBRATED``  -> genuine historical forecast-error residuals.
    ``SAMPLE_DERIVED`` -> derived from SAMPLE simulated residuals (demo only).
    ``ASSUMPTION_BASED`` -> an explicit, caller-supplied assumed std.
    ``UNAVAILABLE`` -> no usable statistic; z-score/significance are null.
    """

    CALIBRATED = "CALIBRATED"
    SAMPLE_DERIVED = "SAMPLE_DERIVED"
    ASSUMPTION_BASED = "ASSUMPTION_BASED"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Errors (reject, never silently repair)
# ---------------------------------------------------------------------------


class ForecastRevisionError(ValueError):
    code = "FORECAST_REVISION_ERROR"


class LookAheadError(ForecastRevisionError):
    code = "LOOK_AHEAD"


class MissingLatestVintageError(ForecastRevisionError):
    code = "MISSING_LATEST_VINTAGE"


class MissingPreviousVintageError(ForecastRevisionError):
    code = "MISSING_PREVIOUS_VINTAGE"


class InvalidQuantileOrderError(ForecastRevisionError):
    code = "INVALID_QUANTILE_ORDER"


class UnitMismatchError(ForecastRevisionError):
    code = "UNIT_MISMATCH"


class SettlementPeriodMismatchError(ForecastRevisionError):
    code = "SETTLEMENT_PERIOD_MISMATCH"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class VintageForecastPoint(_Frozen):
    """One vintage's P10/P50/P90 forecast for one settlement period.

    The sole forecast input to the service, deliberately free of market/portfolio
    concepts (Q and Gate Closure are passed separately).
    """

    vintage_id: str
    published_at: datetime
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    p10_mwh: float
    p50_mwh: float
    p90_mwh: float
    unit: str = "MWh"
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    lineage_value_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class MaterialityConfig(_Frozen):
    """Configurable, explainable materiality thresholds and score weights."""

    absolute_mwh_threshold: float = 10.0
    percentage_threshold: float = 0.10
    z_score_threshold: float = 1.5
    p50_exposure_change_threshold_mwh: float = 10.0
    gate_closure_minutes_threshold: float = 90.0
    direction_flip_is_material: bool = True
    min_percentage_denominator_mwh: float = 1.0
    flat_tolerance_mwh: float = 0.05

    weight_absolute: float = 1.0
    weight_zscore: float = 1.0
    weight_exposure: float = 1.0
    weight_direction_flip: float = 1.0
    weight_gate_closure: float = 0.5


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


class ForecastComparison(_Frozen):
    previous_p10_mwh: float
    previous_p50_mwh: float
    previous_p90_mwh: float
    latest_p10_mwh: float
    latest_p50_mwh: float
    latest_p90_mwh: float
    delta_p10_mwh: float
    delta_p50_mwh: float
    delta_p90_mwh: float
    previous_uncertainty_width_mwh: float
    latest_uncertainty_width_mwh: float
    uncertainty_width_change_mwh: float
    absolute_revision_mwh: float
    percentage_revision: float | None
    forecast_horizon_minutes: float
    unit: str = "MWh"


class PortfolioEffect(_Frozen):
    contracted_position_q_mwh: float
    previous_p10_exposure_mwh: float
    previous_p50_exposure_mwh: float
    previous_p90_exposure_mwh: float
    latest_p10_exposure_mwh: float
    latest_p50_exposure_mwh: float
    latest_p90_exposure_mwh: float
    delta_p50_exposure_mwh: float
    direction_before: str
    direction_after: str
    crossed_zero_exposure: bool


class RevisionSignificance(_Frozen):
    """Standardisation of the P50 revision against forecast-error dispersion.

    ``revision_significance_score`` measures the *statistical unusualness* of the
    revision (how far it is from zero in units of forecast-error dispersion), not
    the reliability of the latest forecast. See ``docs/forecast-revision.md``.
    """

    revision_z_score: float | None
    error_std_mwh: float | None
    revision_significance_score: float | None
    calibration_basis: CalibrationBasis
    calibration_sample_size: int
    calibration_horizon_bucket: str
    note: str | None = None


class MaterialityAssessment(_Frozen):
    absolute_volume_material: bool
    standardised_revision_material: bool
    exposure_change_material: bool
    direction_flip_material: bool
    gate_closure_material: bool
    signal_materiality_score: float
    is_material: bool
    materiality_reasons: tuple[str, ...]


class ForecastRevision(_Frozen):
    """A single-period, frozen forecast-revision result."""

    revision_id: str
    calculated_at: datetime
    as_of: datetime

    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime

    latest_forecast_vintage_id: str
    previous_forecast_vintage_id: str
    latest_publication_time: datetime
    previous_publication_time: datetime
    source_mode: SourceMode
    quality: Quality
    run_mode: RunMode
    lineage_value_ids: tuple[str, ...] = ()

    comparison: ForecastComparison
    portfolio: PortfolioEffect
    significance: RevisionSignificance
    materiality: MaterialityAssessment

    @property
    def is_material(self) -> bool:
        return self.materiality.is_material

    @property
    def is_trigger_candidate(self) -> bool:
        return self.materiality.is_material


class ForecastRevisionBatch(_Frozen):
    """Lightweight index over the revisions from one vintage update.

    Holds only IDs and affected periods — no aggregate exposure or P&L.
    """

    batch_id: str
    created_at: datetime
    as_of: datetime
    latest_vintage_id: str
    run_mode: RunMode = RunMode.SAMPLE_DEMO
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    revision_ids: tuple[str, ...] = ()
    affected_delivery_periods: tuple[str, ...] = ()


class SkippedPeriod(_Frozen):
    settlement_period: int
    delivery_period: str | None
    error_code: str
    message: str


class ForecastRevisionRun(_Frozen):
    """Everything produced from one call across many periods."""

    run_id: str
    calculated_at: datetime
    as_of: datetime
    latest_vintage_id: str | None
    run_mode: RunMode
    revisions: tuple[ForecastRevision, ...]
    batch: ForecastRevisionBatch | None
    trigger_candidate_ids: tuple[str, ...]
    skipped: tuple[SkippedPeriod, ...]
