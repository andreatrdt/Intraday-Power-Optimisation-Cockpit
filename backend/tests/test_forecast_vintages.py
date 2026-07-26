"""Milestone 3.5 — immutable complete forecast vintages and bounded retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.forecast_vintages import (
    ForecastVintagePeriod,
    ForecastVintageSnapshot,
    ForecastVintageStore,
)
from cockpit.models import Quality, SourceMode

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _period(sp=24, *, p10=180.0, p50=200.0, p90=220.0, lineage=()):
    start = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * (sp - 24))
    return ForecastVintagePeriod(
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        delivery_start=start,
        delivery_end=start + timedelta(minutes=30),
        p10_mwh=p10,
        p50_mwh=p50,
        p90_mwh=p90,
        lineage_value_ids=lineage,
    )


def _vintage(vintage_id, minutes_before, *periods):
    return ForecastVintageSnapshot(
        vintage_id=vintage_id,
        published_at=AT - timedelta(minutes=minutes_before),
        as_of=AT,
        source_mode=SourceMode.SAMPLE,
        quality=Quality.FRESH,
        periods=tuple(periods) or (_period(),),
    )


def test_first_vintage_has_no_predecessor():
    store = ForecastVintageStore()
    store.retain(_vintage("v1", 10))
    latest, previous = store.latest_and_previous(AT)
    assert latest is not None and latest.vintage_id == "v1"
    assert previous is None


def test_store_retains_full_previous_quantiles():
    store = ForecastVintageStore()
    store.retain(_vintage("v1", 60, _period(24, p10=180, p50=200, p90=220)))
    store.retain(_vintage("v2", 10, _period(24, p10=150, p50=164, p90=196)))
    latest, previous = store.latest_and_previous(AT)
    assert latest.vintage_id == "v2"
    assert previous.vintage_id == "v1"
    # The full previous P10/P50/P90 are retained verbatim (not reconstructed).
    prev_period = previous.period_for("SP24")
    assert (prev_period.p10_mwh, prev_period.p50_mwh, prev_period.p90_mwh) == (180.0, 200.0, 220.0)


def test_vintages_are_immutable():
    vintage = _vintage("v1", 10, _period(24))
    with pytest.raises(ValidationError):
        vintage.vintage_id = "hacked"
    with pytest.raises(ValidationError):
        vintage.periods[0].p50_mwh = 0.0
    assert isinstance(vintage.periods, tuple)
    with pytest.raises(AttributeError):
        vintage.periods.append(_period(25))


def test_retention_is_bounded():
    store = ForecastVintageStore(max_vintages=3)
    for index in range(5):
        store.retain(_vintage(f"v{index}", 100 - index * 10))  # publication time increasing
    assert len(store) == 3
    kept = {vintage.vintage_id for vintage in store.all()}
    assert kept == {"v2", "v3", "v4"}  # oldest two dropped


def test_selection_uses_publication_time_not_insertion_order():
    store = ForecastVintageStore()
    # Insert the later-published vintage FIRST, then the earlier one.
    store.retain(_vintage("v_late", 10))
    store.retain(_vintage("v_early", 60))
    latest, previous = store.latest_and_previous(AT)
    assert latest.vintage_id == "v_late"  # by publication time, not insertion accident
    assert previous.vintage_id == "v_early"


def test_future_vintage_is_excluded():
    store = ForecastVintageStore()
    store.retain(_vintage("v1", 60))
    store.retain(_vintage("v_future", -30))  # published after as_of
    eligible_ids = [v.vintage_id for v in store.eligible(AT)]
    assert "v_future" not in eligible_ids
    latest, previous = store.latest_and_previous(AT)
    assert latest.vintage_id == "v1"
    assert previous is None


def test_lineage_ids_are_preserved():
    store = ForecastVintageStore()
    store.retain(_vintage("v1", 10, _period(24, lineage=("id-p10", "id-p50", "id-p90"))))
    period = store.all()[0].period_for("SP24")
    assert period.lineage_value_ids == ("id-p10", "id-p50", "id-p90")


def test_store_requires_capacity_for_a_predecessor():
    with pytest.raises(ValueError):
        ForecastVintageStore(max_vintages=1)
