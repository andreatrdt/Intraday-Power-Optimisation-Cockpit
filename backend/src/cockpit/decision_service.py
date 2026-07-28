"""In-memory, append-only store and lifecycle API for trade decisions.

Follows the same conventions as :data:`cockpit.rolling_service.ROLLING` and
:data:`cockpit.pipeline.PIPELINE`: a process-level singleton registry. Because
:class:`~cockpit.decision_models.TradeDecision` and its records are frozen, the
store can hand out stored instances directly — callers cannot mutate them.

Lifecycle helpers build the frozen stage record for their stage and route
through :func:`cockpit.decision_state_machine.apply_transition`, which validates
both the transition and the stage payload. There is no path that changes the
authoritative status without the appropriate record.

A trigger that affects several settlement periods is represented by
:meth:`DecisionStore.create_batch`, which creates one single-period decision per
period and a :class:`~cockpit.decision_models.DecisionBatch` grouping their IDs.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from cockpit.decision_models import (
    BenchmarkResult,
    DecisionActor,
    DecisionBatch,
    DecisionContext,
    DecisionStatus,
    ExecutionResult,
    ExecutionStatus,
    ModelRecommendation,
    SettlementResult,
    TradeDecision,
    TraderAction,
    TraderInstruction,
)
from cockpit.decision_state_machine import _FILL_TOLERANCE_MWH, apply_transition, record_creation

_ACTION_TOLERANCE_MWH = 1e-6


class StaleDecisionError(Exception):
    """Raised when an optimistic-concurrency guard does not match current state.

    Carries the current status/sequence so the caller (API) can return a 409 that
    lets the client reconcile.
    """

    def __init__(self, current_status: DecisionStatus, current_sequence: int, message: str) -> None:
        self.current_status = current_status
        self.current_sequence = current_sequence
        super().__init__(message)


class DecisionValidationError(ValueError):
    """Raised for a stateful trader-instruction validation failure."""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def new_decision_id(as_of: datetime | None = None) -> str:
    stamp = (as_of or _utcnow()).strftime("%Y%m%dT%H%M%S")
    return f"dec-{stamp}-{uuid4().hex[:8]}"


def new_batch_id(as_of: datetime | None = None) -> str:
    stamp = (as_of or _utcnow()).strftime("%Y%m%dT%H%M%S")
    return f"batch-{stamp}-{uuid4().hex[:8]}"


class DecisionStore:
    """Append-only, thread-safe registry of single-period trade decisions."""

    def __init__(self) -> None:
        self._decisions: dict[str, TradeDecision] = {}
        self._order: list[str] = []
        self._batches: dict[str, DecisionBatch] = {}
        self._lock = RLock()

    # -- construction -------------------------------------------------------

    def create(
        self,
        *,
        context: DecisionContext,
        recommendation: ModelRecommendation | None = None,
        decision_id: str | None = None,
        created_at: datetime | None = None,
        batch_id: str | None = None,
        actor: DecisionActor = DecisionActor.MODEL,
        creation_reason: str | None = None,
    ) -> TradeDecision:
        """Create and register a new ``PROPOSED`` single-period decision."""
        with self._lock:
            created = created_at or context.as_of
            decision = TradeDecision(
                decision_id=decision_id or new_decision_id(created),
                created_at=created,
                context=context,
                recommendation=recommendation or ModelRecommendation(),
                batch_id=batch_id,
            )
            decision = record_creation(
                decision,
                actor=actor,
                reason=creation_reason
                or "Decision proposed from rolling state and optimiser recommendation.",
                at=created,
            )
            self._store(decision)
            return decision

    def create_batch(
        self,
        *,
        contexts: list[DecisionContext],
        recommendations: list[ModelRecommendation | None] | None = None,
        batch_id: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[DecisionBatch, list[TradeDecision]]:
        """Create one single-period decision per context, grouped by a batch.

        The batch stores only decision IDs and affected periods — never a scalar
        exposure/execution/P&L spanning periods.
        """
        if not contexts:
            raise ValueError("A decision batch requires at least one period context")
        with self._lock:
            recs = recommendations or [None] * len(contexts)
            if len(recs) != len(contexts):
                raise ValueError("recommendations must align 1:1 with contexts")
            created = created_at or contexts[0].as_of
            bid = batch_id or new_batch_id(created)
            decisions = [
                self.create(context=ctx, recommendation=rec, batch_id=bid, created_at=created)
                for ctx, rec in zip(contexts, recs, strict=True)
            ]
            first = contexts[0]
            batch = DecisionBatch(
                batch_id=bid,
                created_at=created,
                trigger_type=first.trigger_type,
                trigger_description=first.trigger_description,
                run_mode=first.run_mode,
                source_mode=first.source_mode,
                quality=first.quality,
                decision_ids=tuple(decision.decision_id for decision in decisions),
                affected_delivery_periods=tuple(ctx.delivery_period for ctx in contexts),
            )
            self._batches[bid] = batch
            return batch, decisions

    # -- reads --------------------------------------------------------------

    def get(self, decision_id: str) -> TradeDecision | None:
        with self._lock:
            return self._decisions.get(decision_id)

    def list(self, *, newest_first: bool = True) -> list[TradeDecision]:
        with self._lock:
            order = list(reversed(self._order)) if newest_first else list(self._order)
            return [self._decisions[decision_id] for decision_id in order]

    def get_batch(self, batch_id: str) -> DecisionBatch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def list_batches(self) -> list[DecisionBatch]:
        with self._lock:
            return list(self._batches.values())

    # -- generic transition -------------------------------------------------

    def transition(
        self,
        decision_id: str,
        target: DecisionStatus,
        *,
        actor: DecisionActor,
        reason: str,
        at: datetime | None = None,
        actor_id: str | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
        updates: dict | None = None,
    ) -> TradeDecision:
        with self._lock:
            current = self._require(decision_id)
            _assert_precondition(current, expected_status, expected_sequence)
            updated = apply_transition(
                current, target, actor=actor, reason=reason, at=at, actor_id=actor_id, updates=updates
            )
            self._store(updated)
            return updated

    # -- trader lifecycle ---------------------------------------------------

    def accept(
        self,
        decision_id: str,
        *,
        rationale: str | None = None,
        actor_id: str | None = None,
        at: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
    ) -> TradeDecision:
        """Record trader acceptance, preserving the model recommendation as the
        trader instruction."""
        decided_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            _assert_precondition(current, expected_status, expected_sequence)
            rec = current.recommendation
            instruction = TraderInstruction(
                action=TraderAction.ACCEPT,
                decided_at=decided_at,
                buy_mwh=rec.buy_mwh,
                sell_mwh=rec.sell_mwh,
                limit_price=rec.limit_price,
                rationale=rationale,
            )
            return self.transition(
                decision_id,
                DecisionStatus.ACCEPTED,
                actor=DecisionActor.TRADER,
                actor_id=actor_id,
                reason=rationale or "Trader accepted the recommendation as-is.",
                at=decided_at,
                updates={"trader_instruction": instruction},
            )

    def modify(
        self,
        decision_id: str,
        *,
        buy_mwh: float | None = None,
        sell_mwh: float | None = None,
        limit_price: float | None = None,
        rationale: str | None = None,
        actor_id: str | None = None,
        at: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
    ) -> TradeDecision:
        decided_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            _assert_precondition(current, expected_status, expected_sequence)
            buy = buy_mwh or 0.0
            sell = sell_mwh or 0.0
            _validate_modify(current.recommendation, buy, sell, limit_price)
            instruction = TraderInstruction(
                action=TraderAction.MODIFY,
                decided_at=decided_at,
                buy_mwh=round(buy, 6),
                sell_mwh=round(sell, 6),
                limit_price=limit_price,
                rationale=rationale,
            )
            return self.transition(
                decision_id,
                DecisionStatus.MODIFIED,
                actor=DecisionActor.TRADER,
                actor_id=actor_id,
                reason=rationale or "Trader modified the recommended volume/price.",
                at=decided_at,
                updates={"trader_instruction": instruction},
            )

    def reject(
        self,
        decision_id: str,
        *,
        rationale: str | None = None,
        actor_id: str | None = None,
        at: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
    ) -> TradeDecision:
        decided_at = at or _utcnow()
        instruction = TraderInstruction(action=TraderAction.REJECT, decided_at=decided_at, rationale=rationale)
        return self.transition(
            decision_id,
            DecisionStatus.REJECTED,
            actor=DecisionActor.TRADER,
            actor_id=actor_id,
            reason=rationale or "Trader rejected the recommendation.",
            at=decided_at,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            updates={"trader_instruction": instruction},
        )

    def delay(
        self,
        decision_id: str,
        *,
        until: datetime | None = None,
        rationale: str | None = None,
        actor_id: str | None = None,
        at: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
    ) -> TradeDecision:
        decided_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            _assert_precondition(current, expected_status, expected_sequence)
            _validate_delay(current, until, decided_at)
            instruction = TraderInstruction(
                action=TraderAction.DELAY,
                decided_at=decided_at,
                delayed_until=until,
                rationale=rationale,
            )
            return self.transition(
                decision_id,
                DecisionStatus.DELAYED,
                actor=DecisionActor.TRADER,
                actor_id=actor_id,
                reason=rationale or "Trader delayed the decision to a later gate.",
                at=decided_at,
                updates={"trader_instruction": instruction},
            )

    def reopen(
        self,
        decision_id: str,
        *,
        rationale: str | None = None,
        actor_id: str | None = None,
        at: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
    ) -> TradeDecision:
        """Reopen a DELAYED decision to ``PROPOSED`` (trader-initiated), clearing
        the instruction. The state machine permits this only from DELAYED."""
        return self.transition(
            decision_id,
            DecisionStatus.PROPOSED,
            actor=DecisionActor.TRADER,
            actor_id=actor_id,
            reason=rationale or "Trader reopened the delayed decision.",
            at=at,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            updates={"trader_instruction": None},
        )

    def re_propose(self, decision_id: str, *, reason: str | None = None, at: datetime | None = None) -> TradeDecision:
        """Return a delayed decision to ``PROPOSED``, clearing the instruction (system-initiated)."""
        return self.transition(
            decision_id,
            DecisionStatus.PROPOSED,
            actor=DecisionActor.SYSTEM,
            reason=reason or "Delayed decision re-proposed at the next decision gate.",
            at=at,
            updates={"trader_instruction": None},
        )

    # -- execution lifecycle ------------------------------------------------

    def submit(
        self,
        decision_id: str,
        *,
        requested_mwh: float | None = None,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> TradeDecision:
        submitted_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            requested = requested_mwh if requested_mwh is not None else _effective_requested_mwh(current)
            execution = ExecutionResult(
                status=ExecutionStatus.SUBMITTED,
                submitted_at=submitted_at,
                requested_mwh=round(requested, 6),
                last_update_at=submitted_at,
            )
            return self.transition(
                decision_id,
                DecisionStatus.SUBMITTED,
                actor=DecisionActor.SYSTEM,
                reason=reason or "Instruction submitted to the simulated market.",
                at=submitted_at,
                updates={"execution_result": execution},
            )

    def apply_fill(
        self,
        decision_id: str,
        *,
        executed_buy_mwh: float = 0.0,
        executed_sell_mwh: float = 0.0,
        average_price: float | None = None,
        cost_gbp: float | None = None,
        fees_gbp: float | None = None,
        slippage_gbp_per_mwh: float | None = None,
        unfilled_mwh: float | None = None,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> TradeDecision:
        """Record a (partial or full) simulated fill against the submitted order.

        Executed — not requested — volume is what later milestones propagate to
        position/SoC state (Phase 12).
        """
        occurred_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            base = current.execution_result
            if base is None:
                raise ValueError(f"Decision '{decision_id}' has no submitted order to fill")
            executed = executed_buy_mwh + executed_sell_mwh
            resolved_unfilled = unfilled_mwh if unfilled_mwh is not None else max(0.0, base.requested_mwh - executed)
            fully_filled = resolved_unfilled <= _FILL_TOLERANCE_MWH
            target = DecisionStatus.FILLED if fully_filled else DecisionStatus.PARTIALLY_FILLED
            execution = base.model_copy(
                update={
                    "status": ExecutionStatus.FILLED if fully_filled else ExecutionStatus.PARTIALLY_FILLED,
                    "executed_buy_mwh": round(executed_buy_mwh, 6),
                    "executed_sell_mwh": round(executed_sell_mwh, 6),
                    "average_execution_price": average_price,
                    "execution_cost_gbp": cost_gbp,
                    "execution_fees_gbp": fees_gbp,
                    "execution_slippage_gbp_per_mwh": slippage_gbp_per_mwh,
                    "unfilled_volume_mwh": round(resolved_unfilled, 6),
                    "last_update_at": occurred_at,
                }
            )
            return self.transition(
                decision_id,
                target,
                actor=DecisionActor.MARKET,
                reason=reason
                or (
                    f"Simulated {'full' if fully_filled else 'partial'} fill: "
                    f"{executed:.3f} MWh executed, {resolved_unfilled:.3f} MWh unfilled."
                ),
                at=occurred_at,
                updates={"execution_result": execution},
            )

    def cancel(self, decision_id: str, *, reason: str | None = None, at: datetime | None = None) -> TradeDecision:
        occurred_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            base = current.execution_result
            if base is None:
                raise ValueError(f"Decision '{decision_id}' has no submitted order to cancel")
            execution = base.model_copy(update={"status": ExecutionStatus.CANCELLED, "last_update_at": occurred_at})
            return self.transition(
                decision_id,
                DecisionStatus.CANCELLED,
                actor=DecisionActor.TRADER,
                reason=reason or "Instruction cancelled before completion.",
                at=occurred_at,
                updates={"execution_result": execution},
            )

    def expire(self, decision_id: str, *, reason: str | None = None, at: datetime | None = None) -> TradeDecision:
        occurred_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            base = current.execution_result
            if base is None:
                raise ValueError(f"Decision '{decision_id}' has no submitted order to expire")
            unfilled = max(0.0, base.requested_mwh - (base.executed_buy_mwh + base.executed_sell_mwh))
            execution = base.model_copy(
                update={
                    "status": ExecutionStatus.EXPIRED,
                    "unfilled_volume_mwh": round(unfilled, 6),
                    "last_update_at": occurred_at,
                }
            )
            return self.transition(
                decision_id,
                DecisionStatus.EXPIRED,
                actor=DecisionActor.MARKET,
                reason=reason or "Instruction expired unfilled at Gate Closure.",
                at=occurred_at,
                updates={"execution_result": execution},
            )

    # -- delivery / settlement / evaluation ---------------------------------

    def deliver(
        self,
        decision_id: str,
        *,
        realised_generation_mwh: float,
        position_after_mwh: float | None = None,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> TradeDecision:
        delivered_at = at or _utcnow()
        settlement = SettlementResult(
            realised_generation_mwh=realised_generation_mwh,
            position_after_mwh=position_after_mwh,
            delivered_at=delivered_at,
        )
        return self.transition(
            decision_id,
            DecisionStatus.DELIVERED,
            actor=DecisionActor.SYSTEM,
            reason=reason or "Delivery period completed; physical outcome observed.",
            at=delivered_at,
            updates={"settlement_result": settlement},
        )

    def settle(
        self,
        decision_id: str,
        *,
        realised_pnl_gbp: float,
        realised_reference_price: float | None = None,
        realised_imbalance_mwh: float | None = None,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> TradeDecision:
        settled_at = at or _utcnow()
        with self._lock:
            current = self._require(decision_id)
            base = current.settlement_result or SettlementResult()
            settlement = base.model_copy(
                update={
                    "realised_reference_price": realised_reference_price,
                    "realised_imbalance_mwh": realised_imbalance_mwh,
                    "realised_pnl_gbp": realised_pnl_gbp,
                    "settled_at": settled_at,
                }
            )
            return self.transition(
                decision_id,
                DecisionStatus.SETTLED,
                actor=DecisionActor.SYSTEM,
                reason=reason or "Settlement computed against realised reference price.",
                at=settled_at,
                updates={"settlement_result": settlement},
            )

    def evaluate(
        self,
        decision_id: str,
        *,
        benchmark_results: list[BenchmarkResult] | tuple[BenchmarkResult, ...] | None = None,
        regret_vs_best_benchmark_gbp: float | None = None,
        decision_quality_note: str | None = None,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> TradeDecision:
        from cockpit.decision_models import EvaluationResult

        evaluated_at = at or _utcnow()
        evaluation = EvaluationResult(
            benchmark_results=tuple(benchmark_results or ()),
            regret_vs_best_benchmark_gbp=regret_vs_best_benchmark_gbp,
            decision_quality_note=decision_quality_note,
            evaluated_at=evaluated_at,
        )
        return self.transition(
            decision_id,
            DecisionStatus.EVALUATED,
            actor=DecisionActor.SYSTEM,
            reason=reason or "Decision evaluated against benchmarks.",
            at=evaluated_at,
            updates={"evaluation_result": evaluation},
        )

    # -- internals ----------------------------------------------------------

    def _require(self, decision_id: str) -> TradeDecision:
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Unknown decision '{decision_id}'")
        return decision

    def _store(self, decision: TradeDecision) -> None:
        if decision.decision_id not in self._decisions:
            self._order.append(decision.decision_id)
        self._decisions[decision.decision_id] = decision

    def clear(self) -> None:
        """Reset the store. Intended for tests and replay resets only."""
        with self._lock:
            self._decisions.clear()
            self._order.clear()
            self._batches.clear()


def _current_sequence(decision: TradeDecision) -> int:
    return decision.transitions[-1].sequence if decision.transitions else 0


def _assert_precondition(
    current: TradeDecision,
    expected_status: DecisionStatus | None,
    expected_sequence: int | None,
) -> None:
    """Optimistic-concurrency guard: raise StaleDecisionError on a mismatch."""
    last_seq = _current_sequence(current)
    if expected_status is not None and current.status is not expected_status:
        raise StaleDecisionError(
            current.status, last_seq, f"Decision is {current.status.value}, expected {expected_status.value}."
        )
    if expected_sequence is not None and last_seq != expected_sequence:
        raise StaleDecisionError(
            current.status, last_seq, f"Decision is at sequence {last_seq}, expected {expected_sequence}."
        )


def _validate_modify(recommendation: ModelRecommendation, buy: float, sell: float, limit_price: float | None) -> None:
    if buy < 0 or sell < 0:
        raise DecisionValidationError("Trader volumes must be non-negative.")
    if buy > _ACTION_TOLERANCE_MWH and sell > _ACTION_TOLERANCE_MWH:
        raise DecisionValidationError("Buy and sell cannot both be positive; the market hedge is one-sided.")
    if limit_price is not None and not math.isfinite(limit_price):
        raise DecisionValidationError("Limit price must be finite when supplied.")
    unchanged = (
        abs(buy - recommendation.buy_mwh) <= _ACTION_TOLERANCE_MWH
        and abs(sell - recommendation.sell_mwh) <= _ACTION_TOLERANCE_MWH
        and (limit_price is None or limit_price == recommendation.limit_price)
    )
    if unchanged:
        raise DecisionValidationError("A modification must differ from the model recommendation.")


def _validate_delay(current: TradeDecision, until: datetime | None, now: datetime) -> None:
    if until is None:
        raise DecisionValidationError("delayed_until is required.")
    if until.tzinfo is None:
        raise DecisionValidationError("delayed_until must be timezone-aware.")
    if until <= now:
        raise DecisionValidationError("delayed_until must be later than the current time.")
    gate = current.context.gate_closure_at
    if gate is not None:
        if now >= gate:
            raise DecisionValidationError("Gate Closure has already passed; the decision cannot be delayed.")
        if until >= gate:
            raise DecisionValidationError("delayed_until must be before Gate Closure.")


def _effective_requested_mwh(decision: TradeDecision) -> float:
    """Requested volume for submission: trader override if modified, else model."""
    instruction = decision.trader_instruction
    if instruction is not None and instruction.action is TraderAction.MODIFY:
        buy = instruction.buy_mwh or 0.0
        sell = instruction.sell_mwh or 0.0
        if buy or sell:
            return abs(buy - sell)
    return abs(decision.recommendation.net_mwh)


DECISIONS = DecisionStore()
"""Process-level singleton decision store (mirrors ROLLING / PIPELINE)."""
