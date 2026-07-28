"""Milestone 6A — trader lifecycle service (accept/modify/reject/delay/reopen).

Records the trader decision only; nothing here submits an order, executes, fills
or settles. Lifecycle rules live in the state machine; these test the service
wrappers, validation and optimistic concurrency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cockpit.decision_models import (
    DecisionActor,
    DecisionContext,
    DecisionStatus,
    ModelRecommendation,
    RecommendedAction,
    TraderAction,
    TriggerType,
)
from cockpit.decision_service import DecisionStore, DecisionValidationError, StaleDecisionError
from cockpit.decision_state_machine import InvalidTransitionError

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _ctx(*, gate: datetime | None = None) -> DecisionContext:
    return DecisionContext(
        settlement_period=24,
        delivery_period="SP24",
        delivery_start=AT + timedelta(hours=1),
        delivery_end=AT + timedelta(hours=1, minutes=30),
        as_of=AT,
        trigger_type=TriggerType.FORECAST_REVISION,
        trigger_description="P50 revised down",
        minutes_to_gate_closure=76.0,
        gate_closure_at=gate,
    )


@pytest.fixture()
def store() -> DecisionStore:
    return DecisionStore()


def _proposed(store: DecisionStore, *, buy=32.0, sell=0.0, gate=None):
    return store.create(
        context=_ctx(gate=gate),
        recommendation=ModelRecommendation(action=RecommendedAction.BUY, buy_mwh=buy, sell_mwh=sell),
    )


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------


def test_accept_preserves_model_recommendation(store):
    decision = _proposed(store, buy=32.0)
    accepted = store.accept(decision.decision_id, rationale="Looks right", actor_id="alice")
    assert accepted.status is DecisionStatus.ACCEPTED
    assert accepted.trader_instruction.action is TraderAction.ACCEPT
    assert accepted.trader_instruction.buy_mwh == 32.0  # recommendation preserved
    assert accepted.trader_instruction.sell_mwh == 0.0
    assert accepted.transitions[-1].actor_id == "alice"


def test_modify_stores_trader_override_separately(store):
    decision = _proposed(store, buy=32.0)
    modified = store.modify(decision.decision_id, buy_mwh=20.0, limit_price=73.0, rationale="Hedge part")
    assert modified.status is DecisionStatus.MODIFIED
    assert modified.trader_instruction.buy_mwh == 20.0
    assert modified.trader_instruction.limit_price == 73.0
    # The model recommendation is untouched.
    assert modified.recommendation.buy_mwh == 32.0


def test_reject_records_rationale(store):
    decision = _proposed(store)
    rejected = store.reject(decision.decision_id, rationale="Disagree with revision")
    assert rejected.status is DecisionStatus.REJECTED
    assert rejected.trader_instruction.rationale == "Disagree with revision"


def test_delay_then_reopen(store):
    gate = AT + timedelta(minutes=76)
    decision = _proposed(store, gate=gate)
    delayed = store.delay(decision.decision_id, until=AT + timedelta(minutes=40), rationale="Wait", at=AT + timedelta(minutes=5))
    assert delayed.status is DecisionStatus.DELAYED
    assert delayed.trader_instruction.delayed_until == AT + timedelta(minutes=40)
    reopened = store.reopen(decision.decision_id, rationale="Reconsider")
    assert reopened.status is DecisionStatus.PROPOSED
    assert reopened.trader_instruction is None


# ---------------------------------------------------------------------------
# Modify validation
# ---------------------------------------------------------------------------


def test_modify_rejects_simultaneous_buy_and_sell(store):
    decision = _proposed(store)
    with pytest.raises(DecisionValidationError):
        store.modify(decision.decision_id, buy_mwh=5.0, sell_mwh=5.0, rationale="x")


def test_modify_rejects_negative_volume(store):
    decision = _proposed(store)
    with pytest.raises(DecisionValidationError):
        store.modify(decision.decision_id, buy_mwh=-5.0, rationale="x")


def test_modify_requires_a_meaningful_change(store):
    decision = _proposed(store, buy=32.0)
    with pytest.raises(DecisionValidationError):
        store.modify(decision.decision_id, buy_mwh=32.0, rationale="no change")


def test_modify_rejects_non_finite_limit(store):
    decision = _proposed(store)
    with pytest.raises(DecisionValidationError):
        store.modify(decision.decision_id, buy_mwh=20.0, limit_price=float("inf"), rationale="x")


# ---------------------------------------------------------------------------
# Delay validation
# ---------------------------------------------------------------------------


def test_delay_after_gate_closure_is_rejected(store):
    gate = AT + timedelta(minutes=76)
    decision = _proposed(store, gate=gate)
    # 'now' after the gate.
    with pytest.raises(DecisionValidationError):
        store.delay(decision.decision_id, until=AT + timedelta(minutes=200), rationale="x", at=AT + timedelta(minutes=90))


def test_delay_beyond_gate_closure_is_rejected(store):
    gate = AT + timedelta(minutes=76)
    decision = _proposed(store, gate=gate)
    with pytest.raises(DecisionValidationError):
        store.delay(decision.decision_id, until=AT + timedelta(minutes=100), rationale="x", at=AT + timedelta(minutes=10))


def test_delay_in_the_past_is_rejected(store):
    decision = _proposed(store)
    with pytest.raises(DecisionValidationError):
        store.delay(decision.decision_id, until=AT - timedelta(minutes=10), rationale="x", at=AT)


def test_delay_requires_timezone_aware(store):
    decision = _proposed(store)
    with pytest.raises(DecisionValidationError):
        store.delay(decision.decision_id, until=datetime(2026, 7, 26, 13, 0), rationale="x", at=AT)


# ---------------------------------------------------------------------------
# Invalid source states & concurrency
# ---------------------------------------------------------------------------


def test_invalid_source_status_rejected(store):
    decision = _proposed(store)
    store.accept(decision.decision_id)
    with pytest.raises(InvalidTransitionError):
        store.accept(decision.decision_id)  # ACCEPTED -> ACCEPTED illegal


def test_reopen_only_from_delayed(store):
    decision = _proposed(store)
    with pytest.raises(InvalidTransitionError):
        store.reopen(decision.decision_id)  # PROPOSED -> PROPOSED illegal


def test_stale_expected_status_raises(store):
    decision = _proposed(store)
    store.accept(decision.decision_id)
    with pytest.raises(StaleDecisionError):
        store.reject(decision.decision_id, rationale="x", expected_status=DecisionStatus.PROPOSED)


def test_stale_expected_sequence_raises(store):
    decision = _proposed(store)
    store.accept(decision.decision_id)  # sequence now 2
    with pytest.raises(StaleDecisionError) as excinfo:
        store.transition(
            decision.decision_id,
            DecisionStatus.SUBMITTED,
            actor=DecisionActor.SYSTEM,
            reason="x",
            expected_sequence=1,
        )
    assert excinfo.value.current_sequence == 2


def test_two_simultaneous_accepts_only_one_succeeds(store):
    decision = _proposed(store)
    store.accept(decision.decision_id, expected_status=DecisionStatus.PROPOSED)
    with pytest.raises(StaleDecisionError):
        store.accept(decision.decision_id, expected_status=DecisionStatus.PROPOSED)


# ---------------------------------------------------------------------------
# Audit & trust
# ---------------------------------------------------------------------------


def test_transition_history_is_append_only_with_actor(store):
    decision = _proposed(store)
    accepted = store.accept(decision.decision_id, rationale="ok", actor_id="bob")
    assert [t.sequence for t in accepted.transitions] == [1, 2]
    assert accepted.transitions[0].from_status is None
    assert accepted.transitions[1].from_status is DecisionStatus.PROPOSED
    assert accepted.transitions[1].to_status is DecisionStatus.ACCEPTED
    assert accepted.transitions[1].actor_id == "bob"


def test_trust_flags_remain_diagnostic_after_accept(store):
    decision = _proposed(store)
    accepted = store.accept(decision.decision_id)
    assert accepted.diagnostic_only is True
    assert accepted.not_executable is True
    assert accepted.context.trustworthy_for_live_trading is False
