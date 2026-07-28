"""Decision lifecycle state machine.

This module owns the *only* legal way a :class:`~cockpit.decision_models.TradeDecision`
may change state. It:

* declares the allowed transition graph and rejects invalid transitions with
  :class:`InvalidTransitionError`;
* validates the **stage payload** so the authoritative ``status`` can never move
  without the record that stage requires (:class:`StagePayloadError`);
* applies a transition **immutably** — returning a new frozen decision with an
  appended, append-only :class:`~cockpit.decision_models.DecisionTransition`
  record. The models are frozen and the history is a tuple, so append-only holds
  structurally, not by convention.

Trader-facing helpers (accept / modify / submit / fill / deliver / settle /
evaluate) live in :mod:`cockpit.decision_service`; they build the stage record
and call :func:`apply_transition`.

State graph::

    PROPOSED  ──▶ ACCEPTED · MODIFIED · REJECTED · DELAYED
    DELAYED   ──▶ PROPOSED · REJECTED
    ACCEPTED  ──▶ SUBMITTED · REJECTED
    MODIFIED  ──▶ SUBMITTED · REJECTED
    SUBMITTED ──▶ PARTIALLY_FILLED · FILLED · CANCELLED · EXPIRED
    PARTIALLY_FILLED ─▶ FILLED · DELIVERED · CANCELLED · EXPIRED
    FILLED    ──▶ DELIVERED
    REJECTED  ──▶ DELIVERED
    CANCELLED ──▶ DELIVERED
    EXPIRED   ──▶ DELIVERED
    DELIVERED ──▶ SETTLED
    SETTLED   ──▶ EVALUATED
    EVALUATED     (terminal)

A rejected, cancelled or expired decision still has a delivery-period outcome,
so it flows ``… → DELIVERED → SETTLED → EVALUATED`` and is evaluated against the
realised result. ``EVALUATED`` is the only terminal state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cockpit.decision_models import (
    DecisionActor,
    DecisionStatus,
    DecisionTransition,
    ExecutionStatus,
    TradeDecision,
    TraderAction,
)

_FILL_TOLERANCE_MWH = 1e-6

S = DecisionStatus


class InvalidTransitionError(ValueError):
    """Raised when a lifecycle transition is not permitted by the graph."""

    def __init__(self, current: DecisionStatus, target: DecisionStatus) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(status.value for status in allowed_targets(current)) or "none (terminal state)"
        super().__init__(
            f"Illegal decision transition {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {allowed}."
        )


class StagePayloadError(ValueError):
    """Raised when a transition is graph-legal but its stage record is missing
    or inconsistent (e.g. moving to FILLED without an ExecutionResult)."""

    def __init__(self, target: DecisionStatus, requirement: str) -> None:
        self.target = target
        self.requirement = requirement
        super().__init__(f"Transition to {target.value} {requirement}.")


ALLOWED_TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    S.PROPOSED: frozenset({S.ACCEPTED, S.MODIFIED, S.REJECTED, S.DELAYED}),
    S.DELAYED: frozenset({S.PROPOSED, S.REJECTED}),
    S.ACCEPTED: frozenset({S.SUBMITTED, S.REJECTED}),
    S.MODIFIED: frozenset({S.SUBMITTED, S.REJECTED}),
    S.SUBMITTED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}),
    S.PARTIALLY_FILLED: frozenset({S.FILLED, S.DELIVERED, S.CANCELLED, S.EXPIRED}),
    S.FILLED: frozenset({S.DELIVERED}),
    S.REJECTED: frozenset({S.DELIVERED}),
    S.CANCELLED: frozenset({S.DELIVERED}),
    S.EXPIRED: frozenset({S.DELIVERED}),
    S.DELIVERED: frozenset({S.SETTLED}),
    S.SETTLED: frozenset({S.EVALUATED}),
    S.EVALUATED: frozenset(),
}

TERMINAL_STATES: frozenset[DecisionStatus] = frozenset({S.EVALUATED})

# The graph must be total: every status names its outgoing edges explicitly.
assert set(ALLOWED_TRANSITIONS) == set(DecisionStatus), "transition graph is not total"

# Statuses whose execution status must match a same-named ExecutionResult.status.
_EXECUTION_STAGE_STATES: frozenset[DecisionStatus] = frozenset(
    {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}
)


def can_transition(current: DecisionStatus, target: DecisionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: DecisionStatus, target: DecisionStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def allowed_targets(current: DecisionStatus) -> list[DecisionStatus]:
    return sorted(ALLOWED_TRANSITIONS.get(current, frozenset()), key=lambda status: status.value)


def _validate_stage_payload(candidate: TradeDecision, previous: TradeDecision) -> None:
    """Ensure the record required by ``candidate.status`` is present & consistent."""
    status = candidate.status
    instruction = candidate.trader_instruction
    execution = candidate.execution_result
    settlement = candidate.settlement_result
    evaluation = candidate.evaluation_result

    trader_expectation = {
        S.ACCEPTED: TraderAction.ACCEPT,
        S.MODIFIED: TraderAction.MODIFY,
        S.REJECTED: TraderAction.REJECT,
        S.DELAYED: TraderAction.DELAY,
    }

    if status in trader_expectation:
        expected = trader_expectation[status]
        if instruction is None or instruction.action is not expected:
            raise StagePayloadError(status, f"requires a TraderInstruction with action {expected.value}")

    elif status is S.PROPOSED:
        # Re-proposal from DELAYED must clear the prior trader instruction.
        if previous.status is S.DELAYED and instruction is not None:
            raise StagePayloadError(status, "re-proposal must clear the trader instruction")

    elif status is S.SUBMITTED:
        if instruction is None or instruction.action not in (TraderAction.ACCEPT, TraderAction.MODIFY):
            raise StagePayloadError(status, "requires an accepted or modified trader instruction")
        if execution is None or execution.status is not ExecutionStatus.SUBMITTED:
            raise StagePayloadError(status, "requires an ExecutionResult in SUBMITTED status")

    elif status in _EXECUTION_STAGE_STATES:
        expected_exec = ExecutionStatus(status.value)
        if execution is None or execution.status is not expected_exec:
            raise StagePayloadError(status, f"requires an ExecutionResult in {expected_exec.value} status")
        if status is S.FILLED and execution.unfilled_volume_mwh > _FILL_TOLERANCE_MWH:
            raise StagePayloadError(status, "requires zero unfilled volume")
        if status is S.PARTIALLY_FILLED and execution.unfilled_volume_mwh <= _FILL_TOLERANCE_MWH:
            raise StagePayloadError(status, "requires non-zero unfilled volume")

    elif status is S.DELIVERED:
        if settlement is None or settlement.realised_generation_mwh is None:
            raise StagePayloadError(status, "requires a SettlementResult with realised_generation_mwh")

    elif status is S.SETTLED:
        if settlement is None or settlement.realised_pnl_gbp is None:
            raise StagePayloadError(status, "requires a SettlementResult with realised_pnl_gbp")

    elif status is S.EVALUATED:
        if evaluation is None:
            raise StagePayloadError(status, "requires an EvaluationResult")


def apply_transition(
    decision: TradeDecision,
    target: DecisionStatus,
    *,
    actor: DecisionActor,
    reason: str,
    at: datetime | None = None,
    actor_id: str | None = None,
    updates: dict[str, Any] | None = None,
) -> TradeDecision:
    """Validate and apply a lifecycle transition, returning a **new** decision.

    Steps: (1) reject graph-illegal transitions; (2) build the appended history
    tuple; (3) construct the candidate with the caller's stage-record
    ``updates``; (4) reject inconsistent stage payloads; (5) return the frozen
    candidate. The input ``decision`` is never mutated. ``status`` and
    ``transitions`` cannot be overridden through ``updates``.
    """
    assert_transition(decision.status, target)

    occurred_at = at or datetime.now(tz=timezone.utc)
    if occurred_at.tzinfo is None:
        raise ValueError("Transition timestamp must be timezone-aware")

    next_sequence = (decision.transitions[-1].sequence + 1) if decision.transitions else 1
    record = DecisionTransition(
        sequence=next_sequence,
        from_status=decision.status,
        to_status=target,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
        actor_id=actor_id,
    )

    merged: dict[str, Any] = dict(updates or {})
    merged.pop("status", None)
    merged.pop("transitions", None)
    merged["status"] = target
    merged["transitions"] = (*decision.transitions, record)

    candidate = decision.model_copy(update=merged)
    _validate_stage_payload(candidate, decision)
    return candidate


def record_creation(
    decision: TradeDecision,
    *,
    actor: DecisionActor = DecisionActor.MODEL,
    reason: str = "Decision proposed from rolling state and optimiser recommendation.",
    at: datetime | None = None,
) -> TradeDecision:
    """Seed the append-only log with the creation record for a fresh decision."""
    if decision.transitions:
        return decision
    occurred_at = at or decision.created_at or datetime.now(tz=timezone.utc)
    record = DecisionTransition(
        sequence=1,
        from_status=None,
        to_status=decision.status,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    return decision.model_copy(update={"transitions": (record,)})
