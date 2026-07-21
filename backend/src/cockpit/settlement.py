"""DST-aware GB settlement-period utilities.

Storage is UTC. Europe/London is used only to derive GB settlement-date and
settlement-period labels. A period is always thirty real minutes, including on
46- and 50-period clock-change days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
PERIOD = timedelta(minutes=30)


@dataclass(frozen=True)
class SettlementPeriod:
    settlement_date: date
    settlement_period: int
    start_utc: datetime
    end_utc: datetime

    @property
    def duration_hours(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 3600

    @property
    def label(self) -> str:
        return f"{self.settlement_date.isoformat()} SP{self.settlement_period:02d}"


def _midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=LONDON).astimezone(UTC)


def settlement_periods_for_day(day: date) -> list[SettlementPeriod]:
    start = _midnight_utc(day)
    end = _midnight_utc(day + timedelta(days=1))
    count = int((end - start) / PERIOD)
    return [
        SettlementPeriod(day, index + 1, start + index * PERIOD, start + (index + 1) * PERIOD)
        for index in range(count)
    ]


def settlement_period_for_instant(instant: datetime) -> SettlementPeriod:
    if instant.tzinfo is None:
        raise ValueError("Settlement instants must be timezone-aware")
    instant = instant.astimezone(UTC)
    local_day = instant.astimezone(LONDON).date()
    start = _midnight_utc(local_day)
    index = int((instant - start) / PERIOD)
    period_start = start + index * PERIOD
    return SettlementPeriod(local_day, index + 1, period_start, period_start + PERIOD)


def upcoming_periods(as_of: datetime, count: int = 8) -> list[SettlementPeriod]:
    current = settlement_period_for_instant(as_of)
    result: list[SettlementPeriod] = []
    cursor = current.start_utc
    for _ in range(count):
        result.append(settlement_period_for_instant(cursor))
        cursor += PERIOD
    return result
