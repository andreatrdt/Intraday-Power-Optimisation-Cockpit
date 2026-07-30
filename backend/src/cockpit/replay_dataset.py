"""Replay dataset contract, SAMPLE builder and the point-in-time look-ahead guard (M8).

The dataset is an immutable, event-time-indexed source of every input the decision
workflow needs. The :class:`PointInTimeView` is the mandatory access layer: at replay
clock ``t`` it only returns information with ``publication_time <= t`` /
``source_available_at <= t`` and raises :class:`LookAheadViolation` otherwise. All
dataset reads used by decision creation must go through it — this is enforced
programmatically, not by developer discipline.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from cockpit.models import Quality, SourceMode
from cockpit.replay_models import (
    DatasetOrderBookLevel,
    DatasetPeriodRecord,
    DatasetValidation,
    DatasetVintagePoint,
    IntegrityStatus,
    LookAheadKind,
    LookAheadViolationRecord,
    ReplayMode,
)


class LookAheadViolation(Exception):
    """A dataset read requested information not yet available at the replay clock."""

    def __init__(self, kind: LookAheadKind, *, replay_clock: datetime, delivery_period: str | None, requested_field: str, detail: str) -> None:
        self.record = LookAheadViolationRecord(
            kind=kind, replay_clock=replay_clock, delivery_period=delivery_period, requested_field=requested_field, detail=detail
        )
        super().__init__(detail)


class ReplayDataset:
    """Immutable dataset: periods + their two forecast vintages, with metadata."""

    def __init__(
        self,
        *,
        dataset_id: str,
        run_mode: ReplayMode,
        source_mode: SourceMode,
        quality: Quality,
        periods: tuple[DatasetPeriodRecord, ...],
        vintages: tuple[DatasetVintagePoint, ...],
    ) -> None:
        self.dataset_id = dataset_id
        self.run_mode = run_mode
        self.source_mode = source_mode
        self.quality = quality
        self.periods = periods
        self.vintages = vintages
        self._by_sp = {p.settlement_period: p for p in periods}

    # -- access -------------------------------------------------------------

    def period_for_sp(self, settlement_period: int) -> DatasetPeriodRecord | None:
        return self._by_sp.get(settlement_period)

    def vintages_for(self, delivery_period: str) -> list[DatasetVintagePoint]:
        return sorted(
            (v for v in self.vintages if v.delivery_period == delivery_period),
            key=lambda v: (v.published_at, v.vintage_id),
        )

    def bounds(self) -> tuple[datetime, datetime]:
        starts = [p.delivery_start for p in self.periods]
        ends = [p.delivery_end for p in self.periods]
        return (min(starts), max(ends)) if self.periods else (datetime.min.replace(tzinfo=timezone.utc), datetime.min.replace(tzinfo=timezone.utc))

    def validate(self) -> DatasetValidation:
        """Structural validation: no silent gaps; every period has ≥2 vintages and
        realised fields carry a source_available_at not before delivery_end."""
        issues: list[str] = []
        for period in self.periods:
            vintages = self.vintages_for(period.delivery_period)
            if len(vintages) < 2:
                issues.append(f"{period.delivery_period}: fewer than two forecast vintages")
            if period.source_available_at is not None and period.source_available_at < period.delivery_end:
                issues.append(f"{period.delivery_period}: realised source_available_at precedes delivery_end")
            if period.realised_generation_mwh is not None and period.source_available_at is None:
                issues.append(f"{period.delivery_period}: realised generation without source_available_at")
        status = IntegrityStatus.OK if not issues else IntegrityStatus.DATASET_INVALID
        return DatasetValidation(
            status=status,
            dataset_id=self.dataset_id,
            period_count=len(self.periods),
            vintage_count=len(self.vintages),
            issues=tuple(issues),
        )


# ---------------------------------------------------------------------------
# Point-in-time guard
# ---------------------------------------------------------------------------


class PointInTimeView:
    """Clock-gated access layer. Reject any read of information dated after the clock."""

    def __init__(self, dataset: ReplayDataset, clock: "callable[[], datetime]", *, violation_sink: list[LookAheadViolationRecord] | None = None) -> None:
        self._dataset = dataset
        self._clock = clock
        self.violations: list[LookAheadViolationRecord] = violation_sink if violation_sink is not None else []

    @property
    def now(self) -> datetime:
        return self._clock()

    def _violation(self, kind: LookAheadKind, *, delivery_period: str | None, field: str, detail: str) -> LookAheadViolation:
        error = LookAheadViolation(kind, replay_clock=self.now, delivery_period=delivery_period, requested_field=field, detail=detail)
        self.violations.append(error.record)
        return error

    # -- point-in-time reads for decision creation --------------------------

    def vintage_points_asof(self) -> tuple[DatasetVintagePoint, ...]:
        """Only vintages published at or before the clock (latest look-ahead-safe view)."""
        t = self.now
        return tuple(v for v in self._dataset.vintages if v.published_at <= t)

    def active_market_periods(self) -> tuple[DatasetPeriodRecord, ...]:
        """Periods whose market snapshot is available and whose delivery is still in
        the future at the clock (the tradeable horizon)."""
        t = self.now
        return tuple(
            p for p in self._dataset.periods
            if p.market_available_at <= t and p.delivery_end > t
        )

    def market_period(self, settlement_period: int) -> DatasetPeriodRecord:
        """A period's market data, only if its market snapshot is available now."""
        period = self._dataset.period_for_sp(settlement_period)
        if period is None:
            raise KeyError(f"No dataset period for SP{settlement_period}")
        if period.market_available_at > self.now:
            raise self._violation(
                LookAheadKind.FUTURE_MARKET_SNAPSHOT, delivery_period=period.delivery_period,
                field="market_snapshot", detail=f"Market snapshot for {period.delivery_period} not available until {period.market_available_at.isoformat()}.",
            )
        return period

    def realised_period(self, settlement_period: int) -> DatasetPeriodRecord:
        """A period's realised generation + settlement prices — only after they are
        genuinely available (never before delivery_end)."""
        period = self._dataset.period_for_sp(settlement_period)
        if period is None:
            raise KeyError(f"No dataset period for SP{settlement_period}")
        if period.source_available_at is None or period.realised_generation_mwh is None:
            raise self._violation(
                LookAheadKind.REALISED_BEFORE_DELIVERY, delivery_period=period.delivery_period,
                field="realised_generation", detail=f"No realised data recorded for {period.delivery_period}.",
            )
        if self.now < period.source_available_at:
            raise self._violation(
                LookAheadKind.REALISED_BEFORE_DELIVERY, delivery_period=period.delivery_period,
                field="realised_generation", detail=f"Realised data for {period.delivery_period} not available until {period.source_available_at.isoformat()}.",
            )
        if (period.imbalance_buy_price_gbp_per_mwh is None or period.imbalance_sell_price_gbp_per_mwh is None):
            raise self._violation(
                LookAheadKind.SETTLEMENT_BEFORE_AVAILABLE, delivery_period=period.delivery_period,
                field="imbalance_prices", detail=f"Settlement prices for {period.delivery_period} unavailable.",
            )
        return period


