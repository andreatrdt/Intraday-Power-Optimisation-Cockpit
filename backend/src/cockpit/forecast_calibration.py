"""Forecast-error calibration providers for the forecast revision service.

A calibration provider supplies the forecast-error standard deviation used to
standardise a P50 revision into a z-score. Every result is honestly labelled via
:class:`~cockpit.forecast_revision_models.CalibrationBasis`; **no provider ever
fabricates a std**. The default provider is :class:`UnavailableCalibration`.

Note the statistical caveat (see ``docs/forecast-revision.md``): this std is the
dispersion of forecast *errors* (actual − forecast); it is used as a proxy to
standardise a forecast-to-forecast *revision*. Error dispersion and revision
dispersion are not generally identical.
"""

from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime

from cockpit.forecast_revision_models import CalibrationBasis, _Frozen


def horizon_bucket(minutes: float) -> str:
    """Coarse, transparent horizon bucket used to look up an error std."""
    m = max(0.0, minutes)
    if m < 30:
        return "0-30m"
    if m < 60:
        return "30-60m"
    if m < 120:
        return "1-2h"
    if m < 240:
        return "2-4h"
    if m < 480:
        return "4-8h"
    return "8h+"


class CalibrationResult(_Frozen):
    error_std_mwh: float | None
    sample_size: int
    horizon_bucket: str
    basis: CalibrationBasis
    note: str | None = None


class ResidualSample(_Frozen):
    """One historical/simulated forecast-error observation.

    ``error_mwh`` is the signed forecast error for a delivered period at the
    given lead time (e.g. ``actual − forecast``); only its dispersion is used.
    """

    settlement_period: int
    horizon_minutes: float
    error_mwh: float


class ForecastErrorCalibration(ABC):
    """Pluggable forecast-error standard-deviation provider.

    Implementations must be honest about provenance via
    :class:`CalibrationBasis`; the service never fabricates a std.
    """

    @abstractmethod
    def error_std(
        self,
        *,
        settlement_period: int,
        horizon_minutes: float,
        delivery_start: datetime,
        as_of: datetime,
    ) -> CalibrationResult: ...


class UnavailableCalibration(ForecastErrorCalibration):
    """Default provider: no historical residuals are configured."""

    def error_std(self, *, settlement_period, horizon_minutes, delivery_start, as_of) -> CalibrationResult:
        return CalibrationResult(
            error_std_mwh=None,
            sample_size=0,
            horizon_bucket=horizon_bucket(horizon_minutes),
            basis=CalibrationBasis.UNAVAILABLE,
            note="No historical forecast-error residuals are configured; z-score is unavailable.",
        )


class AssumptionCalibration(ForecastErrorCalibration):
    """An explicit, honestly-labelled assumed std (never silent)."""

    def __init__(self, error_std_mwh: float, *, note: str | None = None) -> None:
        if error_std_mwh <= 0:
            raise ValueError("Assumed forecast-error std must be positive")
        self._std = float(error_std_mwh)
        self._note = note or "Explicit assumed forecast-error std; not historically calibrated."

    def error_std(self, *, settlement_period, horizon_minutes, delivery_start, as_of) -> CalibrationResult:
        return CalibrationResult(
            error_std_mwh=self._std,
            sample_size=0,
            horizon_bucket=horizon_bucket(horizon_minutes),
            basis=CalibrationBasis.ASSUMPTION_BASED,
            note=self._note,
        )


class SampleDerivedCalibration(ForecastErrorCalibration):
    """Std derived from SAMPLE simulated residuals — demonstration only.

    Buckets residuals by horizon and returns a per-bucket std, always labelled
    ``SAMPLE_DERIVED``. A bucket with fewer than ``min_samples`` observations, or
    zero dispersion, is reported as ``UNAVAILABLE`` — never back-filled.
    """

    def __init__(self, samples: Iterable[ResidualSample], *, min_samples: int = 5) -> None:
        if min_samples < 2:
            raise ValueError("min_samples must be at least 2 to estimate a std")
        self._min_samples = min_samples
        self._by_bucket: dict[str, list[float]] = {}
        for sample in samples:
            self._by_bucket.setdefault(horizon_bucket(sample.horizon_minutes), []).append(sample.error_mwh)

    def error_std(self, *, settlement_period, horizon_minutes, delivery_start, as_of) -> CalibrationResult:
        bucket = horizon_bucket(horizon_minutes)
        errors = self._by_bucket.get(bucket, [])
        if len(errors) < self._min_samples:
            return CalibrationResult(
                error_std_mwh=None,
                sample_size=len(errors),
                horizon_bucket=bucket,
                basis=CalibrationBasis.UNAVAILABLE,
                note=(
                    f"Only {len(errors)} SAMPLE residual(s) in bucket {bucket}; "
                    f"need {self._min_samples} to derive a std."
                ),
            )
        std = statistics.stdev(errors)
        if std <= 0:
            return CalibrationResult(
                error_std_mwh=None,
                sample_size=len(errors),
                horizon_bucket=bucket,
                basis=CalibrationBasis.UNAVAILABLE,
                note=f"SAMPLE residuals in bucket {bucket} have zero dispersion; z-score unavailable.",
            )
        return CalibrationResult(
            error_std_mwh=round(std, 6),
            sample_size=len(errors),
            horizon_bucket=bucket,
            basis=CalibrationBasis.SAMPLE_DERIVED,
            note="Std derived from SAMPLE simulated residuals; not historically calibrated.",
        )
