"""Milestone 8 — replay API routes, idempotency, bounds and mode boundary."""

from __future__ import annotations

import pytest

from cockpit.replay_service import REPLAY


@pytest.fixture(autouse=True)
def _clean():
    REPLAY.reset()
    yield
    REPLAY.reset()


def _create(client, **overrides):
    body = {"run_mode": "SAMPLE_REPLAY", "trader_policy": "TIMING_POLICY", "execution_mode": "REALISTIC"}
    body.update(overrides)
    return client.post("/api/v1/replay-runs", json=body)


def test_datasets_lists_only_sample(client):
    datasets = client.get("/api/v1/replay-datasets").json()["datasets"]
    assert [d["dataset_id"] for d in datasets] == ["sample-replay-v1"]
    assert all(d["run_mode"] == "SAMPLE_REPLAY" for d in datasets)


def test_create_sample_replay_is_diagnostic_with_zero_lookahead(client):
    response = _create(client, idempotency_key="k1")
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_only"] is True and body["not_executable"] is True
    run = body["run"]
    assert run["run_mode"] == "SAMPLE_REPLAY"
    assert run["trustworthy_for_live_trading"] is False
    assert run["lookahead_violation_count"] == 0
    assert run["decision_count"] > 0 and run["evaluated_count"] > 0
    assert body["integrity"]["status"] == "OK"


def test_sample_replay_never_labelled_historical(client):
    run = _create(client).json()["run"]
    assert run["run_mode"] != "HISTORICAL_REPLAY"
    assert run["source_mode"] == "SAMPLE"


def test_historical_replay_rejected_without_historical_dataset(client):
    response = _create(client, run_mode="HISTORICAL_REPLAY")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_unknown_dataset_rejected(client):
    response = _create(client, dataset_id="does-not-exist")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "validation_error"


def test_idempotent_retry_returns_same_run(client):
    first = _create(client, idempotency_key="k1").json()["run"]["replay_run_id"]
    second = _create(client, idempotency_key="k1").json()["run"]["replay_run_id"]
    assert first == second
    assert len(client.get("/api/v1/replay-runs").json()["runs"]) == 1


def test_conflicting_idempotency_key_409(client):
    _create(client, idempotency_key="k1", trader_policy="TIMING_POLICY")
    conflict = _create(client, idempotency_key="k1", trader_policy="MODEL_FOLLOW")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"


def test_bounded_run_limit_422(client):
    response = _create(client, max_periods=5)  # dataset has 12 periods
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "bounded_run_limit"


def test_list_get_episodes_metrics_cumulative(client):
    rid = _create(client).json()["run"]["replay_run_id"]

    got = client.get(f"/api/v1/replay-runs/{rid}")
    assert got.status_code == 200 and got.json()["run"]["replay_run_id"] == rid

    episodes = client.get(f"/api/v1/replay-runs/{rid}/episodes").json()["episodes"]
    assert episodes and all(e["replay_run_id"] == rid for e in episodes)

    metrics = client.get(f"/api/v1/replay-runs/{rid}/metrics").json()["metrics"]
    assert metrics["sample_size"] >= 1
    assert "sample_size_note" in metrics
    # perfect-foresight capture is separated from a "strategy return" — it lives under hit_regret.
    assert "perfect_foresight_capture_ratio" in metrics["hit_regret"]

    points = client.get(f"/api/v1/replay-runs/{rid}/cumulative-pnl").json()["points"]
    assert points and all(p["cumulative_no_action_gbp"] == 0.0 for p in points)


def test_unknown_run_404(client):
    assert client.get("/api/v1/replay-runs/nope").status_code == 404
    assert client.get("/api/v1/replay-runs/nope/episodes").status_code == 404
    assert client.get("/api/v1/replay-runs/nope/metrics").status_code == 404
    assert client.get("/api/v1/replay-runs/nope/cumulative-pnl").status_code == 404


def test_execution_mode_stored_on_run(client):
    run = _create(client, execution_mode="STRESS", trader_policy="MODEL_FOLLOW").json()["run"]
    assert run["execution_mode"] == "STRESS"
