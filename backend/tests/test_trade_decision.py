"""Milestone 1 (revised) — TradeDecision schema, state machine and audit log.

Additive and independent of the existing rolling/optimiser suites. Covers:

* one decision == one settlement period; multi-period triggers create a batch;
* structural immutability (frozen models, tuple history that cannot be mutated);
* the single source of lifecycle truth with derived sub-status properties;
* the full valid path and each branch, including reject/cancel/expire routing
  through DELIVERED -> SETTLED -> EVALUATED;
* rejection of every illegal transition and every inconsistent stage payload;
* append-only integrity and store immutability.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.decision_models import (
    BenchmarkResult,
    ConfidenceBasis,
    DecisionActor,
    DecisionBatch,
    DecisionContext,
    DecisionStatus,
    EvaluationStatus,
    ExecutionStatus,
    ModelRecommendation,
    RecommendedAction,
    TradeDecision,
    TraderInstruction,
    TraderAction,
    TraderStatus,
    TriggerType,
    Urgency,
)
from cockpit.decision_service import DecisionStore
from cockpit.decision_state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    StagePayloadError,
    TERMINAL_STATES,
    allowed_targets,
    apply_transition,
    can_transition,
)

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _tick(minutes: int) -> datetime:
    return AT0 + timedelta(minutes=minutes)


def _context(sp: int = 24, *, as_of: datetime = AT0) -> DecisionContext:
    start = datetime(2026, 7, 26, 11, 30, tzinfo=timezone.utc) + timedelta(minutes=30 * (sp - 24))
    return DecisionContext(
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        delivery_start=start,
        delivery_end=start + timedelta(minutes=30),
        as_of=as_of,
        trigger_type=TriggerType.FORECAST_REVISION,
        trigger_description="Wind P50 revised down 58 MWh.",
        minutes_to_gate_closure=76.0,
        position_before_mwh=120.0,
        forecast_revision_mwh=-58.0,
        p50_exposure_before_mwh=-54.0,
    )


def _recommendation() -> ModelRecommendation:
    return ModelRecommendation(
        action=RecommendedAction.BUY,
        buy_mwh=32.0,
        urgency=Urgency.HIGH,
        confidence_score=0.61,
        confidence_basis=ConfidenceBasis.ASSUMPTION_BASED,
        reasoning=("revision 1.8 std", "76 min to gate closure"),
    )


@pytest.fixture()
def store() -> DecisionStore:
    return DecisionStore()


def _proposed(store: DecisionStore, sp: int = 24) -> TradeDecision:
    return store.create(context=_context(sp), recommendation=_recommendation())


# ---------------------------------------------------------------------------
# Graph shape
# ---------------------------------------------------------------------------


def test_graph_is_total_with_single_terminal():
    assert set(ALLOWED_TRANSITIONS) == set(DecisionStatus)
    assert TERMINAL_STATES == frozenset({DecisionStatus.EVALUATED})
    assert allowed_targets(DecisionStatus.EVALUATED) == []


# ---------------------------------------------------------------------------
# Single-period identity & creation
# ---------------------------------------------------------------------------


def test_decision_is_single_settlement_period(store):
    decision = _proposed(store, sp=24)
    assert decision.settlement_period == 24
    assert decision.delivery_period == "SP24"
    assert decision.as_of == AT0
    # No collection-of-periods on the decision itself.
    assert not hasattr(decision, "affected_delivery_periods")
    assert decision.batch_id is None
    assert decision.status is DecisionStatus.PROPOSED
    assert decision.diagnostic_only and decision.not_executable
    assert decision.context.trustworthy_for_live_trading is False
    # Composed records.
    assert isinstance(decision.context, DecisionContext)
    assert isinstance(decision.recommendation, ModelRecommendation)
    assert decision.net_recommended_mwh == 32.0
    # Seeded creation record.
    assert len(decision.transitions) == 1
    assert decision.transitions[0].from_status is None
    assert decision.transitions[0].to_status is DecisionStatus.PROPOSED


def test_confidence_defaults_to_unavailable(store):
    decision = store.create(context=_context())
    assert decision.recommendation.confidence_basis is ConfidenceBasis.UNAVAILABLE
    assert decision.recommendation.confidence_score is None
    assert decision.recommendation.action is RecommendedAction.NO_ACTION


def test_multi_period_trigger_creates_batch_of_separate_decisions(store):
    contexts = [_context(sp) for sp in (24, 25, 26)]
    recs = [_recommendation() for _ in contexts]
    batch, decisions = store.create_batch(contexts=contexts, recommendations=recs)

    assert len(decisions) == 3
    assert batch.decision_ids == tuple(d.decision_id for d in decisions)
    assert batch.affected_delivery_periods == ("SP24", "SP25", "SP26")
    # Each decision owns exactly one distinct period and links back to the batch.
    assert [d.settlement_period for d in decisions] == [24, 25, 26]
    assert all(d.batch_id == batch.batch_id for d in decisions)
    # The batch is a pure index: it carries no exposure/execution/P&L.
    for banned in ("exposure", "pnl", "executed", "position"):
        assert not any(banned in name for name in DecisionBatch.model_fields)
    assert store.get_batch(batch.batch_id) == batch


# ---------------------------------------------------------------------------
# Structural immutability
# ---------------------------------------------------------------------------


def test_decision_is_frozen(store):
    decision = _proposed(store)
    with pytest.raises(ValidationError):
        decision.status = DecisionStatus.FILLED
    with pytest.raises(ValidationError):
        decision.context.settlement_period = 99


def test_transitions_are_an_immutable_tuple(store):
    decision = _proposed(store)
    assert isinstance(decision.transitions, tuple)
    with pytest.raises(AttributeError):
        decision.transitions.append("x")  # tuples have no append


def test_benchmark_results_are_a_tuple(store):
    decision = _proposed(store)
    did = decision.decision_id
    _run_to_settled(store, did)
    evaluated = store.evaluate(
        did,
        benchmark_results=[BenchmarkResult(name="no_action", label="No action", net_pnl_gbp=120.0)],
        at=_tick(50),
    )
    assert isinstance(evaluated.evaluation_result.benchmark_results, tuple)
    with pytest.raises(AttributeError):
        evaluated.evaluation_result.benchmark_results.append("x")


# ---------------------------------------------------------------------------
# Derived sub-status consistency
# ---------------------------------------------------------------------------


def test_derived_substatuses_track_lifecycle(store):
    decision = _proposed(store)
    did = decision.decision_id
    assert (decision.trader_status, decision.execution_status, decision.evaluation_status) == (
        TraderStatus.PENDING,
        ExecutionStatus.NOT_SUBMITTED,
        EvaluationStatus.PENDING,
    )
    accepted = store.accept(did, at=_tick(1))
    assert accepted.trader_status is TraderStatus.ACCEPTED
    assert accepted.execution_status is ExecutionStatus.NOT_SUBMITTED
    submitted = store.submit(did, at=_tick(2))
    assert submitted.execution_status is ExecutionStatus.SUBMITTED
    assert submitted.trader_status is TraderStatus.ACCEPTED
    filled = store.apply_fill(did, executed_buy_mwh=32.0, at=_tick(3))
    assert filled.execution_status is ExecutionStatus.FILLED
    delivered = store.deliver(did, realised_generation_mwh=210.0, at=_tick(30))
    assert delivered.evaluation_status is EvaluationStatus.DELIVERED
    settled = store.settle(did, realised_pnl_gbp=910.0, at=_tick(35))
    assert settled.evaluation_status is EvaluationStatus.SETTLED
    evaluated = store.evaluate(did, at=_tick(40))
    assert evaluated.evaluation_status is EvaluationStatus.EVALUATED


# ---------------------------------------------------------------------------
# Full valid path & branches
# ---------------------------------------------------------------------------


def _run_to_settled(store: DecisionStore, did: str) -> TradeDecision:
    store.accept(did, at=_tick(1))
    store.submit(did, at=_tick(2))
    store.apply_fill(did, executed_buy_mwh=32.0, at=_tick(3))
    store.deliver(did, realised_generation_mwh=210.0, position_after_mwh=152.0, at=_tick(30))
    return store.settle(did, realised_pnl_gbp=910.0, realised_reference_price=80.0, at=_tick(35))


def test_full_happy_path_accept_to_evaluated(store):
    decision = _proposed(store)
    did = decision.decision_id
    _run_to_settled(store, did)
    evaluated = store.evaluate(
        did,
        benchmark_results=[
            BenchmarkResult(name="no_action", label="No action", net_pnl_gbp=120.0),
            BenchmarkResult(name="perfect_foresight", label="Perfect foresight", net_pnl_gbp=1400.0, tradable=False),
        ],
        regret_vs_best_benchmark_gbp=490.0,
        at=_tick(40),
    )
    assert evaluated.status is DecisionStatus.EVALUATED
    assert evaluated.is_terminal
    assert evaluated.execution_result.net_executed_mwh == 32.0
    assert evaluated.settlement_result.realised_pnl_gbp == 910.0
    assert len(evaluated.evaluation_result.benchmark_results) == 2

    statuses = [(t.from_status, t.to_status) for t in evaluated.transitions]
    assert statuses == [
        (None, DecisionStatus.PROPOSED),
        (DecisionStatus.PROPOSED, DecisionStatus.ACCEPTED),
        (DecisionStatus.ACCEPTED, DecisionStatus.SUBMITTED),
        (DecisionStatus.SUBMITTED, DecisionStatus.FILLED),
        (DecisionStatus.FILLED, DecisionStatus.DELIVERED),
        (DecisionStatus.DELIVERED, DecisionStatus.SETTLED),
        (DecisionStatus.SETTLED, DecisionStatus.EVALUATED),
    ]
    assert [t.sequence for t in evaluated.transitions] == [1, 2, 3, 4, 5, 6, 7]


def test_modify_uses_trader_volume_as_request(store):
    decision = _proposed(store)
    did = decision.decision_id
    modified = store.modify(did, buy_mwh=20.0, limit_price=73.0, rationale="Hedge part now.", at=_tick(1))
    assert modified.status is DecisionStatus.MODIFIED
    assert modified.trader_status is TraderStatus.MODIFIED
    assert modified.trader_instruction.buy_mwh == 20.0
    submitted = store.submit(did, at=_tick(2))
    assert submitted.execution_result.requested_mwh == 20.0


def test_partial_fill_then_deliver(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.accept(did, at=_tick(1))
    store.submit(did, at=_tick(2))
    partial = store.apply_fill(did, executed_buy_mwh=20.0, at=_tick(3))
    assert partial.status is DecisionStatus.PARTIALLY_FILLED
    assert partial.execution_result.unfilled_volume_mwh == pytest.approx(12.0)
    delivered = store.deliver(did, realised_generation_mwh=180.0, at=_tick(30))
    assert delivered.status is DecisionStatus.DELIVERED


def test_delay_then_re_propose_clears_instruction(store):
    decision = _proposed(store)
    did = decision.decision_id
    delayed = store.delay(did, until=_tick(30), rationale="Wait for next vintage.", at=_tick(1))
    assert delayed.status is DecisionStatus.DELAYED
    assert delayed.trader_status is TraderStatus.DELAYED
    re_proposed = store.re_propose(did, at=_tick(31))
    assert re_proposed.status is DecisionStatus.PROPOSED
    assert re_proposed.trader_instruction is None
    assert re_proposed.trader_status is TraderStatus.PENDING


@pytest.mark.parametrize("dead_end", [DecisionStatus.REJECTED, DecisionStatus.CANCELLED, DecisionStatus.EXPIRED])
def test_reject_cancel_expire_reach_evaluation_via_delivery(store, dead_end):
    decision = _proposed(store)
    did = decision.decision_id
    if dead_end is DecisionStatus.REJECTED:
        current = store.reject(did, at=_tick(1))
    else:
        store.accept(did, at=_tick(1))
        store.submit(did, at=_tick(2))
        current = store.cancel(did, at=_tick(3)) if dead_end is DecisionStatus.CANCELLED else store.expire(did, at=_tick(3))
    assert current.status is dead_end
    # Still delivers, settles and is evaluated against the realised outcome.
    store.deliver(did, realised_generation_mwh=205.0, at=_tick(30))
    store.settle(did, realised_pnl_gbp=-140.0, at=_tick(35))
    evaluated = store.evaluate(
        did,
        decision_quality_note="No successful trade; exposure carried to imbalance.",
        at=_tick(40),
    )
    assert evaluated.status is DecisionStatus.EVALUATED
    assert evaluated.settlement_result.realised_pnl_gbp == -140.0


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,illegal_target",
    [
        ([], DecisionStatus.FILLED),
        ([], DecisionStatus.DELIVERED),
        ([], DecisionStatus.SETTLED),
        ([], DecisionStatus.SUBMITTED),
        ([DecisionStatus.ACCEPTED], DecisionStatus.DELIVERED),
        ([DecisionStatus.ACCEPTED, DecisionStatus.SUBMITTED], DecisionStatus.SETTLED),
    ],
)
def test_illegal_transitions_are_rejected(store, path, illegal_target):
    decision = _proposed(store)
    did = decision.decision_id
    for target in path:
        # Legal prefix, executed through the trader/execution helpers.
        if target is DecisionStatus.ACCEPTED:
            store.accept(did, at=_tick(1))
        elif target is DecisionStatus.SUBMITTED:
            store.submit(did, at=_tick(2))
    with pytest.raises(InvalidTransitionError):
        store.transition(did, illegal_target, actor=DecisionActor.SYSTEM, reason="test")


def test_terminal_state_rejects_everything():
    for target in DecisionStatus:
        assert can_transition(DecisionStatus.EVALUATED, target) is False


def test_no_backwards_transition_filled_to_proposed(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.accept(did, at=_tick(1))
    store.submit(did, at=_tick(2))
    store.apply_fill(did, executed_buy_mwh=32.0, at=_tick(3))
    with pytest.raises(InvalidTransitionError):
        store.transition(did, DecisionStatus.PROPOSED, actor=DecisionActor.SYSTEM, reason="rewind")


def test_invalid_transition_error_lists_allowed_targets(store):
    decision = _proposed(store)
    with pytest.raises(InvalidTransitionError) as excinfo:
        apply_transition(decision, DecisionStatus.FILLED, actor=DecisionActor.SYSTEM, reason="x")
    message = str(excinfo.value)
    assert "PROPOSED -> FILLED" in message
    assert "ACCEPTED" in message


# ---------------------------------------------------------------------------
# Stage payload validation
# ---------------------------------------------------------------------------


def test_status_cannot_advance_without_its_stage_record(store):
    decision = _proposed(store)
    # ACCEPTED without a TraderInstruction.
    with pytest.raises(StagePayloadError):
        apply_transition(decision, DecisionStatus.ACCEPTED, actor=DecisionActor.TRADER, reason="x")


def test_fill_requires_execution_record(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.accept(did, at=_tick(1))
    store.submit(did, at=_tick(2))
    current = store.get(did)
    # Jump straight to FILLED without updating the execution record's status.
    with pytest.raises(StagePayloadError):
        apply_transition(current, DecisionStatus.FILLED, actor=DecisionActor.MARKET, reason="x", at=_tick(3))


def test_filled_requires_zero_unfilled(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.accept(did, at=_tick(1))
    store.submit(did, at=_tick(2))
    current = store.get(did)
    bad_exec = current.execution_result.model_copy(
        update={"status": ExecutionStatus.FILLED, "unfilled_volume_mwh": 5.0}
    )
    with pytest.raises(StagePayloadError):
        apply_transition(
            current,
            DecisionStatus.FILLED,
            actor=DecisionActor.MARKET,
            reason="x",
            at=_tick(3),
            updates={"execution_result": bad_exec},
        )


def test_delivered_requires_realised_generation(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.reject(did, at=_tick(1))
    current = store.get(did)
    with pytest.raises(StagePayloadError):
        apply_transition(current, DecisionStatus.DELIVERED, actor=DecisionActor.SYSTEM, reason="x", at=_tick(30))


def test_settled_requires_realised_pnl(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.reject(did, at=_tick(1))
    store.deliver(did, realised_generation_mwh=200.0, at=_tick(30))
    current = store.get(did)
    delivery_only = current.settlement_result  # has generation, no pnl
    with pytest.raises(StagePayloadError):
        apply_transition(
            current,
            DecisionStatus.SETTLED,
            actor=DecisionActor.SYSTEM,
            reason="x",
            at=_tick(35),
            updates={"settlement_result": delivery_only},
        )


# ---------------------------------------------------------------------------
# Append-only integrity & store behaviour
# ---------------------------------------------------------------------------


def test_apply_transition_does_not_mutate_input(store):
    decision = _proposed(store)
    original = decision.transitions
    accepted = apply_transition(
        decision,
        DecisionStatus.ACCEPTED,
        actor=DecisionActor.TRADER,
        reason="x",
        at=_tick(1),
        updates={"trader_instruction": TraderInstruction(action=TraderAction.ACCEPT, decided_at=_tick(1))},
    )
    assert decision.transitions is original  # same tuple object, untouched
    assert decision.status is DecisionStatus.PROPOSED
    assert accepted.status is DecisionStatus.ACCEPTED


def test_audit_log_is_append_only(store):
    decision = _proposed(store)
    did = decision.decision_id
    accepted = store.accept(did, at=_tick(1))
    snapshot = accepted.transitions
    submitted = store.submit(did, at=_tick(2))
    assert submitted.transitions[: len(snapshot)] == snapshot
    assert len(submitted.transitions) == len(snapshot) + 1
    assert submitted.transitions[-1].to_status is DecisionStatus.SUBMITTED


def test_timestamps_must_be_timezone_aware(store):
    decision = _proposed(store)
    naive = datetime(2026, 7, 26, 12, 0)
    with pytest.raises(ValueError):
        apply_transition(
            decision,
            DecisionStatus.ACCEPTED,
            actor=DecisionActor.TRADER,
            reason="x",
            at=naive,
            updates={"trader_instruction": TraderInstruction(action=TraderAction.ACCEPT, decided_at=AT0)},
        )


def test_store_returns_immutable_current_version(store):
    decision = _proposed(store)
    did = decision.decision_id
    store.accept(did, at=_tick(1))
    fetched = store.get(did)
    assert fetched.status is DecisionStatus.ACCEPTED
    with pytest.raises(ValidationError):
        fetched.status = DecisionStatus.PROPOSED  # cannot mutate stored state


def test_list_orders_newest_first(store):
    first = _proposed(store, sp=24)
    second = _proposed(store, sp=25)
    ids = [d.decision_id for d in store.list()]
    assert ids[0] == second.decision_id
    assert ids[1] == first.decision_id


def test_unknown_decision_raises(store):
    with pytest.raises(KeyError):
        store.accept("dec-does-not-exist")
