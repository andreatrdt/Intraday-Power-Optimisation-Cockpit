from datetime import date, datetime

from cockpit.settlement import UTC, settlement_periods_for_day, upcoming_periods


def test_gb_dst_days_have_46_48_50_periods() -> None:
    assert len(settlement_periods_for_day(date(2026, 3, 29))) == 46
    assert len(settlement_periods_for_day(date(2026, 7, 21))) == 48
    assert len(settlement_periods_for_day(date(2026, 10, 25))) == 50


def test_upcoming_periods_cross_midnight_with_correct_labels() -> None:
    periods = upcoming_periods(datetime(2026, 7, 21, 22, 45, tzinfo=UTC), 4)
    assert periods[0].settlement_period == 48
    assert periods[1].settlement_date == date(2026, 7, 22)
    assert periods[1].settlement_period == 1
