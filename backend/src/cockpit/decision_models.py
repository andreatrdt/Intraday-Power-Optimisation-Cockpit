"""Typed contracts for the trader-in-the-loop ``TradeDecision`` lifecycle.

This module is **purely additive**. It introduces the decision object that
connects a forecast/market trigger to a model recommendation, a trader
instruction, a *simulated* execution, physical delivery, settlement and a
post-hoc evaluation against benchmarks.

Key design decisions (Milestone 1, revised):

* **One decision = exactly one settlement period.** A ``TradeDecision`` carries
  a single ``settlement_period`` / ``delivery_period``; every downstream field
  (exposure, execution, realised generation, imbalance, P&L) refers
  unambiguously to that one period. When a trigger affects several periods the
  caller creates a :class:`DecisionBatch` grouping several decision IDs — never
  a single scalar spread across periods.
* **Composition, not a flat model.** ``TradeDecision`` composes stage records
  (:class:`DecisionContext`, :class:`ModelRecommendation`,
  :class:`TraderInstruction`, :class:`ExecutionResult`,
  :class:`SettlementResult`, :class:`EvaluationResult`) rather than carrying all
  stage fields flat. Each stage record is populated when its lifecycle stage is
  reached.
* **Structural immutability.** Every model here is ``frozen``; collections are
  tuples. The append-only history therefore cannot be mutated in place — the
  guarantee does not rely on store discipline or deep-copy convention.
* **Single source of lifecycle truth.** ``DecisionStatus`` is the only persisted
  lifecycle state. ``trader_status`` / ``execution_status`` /
  ``evaluation_status`` are **computed properties**, so they can never drift out
  of sync with the authoritative status and stage records.

Nothing here submits an order or controls an asset; every decision is created
``diagnostic_only`` and ``not_executable`` and carries the same trust semantics
(``calculation_allowed`` / ``trustworthy_for_live_trading``) used elsewhere in
the cockpit.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cockpit.models import Quality, SourceMode


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DecisionStatus(StrEnum):
    """The single authoritative lifecycle state of a decision.

    Legal transitions are owned by :mod:`cockpit.decision_state_machine`.
    """

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    MODIFIED = "MODIFIED"
    REJECTED = "REJECTED"
    DELAYED = "DELAYED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DELIVERED = "DELIVERED"
    SETTLED = "SETTLED"
    EVALUATED = "EVALUATED"


class TriggerType(StrEnum):
    FORECAST_REVISION = "FORECAST_REVISION"
    MARKET_MOVE = "MARKET_MOVE"
    POSITION_CHANGE = "POSITION_CHANGE"
    GATE_CLOSURE_APPROACHING = "GATE_CLOSURE_APPROACHING"
    RISK_LIMIT_BREACH = "RISK_LIMIT_BREACH"
    MANUAL = "MANUAL"
    SCHEDULED_REVIEW = "SCHEDULED_REVIEW"


class RecommendedAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    PARTIAL_HEDGE = "PARTIAL_HEDGE"
    WAIT = "WAIT"
    NO_ACTION = "NO_ACTION"


class Urgency(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    IMMEDIATE = "IMMEDIATE"


class ConfidenceBasis(StrEnum):
    """How ``confidence_score`` should be interpreted.

    Confidence is never presented as calibrated unless it is backed by
    historical forecast-error statistics.
    """

    HISTORICAL_CALIBRATION = "HISTORICAL_CALIBRATION"
    ASSUMPTION_BASED = "ASSUMPTION_BASED"
    UNAVAILABLE = "UNAVAILABLE"


class TraderAction(StrEnum):
    ACCEPT = "ACCEPT"
    MODIFY = "MODIFY"
    REJECT = "REJECT"
    DELAY = "DELAY"


class TraderStatus(StrEnum):
    """Coarse trader-stage label. Computed, never persisted."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    MODIFIED = "MODIFIED"
    REJECTED = "REJECTED"
    DELAYED = "DELAYED"