# ---------------------------------------------------------------------------
# Deterministic SAMPLE dataset builder
# ---------------------------------------------------------------------------

SAMPLE_DATASET_ID = "sample-replay-v1"


def build_sample_dataset(
    *,
    dataset_id: str = SAMPLE_DATASET_ID,
    anchor: datetime | None = None,
    start_settlement_period: int = 20,
    period_count: int = 12,
    depth_scale: float = 1.0,
) -> ReplayDataset:
    """A fully deterministic SAMPLE replay dataset.

    Two forecast vintages per delivery period (a prior vintage and a materially
    revised latest vintage), a contracted position, an order book, gate closure,
    and realised generation + dual imbalance prices that become available only at
    ``delivery_end``. Values mirror the live SAMPLE conventions (exposure = gen − Q;
    realised = latest p50 + a deterministic per-period deviation). It is SAMPLE, not
    historical, and is never labelled otherwise.
    """
    anchor = anchor or datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc)
    previous_pub = anchor
    latest_pub = anchor + timedelta(minutes=30)

    periods: list[DatasetPeriodRecord] = []
    vintages: list[DatasetVintagePoint] = []

    for offset in range(period_count):
        sp = start_settlement_period + offset
        delivery_start = anchor + timedelta(hours=2) + timedelta(minutes=30 * offset)
        delivery_end = delivery_start + timedelta(minutes=30)
        gate_closure = delivery_start - timedelta(minutes=15)
        delivery_period = f"{delivery_start.date().isoformat()} SP{sp:02d}"

        # Forecast: a prior p50 and a materially lower revised p50 (a wind miss). The
        # downward revision is sized to clear the materiality threshold for every
        # period so the replay has a full sample; it does not tune realised outcomes.
        base_p50 = 120.0 + 6.0 * math.sin(offset * 0.7)
        previous_p50 = base_p50
        latest_p50 = base_p50 - (16.0 + 3.0 * math.cos(offset * 0.5))  # material downward revision
        spread = 22.0
        for vintage_id, pub, p50 in (
            (f"vint-prev-{sp}", previous_pub, previous_p50),
            (f"vint-late-{sp}", latest_pub, latest_p50),
        ):
            vintages.append(DatasetVintagePoint(
                vintage_id=vintage_id, published_at=pub, settlement_period=sp, delivery_period=delivery_period,
                delivery_start=delivery_start, delivery_end=delivery_end,
                p10_mwh=round(p50 - spread, 3), p50_mwh=round(p50, 3), p90_mwh=round(p50 + spread, 3),
                lineage_value_ids=(f"lin-{vintage_id}",),
            ))

        contracted_q = round(base_p50, 3)  # hedged to the prior p50 → revision creates exposure
        reference = round(68.0 + 6.0 * math.sin(offset * 0.9), 3)
        # Realised generation: latest p50 plus a deterministic deviation (mirrors the
        # live SAMPLE actual-generation model). Available only at delivery_end.
        realised_generation = round(max(0.0, latest_p50 + 2.8 * math.sin(sp * 1.17 + 0.4)), 3)
        imbalance_spread = 0.12
        # Order book around the reference (finite depth on each side).
        bids = tuple(
            DatasetOrderBookLevel(side="BID", level=i, price_gbp_per_mwh=round(reference - 0.4 - 0.55 * i, 3), volume_mwh=round((18.0 - 2.0 * i) * depth_scale, 3))
            for i in range(4)
        )
        asks = tuple(
            DatasetOrderBookLevel(side="ASK", level=i, price_gbp_per_mwh=round(reference + 0.4 + 0.62 * i, 3), volume_mwh=round((18.0 - 2.0 * i) * depth_scale, 3))
            for i in range(4)
        )
        # Model recommendation: hedge toward the revised exposure (exposure = gen − Q).
        # Latest p50 < Q ⇒ SHORT exposure ⇒ reduce Q by SELLing (matches project convention).
        exposure = latest_p50 - contracted_q
        recommended_buy = round(max(0.0, exposure), 3)
        recommended_sell = round(max(0.0, -exposure), 3)

        periods.append(DatasetPeriodRecord(
            settlement_period=sp, delivery_period=delivery_period,
            delivery_start=delivery_start, delivery_end=delivery_end, gate_closure_at=gate_closure,
            market_snapshot_id=f"mkt-{sp}", market_available_at=latest_pub,
            reference_price_gbp_per_mwh=reference, bids=bids, asks=asks, contracted_q_mwh=contracted_q,
            recommended_buy_mwh=recommended_buy, recommended_sell_mwh=recommended_sell, tradeable=True,
            realised_generation_mwh=realised_generation,
            realised_reference_price_gbp_per_mwh=reference,
            imbalance_buy_price_gbp_per_mwh=round(reference * (1 + imbalance_spread), 3),
            imbalance_sell_price_gbp_per_mwh=round(reference * (1 - imbalance_spread), 3),
            source_available_at=delivery_end,
            lineage_value_ids=(f"lin-mkt-{sp}",),
        ))

    return ReplayDataset(
        dataset_id=dataset_id, run_mode=ReplayMode.SAMPLE_REPLAY,
        source_mode=SourceMode.SAMPLE, quality=Quality.FRESH,
        periods=tuple(periods), vintages=tuple(vintages),
    )
