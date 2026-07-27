"""Frozen contracts for the hedge-timing / decision-prioritisation layer (M4).

This is an **explainable timing policy** over already-available observable
conditions — not a price forecast, an optimal-stopping model, or an
expected-value calculation. Naming is deliberately precise: revision
significance is never called forecast confidence.

Models only. The deterministic policy lives in :mod:`cockpit.hedge_timing`; the
store / ranking / batch summaries live in :mod:`cockpit.decision_prioritisation`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cockpit.models import Quality, SourceMode


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TimingVerdict(StrEnum):
    HEDGE_NOW = "HEDGE_NOW"
    PARTIAL_HEDGE_NOW = "PARTIAL_HEDGE_NOW"
    WAIT = "WAIT"
    NO_ACTION = "NO_ACTION"


class TimingPriority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"


_PRIORITY_RANK: dict[TimingPriority, int] = {
    TimingPriority.CRITICAL: 0,
    TimingPriority.HIGH: 1,
    TimingPriority.MEDIUM: 2,
    TimingPriority.LOW: 3,
    TimingPriority.INFORMATIONAL: 4,
}


def priority_rank(priority: TimingPriority) -> int:
    return _PRIORITY_RANK[priority]


POLICY_VERSION = "hedge-timing-v1"


# ---------------------------------------------------------------------------
# Configuration (all thresholds documented in docs/hedge-timing.md)
# ---------------------------------------------------------------------------


class TimingConfig(_Frozen):
    # Gate Closure
    gate_closure_near_minutes: float = 45.0
    gate_closure_score_horizon_minutes: float = 240.0
    # Exposure
    exposure_tolerance_mwh: float = 5.0
    exposure_scale_mwh: float = 60.0
    small_recommendation_ratio: float = 0.25
    # Execution quality
    max_spread_gbp_per_mwh: float = 6.0
    max_slippage_gbp_per_mwh: float = 4.0
    depth_sufficiency_ratio: float = 0.9
    partial_min_ratio: float = 0.1
    uncertainty_width_increase_mwh: float = 5.0
    action_tolerance_mwh: float = 1e-6
    # Priority band thresholds (on the 0..1 weighted total)
    critical_threshold: float = 0.72
    high_threshold: float = 0.55
    medium_threshold: float = 0.38
    low_threshold: float = 0.20
    # Component weights
    weight_gate: float = 1.0
    weight_exposure: float = 1.0
    weight_tail: float = 0.5
    weight_significance: float = 0.6
    weight_direction_flip: float = 0.4
    weight_liquidity: float = 0.5
    weight_trust: float = 0.4
    weight_spread_penalty: float = 0.5
    # Presentation
    top_n_cap: int = 8
    policy_version: str = POLICY_VERSION


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class TimingMarketView(_Frozen):
    """Observable executable-market inputs for one settlement period.

    ``available`` is False when no current book was found for the period (e.g. a
    decision whose settlement period is not in the current horizon) — the policy
    then cannot justify acting now and says so explicitly.
    """

    settlement_period: int
    delivery_period: str
    market_snapshot_id: str | None = None
    optimisation_run_id: str | None = None
    available: bool = True
    tradeable: bool = True
    recommended_side: str = "NONE"  # BUY / SELL / NONE
    required_volume_mwh: float = 0.0
    best_bid_gbp_per_mwh: float | None = None
    best_ask_gbp_per_mwh: float | None = None
    spread_gbp_per_mwh: float | None = None
    bid_depth_mwh: float | None = None
    ask_depth_mwh: float | None = None
    executable_volume_mwh: float | None = None
    wap_gbp_per_mwh: float | None = None
    wap_slippage_gbp_per_mwh: float | None = None


class TimingRevisionSignals(_Frozen):
    """The revision-derived signals the policy uses (extracted from a
    ForecastRevision). Significance is statistical unusualness, NOT reliability."""

    revision_magnitude_mwh: float
    revision_significance_score: float | None = None
    revision_z_score: float | None = None
    calibration_basis: str | None = None
    uncertainty_width_change_mwh: float = 0.0
    direction_flip: bool = False
    materiality_reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class PriorityComponents(_Frozen):
    """The visible decomposition of the priority score (each in 0..1)."""

    gate_closure_component: float
    exposure_component: float
    tail_exposure_component: float
    significance_component: float
    direction_flip_component: float
    liquidity_component: float
    spread_slippage_penalty: float
    trust_quality_component: float
    weighted_total: float


class HedgeTimingAssessment(_Frozen):
    assessment_id: str
    decision_id: str
    assessed_at: datetime
    settlement_period: int
    delivery_period: str

    verdict: TimingVerdict
    priority: TimingPriority
    priority_score: float

    recommended_now_buy_mwh: float
    recommended_now_sell_mwh: float
    deferred_buy_mwh: float
    deferred_sell_mwh: float

    urgency_score: float
    liquidity_score: float
    exposure_risk_score: float
    gate_closure_score: float
    confidence_or_significance_component: float
    significance_available: bool

    priority_components: PriorityComponents
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    market: TimingMarketView | None = None
    market_snapshot_id: str | None
    optimisation_run_id: str | None
    policy_version: str
    source_mode: SourceMode
    quality: Quality
    diagnostic_only: bool = True
    not_executable: bool = True


class DecisionBatchSummary(_Frozen):
    """Lightweight presentation summary over a DecisionBatch's assessments.

    Carries counts and top IDs only — no aggregate execution, P&L or portfolio
    exposure (those never belong on DecisionBatch itself)."""

    batch_id: str
    total_decisions: int
    assessed_decisions: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    informational_count: int
    hedge_now_periods: int
    partial_hedge_periods: int
    wait_periods: int
    no_action_periods: int
    top_decision_ids: tuple[str, ...]
    affected_period_range: str | None


class PrioritisedItem(_Frozen):
    """A single ranked row for a capped prioritised view."""

    assessment_id: str
    decision_id: str
    settlement_period: int
    delivery_period: str
    verdict: TimingVerdict
    priority: TimingPriority
    priority_score: float


class AssessTimingRequest(BaseModel):
    """Optional body for POST /decisions/assess-timing. Empty assesses all
    currently stored decisions."""

    decision_ids: list[str] | None = None


class AssessTimingResult(_Frozen):
    assessed_at: datetime
    policy_version: str
    created_assessment_ids: tuple[str, ...]
    existing_assessment_ids: tuple[str, ...]
    prioritised: tuple[PrioritisedItem, ...]
    batch_summaries: tuple[DecisionBatchSummary, ...]
    skipped_decision_ids: tuple[str, ...] = ()
    diagnostic_only: bool = True
    trustworthy_for_live_trading: bool = False