class ExecutionStatus(StrEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class EvaluationStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    SETTLED = "SETTLED"
    EVALUATED = "EVALUATED"


class DecisionActor(StrEnum):
    """Who caused a transition, for the audit log."""

    MODEL = "MODEL"
    TRADER = "TRADER"
    SYSTEM = "SYSTEM"
    MARKET = "MARKET"


class RunMode(StrEnum):
    """Explicit product boundary a decision was produced under."""

    SAMPLE_DEMO = "SAMPLE_DEMO"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    LIVE_OBSERVATION = "LIVE_OBSERVATION"


_TRADER_STATUS_BY_ACTION: dict[TraderAction, TraderStatus] = {
    TraderAction.ACCEPT: TraderStatus.ACCEPTED,
    TraderAction.MODIFY: TraderStatus.MODIFIED,
    TraderAction.REJECT: TraderStatus.REJECTED,
    TraderAction.DELAY: TraderStatus.DELAYED,
}

_EVALUATION_STATUS_BY_STATUS: dict[DecisionStatus, EvaluationStatus] = {
    DecisionStatus.DELIVERED: EvaluationStatus.DELIVERED,
    DecisionStatus.SETTLED: EvaluationStatus.SETTLED,
    DecisionStatus.EVALUATED: EvaluationStatus.EVALUATED,
}


class _Frozen(BaseModel):
    """Base for every decision record: immutable, ignores unknown extras."""

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Stage records
# ---------------------------------------------------------------------------


class DecisionContext(_Frozen):
    """Everything known at proposal time about *one* period and why we decide.

    Absorbs period identity, trigger, provenance/trust and the pre-action
    exposure snapshot for this single settlement period.
    """

    # Period identity (exactly one) -----------------------------------------
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    as_of: datetime

    # Trigger ----------------------------------------------------------------
    trigger_type: TriggerType
    trigger_description: str

    # Provenance / trust -----------------------------------------------------
    run_mode: RunMode = RunMode.SAMPLE_DEMO
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    calculation_allowed: bool = True
    trustworthy_for_live_trading: bool = False
    forecast_vintage_id: str | None = None
    previous_forecast_vintage_id: str | None = None
    forecast_revision_id: str | None = None
    market_snapshot_id: str | None = None
    optimisation_run_id: str | None = None
    minutes_to_gate_closure: float | None = None

    # Exposure before action (this period) ----------------------------------
    # The *_exposure_before_mwh fields are the LATEST (post-revision, pre-hedge)
    # exposures. The PREVIOUS-vintage exposures live on the linked
    # ForecastRevision (see forecast_revision_id) — the single source of truth,
    # not duplicated here.
    position_before_mwh: float | None = None
    forecast_revision_mwh: float | None = None
    p10_exposure_before_mwh: float | None = None
    p50_exposure_before_mwh: float | None = None
    p90_exposure_before_mwh: float | None = None


class ModelRecommendation(_Frozen):
    """What the model recommends for this period."""

    action: RecommendedAction = RecommendedAction.NO_ACTION
    buy_mwh: float = 0.0
    sell_mwh: float = 0.0
    limit_price: float | None = None
    urgency: Urgency = Urgency.NONE
    confidence_score: float | None = None
    confidence_basis: ConfidenceBasis = ConfidenceBasis.UNAVAILABLE
    risk_if_no_action_gbp: float | None = None
    expected_action_value_gbp: float | None = None
    expected_wait_value_gbp: float | None = None
    reasoning: tuple[str, ...] = ()

    @property
    def net_mwh(self) -> float:
        """Signed recommended volume: positive = net buy, negative = net sell."""
        return round(self.buy_mwh - self.sell_mwh, 6)


class TraderInstruction(_Frozen):
    """What the trader decided. Present once the trader has acted."""

    action: TraderAction
    decided_at: datetime
    buy_mwh: float | None = None
    sell_mwh: float | None = None
    limit_price: float | None = None
    rationale: str | None = None
    delayed_until: datetime | None = None


class ExecutionResult(_Frozen):
    """The *simulated* execution outcome. Present once submitted."""

    status: ExecutionStatus
    submitted_at: datetime
    requested_mwh: float = 0.0
    executed_buy_mwh: float = 0.0
    executed_sell_mwh: float = 0.0
    average_execution_price: float | None = None
    execution_cost_gbp: float | None = None
    execution_fees_gbp: float | None = None
    execution_slippage_gbp_per_mwh: float | None = None
    unfilled_volume_mwh: float = 0.0
    last_update_at: datetime | None = None

    @property
    def net_executed_mwh(self) -> float:
        """Signed executed volume: positive = net buy, negative = net sell."""
        return round(self.executed_buy_mwh - self.executed_sell_mwh, 6)


class SettlementResult(_Frozen):
    """Realised delivery + settlement outcome for this period.

    Delivery data (realised generation, position after) is set at ``DELIVERED``;
    settlement cash data (reference price, imbalance, P&L) is added at
    ``SETTLED``. Because records are frozen, each stage produces a new record.
    """

    realised_generation_mwh: float | None = None
    position_after_mwh: float | None = None
    realised_reference_price: float | None = None
    realised_imbalance_mwh: float | None = None
    realised_pnl_gbp: float | None = None
    delivered_at: datetime | None = None
    settled_at: datetime | None = None


class BenchmarkResult(_Frozen):
    """One comparator's outcome for the evaluation stage (Phase 11)."""

    name: str
    label: str
    net_pnl_gbp: float | None = None
    regret_gbp: float | None = None
    tradable: bool = True
    note: str | None = None


class EvaluationResult(_Frozen):
    """Post-hoc scoring of the decision against benchmarks."""

    benchmark_results: tuple[BenchmarkResult, ...] = ()
    regret_vs_best_benchmark_gbp: float | None = None
    decision_quality_note: str | None = None
    evaluated_at: datetime | None = None


class DecisionTransition(_Frozen):
    """An append-only audit record of a single lifecycle transition.

    ``sequence`` is a 1-based, strictly increasing index; the creation record is
    ``sequence = 1`` with ``from_status = None``.
    """

    sequence: int
    from_status: DecisionStatus | None
    to_status: DecisionStatus
    occurred_at: datetime
    actor: DecisionActor
    reason: str


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


class TradeDecision(_Frozen):
    """A single-period decision composed of frozen stage records.

    Created in :data:`DecisionStatus.PROPOSED` carrying only ``context`` and
    ``recommendation``; the trader / execution / settlement / evaluation records
    are attached as the decision advances through the state machine. Every state
    change appends an immutable record to :attr:`transitions`.
    """

    decision_id: str
    created_at: datetime
    status: DecisionStatus = DecisionStatus.PROPOSED

    context: DecisionContext
    recommendation: ModelRecommendation = Field(default_factory=ModelRecommendation)
    trader_instruction: TraderInstruction | None = None
    execution_result: ExecutionResult | None = None
    settlement_result: SettlementResult | None = None
    evaluation_result: EvaluationResult | None = None

    transitions: tuple[DecisionTransition, ...] = ()
    batch_id: str | None = None

    diagnostic_only: bool = True
    not_executable: bool = True

    # -- convenience passthroughs -------------------------------------------

    @property
    def settlement_period(self) -> int:
        return self.context.settlement_period

    @property
    def delivery_period(self) -> str:
        return self.context.delivery_period

    @property
    def as_of(self) -> datetime:
        return self.context.as_of

    @property
    def is_terminal(self) -> bool:
        return self.status is DecisionStatus.EVALUATED

    @property
    def net_recommended_mwh(self) -> float:
        return self.recommendation.net_mwh

    @property
    def net_executed_mwh(self) -> float:
        return self.execution_result.net_executed_mwh if self.execution_result else 0.0

    # -- computed sub-status labels (never persisted) -----------------------

    @property
    def trader_status(self) -> TraderStatus:
        if self.trader_instruction is None:
            return TraderStatus.PENDING
        return _TRADER_STATUS_BY_ACTION[self.trader_instruction.action]

    @property
    def execution_status(self) -> ExecutionStatus:
        if self.execution_result is not None:
            return self.execution_result.status
        if self.status is DecisionStatus.SUBMITTED:
            return ExecutionStatus.SUBMITTED
        return ExecutionStatus.NOT_SUBMITTED

    @property
    def evaluation_status(self) -> EvaluationStatus:
        return _EVALUATION_STATUS_BY_STATUS.get(self.status, EvaluationStatus.PENDING)


class DecisionBatch(_Frozen):
    """A lightweight, immutable grouping of decision IDs from one trigger.

    Holds no exposure, execution or P&L — it is purely an index over the
    per-period :class:`TradeDecision` objects created for a single trigger that
    affected several settlement periods.
    """

    batch_id: str
    created_at: datetime
    trigger_type: TriggerType
    trigger_description: str
    run_mode: RunMode = RunMode.SAMPLE_DEMO
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    decision_ids: tuple[str, ...] = ()
    affected_delivery_periods: tuple[str, ...] = ()
