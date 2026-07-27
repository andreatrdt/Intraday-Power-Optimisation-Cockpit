"""Milestone 4 — hedge-timing API routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_orchestrator import ORCHESTRATOR, AdapterSnapshot, OptimiserPeriodView
from cockpit.decision_prioritisation import HEDGE_TIMING
from cockpit.decision_service import DECISIONS
from cockpit.forecast_revision import VintageForecastPoint

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DSTART = AT0 + timedelta(minutes=90)


@pytest.fixture(autouse=True)
def _clean_stores():
    DECISIONS.clear()
    ORCHESTRATOR.reset()
    HEDGE_TIMING.reset()
    yield
    DECISIONS.clear()
    ORCHESTRATOR.reset()
    HEDGE_TIMING.reset()


def _seed():
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


def test_assess_timing_route_is_diagnostic_and_creates_assessments(client):
    _seed()
    response = client.post("/api/v1/decisions/assess-timing")
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True
    assert body["trustworthy_for_live_trading"] is False
    result = body["assessment"]
    assert len(result["created_assessment_ids"]) >= 1
    assert isinstance(result["prioritised"], list)
    assert isinstance(result["batch_summaries"], list)


def test_list_and_get_assessment(client):
    _seed()
    client.post("/api/v1/decisions/assess-timing")
    listing = client.get("/api/v1/hedge-timing-assessments")
    assert listing.status_code == 200
    assessments = listing.json()["assessments"]
    assert len(assessments) >= 1
    assessment_id = assessments[0]["assessment_id"]
    one = client.get(f"/api/v1/hedge-timing-assessments/{assessment_id}")
    assert one.status_code == 200
    body = one.json()["assessment"]
    assert body["not_executable"] is True
    assert "verdict" in body and "priority" in body
    assert body["policy_version"] == "hedge-timing-v1"


def test_unknown_assessment_returns_404(client):
    assert client.get("/api/v1/hedge-timing-assessments/tim-nope").status_code == 404


def test_repeated_assess_timing_is_idempotent(client):
    _seed()
    first = client.post("/api/v1/decisions/assess-timing").json()["assessment"]
    second = client.post("/api/v1/decisions/assess-timing").json()["assessment"]
    assert len(first["created_assessment_ids"]) >= 1
    assert second["created_assessment_ids"] == []
    assert len(second["existing_assessment_ids"]) >= 1


def test_batch_summaries_routes(client):
    result = _seed()
    client.post("/api/v1/decisions/assess-timing")
    summaries = client.get("/api/v1/decision-batch-summaries")
    assert summaries.status_code == 200
    ids = [s["batch_id"] for s in summaries.json()["summaries"]]
    assert result.batch_id in ids

    one = client.get(f"/api/v1/decision-batch-summaries/{result.batch_id}")
    assert one.status_code == 200
    body = one.json()["summary"]
    assert body["total_decisions"] == len(result.created_decision_ids)
    assert len(body["top_decision_ids"]) <= 8


def test_unknown_batch_summary_returns_404(client):
    assert client.get("/api/v1/decision-batch-summaries/batch-nope").status_code == 404


def test_underlying_decisions_remain_retrievable_after_assessment(client):
    result = _seed()
    client.post("/api/v1/decisions/assess-timing")
    for decision_id in result.created_decision_ids:
        assert client.get(f"/api/v1/decisions/{decision_id}").status_code == 200
