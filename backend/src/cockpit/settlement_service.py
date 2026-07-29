"""Delivery + settlement lifecycle coordination, storage and idempotency (M7).

Coordinates the decision lifecycle with the pure realised-P&L calculations in
:mod:`cockpit.pnl_attribution`. Nothing here settles a real imbalance. It:

* sources realised inputs for a completed period (SAMPLE) without fabricating
  unavailable values (:class:`SampleRealisedInputsProvider`);
* enforces the point-in-time guards (delivery only after the period ends; realised
  generation / imbalance prices / contracted position must be available);
* advances the decision (``…→DELIVERED→SETTLED``) through the decision service /
  state machine, which owns transition legality + optimistic concurrency;
* stores immutable :class:`DeliveryResult` / :class:`SettlementCalculation` records
  and enforces idempotency (client key + request-payload hash).

Realised-P&L arithmetic lives in ``pnl_attribution``; lifecycle legality lives in
the decision service/state machine. This module owns coordination + storage only.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from threading import RLock

from cockpit.decision_models import DecisionStatus, TradeDecision
from cockpit.decision_service import DECISIONS, DecisionStore
from cockpit.execution_models import ExecutionConfig
from cockpit.pnl_attribution import (
    compute_settlement,
    imbalance_direction,
    realised_imbalance,
    reconstruct_final_position,
)
from cockpit.settlement_models import (
    DeliveryResult,
    ProcessSkipReason,
    RealisedInputs,
    SettlementCalculation,
    new_delivery_id,
    new_settlement_id,
)

# SAMPLE settlement-pricing constants (deterministic, clearly diagnostic).
IMBALANCE_SPREAD_FRACTION = 0.12          # imbalance buy = ref×(1+s), sell = ref×(1−s)
SAMPLE_BASE_REFERENCE_PRICE = 70.0        # fallback reference when nothing executed
SAMPLE_GENERATION_DEVIATION_AMPLITUDE = 2.8  # realised = p50 + amplitude·sin(period·1.17+0.4)

# Execution-complete / no-trade states that may transition to DELIVERED.
DELIVERABLE_STATES = frozenset(
    {
        DecisionStatus.FILLED,
        DecisionStatus.PARTIALLY_FILLED,
        DecisionStatus.EXPIRED,
        DecisionStatus.REJECTED,
        DecisionStatus.CANCELLED,
    }
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class SettlementInputError(ValueError):
    """A required realised input is unavailable / a point-in-time guard failed (HTTP 422).

    Carries a structured :class:`ProcessSkipReason` so the SAMPLE batch helper can
    report why a period was skipped.
    """

    def __init__(self, reason: ProcessSkipReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class IdempotencyConflictError(Exception):
    """Same idempotency key re-used with a different payload (HTTP 409)."""


def _payload_hash(*, op: str, decision_id: str, expected_status: DecisionStatus | None, expected_sequence: int | None) -> str:
    """Canonical hash of a lifecycle request. The idempotency record is keyed by the
    client idempotency key; this is the payload signature stored alongside it, so a
    retry with any differing field is a conflict (409), not a silent replay."""
    canonical = json.dumps(
        {
            "op": op,
            "decision_id": decision_id,
            "expected_status": expected_status.value if expected_status is not None else None,
            "expected_sequence": expected_sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _RecordStore:
    """In-memory, immutable-out store for one record type + idempotency records."""

    def __init__(self, id_attr: str) -> None:
        self._id_attr = id_attr
        self._records: dict[str, object] = {}
        self._ids: list[str] = []
        self._by_decision: dict[str, list[str]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}  # client key -> (payload hash, record id)
        self._lock = RLock()

    def record(self, item, *, idempotency_key: str | None, payload_key: str) -> None:
        with self._lock:
            record_id = getattr(item, self._id_attr)
            self._records[record_id] = item
            self._ids.append(record_id)
            self._by_decision.setdefault(item.decision_id, []).append(record_id)
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (payload_key, record_id)

    def existing_for_key(self, idempotency_key: str, payload_key: str):
        with self._lock:
            found = self._idempotency.get(idempotency_key)
            if found is None:
                return None
            stored_payload, record_id = found
            if stored_payload != payload_key:
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was used with a different payload."
                )
            return self._records.get(record_id)

    def get(self, record_id: str):
        with self._lock:
            return self._records.get(record_id)

    def list(self):
        with self._lock:
            return [self._records[rid] for rid in reversed(self._ids)]

    def for_decision(self, decision_id: str):
        with self._lock:
            ids = self._by_decision.get(decision_id, [])
            return self._records[ids[-1]] if ids else None

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._ids.clear()
            self._by_decision.clear()
            self._idempotency.clear()


class SampleRealisedInputsProvider:
    """Sources realised inputs for a completed period from the decision's own stored
    records + a documented deterministic SAMPLE price/generation model.

    Realised generation (SAMPLE) = the decision's p50 generation forecast plus a
    deterministic per-period deviation (mirrors the simulated environment's
    ``actual_generation`` model). p50 generation is recovered from the context as
    ``p50_exposure_before + contracted_position`` (exposure ≡ generation − Q).
    Reference market price = the realised average execution price when the decision
    traded, else a SAMPLE base price; imbalance buy/sell prices bracket the reference
    by ±:data:`IMBALANCE_SPREAD_FRACTION`. Values are never fabricated: when a
    required input is missing, a :class:`SettlementInputError` is raised.
    """

    def __init__(self, *, config: ExecutionConfig | None = None) -> None:
        self.config = config or ExecutionConfig()

    def realised_for(self, decision: TradeDecision, *, now: datetime, require_period_ended: bool = True) -> RealisedInputs:
        ctx = decision.context
        if require_period_ended and now < ctx.delivery_end:
            raise SettlementInputError(
                ProcessSkipReason.DELIVERY_PERIOD_NOT_ENDED,
                f"Delivery period ends at {ctx.delivery_end.isoformat()}; cannot deliver before it completes.",
            )
        if ctx.position_before_mwh is None:
            raise SettlementInputError(
                ProcessSkipReason.MISSING_CONTRACTED_POSITION,
                "No pre-decision contracted position on the decision context.",
            )
        if ctx.p50_exposure_before_mwh is None:
            raise SettlementInputError(
                ProcessSkipReason.MISSING_REALISED_GENERATION,
                "No p50 exposure on the decision context; cannot derive SAMPLE realised generation.",
            )

        initial_q = ctx.position_before_mwh
        p50_generation = ctx.p50_exposure_before_mwh + initial_q  # exposure = generation − Q
        deviation = SAMPLE_GENERATION_DEVIATION_AMPLITUDE * math.sin(decision.settlement_period * 1.17 + 0.4)
        realised_generation = max(0.0, p50_generation + deviation)

        execution = decision.execution_result
        executed_buy = execution.executed_buy_mwh if execution else 0.0
        executed_sell = execution.executed_sell_mwh if execution else 0.0
        avg_price = execution.average_execution_price if execution else None
        fees = (execution.execution_fees_gbp or 0.0) if execution else 0.0

        reference = avg_price if avg_price is not None else (
            SAMPLE_BASE_REFERENCE_PRICE + 5.0 * math.sin(decision.settlement_period * 0.7)
        )
        if reference is None or not math.isfinite(reference) or reference <= 0:
            raise SettlementInputError(
                ProcessSkipReason.MISSING_SETTLEMENT_PRICES,
                "Could not derive a finite positive reference/imbalance price for the period.",
            )
        imbalance_buy = reference * (1.0 + IMBALANCE_SPREAD_FRACTION)
        imbalance_sell = reference * (1.0 - IMBALANCE_SPREAD_FRACTION)

        delivered_at = (
            decision.settlement_result.delivered_at
            if decision.settlement_result and decision.settlement_result.delivered_at
            else now
        )
        lineage = tuple(
            value
            for value in (
                ctx.forecast_vintage_id,
                ctx.previous_forecast_vintage_id,
                ctx.forecast_revision_id,
                ctx.market_snapshot_id,
                ctx.optimisation_run_id,
            )
            if value is not None
        )
        return RealisedInputs(
            decision_id=decision.decision_id,
            settlement_period=decision.settlement_period,
            delivery_start=ctx.delivery_start,
            delivery_end=ctx.delivery_end,
            delivered_at=delivered_at,
            realised_generation_mwh=round(realised_generation, 6),
            initial_contracted_position_mwh=round(initial_q, 6),
            executed_buy_mwh=round(executed_buy, 6),
            executed_sell_mwh=round(executed_sell, 6),
            average_execution_price_gbp_per_mwh=avg_price,
            execution_fees_gbp=round(fees, 6),
            imbalance_buy_price_gbp_per_mwh=round(imbalance_buy, 4),
            imbalance_sell_price_gbp_per_mwh=round(imbalance_sell, 4),
            reference_market_price_gbp_per_mwh=round(reference, 4),
            source_mode=ctx.source_mode,
            quality=ctx.quality,
            lineage_ids=lineage,
            run_mode=ctx.run_mode.value,
        )


class SettlementService:
    """Coordinates delivery + settlement of decisions with the pure P&L calcs."""

    def __init__(
        self,
        *,
        decisions: DecisionStore | None = None,
        provider: SampleRealisedInputsProvider | None = None,
    ) -> None:
        self.decisions = decisions or DECISIONS
        self.provider = provider or SampleRealisedInputsProvider()
        self.deliveries = _RecordStore("delivery_id")
        self.settlements = _RecordStore("settlement_id")

    # -- delivery -----------------------------------------------------------

    def deliver(
        self,
        decision_id: str,
        *,
        now: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
    ) -> DeliveryResult:
        at = now or _utcnow()
        payload = _payload_hash(op="deliver", decision_id=decision_id, expected_status=expected_status, expected_sequence=expected_sequence)
        if idempotency_key is not None:
            existing = self.deliveries.existing_for_key(idempotency_key, payload)
            if existing is not None:
                return existing  # idempotent retry

        decision = self.decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Unknown decision '{decision_id}'")

        inputs = self.provider.realised_for(decision, now=at, require_period_ended=True)
        final_q = reconstruct_final_position(
            inputs.initial_contracted_position_mwh, inputs.executed_buy_mwh, inputs.executed_sell_mwh
        )
        imbalance = realised_imbalance(inputs.realised_generation_mwh, final_q)

        # Advance the decision first — the state machine rejects a non-deliverable
        # state (InvalidTransitionError → 409) and enforces optimistic concurrency.
        self.decisions.deliver(
            decision_id,
            realised_generation_mwh=inputs.realised_generation_mwh,
            position_after_mwh=round(final_q, 6),
            at=at,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            reason="SAMPLE delivery: realised generation observed for the completed period.",
        )
        delivery = DeliveryResult(
            delivery_id=new_delivery_id(decision_id),
            decision_id=decision_id,
            settlement_period=decision.settlement_period,
            delivery_start=inputs.delivery_start,
            delivery_end=inputs.delivery_end,
            delivered_at=at,
            initial_contracted_position_mwh=inputs.initial_contracted_position_mwh,
            executed_buy_mwh=inputs.executed_buy_mwh,
            executed_sell_mwh=inputs.executed_sell_mwh,
            final_contracted_position_mwh=round(final_q, 6),
            realised_generation_mwh=inputs.realised_generation_mwh,
            realised_imbalance_mwh=round(imbalance, 6),
            imbalance_direction=imbalance_direction(imbalance),
            source_mode=inputs.source_mode,
            quality=inputs.quality,
            lineage_ids=inputs.lineage_ids,
            run_mode=inputs.run_mode,
        )
        self.deliveries.record(delivery, idempotency_key=idempotency_key, payload_key=payload)
        return delivery

    # -- settlement ---------------------------------------------------------

    def settle(
        self,
        decision_id: str,
        *,
        now: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
    ) -> SettlementCalculation:
        at = now or _utcnow()
        payload = _payload_hash(op="settle", decision_id=decision_id, expected_status=expected_status, expected_sequence=expected_sequence)
        if idempotency_key is not None:
            existing = self.settlements.existing_for_key(idempotency_key, payload)
            if existing is not None:
                return existing

        decision = self.decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Unknown decision '{decision_id}'")

        inputs = self.provider.realised_for(decision, now=at, require_period_ended=False)
        figures = compute_settlement(inputs)

        # Advance the decision (DELIVERED → SETTLED); non-DELIVERED raises
        # InvalidTransitionError (409); a duplicate settle likewise → 409.
        self.decisions.settle(
            decision_id,
            realised_pnl_gbp=round(figures.realised_pnl_gbp, 6),
            realised_reference_price=inputs.reference_market_price_gbp_per_mwh,
            realised_imbalance_mwh=round(figures.realised_imbalance_mwh, 6),
            at=at,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            reason="SAMPLE settlement: realised cash flow + incremental P&L vs NO_ACTION.",
        )
        settlement = SettlementCalculation(
            settlement_id=new_settlement_id(decision_id),
            decision_id=decision_id,
            settled_at=at,
            realised_generation_mwh=inputs.realised_generation_mwh,
            final_contracted_position_mwh=round(figures.final_contracted_position_mwh, 6),
            realised_imbalance_mwh=round(figures.realised_imbalance_mwh, 6),
            imbalance_direction=figures.imbalance_direction,
            imbalance_buy_price_gbp_per_mwh=inputs.imbalance_buy_price_gbp_per_mwh,
            imbalance_sell_price_gbp_per_mwh=inputs.imbalance_sell_price_gbp_per_mwh,
            execution_cashflow_gbp=round(figures.execution_cashflow_gbp, 4),
            execution_fees_gbp=round(figures.execution_fees_gbp, 4),
            imbalance_cashflow_gbp=round(figures.imbalance_cashflow_gbp, 4),
            total_realised_cashflow_gbp=round(figures.total_realised_cashflow_gbp, 4),
            realised_pnl_gbp=round(figures.realised_pnl_gbp, 4),
            calculation_basis=(
                "cash received positive; realised_pnl = total_realised_cashflow − NO_ACTION cashflow; "
                "I_t = G_t − Q_t; imbalance LONG→sell price, SHORT→buy price."
            ),
            warnings=(),
            lineage_ids=inputs.lineage_ids,
            source_mode=inputs.source_mode,
            quality=inputs.quality,
        )
        self.settlements.record(settlement, idempotency_key=idempotency_key, payload_key=payload)
        return settlement

    # -- reads --------------------------------------------------------------

    def list_deliveries(self) -> list[DeliveryResult]:
        return self.deliveries.list()

    def get_delivery(self, delivery_id: str) -> DeliveryResult | None:
        return self.deliveries.get(delivery_id)

    def delivery_for_decision(self, decision_id: str) -> DeliveryResult | None:
        return self.deliveries.for_decision(decision_id)

    def list_settlements(self) -> list[SettlementCalculation]:
        return self.settlements.list()

    def get_settlement(self, settlement_id: str) -> SettlementCalculation | None:
        return self.settlements.get(settlement_id)

    def settlement_for_decision(self, decision_id: str) -> SettlementCalculation | None:
        return self.settlements.for_decision(decision_id)

    def realised_inputs_for(self, decision: TradeDecision, *, now: datetime | None = None) -> RealisedInputs:
        """Re-source realised inputs for an already-delivered decision (deterministic)."""
        return self.provider.realised_for(decision, now=now or _utcnow(), require_period_ended=False)

    def reset(self) -> None:
        self.deliveries.reset()
        self.settlements.reset()


SETTLEMENT = SettlementService()
"""Process-level singleton delivery/settlement service."""
