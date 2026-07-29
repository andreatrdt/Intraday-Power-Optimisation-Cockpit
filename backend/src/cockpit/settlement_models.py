"""Frozen typed contracts for the delivery → settlement → evaluation stage (Milestone 7).

This is the **diagnostic SAMPLE** realised-outcome layer. Nothing here settles a
real imbalance, values real generation, or reaches a balancing mechanism. Every
record is ``diagnostic_only`` / ``not_executable``.

Sign conventions (documented once, used everywhere) — see also
:mod:`cockpit.pnl_attribution`:

* **Portfolio position** ``Q_t`` is the *contracted* position for the period.
  Position reconstruction: ``final_Q = initial_Q + executed_buy - executed_sell``.
* **Realised imbalance** ``I_t = G_t - Q_t`` where ``G_t`` is realised generation
  and ``Q_t`` is the *final* contracted position after simulated execution.

  * ``I_t > 0`` → **LONG** generation (surplus vs. contracted);
  * ``I_t < 0`` → **SHORT** generation (deficit vs. contracted);
  * ``I_t == 0`` (within tolerance) → **FLAT**.
* **Cash convention:** cash *received* is **positive**, cash *paid* is **negative**.

  * Execution BUY:  ``execution_cashflow = - executed_buy  × average_buy_price``  (paid).
  * Execution SELL: ``execution_cashflow = + executed_sell × average_sell_price`` (received).
  * Fees are always a cost, subtracted separately (negative contribution).
  * Imbalance LONG (``I_t ≥ 0``):  ``imbalance_cashflow = I_t × imbalance_sell_price``.
  * Imbalance SHORT (``I_t < 0``): ``imbalance_cashflow = I_t × imbalance_buy_price``
    (negative ``I_t`` × positive buy price ⇒ negative cash flow ⇒ a payment).

``total_realised_cashflow_gbp`` is the raw realised trading + imbalance cash flow.
``realised_pnl_gbp`` is the **incremental P&L versus the NO_ACTION baseline**
(decision cash flow − no-action cash flow); it is the economically interpretable
metric and is never a raw cash flow relabelled as P&L.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cockpit.decision_models import DecisionStatus
from cockpit.models import Quality, SourceMode

SETTLEMENT_VERSION = "settlement-eval-v1"

# Tolerances (documented, shared).
IMBALANCE_FLAT_TOLERANCE_MWH = 1e-6
RECONCILIATION_TOLERANCE_GBP = 1e-6
QUALITY_FLAT_TOLERANCE_GBP = 1e-6


class _Frozen(BaseModel):
    """Immutable base for every settlement/evaluation record."""

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ImbalanceDirection(StrEnum):
    """Sign of the realised imbalance ``I_t = G_t - Q_t``."""

    LONG = "LONG"    # I_t > 0 : surplus generation vs. contracted position
    SHORT = "SHORT"  # I_t < 0 : deficit generation vs. contracted position
    FLAT = "FLAT"    # |I_t| <= tolerance


class BenchmarkName(StrEnum):
    NO_ACTION = "NO_ACTION"
    MODEL_RECOMMENDATION = "MODEL_RECOMMENDATION"
    TRADER_INSTRUCTION = "TRADER_INSTRUCTION"
    PERFECT_FORESIGHT = "PERFECT_FORESIGHT"


class DecisionQualityLabel(StrEnum):
    """Cautious, single-observation quality labels — never a statistical verdict."""

    OUTPERFORMED_NO_ACTION = "OUTPERFORMED_NO_ACTION"
    UNDERPERFORMED_NO_ACTION = "UNDERPERFORMED_NO_ACTION"
    IN_LINE_WITH_NO_ACTION = "IN_LINE_WITH_NO_ACTION"
    UNAVAILABLE = "UNAVAILABLE"


class ProcessSkipReason(StrEnum):
    """Structured reasons a decision was skipped by the SAMPLE process-completed run."""

    DELIVERY_PERIOD_NOT_ENDED = "DELIVERY_PERIOD_NOT_ENDED"
    NOT_EXECUTION_COMPLETE = "NOT_EXECUTION_COMPLETE"
    MISSING_REALISED_GENERATION = "MISSING_REALISED_GENERATION"
    MISSING_SETTLEMENT_PRICES = "MISSING_SETTLEMENT_PRICES"
    MISSING_CONTRACTED_POSITION = "MISSING_CONTRACTED_POSITION"
    ALREADY_EVALUATED = "ALREADY_EVALUATED"


# ---------------------------------------------------------------------------
# Realised inputs
# ---------------------------------------------------------------------------


class RealisedInputs(_Frozen):
    """The clearly-identified realised inputs for one settlement period.

    In SAMPLE mode these are sourced from the simulated environment / the
    decision's own stored records (see
    :class:`cockpit.settlement_service.SampleRealisedInputsProvider`). Values are
    never fabricated: if a required input is unavailable the provider returns
    ``None`` and the period is skipped with a structured reason.
    """

    decision_id: str
    settlement_period: int
    delivery_start: datetime
    delivery_end: datetime
    delivered_at: datetime

    realised_generation_mwh: float
    initial_contracted_position_mwh: float
    executed_buy_mwh: float = 0.0
    executed_sell_mwh: float = 0.0
    average_execution_price_gbp_per_mwh: float | None = None
    execution_fees_gbp: float = 0.0

    imbalance_buy_price_gbp_per_mwh: float
    imbalance_sell_price_gbp_per_mwh: float
    reference_market_price_gbp_per_mwh: float | None = None

    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    lineage_ids: tuple[str, ...] = ()
    run_mode: str = "SAMPLE_DEMO"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class DeliveryResult(_Frozen):
    """Immutable physical-delivery outcome for one settlement period.

    A decision transitions to ``DELIVERED`` only once the delivery period has
    completed and the required realised generation is available.
    """

    delivery_id: str
    decision_id: str
    settlement_period: int
    delivery_start: datetime
    delivery_end: datetime
    delivered_at: datetime

    initial_contracted_position_mwh: float
    executed_buy_mwh: float
    executed_sell_mwh: float
    final_contracted_position_mwh: float
    realised_generation_mwh: float
    realised_imbalance_mwh: float
    imbalance_direction: ImbalanceDirection

    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    lineage_ids: tuple[str, ...] = ()
    run_mode: str = "SAMPLE_DEMO"
    diagnostic_only: bool = True
    not_executable: bool = True
    settlement_version: str = SETTLEMENT_VERSION


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


class SettlementCalculation(_Frozen):
    """Immutable realised cash-flow + P&L outcome for one settlement period.

    Formulae (cash received positive; see module docstring):

    * ``execution_cashflow_gbp`` = ``+ sell×sell_price − buy×buy_price`` (fees excluded);
    * ``imbalance_cashflow_gbp`` = ``I_t × (sell_price if I_t≥0 else buy_price)``;
    * ``total_realised_cashflow_gbp`` = ``execution_cashflow + imbalance_cashflow − fees``;
    * ``realised_pnl_gbp`` = incremental P&L vs NO_ACTION = decision cash flow −
      no-action cash flow (NO_ACTION keeps the pre-decision contracted position and
      settles ``G − initial_Q`` at imbalance prices, with no execution or fees).
    """

    settlement_id: str
    decision_id: str
    settled_at: datetime

    realised_generation_mwh: float
    final_contracted_position_mwh: float
    realised_imbalance_mwh: float
    imbalance_direction: ImbalanceDirection
    imbalance_buy_price_gbp_per_mwh: float
    imbalance_sell_price_gbp_per_mwh: float

    execution_cashflow_gbp: float
    execution_fees_gbp: float
    imbalance_cashflow_gbp: float
    total_realised_cashflow_gbp: float
    realised_pnl_gbp: float

    calculation_basis: str
    warnings: tuple[str, ...] = ()
    lineage_ids: tuple[str, ...] = ()
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    diagnostic_only: bool = True
    not_executable: bool = True
    settlement_version: str = SETTLEMENT_VERSION


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class RealisedPnlAttribution(_Frozen):
    """Decomposition of the incremental (vs NO_ACTION) realised P&L.

    The four effects reconcile to ``total_incremental_pnl_gbp`` within
    :data:`RECONCILIATION_TOLERANCE_GBP`:

        execution_price_effect + execution_fees_effect
        + imbalance_reduction_effect + imbalance_residual_effect
        = total_incremental_pnl   (+ reconciliation_error)

    * ``execution_price_effect_gbp`` — the signed execution cash flow (what
      executing the order paid/received, fees excluded);
    * ``execution_fees_effect_gbp`` — ``-fees`` (a pure cost);
    * ``imbalance_reduction_effect_gbp`` — the change in imbalance settlement value
      attributable to the executed *volume* shifting the imbalance
      (``- net_executed × applicable_decision_imbalance_price``);
    * ``imbalance_residual_effect_gbp`` — the change in imbalance value from the
      remaining position crossing a price regime (LONG↔SHORT). It is exactly zero
      unless the imbalance sign differs between the no-action and decision cases;
    * ``reconciliation_error_gbp`` — ``total_incremental_pnl − sum(effects)``
      (floating-point residual only; ~0 by construction).
    """

    execution_price_effect_gbp: float
    execution_fees_effect_gbp: float
    imbalance_reduction_effect_gbp: float
    imbalance_residual_effect_gbp: float
    total_incremental_pnl_gbp: float
    reconciliation_error_gbp: float

    @property
    def reconciled(self) -> bool:
        return abs(self.reconciliation_error_gbp) <= RECONCILIATION_TOLERANCE_GBP


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


class BenchmarkResult(_Frozen):
    """One comparator's realised outcome, valued at the same realised generation
    and imbalance prices as the decision. Immutable; collections are tuples."""

    benchmark_name: BenchmarkName
    description: str
    attainable: bool
    hindsight_only: bool
    assumed_execution_mode: str | None

    hedge_buy_mwh: float
    hedge_sell_mwh: float
    execution_price_gbp_per_mwh: float | None
    execution_fees_gbp: float
    final_position_mwh: float
    realised_imbalance_mwh: float
    imbalance_direction: ImbalanceDirection
    total_cashflow_gbp: float
    incremental_pnl_vs_no_action_gbp: float

    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class EvaluationResult(_Frozen):
    """Post-hoc scoring of one decision against benchmarks. Immutable.

    Regret convention (identical for every benchmark):

        regret_vs_benchmark = benchmark_incremental_pnl − realised_incremental_pnl

    Positive regret means the benchmark would have done better than the decision.
    """

    evaluation_id: str
    decision_id: str
    evaluated_at: datetime

    realised_outcome: SettlementCalculation
    pnl_attribution: RealisedPnlAttribution
    benchmark_results: tuple[BenchmarkResult, ...]

    regret_vs_no_action_gbp: float
    regret_vs_model_recommendation_gbp: float | None
    regret_vs_perfect_foresight_gbp: float

    decision_quality_label: DecisionQualityLabel
    decision_quality_note: str
    warnings: tuple[str, ...] = ()
    lineage_ids: tuple[str, ...] = ()
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    diagnostic_only: bool = True
    not_executable: bool = True
    settlement_version: str = SETTLEMENT_VERSION


# ---------------------------------------------------------------------------
# process-completed response
# ---------------------------------------------------------------------------


class ProcessedDecision(_Frozen):
    """One decision advanced by the SAMPLE process-completed run."""

    decision_id: str
    settlement_period: int
    delivery_id: str
    settlement_id: str
    evaluation_id: str
    decision_quality_label: DecisionQualityLabel


class SkippedDecision(_Frozen):
    decision_id: str
    settlement_period: int
    reason: ProcessSkipReason
    detail: str


class ProcessCompletedResult(_Frozen):
    """Structured outcome of ``POST /decisions/process-completed``."""

    as_of: datetime
    processed: tuple[ProcessedDecision, ...] = ()
    existing: tuple[str, ...] = ()
    skipped: tuple[SkippedDecision, ...] = ()
    diagnostic_only: bool = True
    not_executable: bool = True
    warning: str = (
        "SAMPLE diagnostic: uses simulated realised generation and settlement "
        "prices. Does not represent live or historical trading performance."
    )


# ---------------------------------------------------------------------------
# Request models (API-facing; mutable by design)
# ---------------------------------------------------------------------------


class LifecycleActionRequest(BaseModel):
    """Body for the deliver / settle / evaluate diagnostic routes."""

    expected_status: DecisionStatus | None = None
    expected_sequence: int | None = None
    idempotency_key: str | None = None
    actor_id: str | None = None


# ---------------------------------------------------------------------------
# ID factories
# ---------------------------------------------------------------------------


def new_delivery_id(decision_id: str) -> str:
    return f"delivery-{decision_id}-{uuid4().hex[:8]}"


def new_settlement_id(decision_id: str) -> str:
    return f"settle-{decision_id}-{uuid4().hex[:8]}"


def new_evaluation_id(decision_id: str) -> str:
    return f"eval-{decision_id}-{uuid4().hex[:8]}"
