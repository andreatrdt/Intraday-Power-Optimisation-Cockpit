"""Milestone 6A — trader lifecycle API (accept/modify/reject/delay/reopen)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_models import DecisionContext, ModelRecommendation, RecommendedAction, TriggerType
from cockpit.decision_service import DECISIONS

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    DECISIONS.clear()
    yield
    DECISIONS.clear()


def _seed(*, buy=32.0, sell=0.0, gate: datetime | None = None):
    context = DecisionContext(
        settlement_period=24, delivery_period="SP24",
        delivery_start=AT + timedelta(hours=1), delivery_end=AT + timedelta(hours=1, minutes=30), as_of=AT,
        trigger_type=TriggerType.FORECAST_REVISION, trigger_description="revised", minutes_to_gate_closure=76.0,
        gate_closure_at=gate,
    )
    return DECISIONS.create(context=context, recommendation=ModelRecommendation(action=RecommendedAction.BUY, buy_mwh=buy, sell_mwh=sell))


def test_accept_route_records_decision_only(client):
    decision = _seed()
    response = client.post(f"/api/v1/decisions/{decision.decision_id}/accept", json={"trader_rationale": "ok"})
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True
    assert body["trustworthy_for_live_trading"] is False
    assert body["decision"]["status"] == "ACCEPTED"
    assert body["decision"]["not_executable"] is True


def test_modify_route(client):
    decision = _seed()
    response = client.post(
        f"/api/v1/decisions/{decision.decision_id}/modify",
        json={"trader_buy_mwh": 20.0, "trader_sell_mwh": 0.0, "trader_rationale": "part"},
    )
    assert response.status_code == 200
    assert response.json()["decision"]["status"] == "MODIFIED"


def test_modify_rejects_both_positive(client):
    decision = _seed()
    response = client.post(
        f"/api/v1/decisions/{decision.decision_id}/modify",
        json={"trader_buy_mwh": 5.0, "trader_sell_mwh": 5.0, "trader_rationale": "x"},
    )
    assert response.status_code == 422


def test_modify_rejects_negative(client):
    decision = _seed()
    response = client.post(
        f"/api/v1/decisions/{decision.decision_id}/modify",
        json={"trader_buy_mwh": -5.0, "trader_sell_mwh": 0.0, "trader_rationale": "x"},
    )
    assert response.status_code == 422


def test_reject_requires_rationale(client):
    decision = _seed()
    blank = client.post(f"/api/v1/decisions/{decision.decision_id}/reject", json={"trader_rationale": "   "})
    assert blank.status_code == 422
    ok = client.post(f"/api/v1/decisions/{decision.decision_id}/reject", json={"trader_rationale": "no thanks"})
    assert ok.status_code == 200
    assert ok.json()["decision"]["status"] == "REJECTED"


def test_delay_gate_closure_validation(client):
    now = datetime.now(tz=timezone.utc)
    decision = _seed(gate=now + timedelta(minutes=60))
    beyond = client.post(
        f"/api/v1/decisions/{decision.decision_id}/delay",
        json={"delayed_until": (now + timedelta(minutes=90)).isoformat(), "trader_rationale": "x"},
    )
    assert beyond.status_code == 422  # after Gate Closure
    ok = client.post(
        f"/api/v1/decisions/{decision.decision_id}/delay",
        json={"delayed_until": (now + timedelta(minutes=30)).isoformat(), "trader_rationale": "wait"},
    )
    assert ok.status_code == 200
    assert ok.json()["decision"]["status"] == "DELAYED"


def test_reopen_route(client):
    now = datetime.now(tz=timezone.utc)
    decision = _seed(gate=now + timedelta(minutes=60))
    client.post(
        f"/api/v1/decisions/{decision.decision_id}/delay",
        json={"delayed_until": (now + timedelta(minutes=30)).isoformat(), "trader_rationale": "wait"},
    )
    response = client.post(f"/api/v1/decisions/{decision.decision_id}/reopen", json={"trader_rationale": "reconsider"})
    assert response.status_code == 200
    assert response.json()["decision"]["status"] == "PROPOSED"


def test_unknown_decision_returns_404(client):
    response = client.post("/api/v1/decisions/dec-nope/accept", json={})
    assert response.status_code == 404


def test_stale_expected_status_returns_409(client):
    decision = _seed()
    first = client.post(
        f"/api/v1/decisions/{decision.decision_id}/accept",
        json={"trader_rationale": "ok", "expected_status": "PROPOSED"},
    )
    assert first.status_code == 200
    stale = client.post(
        f"/api/v1/decisions/{decision.decision_id}/reject",
        json={"trader_rationale": "changed mind", "expected_status": "PROPOSED"},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "stale_decision"
    assert detail["current_status"] == "ACCEPTED"


def test_double_accept_conflicts(client):
    decision = _seed()
    client.post(f"/api/v1/decisions/{decision.decision_id}/accept", json={})
    second = client.post(f"/api/v1/decisions/{decision.decision_id}/accept", json={})
    assert second.status_code == 409  # ACCEPTED -> ACCEPTED is an invalid transition


def test_audit_history_grows_and_is_returned(client):
    decision = _seed()
    body = client.post(f"/api/v1/decisions/{decision.decision_id}/accept", json={"trader_rationale": "ok", "actor_id": "alice"}).json()
    transitions = body["decision"]["transitions"]
    assert [t["sequence"] for t in transitions] == [1, 2]
    assert transitions[1]["to_status"] == "ACCEPTED"
    assert transitions[1]["actor_id"] == "alice"
