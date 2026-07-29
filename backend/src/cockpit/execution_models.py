"""Frozen models for the simulated execution stage (Milestone 6B).

This is a **diagnostic SAMPLE** execution simulator. Nothing here routes a live
order or reaches a broker/exchange. Keep the semantic boundary explicit:

    ModelRecommendation  →  TraderInstruction  →  SimulatedOrder
                                                      →  ExecutionOutcome (SimulatedFill…)

``SUBMITTED`` means submitted to the internal simulator only; a filled order is a
**simulated** fill. Every record is ``diagnostic_only`` / ``not_executable``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cockpit.decision_models import DecisionActor, DecisionStatus, ExecutionStatus
from cockpit.models import Quality, SourceMode

SIMULATOR_VERSION = "execution-sim-v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ExecutionMode(StrEnum):
    """IDEAL is a benchmark; REALISTIC and STRESS are assumption-driven SAMPLE
    simulations, not calibrated to any real exchange."""

    IDEAL = "IDEAL"
    REALISTIC = "REALISTIC"
    STRESS = "STRESS"


class ExecutionConfig(_Frozen):
    """Transparent, deterministic simulator parameters (no stochastic process)."""

    fee_gbp_per_mwh: float = 0.15
    # REALISTIC
    realistic_depth_haircut: float = 0.10   # fraction of visible depth assumed unavailable
    realistic_latency_ms: int = 250
    # STRESS
    stress_depth_haircut: float = 0.40
    stress_latency_ms: int = 1500
    stress_adverse_price_gbp_per_mwh: float = 8.0  # adverse per-level price shift
    tolerance_mwh: float = 1e-6
    simulator_version: str = SIMULATOR_VERSION


class BookLevel(_Frozen):
    """One visible order-book level on the relevant side (best-first)."""

    price_gbp_per_mwh: float
    volume_mwh: float


class SimulatedOrder(_Frozen):
    order_id: str
    decision_id: str
    submitted_at: datetime
    actor: DecisionActor = DecisionActor.TRADER
    actor_id: str | None = None
    side: str  # BUY or SELL
    requested_volume_mwh: float
    limit_price: float | None = None
    execution_mode: ExecutionMode
    market_snapshot_id: str | None = None
    optimisation_run_id: str | None = None
    settlement_period: int
    delivery_start: datetime
    gate_closure_at: datetime | None = None
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    diagnostic_only: bool = True
    not_executable: bool = True
    simulator_version: str = SIMULATOR_VERSION


class SimulatedFill(_Frozen):
    fill_id: str
    order_id: str
    filled_at: datetime
    side: str
    filled_volume_mwh: float
    fill_price_gbp_per_mwh: float
    fee_gbp: float
    slippage_gbp_per_mwh: float
    order_book_level: int | None = None
    assumption_basis: str = "SAMPLE order book"


class ExecutionOutcome(_Frozen):
    order: SimulatedOrder
    fills: tuple[SimulatedFill, ...] = ()
    total_filled_volume_mwh: float
    unfilled_volume_mwh: float
    average_fill_price_gbp_per_mwh: float | None
    best_price_before_execution_gbp_per_mwh: float | None
    total_fees_gbp: float
    total_execution_cost_gbp: float
    total_slippage_gbp: float
    execution_status: ExecutionStatus
    execution_mode: ExecutionMode
    levels_consumed: int
    warnings: tuple[str, ...] = ()
    assumptions_used: tuple[str, ...] = ()
    diagnostic_only: bool = True
    not_executable: bool = True
    trustworthy_for_live_trading: bool = False
    simulator_version: str = SIMULATOR_VERSION


class SubmitSimulatedRequest(BaseModel):
    """Body for POST /decisions/{id}/submit-simulated."""

    execution_mode: ExecutionMode = ExecutionMode.REALISTIC
    expected_status: DecisionStatus | None = None
    expected_sequence: int | None = None
    idempotency_key: str | None = None
    actor_id: str | None = None


def new_order_id(decision_id: str) -> str:
    return f"simord-{decision_id}-{uuid4().hex[:8]}"


def new_fill_id(order_id: str, level: int) -> str:
    # Deterministic within an order so a re-run with the same inputs reproduces
    # identical fills (see the reproducibility test).
    return f"simfill-{order_id}-L{level}"


# Field factory used by the store's dedupe; kept here so callers share the shape.
IdempotencyKey = str
