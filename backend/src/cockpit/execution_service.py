"""Simulated-execution lifecycle coordination, storage and idempotency (M6B).

Coordinates the existing decision lifecycle with the pure execution simulator.
Nothing here routes a live order. It:

* checks submission eligibility (status ACCEPTED/MODIFIED, valid one-sided trader
  instruction, tradeable period, Gate Closure not passed, trust calculable);
* builds a :class:`SimulatedOrder` from the **TraderInstruction** (never silently
  from the model recommendation);
* runs the synchronous IOC-style simulator;
* advances the decision (SUBMITTED → FILLED/PARTIALLY_FILLED/EXPIRED) and
  populates the decision's ``ExecutionResult`` stage record;
* stores orders/outcomes and enforces optimistic concurrency + idempotency.

Execution logic itself lives in ``execution_simulator``; lifecycle rules stay in
the decision service/state machine. This module owns coordination only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from threading import RLock

from cockpit.decision_models import DecisionStatus, TradeDecision
from cockpit.decision_service import DECISIONS, DecisionStore, DecisionValidationError, StaleDecisionError
from cockpit.execution_models import (
    BookLevel,
    ExecutionConfig,
    ExecutionMode,
    ExecutionOutcome,
    SimulatedOrder,
    new_order_id,
)
from cockpit.execution_simulator import simulate

_TOL = 1e-6


class IdempotencyConflictError(Exception):
    """Same idempotency key re-used with a different payload."""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def submit_payload_hash(
    *,
    decision_id: str,
    mode: ExecutionMode,
    expected_status: DecisionStatus | None,
    expected_sequence: int | None,
    actor_id: str | None,
) -> str:
    """Stable hash of the *full* submit request, used as the idempotency payload key.

    The idempotency **record is keyed by the client-provided idempotency key**; this
    hash is the payload signature stored alongside it. It covers every client-controlled
    field of the request (the path decision id plus the whole ``SubmitSimulatedRequest``
    body — mode, the optimistic-concurrency guards, and the actor), so that:

    * an identical retry (same key, byte-identical request) hashes the same and returns
      the stored outcome; and
    * the same key sent with **any** differing field hashes differently and is rejected
      with :class:`IdempotencyConflictError` (HTTP 409).

    Execution ``mode`` is only one component here — it is deliberately *not* the key, so
    two independent operations that merely share a mode never collide.
    """
    canonical = json.dumps(
        {
            "decision_id": decision_id,
            "mode": mode.value,
            "expected_status": expected_status.value if expected_status is not None else None,
            "expected_sequence": expected_sequence,
            "actor_id": actor_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _MarketView:
    __slots__ = ("available", "side_levels", "gate_closure_at", "tradeable", "market_snapshot_id", "optimisation_run_id", "delivery_start")

    def __init__(self, *, available, side_levels=(), gate_closure_at=None, tradeable=False, market_snapshot_id=None, optimisation_run_id=None, delivery_start=None):
        self.available = available
        self.side_levels = side_levels
        self.gate_closure_at = gate_closure_at
        self.tradeable = tradeable
        self.market_snapshot_id = market_snapshot_id
        self.optimisation_run_id = optimisation_run_id
        self.delivery_start = delivery_start


def _market_for(decision: TradeDecision, side: str, rolling) -> _MarketView:
    live = rolling.live_state()
    run = rolling.current_optimisation()
    period = next((p for p in rolling.period_inputs if p.settlement_period == decision.settlement_period), None)
    if period is None:
        return _MarketView(available=False, market_snapshot_id=live.state.current_market_snapshot_id, optimisation_run_id=run.run_id)
    raw = period.asks if side == "BUY" else period.bids
    ordered = sorted(raw, key=lambda level: level.price_gbp_per_mwh, reverse=(side == "SELL"))
    side_levels = tuple(BookLevel(price_gbp_per_mwh=level.price_gbp_per_mwh, volume_mwh=level.volume_mwh) for level in ordered)
    return _MarketView(
        available=True,
        side_levels=side_levels,
        gate_closure_at=period.gate_closure_at,
        tradeable=period.tradeable,
        market_snapshot_id=live.state.current_market_snapshot_id,
        optimisation_run_id=run.run_id,
        delivery_start=period.delivery_start,
    )


class ExecutionStore:
    """In-memory, deep-copy-out store for simulated orders/outcomes + idempotency."""

    def __init__(self) -> None:
        self._orders: dict[str, SimulatedOrder] = {}
        self._order_ids: list[str] = []
        self._outcomes: dict[str, ExecutionOutcome] = {}
        self._by_decision: dict[str, list[str]] = {}
        # client idempotency key -> (request-payload hash, order_id)
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = RLock()

    def record(self, outcome: ExecutionOutcome, *, idempotency_key: str | None, payload_key: str) -> None:
        with self._lock:
            order = outcome.order
            self._orders[order.order_id] = order
            self._order_ids.append(order.order_id)
            self._outcomes[order.order_id] = outcome
            self._by_decision.setdefault(order.decision_id, []).append(order.order_id)
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (payload_key, order.order_id)

    def existing_for_key(self, idempotency_key: str, payload_key: str) -> ExecutionOutcome | None:
        with self._lock:
            record = self._idempotency.get(idempotency_key)
            if record is None:
                return None
            stored_payload, order_id = record
            if stored_payload != payload_key:
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was used with a different payload."
                )
            return self._outcomes.get(order_id)

    def list_orders(self) -> list[SimulatedOrder]:
        with self._lock:
            return [self._orders[order_id] for order_id in reversed(self._order_ids)]

    def get_order(self, order_id: str) -> SimulatedOrder | None:
        with self._lock:
            return self._orders.get(order_id)

    def list_outcomes(self) -> list[ExecutionOutcome]:
        with self._lock:
            return [self._outcomes[order_id] for order_id in reversed(self._order_ids)]

    def get_outcome(self, order_id: str) -> ExecutionOutcome | None:
        with self._lock:
            return self._outcomes.get(order_id)

    def outcomes_for_decision(self, decision_id: str) -> list[ExecutionOutcome]:
        with self._lock:
            return [self._outcomes[order_id] for order_id in self._by_decision.get(decision_id, [])]

    def reset(self) -> None:
        with self._lock:
            self._orders.clear()
            self._order_ids.clear()
            self._outcomes.clear()
            self._by_decision.clear()
            self._idempotency.clear()


class ExecutionService:
    """Coordinates decision submission with the execution simulator."""

    def __init__(self, *, decisions: DecisionStore | None = None, config: ExecutionConfig | None = None, rolling=None) -> None:
        self.decisions = decisions or DECISIONS
        self.config = config or ExecutionConfig()
        self.store = ExecutionStore()
        self._rolling = rolling

    def submit_simulated(
        self,
        decision_id: str,
        *,
        mode: ExecutionMode = ExecutionMode.REALISTIC,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        now: datetime | None = None,
        rolling=None,
    ) -> ExecutionOutcome:
        submitted_at = now or _utcnow()
        rolling = rolling or self._resolve_rolling()
        payload_key = submit_payload_hash(
            decision_id=decision_id,
            mode=mode,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            actor_id=actor_id,
        )

        if idempotency_key is not None:
            existing = self.store.existing_for_key(idempotency_key, payload_key)
            if existing is not None:
                return existing  # identical idempotent retry

        decision = self.decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Unknown decision '{decision_id}'")

        if decision.status not in (DecisionStatus.ACCEPTED, DecisionStatus.MODIFIED):
            raise StaleDecisionError(
                decision.status,
                decision.transitions[-1].sequence if decision.transitions else 0,
                f"Decision is {decision.status.value}; only ACCEPTED or MODIFIED decisions can be submitted to the simulator.",
            )
        if not decision.context.calculation_allowed:
            raise DecisionValidationError("Source/trust state does not permit calculation; cannot submit.")

        instruction = decision.trader_instruction
        if instruction is None:
            raise DecisionValidationError("No trader instruction to submit.")
        buy = instruction.buy_mwh or 0.0
        sell = instruction.sell_mwh or 0.0
        if buy > _TOL and sell > _TOL:
            raise DecisionValidationError("Trader instruction has both buy and sell positive; cannot submit.")
        side = "BUY" if buy > _TOL else "SELL" if sell > _TOL else "NONE"
        volume = buy if side == "BUY" else sell
        if side == "NONE" or volume <= _TOL:
            raise DecisionValidationError("Trader instruction volume is zero; nothing to submit.")

        market = _market_for(decision, side, rolling)
        if not market.available:
            raise DecisionValidationError("No market snapshot for this settlement period; cannot submit.")
        if not market.tradeable or (market.gate_closure_at is not None and submitted_at >= market.gate_closure_at):
            raise DecisionValidationError("Gate Closure has passed; the simulated order cannot be submitted.")

        order = SimulatedOrder(
            order_id=new_order_id(decision_id),
            decision_id=decision_id,
            submitted_at=submitted_at,
            actor_id=actor_id,
            side=side,
            requested_volume_mwh=round(volume, 6),
            limit_price=instruction.limit_price,
            execution_mode=mode,
            market_snapshot_id=market.market_snapshot_id,
            optimisation_run_id=market.optimisation_run_id,
            settlement_period=decision.settlement_period,
            delivery_start=market.delivery_start or decision.context.delivery_start,
            gate_closure_at=market.gate_closure_at or decision.context.gate_closure_at,
            source_mode=decision.context.source_mode,
            quality=decision.context.quality,
        )
        outcome = simulate(order, market.side_levels, self.config, at=submitted_at)

        # Advance the decision lifecycle (atomic optimistic concurrency on SUBMIT).
        self.decisions.submit(
            decision_id,
            requested_mwh=order.requested_volume_mwh,
            at=submitted_at,
            reason="Submitted to the internal execution simulator (diagnostic).",
            expected_status=expected_status,
            expected_sequence=expected_sequence,
        )
        avg_slippage = (outcome.total_slippage_gbp / outcome.total_filled_volume_mwh) if outcome.total_filled_volume_mwh > _TOL else 0.0
        if outcome.total_filled_volume_mwh > _TOL:
            self.decisions.apply_fill(
                decision_id,
                executed_buy_mwh=outcome.total_filled_volume_mwh if side == "BUY" else 0.0,
                executed_sell_mwh=outcome.total_filled_volume_mwh if side == "SELL" else 0.0,
                average_price=outcome.average_fill_price_gbp_per_mwh,
                cost_gbp=outcome.total_execution_cost_gbp,
                fees_gbp=outcome.total_fees_gbp,
                slippage_gbp_per_mwh=round(avg_slippage, 4),
                unfilled_mwh=outcome.unfilled_volume_mwh,
                at=submitted_at,
                reason=f"Simulated {'full' if outcome.execution_status.value == 'FILLED' else 'partial'} fill ({mode.value}).",
            )
        else:
            self.decisions.expire(decision_id, at=submitted_at, reason="Simulated order expired with no executable fill.")

        self.store.record(outcome, idempotency_key=idempotency_key, payload_key=payload_key)
        return outcome

    # -- reads --------------------------------------------------------------

    def list_orders(self) -> list[SimulatedOrder]:
        return self.store.list_orders()

    def get_order(self, order_id: str) -> SimulatedOrder | None:
        return self.store.get_order(order_id)

    def list_outcomes(self) -> list[ExecutionOutcome]:
        return self.store.list_outcomes()

    def get_outcome(self, order_id: str) -> ExecutionOutcome | None:
        return self.store.get_outcome(order_id)

    def latest_outcome_for_decision(self, decision_id: str) -> ExecutionOutcome | None:
        outcomes = self.store.outcomes_for_decision(decision_id)
        return outcomes[-1] if outcomes else None

    def reset(self) -> None:
        self.store.reset()

    def _resolve_rolling(self):
        if self._rolling is not None:
            return self._rolling
        from cockpit.rolling_service import ROLLING

        self._rolling = ROLLING
        return self._rolling


EXECUTION = ExecutionService()
"""Process-level singleton simulated-execution service."""
