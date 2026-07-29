"""Milestone 7 — delivery/settlement/evaluation lifecycle, guards and idempotency."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_models import (
    DecisionContext,
    DecisionStatus,
    ModelRecommendation,
    RecommendedAction,
    TriggerType,
)
from cockpit.decision_orchestrator import DecisionOrchestrator
from cockpit.decision_service import DecisionStore, StaleDecisionError
from cockpit.decision_state_machine import InvalidTransitionError
from cockpit.execution_models import ExecutionMode
from cockpit.execution_service import ExecutionService
from cockpit.evaluation_service import EvaluationService
from cockpit.models import SampleRegime
from cockpit.settlement_models import DecisionQualityLabel, ImbalanceDirection, ProcessSkipReason
from cockpit.settlement_service import IdempotencyConflictError, SettlementInputError, SettlementService

AS_OF = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def rolling():
    from cockpit.pipeline import DataFlowPipeline
    from cockpit.rolling_service import RollingService
    from cockpit.simulated_environment import SimulatedEnvironment

    service = RollingService(DataFlowPipeline(), SimulatedEnvironment(clock=lambda: AS_OF))
    service.initialise()
    service.set_regime(SampleRegime.WIND_FORECAST_MISS)
    return service


@pytest.fixture
def env(rolling):
    store = DecisionStore()
    result = DecisionOrchestrator(decisions=store, rolling=rolling).refresh()
    assert result.created_decision_ids, "expected the isolated rolling env to create decisions"
    exe = ExecutionService(decisions=store, rolling=rolling)
    settle = SettlementService(decisions=store)
    evaluate = EvaluationService(decisions=store, settlement=settle)
    return types.SimpleNamespace(
        store=store, exe=exe, settle=settle, evaluate=evaluate, ids=list(result.created_decision_ids)
    )


def _after(store, did):
    return store.get(did).context.delivery_end + timedelta(minutes=1)


def _fill(env, did, *, mode=ExecutionMode.IDEAL):
    env.store.accept(did)
    env.exe.submit_simulated(did, mode=mode, now=AS_OF)


# --- happy-path lifecycles --------------------------------------------------


def test_filled_to_delivered_settled_evaluated(env):
    did = env.ids[0]
    _fill(env, did)
    assert env.store.get(did).status is DecisionStatus.FILLED
    after = _after(env.store, did)
    delivery = env.settle.deliver(did, now=after)
    assert env.store.get(did).status is DecisionStatus.DELIVERED
    assert delivery.imbalance_direction in tuple(ImbalanceDirection)
    settlement = env.settle.settle(did, now=after)
    assert env.store.get(did).status is DecisionStatus.SETTLED
    evaluation = env.evaluate.evaluate(did, now=after)
    assert env.store.get(did).status is DecisionStatus.EVALUATED
    # realised_pnl is the incremental-vs-no-action metric shared across the stage.
    assert settlement.realised_pnl_gbp == evaluation.realised_outcome.realised_pnl_gbp
    assert evaluation.pnl_attribution.reconciled


def test_partially_filled_path(env):
    did = env.ids[0]
    env.store.modify(did, buy_mwh=500.0, rationale="oversize")
    env.exe.submit_simulated(did, mode=ExecutionMode.REALISTIC, now=AS_OF)
    assert env.store.get(did).status is DecisionStatus.PARTIALLY_FILLED
    after = _after(env.store, did)
    env.settle.deliver(did, now=after)
    env.settle.settle(did, now=after)
    ev = env.evaluate.evaluate(did, now=after)
    assert env.store.get(did).status is DecisionStatus.EVALUATED
    # Final position uses executed (partial) volume only.
    delivery = env.settle.delivery_for_decision(did)
    executed = env.store.get(did).execution_result
    assert delivery.executed_buy_mwh == executed.executed_buy_mwh
    assert delivery.final_contracted_position_mwh == pytest.approx(
        delivery.initial_contracted_position_mwh + executed.executed_buy_mwh - executed.executed_sell_mwh
    )
    assert ev.decision_quality_label in tuple(DecisionQualityLabel)


def test_expired_path(env):
    did = env.ids[0]
    env.store.modify(did, buy_mwh=env.store.get(did).recommendation.buy_mwh or 10.0, limit_price=1.0, rationale="unreachable")
    env.exe.submit_simulated(did, mode=ExecutionMode.REALISTIC, now=AS_OF)
    assert env.store.get(did).status is DecisionStatus.EXPIRED
    after = _after(env.store, did)
    delivery = env.settle.deliver(did, now=after)
    # Expired = no trade: final Q equals the pre-decision contracted position.
    assert delivery.final_contracted_position_mwh == pytest.approx(delivery.initial_contracted_position_mwh)
    env.settle.settle(did, now=after)
    env.evaluate.evaluate(did, now=after)
    assert env.store.get(did).status is DecisionStatus.EVALUATED


def test_rejected_path_has_no_trade_outcome(env):
    did = env.ids[0]
    env.store.reject(did, rationale="no")
    after = _after(env.store, did)
    delivery = env.settle.deliver(did, now=after)
    assert delivery.executed_buy_mwh == 0.0 and delivery.executed_sell_mwh == 0.0
    assert delivery.final_contracted_position_mwh == pytest.approx(delivery.initial_contracted_position_mwh)
    settlement = env.settle.settle(did, now=after)
    assert settlement.execution_cashflow_gbp == 0.0 and settlement.execution_fees_gbp == 0.0
    ev = env.evaluate.evaluate(did, now=after)
    # A no-trade decision is in line with no-action (incremental P&L 0).
    assert ev.realised_outcome.realised_pnl_gbp == pytest.approx(0.0)
    assert ev.decision_quality_label is DecisionQualityLabel.IN_LINE_WITH_NO_ACTION


# --- point-in-time / lifecycle guards ---------------------------------------


def test_future_delivery_rejected(env):
    did = env.ids[0]
    _fill(env, did)
    with pytest.raises(SettlementInputError) as excinfo:
        env.settle.deliver(did, now=AS_OF)  # before delivery_end
    assert excinfo.value.reason is ProcessSkipReason.DELIVERY_PERIOD_NOT_ENDED
    assert env.store.get(did).status is DecisionStatus.FILLED  # unchanged


def test_settle_before_deliver_is_conflict(env):
    did = env.ids[0]
    _fill(env, did)
    with pytest.raises(InvalidTransitionError):
        env.settle.settle(did, now=_after(env.store, did))


def test_evaluate_before_settle_is_conflict(env):
    did = env.ids[0]
    _fill(env, did)
    env.settle.deliver(did, now=_after(env.store, did))
    with pytest.raises(KeyError):  # no settlement recorded yet
        env.evaluate.evaluate(did, now=_after(env.store, did))


def test_stale_sequence_conflict(env):
    did = env.ids[0]
    _fill(env, did)
    with pytest.raises(StaleDecisionError):
        env.settle.deliver(did, now=_after(env.store, did), expected_sequence=1)


def test_duplicate_delivery_is_conflict(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    env.settle.deliver(did, now=after)
    with pytest.raises(InvalidTransitionError):
        env.settle.deliver(did, now=after)  # already DELIVERED


def test_no_duplicate_evaluation(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    env.settle.deliver(did, now=after)
    env.settle.settle(did, now=after)
    env.evaluate.evaluate(did, now=after)
    with pytest.raises(InvalidTransitionError):
        env.evaluate.evaluate(did, now=after)  # already EVALUATED


# --- idempotency ------------------------------------------------------------


def test_idempotent_delivery_retry(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    first = env.settle.deliver(did, now=after, idempotency_key="k1")
    second = env.settle.deliver(did, now=after, idempotency_key="k1")
    assert first.delivery_id == second.delivery_id
    assert len(env.settle.list_deliveries()) == 1


def test_conflicting_delivery_idempotency_key(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    env.settle.deliver(did, now=after, idempotency_key="k1")
    with pytest.raises(IdempotencyConflictError):
        # same key, different payload (expected_sequence differs)
        env.settle.deliver(did, now=after, idempotency_key="k1", expected_sequence=999)


# --- trust + append-only ----------------------------------------------------


def test_trust_fields_preserved_through_lifecycle(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    delivery = env.settle.deliver(did, now=after)
    settlement = env.settle.settle(did, now=after)
    evaluation = env.evaluate.evaluate(did, now=after)
    for record in (delivery, settlement, evaluation):
        assert record.diagnostic_only is True and record.not_executable is True
    decision = env.store.get(did)
    assert decision.diagnostic_only and decision.not_executable
    assert decision.context.trustworthy_for_live_trading is False
    # Recommendation / instruction / execution stage records are not overwritten.
    assert decision.recommendation is not None
    assert decision.trader_instruction is not None
    assert decision.execution_result is not None


def test_append_only_transitions(env):
    did = env.ids[0]
    _fill(env, did)
    after = _after(env.store, did)
    env.settle.deliver(did, now=after)
    env.settle.settle(did, now=after)
    env.evaluate.evaluate(did, now=after)
    statuses = [t.to_status for t in env.store.get(did).transitions]
    assert statuses == [
        DecisionStatus.PROPOSED,
        DecisionStatus.ACCEPTED,
        DecisionStatus.SUBMITTED,
        DecisionStatus.FILLED,
        DecisionStatus.DELIVERED,
        DecisionStatus.SETTLED,
        DecisionStatus.EVALUATED,
    ]
    assert isinstance(env.store.get(did).transitions, tuple)


# --- process-completed ------------------------------------------------------


def test_process_completed_skips_future_periods(env):
    for did in env.ids:
        _fill(env, did)
    result = env.evaluate.process_completed(now=AS_OF)  # before any period ends
    assert not result.processed
    assert result.skipped
    assert all(s.reason is ProcessSkipReason.DELIVERY_PERIOD_NOT_ENDED for s in result.skipped)


def test_process_completed_evaluates_eligible_periods(env):
    for did in env.ids:
        _fill(env, did)
    latest_end = max(env.store.get(did).context.delivery_end for did in env.ids)
    result = env.evaluate.process_completed(now=latest_end + timedelta(minutes=1))
    processed_ids = {p.decision_id for p in result.processed}
    assert processed_ids == set(env.ids)
    for did in env.ids:
        assert env.store.get(did).status is DecisionStatus.EVALUATED
    # Re-running is a no-op: all now report as existing.
    again = env.evaluate.process_completed(now=latest_end + timedelta(minutes=1))
    assert set(again.existing) == set(env.ids)
    assert not again.processed


# --- unavailable-input guards (manual decisions) ----------------------------


def _manual(store, *, position=50.0, p50_exposure=10.0, delivered_past=True):
    start = AS_OF - timedelta(hours=2) if delivered_past else AS_OF + timedelta(hours=2)
    ctx = DecisionContext(
        settlement_period=42, delivery_period="SP42",
        delivery_start=start, delivery_end=start + timedelta(minutes=30), as_of=AS_OF,
        trigger_type=TriggerType.MANUAL, trigger_description="manual",
        position_before_mwh=position, p50_exposure_before_mwh=p50_exposure,
    )
    decision = store.create(context=ctx, recommendation=ModelRecommendation(action=RecommendedAction.NO_ACTION))
    store.reject(decision.decision_id, rationale="no")  # PROPOSED -> REJECTED (a deliverable no-trade state)
    return decision.decision_id


def test_missing_realised_generation_rejected():
    store = DecisionStore()
    settle = SettlementService(decisions=store)
    did = _manual(store, p50_exposure=None)
    with pytest.raises(SettlementInputError) as excinfo:
        settle.deliver(did, now=AS_OF)
    assert excinfo.value.reason is ProcessSkipReason.MISSING_REALISED_GENERATION


def test_missing_contracted_position_rejected():
    store = DecisionStore()
    settle = SettlementService(decisions=store)
    did = _manual(store, position=None)
    with pytest.raises(SettlementInputError) as excinfo:
        settle.deliver(did, now=AS_OF)
    assert excinfo.value.reason is ProcessSkipReason.MISSING_CONTRACTED_POSITION


def test_missing_settlement_prices_rejected(monkeypatch):
    # Force a non-finite/negative SAMPLE reference so the price guard fires.
    monkeypatch.setattr("cockpit.settlement_service.SAMPLE_BASE_REFERENCE_PRICE", -1000.0)
    store = DecisionStore()
    settle = SettlementService(decisions=store)
    did = _manual(store)  # rejected -> no execution -> reference falls back to the base
    with pytest.raises(SettlementInputError) as excinfo:
        settle.deliver(did, now=AS_OF)
    assert excinfo.value.reason is ProcessSkipReason.MISSING_SETTLEMENT_PRICES
