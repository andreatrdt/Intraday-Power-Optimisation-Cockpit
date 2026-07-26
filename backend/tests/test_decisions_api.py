"""Milestone 3 — decisions API (list / get / batches / refresh)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_orchestrator import ORCHESTRATOR, AdapterSnapshot, OptimiserPeriodView
from cockpit.decision_service import DECISIONS
from cockpit.forecast_revision import VintageForecastPoint

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DSTART = AT0 + timedelta(minutes=90)


@pytest.fixture(autouse=True)
def _clean_store():
    DECISIONS.clear()
    ORCHESTRATOR._revisions.clear()
    yield
    DECISIONS.clear()
    ORCHESTRATOR._revisions.clear()


def _seed_decision_via_orchestrator():
    """Create one real decision in the global store using injected full data."""
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


def test_decisions_list_is_empty_initially(client):
    response = client.get("/api/v1/decisions")
    assert response.status_code == 200
    assert response.json()["decisions"] == []


def test_get_and_list_decisions_and_batches(client):
    result = _seed_decision_via_orchestrator()
    decision_id = result.created_decision_ids[0]

    listing = client.get("/api/v1/decisions")
    assert listing.status_code == 200
    ids = [d["decision_id"] for d in listing.json()["decisions"]]
    assert decision_id in ids

    one = client.get(f"/api/v1/decisions/{decision_id}")
    assert one.status_code == 200
    body = one.json()["decision"]
    assert body["status"] == "PROPOSED"
    assert body["not_executable"] is True
    assert body["context"]["trigger_type"] == "FORECAST_REVISION"
    assert body["recommendation"]["action"] == "BUY"

    batches = client.get("/api/v1/decision-batches")
    assert batches.status_code == 200
    batch_ids = [b["batch_id"] for b in batches.json()["batches"]]
    assert result.batch_id in batch_ids

    one_batch = client.get(f"/api/v1/decision-batches/{result.batch_id}")
    assert one_batch.status_code == 200
    assert decision_id in one_batch.json()["batch"]["decision_ids"]


def test_unknown_decision_and_batch_return_404(client):
    assert client.get("/api/v1/decisions/dec-nope").status_code == 404
    assert client.get("/api/v1/decision-batches/batch-nope").status_code == 404


def test_refresh_on_sample_is_honest_and_non_executable(client):
    response = client.post("/api/v1/decisions/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True
    assert body["trustworthy_for_live_trading"] is False
    # SAMPLE has no previous full quantiles -> nothing created, explicit skips.
    assert body["refresh"]["created_decision_ids"] == []
    assert any(s["code"] == "MISSING_PREVIOUS_VINTAGE" for s in body["refresh"]["skipped"])
    assert body["created"] == []
