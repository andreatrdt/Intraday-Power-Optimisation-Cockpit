"""Milestone 6B — simulated-execution service (lifecycle, storage, concurrency)."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_models import (
    DecisionContext,
    DecisionStatus,
    ExecutionStatus,
    ModelRecommendation,
    RecommendedAction,
    TriggerType,
)
from cockpit.decision_orchestrator import DecisionOrchestrator
from cockpit.decision_service import DecisionStore, DecisionValidationError, StaleDecisionError
from cockpit.execution_models import ExecutionMode
from cockpit.execution_service import ExecutionService, IdempotencyConflictError
from cockpit.models import SampleRegime

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


# --- Group A: status/eligibility rejections (no rolling market needed) -------


def _manual_decision(store: DecisionStore, *, buy=20.0, sell=0.0, calc=True):
    ctx = DecisionContext(
        settlement_period=99, delivery_period="SP99",
        delivery_start=AT + timedelta(hours=1), delivery_end=AT + timedelta(hours=1, minutes=30), as_of=AT,
        trigger_type=TriggerType.FORECAST_REVISION, trigger_description="x",
        minutes_to_gate_closure=60.0, gate_closure_at=AT + timedelta(minutes=60), calculation_allowed=calc,
    )
    return store.create(context=ctx, recommendation=ModelRecommendation(action=RecommendedAction.BUY, buy_mwh=buy, sell_mwh=sell))


def test_proposed_cannot_be_submitted():
    store = DecisionStore()
    exe = ExecutionService(decisions=store, rolling=object())
    decision = _manual_decision(store)
    with pytest.raises(StaleDecisionError):
        exe.submit_simulated(decision.decision_id)


def test_rejected_cannot_be_submitted():
    store = DecisionStore()
    exe = ExecutionService(decisions=store, rolling=object())
    decision = _manual_decision(store)
    store.reject(decision.decision_id, rationale="no")
    with pytest.raises(StaleDecisionError):
        exe.submit_simulated(decision.decision_id)


def test_delayed_cannot_be_submitted():
    store = DecisionStore()
    exe = ExecutionService(decisions=store, rolling=object())
    decision = _manual_decision(store)
    store.delay(decision.decision_id, until=AT + timedelta(minutes=30), rationale="wait", at=AT)
    with pytest.raises(StaleDecisionError):
        exe.submit_simulated(decision.decision_id)


def test_unknown_decision_raises_key_error():
    exe = ExecutionService(decisions=DecisionStore(), rolling=object())
    with pytest.raises(KeyError):
        exe.submit_simulated("dec-nope")


def test_trust_not_calculable_rejected():
    store = DecisionStore()
    exe = ExecutionService(decisions=store, rolling=object())
    decision = _manual_decision(store, calc=False)
    store.accept(decision.decision_id)
    with pytest.raises(DecisionValidationError):
        exe.submit_simulated(decision.decision_id)


# --- Group B: full integration with an isolated rolling environment ----------


@pytest.fixture(scope="module")
def rolling():
    from cockpit.pipeline import DataFlowPipeline
    from cockpit.rolling_service import RollingService
    from cockpit.simulated_environment import SimulatedEnvironment

    service = RollingService(DataFlowPipeline(), SimulatedEnvironment())
    service.initialise()
    service.set_regime(SampleRegime.WIND_FORECAST_MISS)
    return service


@pytest.fixture()
def env(rolling):
    store = DecisionStore()
    result = DecisionOrchestrator(decisions=store, rolling=rolling).refresh()
    exe = ExecutionService(decisions=store, rolling=rolling)
    assert result.created_decision_ids, "expected the isolated rolling env to create decisions"
    return types.SimpleNamespace(store=store, exe=exe, ids=list(result.created_decision_ids), rolling=rolling)


def _seq(store, decision_id):
    return store.get(decision_id).transitions[-1].sequence


def test_submit_accepted_fills_and_populates_execution_result(env):
    did = env.ids[0]
    env.store.accept(did)
    outcome = env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, expected_sequence=_seq(env.store, did))
    assert outcome.execution_status is ExecutionStatus.FILLED
    decision = env.store.get(did)
    assert decision.status is DecisionStatus.FILLED
    exec_result = decision.execution_result
    assert (exec_result.executed_buy_mwh + exec_result.executed_sell_mwh) == pytest.approx(outcome.total_filled_volume_mwh)
    assert exec_result.average_execution_price == outcome.average_fill_price_gbp_per_mwh
    assert exec_result.unfilled_volume_mwh == outcome.unfilled_volume_mwh


def test_recommendation_and_instruction_unchanged(env):
    did = env.ids[0]
    before = env.store.get(did).recommendation
    env.store.accept(did)
    env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL)
    after = env.store.get(did)
    assert after.recommendation == before
    assert after.trader_instruction is not None  # ACCEPT instruction preserved


def test_modified_uses_trader_override(env):
    did = env.ids[0]
    env.store.modify(did, buy_mwh=17.0, rationale="override")
    outcome = env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL)
    assert outcome.order.requested_volume_mwh == 17.0
    assert outcome.total_filled_volume_mwh == 17.0  # IDEAL full fill


def test_partial_fill_marks_partially_filled(env):
    did = env.ids[0]
    env.store.modify(did, buy_mwh=500.0, rationale="oversize")  # exceeds visible depth
    outcome = env.exe.submit_simulated(did, mode=ExecutionMode.REALISTIC)
    assert outcome.execution_status is ExecutionStatus.PARTIALLY_FILLED
    assert env.store.get(did).status is DecisionStatus.PARTIALLY_FILLED


def test_zero_fill_expires(env):
    did = env.ids[0]
    env.store.modify(did, buy_mwh=env.store.get(did).recommendation.buy_mwh or 10.0, limit_price=1.0, rationale="unreachable limit")
    outcome = env.exe.submit_simulated(did, mode=ExecutionMode.REALISTIC)
    assert outcome.execution_status is ExecutionStatus.EXPIRED
    assert env.store.get(did).status is DecisionStatus.EXPIRED


def test_gate_closure_passed_rejected(env):
    did = env.ids[0]
    env.store.accept(did)
    gate = env.store.get(did).context.gate_closure_at
    with pytest.raises(DecisionValidationError):
        env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, now=gate + timedelta(minutes=1))


def test_stale_sequence_returns_conflict(env):
    did = env.ids[0]
    env.store.accept(did)
    with pytest.raises(StaleDecisionError):
        env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, expected_sequence=1)  # stale (real seq is 2)


def test_append_only_transition_history(env):
    did = env.ids[0]
    env.store.accept(did)
    env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL)
    statuses = [t.to_status for t in env.store.get(did).transitions]
    assert statuses[:4] == [DecisionStatus.PROPOSED, DecisionStatus.ACCEPTED, DecisionStatus.SUBMITTED, DecisionStatus.FILLED]


def test_idempotent_retry_returns_existing(env):
    did = env.ids[0]
    env.store.accept(did)
    first = env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, idempotency_key="k1")
    second = env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, idempotency_key="k1")
    assert first.order.order_id == second.order.order_id
    assert len(env.exe.list_orders()) == 1


def test_conflicting_idempotency_key_raises(env):
    did = env.ids[0]
    env.store.accept(did)
    env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL, idempotency_key="k1")
    with pytest.raises(IdempotencyConflictError):
        env.exe.submit_simulated(did, mode=ExecutionMode.REALISTIC, idempotency_key="k1")


def test_trust_flags_preserved_after_fill(env):
    did = env.ids[0]
    env.store.accept(did)
    outcome = env.exe.submit_simulated(did, mode=ExecutionMode.IDEAL)
    assert outcome.diagnostic_only and outcome.not_executable and outcome.trustworthy_for_live_trading is False
    decision = env.store.get(did)
    assert decision.diagnostic_only and decision.not_executable
    assert decision.context.trustworthy_for_live_trading is False
