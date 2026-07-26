"""Immutable complete forecast-vintage records and a bounded retention store.

Milestone 3.5. The rolling SAMPLE environment previously kept only the *latest*
full P10/P50/P90 vintage, so no genuine previous-vintage quantiles existed and
decision creation was impossible without fabricating them. This module holds the
retained history of **complete** vintages, so the forecast-revision adapter can
consume a genuine latest and previous vintage.

Records are frozen; the store never mutates a vintage after publication and
retains a bounded number of the most recent vintages.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from cockpit.models import Quality, SourceMode


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ForecastVintagePeriod(_Frozen):
    """One settlement period's complete P10/P50/P90 within a vintage."""

    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    p10_mwh: float
    p50_mwh: float
    p90_mwh: float
    unit: str = "MWh"
    lineage_value_ids: tuple[str, ...] = ()


class ForecastVintageSnapshot(_Frozen):
    """A complete, immutable forecast vintage covering many settlement periods."""

    vintage_id: str
    published_at: datetime
    as_of: datetime
    source_mode: SourceMode
    quality: Quality
    periods: tuple[ForecastVintagePeriod, ...] = ()

    def period_for(self, delivery_period: str) -> ForecastVintagePeriod | None:
        for period in self.periods:
            if period.delivery_period == delivery_period:
                return period
        return None

    @property
    def settlement_periods(self) -> tuple[int, ...]:
        return tuple(period.settlement_period for period in self.periods)


DEFAULT_MAX_VINTAGES = 16


class ForecastVintageStore:
    """Bounded, ordered, append-only store of complete forecast vintages.

    Retention rule (documented in ``docs/forecast-vintages.md``):

    * retains the most recent ``max_vintages`` complete vintages (default 16);
    * vintages are appended in creation (chronological) order and never mutated;
    * once capacity is exceeded the oldest vintage (by publication time) is
      dropped;
    * selection of latest/previous is by **publication time** (tie-broken by
      ``vintage_id``), not by insertion accident.
    """

    def __init__(self, max_vintages: int = DEFAULT_MAX_VINTAGES) -> None:
        if max_vintages < 2:
            raise ValueError("max_vintages must be at least 2 to retain a predecessor")
        self.max_vintages = max_vintages
        self._vintages: list[ForecastVintageSnapshot] = []

    def retain(self, vintage: ForecastVintageSnapshot) -> None:
        """Append a complete vintage, dropping the oldest beyond capacity."""
        self._vintages.append(vintage)
        if len(self._vintages) > self.max_vintages:
            # Drop the oldest by publication time (stable on ties).
            oldest = min(
                range(len(self._vintages)),
                key=lambda index: (self._vintages[index].published_at, index),
            )
            del self._vintages[oldest]

    def all(self) -> tuple[ForecastVintageSnapshot, ...]:
        """Every retained vintage, in insertion (chronological) order."""
        return tuple(self._vintages)

    def eligible(self, as_of: datetime) -> tuple[ForecastVintageSnapshot, ...]:
        """Retained vintages published on or before ``as_of``, ordered by
        (publication time, vintage_id)."""
        return tuple(
            sorted(
                (vintage for vintage in self._vintages if vintage.published_at <= as_of),
                key=lambda vintage: (vintage.published_at, vintage.vintage_id),
            )
        )

    def latest_and_previous(
        self, as_of: datetime
    ) -> tuple[ForecastVintageSnapshot | None, ForecastVintageSnapshot | None]:
        """The newest eligible vintage and its immediate eligible predecessor."""
        eligible = self.eligible(as_of)
        latest = eligible[-1] if eligible else None
        previous = eligible[-2] if len(eligible) >= 2 else None
        return latest, previous

    def clear(self) -> None:
        self._vintages.clear()

    def __len__(self) -> int:
        return len(self._vintages)
