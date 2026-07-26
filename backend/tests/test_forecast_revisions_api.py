"""Milestone 3.5 — forecast-revisions API and decision→revision resolution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_orchestrator import ORCHESTRATOR, AdapterSnapshot, OptimiserPeriodView
from cockpit.decision_service import DECISIONS
from cockpit.forecast_revision import VintageForecastPoint

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DSTART = AT0 + timedelta(minutes=90)


@pytest.fixture(autouse=True)
def _clean_stores():
    DECISIONS.clear()
    ORCHESTRATOR.reset()
    yield
    DECISIONS.clear()
    ORCHESTRATOR.reset()


def _seed():
    """Create one real decision + revision in the global stores via injected data."""
    points = (
        VintageForecastPoint(
            vintage_id="v1", published_at=AT0 - timedelta(minutes=60), settlement_period=24,
            delivery_period="SP24", delivery_start=DSTART, delivery_end=DSTART + timedelta(minutes=30),
            p10_mwh=180, p50_mwh=200, p90_mwh=220,
        ),
        VintageForecastPoint(
            vintage_id="v2", published_at=AT0 - timedelta(minutes=10), settlement_period=24,
            delivery_period="SP24", delivery_start=DSTART, delivery_end=DSTART + timedelta(minutes=30),
            p10_mwh=150, p50_mwh=164, p90_mwh=196,
        ),
    )
    snap = AdapterSnapshot(
        as_of=AT0,
        market_snapshot_id="book-1",
        optimisation_run_id="opt-1",
        forecast_points=points,
        q_by_period={"SP24": 210.0},
        gate_closure_by_period={"SP24": AT0 + timedelta(minutes=85)},
        optimiser_by_sp={24: OptimiserPeriodView(settlement_period=24, delivery_period="SP24", buy_mwh=32.0, sell_mwh=0.0)},
    )
    return ORCHESTRATOR.process(snap, now=AT0)


def test_forecast_revisions_list_and_get(client):
    _seed()
    listing = client.get("/api/v1/forecast-revisions")
    assert listing.status_code == 200
    revisions = listing.json()["revisions"]
    assert len(revisions) >= 1
    revision_id = revisions[0]["revision_id"]

    one = client.get(f"/api/v1/forecast-revisions/{revision_id}")
    assert one.status_code == 200
    assert one.json()["revision"]["settlement_period"] == 24


def test_decision_forecast_revision_id_resolves_via_api(client):
    result = _seed()
    decision_id = result.created_decision_ids[0]

    decision = client.get(f"/api/v1/decisions/{decision_id}").json()["decision"]
    revision_id = decision["context"]["forecast_revision_id"]
    assert revision_id is not None

    revision = client.get(f"/api/v1/forecast-revisions/{revision_id}")
    assert revision.status_code == 200
    body = revision.json()["revision"]
    assert body["settlement_period"] == decision["context"]["settlement_period"]
    # Previous exposures live on the revision, not duplicated on the decision.
    assert body["portfolio"]["previous_p50_exposure_mwh"] == -10.0  # 200 - 210


def test_unknown_forecast_revision_returns_404(client):
    assert client.get("/api/v1/forecast-revisions/rev-nope").status_code == 404


def test_forecast_revision_runs_route(client):
    _seed()
    runs = client.get("/api/v1/forecast-revision-runs")
    assert runs.status_code == 200
    assert len(runs.json()["runs"]) >= 1
