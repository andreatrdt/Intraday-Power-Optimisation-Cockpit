"""Milestone 6B — simulated-execution API routes."""

from __future__ import annotations

import pytest

from cockpit.decision_orchestrator import ORCHESTRATOR
from cockpit.decision_service import DECISIONS
from cockpit.execution_service import EXECUTION

# The SAMPLE clock is pinned into its favourable intraday window by the shared
# ``client`` fixture (see tests/conftest.py), so decision creation + submission here
# are deterministic regardless of when the suite runs.


@pytest.fixture(autouse=True)
def _clean():
    DECISIONS.clear(); ORCHESTRATOR.reset(); EXECUTION.reset()
    yield
    DECISIONS.clear(); ORCHESTRATOR.reset(); EXECUTION.reset()


def _prepare(client):
    """Reset the rolling env to a material state and create decisions."""
    client.post("/api/v1/live-state/reset")
    client.post("/api/v1/live-state/regime", json={"regime": "wind_forecast_miss"})
    body = client.post("/api/v1/decisions/refresh").json()
    ids = body["refresh"]["created_decision_ids"]
    assert ids, "expected decisions to be created"
    return ids


def _accept(client, decision_id):
    client.post(f"/api/v1/decisions/{decision_id}/accept", json={"trader_rationale": "ok"})
    return client.get(f"/api/v1/decisions/{decision_id}").json()["decision"]["transitions"][-1]["sequence"]


def test_submit_simulated_is_diagnostic_and_fills(client):
    did = _prepare(client)[0]
    seq = _accept(client, did)
    response = client.post(
        f"/api/v1/decisions/{did}/submit-simulated",
        json={"execution_mode": "IDEAL", "expected_sequence": seq},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True
    assert body["not_executable"] is True
    assert body["trustworthy_for_live_trading"] is False
    assert body["execution_mode"] == "IDEAL"
    assert body["simulator_version"] == "execution-sim-v1"
    assert body["outcome"]["execution_status"] == "FILLED"
    assert body["decision"]["status"] == "FILLED"
    assert len(body["outcome"]["fills"]) >= 1
    assert isinstance(body["assumptions_used"], list) and body["assumptions_used"]


def test_list_and_get_orders_and_outcomes(client):
    did = _prepare(client)[0]
    _accept(client, did)
    submit = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "REALISTIC"}).json()
    order_id = submit["outcome"]["order"]["order_id"]

    orders = client.get("/api/v1/simulated-orders")
    assert orders.status_code == 200
    assert order_id in [o["order_id"] for o in orders.json()["orders"]]

    one_order = client.get(f"/api/v1/simulated-orders/{order_id}")
    assert one_order.status_code == 200
    assert one_order.json()["order"]["not_executable"] is True

    outcomes = client.get("/api/v1/execution-outcomes")
    assert order_id in [o["order"]["order_id"] for o in outcomes.json()["outcomes"]]

    one_outcome = client.get(f"/api/v1/execution-outcomes/{order_id}")
    assert one_outcome.status_code == 200

    linked = client.get(f"/api/v1/decisions/{did}/execution")
    assert linked.status_code == 200
    assert linked.json()["outcome"]["order"]["order_id"] == order_id


def test_submit_unknown_decision_404(client):
    response = client.post("/api/v1/decisions/dec-nope/submit-simulated", json={})
    assert response.status_code == 404


def test_unknown_order_and_outcome_404(client):
    assert client.get("/api/v1/simulated-orders/simord-nope").status_code == 404
    assert client.get("/api/v1/execution-outcomes/simord-nope").status_code == 404


def test_submit_proposed_conflicts(client):
    did = _prepare(client)[0]  # not accepted
    response = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_decision"


def test_stale_sequence_conflicts(client):
    did = _prepare(client)[0]
    _accept(client, did)
    response = client.post(
        f"/api/v1/decisions/{did}/submit-simulated",
        json={"execution_mode": "IDEAL", "expected_sequence": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_decision"


def test_idempotent_and_conflicting_key(client):
    did = _prepare(client)[0]
    _accept(client, did)
    first = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL", "idempotency_key": "k1"})
    assert first.status_code == 200
    order_id = first.json()["outcome"]["order"]["order_id"]
    same = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL", "idempotency_key": "k1"})
    assert same.status_code == 200
    assert same.json()["outcome"]["order"]["order_id"] == order_id  # idempotent
    conflict = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "STRESS", "idempotency_key": "k1"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"


def test_same_key_different_body_field_conflicts(client):
    """Reusing an idempotency key with the same mode but a different body field
    (actor_id) is a payload change → 409, proving the key is a full-payload hash."""
    did = _prepare(client)[0]
    _accept(client, did)
    first = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL", "idempotency_key": "k1", "actor_id": "alice"})
    assert first.status_code == 200
    conflict = client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL", "idempotency_key": "k1", "actor_id": "bob"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"
