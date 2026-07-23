"""Live State page relies on the historical time series in /api/v1/live-state.

These guard the backend contract the LiveStatePage time-series charts depend on:
non-empty SAMPLE renewable production history, plausible non-zero values, units and
source mode attached, and timestamps that actually span the Today / 24h / 7d / 30d
history windows the page offers.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _live_state(client) -> dict:
    response = client.get("/api/v1/live-state")
    assert response.status_code == 200, response.text
    return response.json()["live_state"]


def _production_series(live: dict) -> dict[str, dict]:
    return {series["key"]: series for series in live["chart_series"]["production"]}


def test_live_state_returns_non_empty_sample_production_history(client) -> None:
    live = _live_state(client)

    # Source mode is attached at the state level and is SAMPLE in the simulated env.
    assert live["state"]["state_source_mode"] == "SAMPLE"

    series_by_key = _production_series(live)
    for key in ("production", "wind", "solar", "forecast_actual"):
        assert key in series_by_key, f"missing {key} renewable series"
        series = series_by_key[key]
        assert series["points"], f"{key} history must not be empty in SAMPLE mode"
        # Units are attached to every series (MW here, not a mislabelled GW / fraction).
        assert series["unit"] == "MW", f"{key} unit should be MW, got {series['unit']!r}"
        # Every point carries a wall-clock timestamp (the x-axis of the time series).
        assert all(point["timestamp"] for point in series["points"]), f"{key} points need timestamps"


def test_production_wind_solar_forecast_are_not_all_zero(client) -> None:
    series_by_key = _production_series(_live_state(client))

    for key in ("production", "wind", "forecast_actual"):
        values = [point["value"] for point in series_by_key[key]["points"]]
        assert any(abs(value) > 1e-6 for value in values), f"{key} values are all zero"
        # Plausible MW magnitude — not a value accidentally divided down to ~1.
        assert max(values) > 1.0, f"{key} peak {max(values)} MW is implausibly small"

    # Solar legitimately hits zero overnight, but daytime production must be present.
    solar_values = [point["value"] for point in series_by_key["solar"]["points"]]
    assert max(solar_values) > 1.0, "solar never rises above ~1 MW across 30 days"


def test_production_history_timestamps_cover_today_24h_7d_30d(client) -> None:
    live = _live_state(client)
    now = datetime.fromisoformat(live["state"]["current_time"])
    production = _production_series(live)["production"]
    timestamps = [datetime.fromisoformat(point["timestamp"]) for point in production["points"]]

    assert timestamps, "production history has no timestamps"
    assert max(timestamps) - min(timestamps) >= timedelta(days=29), "history should span ~30 days"

    def has_point_within(delta: timedelta) -> bool:
        start = now - delta
        return any(start <= timestamp <= now for timestamp in timestamps)

    assert has_point_within(timedelta(hours=24)), "no point inside the Last 24h window"
    assert has_point_within(timedelta(days=7)), "no point inside the Last 7d window"
    assert has_point_within(timedelta(days=30)), "no point inside the Last 30d window"

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert any(midnight <= timestamp <= now for timestamp in timestamps), "no point inside the Today window"


def test_production_canonical_value_carries_unit_and_source_mode(client) -> None:
    live = _live_state(client)
    production_value = live["production_demand"]["values"]["renewable_production_mw"]

    assert production_value["unit"] == "MW"
    # Lineage carries an explicit source mode so the value is never silently trusted.
    assert production_value["lineage"]["source_mode"] == "SAMPLE"
