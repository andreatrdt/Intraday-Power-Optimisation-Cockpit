"""Milestone 7 — delivery/settlement/evaluation API routes."""

from __future__ import annotations

import pytest

from cockpit.decision_orchestrator import ORCHESTRATOR
from cockpit.decision_service import DECISIONS
from cockpit.evaluation_service import EVALUATION
from cockpit.execution_service import EXECUTION
from cockpit.settlement_service import SETTLEMENT


@pytest.fixture(autouse=True)
def _clean():
    for store in (DECISIONS, ORCHESTRATOR, EXECUTION, SETTLEMENT, EVALUATION):
        store.reset() if hasattr(store, "reset") else store.clear()
    yield


def _prepare(client):
    client.post("/api/v1/live-state/reset")
    client.post("/api/v1/live-state/regime", json={"regime": "wind_forecast_miss"})
    ids = client.post("/api/v1/decisions/refresh").json()["refresh"]["created_decision_ids"]
    assert ids, "expected decisions to be created"
    return ids


def _to_filled(client, did):
    client.post(f"/api/v1/decisions/{did}/accept", json={"trader_rationale": "ok"})
    client.post(f"/api/v1/decisions/{did}/submit-simulated", json={"execution_mode": "IDEAL"})
    return client.get(f"/api/v1/decisions/{did}").json()["decision"]


def _seq(client, did):
    return client.get(f"/api/v1/decisions/{did}").json()["decision"]["transitions"][-1]["sequence"]


def test_full_deliver_settle_evaluate_flow(client):
    did = _prepare(client)[0]
    _to_filled(client, did)

    deliver = client.post(f"/api/v1/decisions/{did}/deliver", json={})
    assert deliver.status_code == 200
    body = deliver.json()
    assert body["diagnostic_only"] is True and body["not_executable"] is True
    assert body["decision"]["status"] == "DELIVERED"
    assert body["delivery"]["imbalance_direction"] in ("LONG", "SHORT", "FLAT")
    assert body["delivery"]["final_contracted_position_mwh"] == pytest.approx(
        body["delivery"]["initial_contracted_position_mwh"]
        + body["delivery"]["executed_buy_mwh"]
        - body["delivery"]["executed_sell_mwh"]
    )

    settle = client.post(f"/api/v1/decisions/{did}/settle", json={})
    assert settle.status_code == 200
    settlement = settle.json()["settlement"]
    assert settle.json()["decision"]["status"] == "SETTLED"
    assert "realised_pnl_gbp" in settlement and "total_realised_cashflow_gbp" in settlement

    evaluate = client.post(f"/api/v1/decisions/{did}/evaluate", json={})
    assert evaluate.status_code == 200
    evaluation = evaluate.json()["evaluation"]
    assert evaluate.json()["decision"]["status"] == "EVALUATED"
    assert evaluation["decision_quality_label"] in (
        "OUTPERFORMED_NO_ACTION", "UNDERPERFORMED_NO_ACTION", "IN_LINE_WITH_NO_ACTION", "UNAVAILABLE",
    )
    names = [b["benchmark_name"] for b in evaluation["benchmark_results"]]
    assert names == ["NO_ACTION", "MODEL_RECOMMENDATION", "TRADER_INSTRUCTION", "PERFECT_FORESIGHT"]
    pf = next(b for b in evaluation["benchmark_results"] if b["benchmark_name"] == "PERFECT_FORESIGHT")
    assert pf["attainable"] is False and pf["hindsight_only"] is True
    assert evaluation["pnl_attribution"]["reconciliation_error_gbp"] == pytest.approx(0.0, abs=1e-6)


def test_list_and_get_routes(client):
    did = _prepare(client)[0]
    _to_filled(client, did)
    delivery_id = client.post(f"/api/v1/decisions/{did}/deliver", json={}).json()["delivery"]["delivery_id"]
    settlement_id = client.post(f"/api/v1/decisions/{did}/settle", json={}).json()["settlement"]["settlement_id"]
    evaluation_id = client.post(f"/api/v1/decisions/{did}/evaluate", json={}).json()["evaluation"]["evaluation_id"]

    assert delivery_id in [d["delivery_id"] for d in client.get("/api/v1/deliveries").json()["deliveries"]]
    assert client.get(f"/api/v1/deliveries/{delivery_id}").status_code == 200
    assert settlement_id in [s["settlement_id"] for s in client.get("/api/v1/settlements").json()["settlements"]]
    assert client.get(f"/api/v1/settlements/{settlement_id}").status_code == 200
    assert evaluation_id in [e["evaluation_id"] for e in client.get("/api/v1/evaluations").json()["evaluations"]]
    assert client.get(f"/api/v1/evaluations/{evaluation_id}").status_code == 200

    bundle = client.get(f"/api/v1/decisions/{did}/evaluation").json()
    assert bundle["delivery"]["delivery_id"] == delivery_id
    assert bundle["settlement"]["settlement_id"] == settlement_id
    assert bundle["evaluation"]["evaluation_id"] == evaluation_id


def test_unknown_ids_404(client):
    assert client.get("/api/v1/deliveries/nope").status_code == 404
    assert client.get("/api/v1/settlements/nope").status_code == 404
    assert client.get("/api/v1/evaluations/nope").status_code == 404


def test_decision_evaluation_resolution_when_missing(client):
    did = _prepare(client)[0]
    bundle = client.get(f"/api/v1/decisions/{did}/evaluation").json()
    assert bundle["delivery"] is None and bundle["settlement"] is None and bundle["evaluation"] is None


def test_settle_before_deliver_conflicts(client):
    did = _prepare(client)[0]
    _to_filled(client, did)
    response = client.post(f"/api/v1/decisions/{did}/settle", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "invalid_transition"


def test_stale_sequence_conflicts(client):
    did = _prepare(client)[0]
    _to_filled(client, did)
    response = client.post(f"/api/v1/decisions/{did}/deliver", json={"expected_sequence": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_decision"


def test_idempotent_and_conflicting_delivery(client):
    did = _prepare(client)[0]
    _to_filled(client, did)
    first = client.post(f"/api/v1/decisions/{did}/deliver", json={"idempotency_key": "k1"})
    assert first.status_code == 200
    delivery_id = first.json()["delivery"]["delivery_id"]
    same = client.post(f"/api/v1/decisions/{did}/deliver", json={"idempotency_key": "k1"})
    assert same.status_code == 200 and same.json()["delivery"]["delivery_id"] == delivery_id
    conflict = client.post(f"/api/v1/decisions/{did}/deliver", json={"idempotency_key": "k1", "expected_sequence": 999})
    assert conflict.status_code == 409 and conflict.json()["detail"]["error"] == "idempotency_conflict"


def test_process_completed_evaluates_and_warns(client):
    ids = _prepare(client)
    for did in ids:
        _to_filled(client, did)
    response = client.post("/api/v1/decisions/process-completed")
    assert response.status_code == 200
    result = response.json()["result"]
    assert response.json()["diagnostic_only"] is True
    assert {p["decision_id"] for p in result["processed"]} == set(ids)
    assert "does not represent live or historical trading performance" in result["warning"].lower()
    for did in ids:
        assert client.get(f"/api/v1/decisions/{did}").json()["decision"]["status"] == "EVALUATED"


def test_process_completed_reports_existing_on_rerun(client):
    ids = _prepare(client)
    for did in ids:
        _to_filled(client, did)
    client.post("/api/v1/decisions/process-completed")
    rerun = client.post("/api/v1/decisions/process-completed").json()["result"]
    assert set(rerun["existing"]) == set(ids)
    assert not rerun["processed"]
