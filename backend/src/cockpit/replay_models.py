"""Frozen contracts, enums and result records for point-in-time replay (Milestone 8).

A replay evaluates the **existing** decision workflow (forecast revision →
TradeDecision → timing → trader policy → simulated execution → delivery →
settlement → evaluation) across many episodes without look-ahead. It reuses the
production economics; it is not a separate backtester.

Every record is SAMPLE/diagnostic unless a genuine historical dataset is supplied.
``SAMPLE_REPLAY`` is never labelled historical.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cockpit.execution_models import ExecutionMode
from cockpit.models import Quality, SourceMode

REPLAY_ENGINE_VERSION = "replay-v1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ReplayMode(StrEnum):
    """SAMPLE_REPLAY uses deterministic SAMPLE data; HISTORICAL_REPLAY may only be
    used when inputs genuinely come from a historical dataset."""

    SAMPLE_REPLAY = "SAMPLE_REPLAY"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"


class TraderPolicy(StrEnum):
    MODEL_FOLLOW = "MODEL_FOLLOW"
    NO_ACTION = "NO_ACTION"
    TIMING_POLICY = "TIMING_POLICY"


class ReplayStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class IntegrityStatus(StrEnum):
    OK = "OK"
    VIOLATIONS_DETECTED = "VIOLATIONS_DETECTED"
    DATASET_INVALID = "DATASET_INVALID"


class LookAheadKind(StrEnum):
    FUTURE_PUBLICATION = "FUTURE_PUBLICATION"
    REALISED_BEFORE_DELIVERY = "REALISED_BEFORE_DELIVERY"
    SETTLEMENT_BEFORE_AVAILABLE = "SETTLEMENT_BEFORE_AVAILABLE"
    FUTURE_MARKET_SNAPSHOT = "FUTURE_MARKET_SNAPSHOT"
    OUTSIDE_REPLAY_CLOCK = "OUTSIDE_REPLAY_CLOCK"
    HINDSIGHT_IN_DECISION = "HINDSIGHT_IN_DECISION"


class EpisodeSkipReason(StrEnum):
    NO_MATERIAL_REVISION = "NO_MATERIAL_REVISION"
    GATE_CLOSURE_PASSED = "GATE_CLOSURE_PASSED"
    NOT_TRADEABLE = "NOT_TRADEABLE"
    NO_OPTIMISER_VIEW = "NO_OPTIMISER_VIEW"
    MISSING_REALISED_GENERATION = "MISSING_REALISED_GENERATION"
    MISSING_SETTLEMENT_PRICES = "MISSING_SETTLEMENT_PRICES"
    POLICY_NO_TRADE = "POLICY_NO_TRADE"
    DECISION_NOT_CREATED = "DECISION_NOT_CREATED"


class LifecyclePath(StrEnum):
    """The terminal execution path an episode took before delivery/settlement."""

    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"  # policy declined / left unexecuted


# ---------------------------------------------------------------------------
# Dataset records (immutable; every read carries point-in-time metadata)
# ---------------------------------------------------------------------------


class DatasetOrderBookLevel(_Frozen):
    side: str  # BID or ASK
    level: int
    price_gbp_per_mwh: float
    volume_mwh: float


class DatasetVintagePoint(_Frozen):
    """One quantile forecast for one delivery period from one vintage."""

    vintage_id: str
    published_at: datetime
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    p10_mwh: float
    p50_mwh: float
    p90_mwh: float
    unit: str = "MWh"
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    lineage_value_ids: tuple[str, ...] = ()


class DatasetPeriodRecord(_Frozen):
    """All non-forecast inputs for one delivery period, with availability metadata.

    ``source_available_at`` marks when a realised/settlement field becomes legally
    readable (never before ``delivery_end``). The look-ahead guard enforces it.
    """

    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    gate_closure_at: datetime
    # Market snapshot (available at market_available_at)
    market_snapshot_id: str
    market_available_at: datetime
    reference_price_gbp_per_mwh: float
    bids: tuple[DatasetOrderBookLevel, ...] = ()
    asks: tuple[DatasetOrderBookLevel, ...] = ()
    contracted_q_mwh: float = 0.0
    # Model/optimiser recommendation for this period (dataset-provided input)
    recommended_buy_mwh: float = 0.0
    recommended_sell_mwh: float = 0.0
    tradeable: bool = True
    # Realised / settlement (available only at/after source_available_at == delivery_end)
    realised_generation_mwh: float | None = None
    realised_reference_price_gbp_per_mwh: float | None = None
    imbalance_buy_price_gbp_per_mwh: float | None = None
    imbalance_sell_price_gbp_per_mwh: float | None = None
    source_available_at: datetime | None = None
    source_mode: SourceMode = SourceMode.SAMPLE
    quality: Quality = Quality.FRESH
    lineage_value_ids: tuple[str, ...] = ()


class DatasetValidation(_Frozen):
    status: IntegrityStatus
    dataset_id: str
    period_count: int
    vintage_count: int
    issues: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


class LookAheadViolationRecord(_Frozen):
    kind: LookAheadKind
    replay_clock: datetime
    delivery_period: str | None
    requested_field: str
    detail: str


class IntegrityReport(_Frozen):
    status: IntegrityStatus
    lookahead_violation_count: int
    violations: tuple[LookAheadViolationRecord, ...] = ()
    skipped_data_reasons: tuple[str, ...] = ()
    missing_lineage_periods: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()
    dataset_validation_status: IntegrityStatus = IntegrityStatus.OK


# ---------------------------------------------------------------------------
# Episode + run
# ---------------------------------------------------------------------------


class ReplayEpisodeResult(_Frozen):
    """One settlement-period episode. Immutable; collections are tuples.

    P&L fields are **incremental versus NO_ACTION** unless named ``cashflow``.
    """

    episode_id: str
    replay_run_id: str
    settlement_period: int
    delivery_period: str
    decision_ids: tuple[str, ...] = ()
    revision_ids: tuple[str, ...] = ()
    timing_assessment_ids: tuple[str, ...] = ()
    simulated_order_ids: tuple[str, ...] = ()
    delivery_id: str | None = None
    settlement_id: str | None = None
    evaluation_id: str | None = None
    lifecycle_path: LifecyclePath | None = None
    skip_reason: EpisodeSkipReason | None = None

    # Outcomes (incremental vs no-action, except total_realised_cashflow_gbp)
    realised_incremental_pnl_gbp: float | None = None
    total_realised_cashflow_gbp: float | None = None
    no_action_pnl_gbp: float | None = None       # 0.0 by definition when evaluated
    model_pnl_gbp: float | None = None
    trader_pnl_gbp: float | None = None
    perfect_foresight_pnl_gbp: float | None = None
    regret_vs_model_gbp: float | None = None
    regret_vs_perfect_foresight_gbp: float | None = None

    # Execution detail (for execution metrics)
    executed_buy_mwh: float = 0.0
    executed_sell_mwh: float = 0.0
    unfilled_mwh: float = 0.0
    average_execution_price_gbp_per_mwh: float | None = None
    fees_gbp: float = 0.0
    slippage_gbp: float = 0.0
    levels_consumed: int = 0

    # Segmentation keys
    timing_verdict: str | None = None
    timing_priority: str | None = None
    recommended_action: str | None = None
    forecast_horizon_minutes: float | None = None
    trader_policy_action: str | None = None

    warnings: tuple[str, ...] = ()
    lineage_ids: tuple[str, ...] = ()
    diagnostic_only: bool = True
    not_executable: bool = True


class ReplayRun(_Frozen):
    replay_run_id: str
    created_at: datetime
    dataset_id: str
    run_mode: ReplayMode
    source_mode: SourceMode
    quality: Quality
    replay_start: datetime
    replay_end: datetime
    trader_policy: TraderPolicy
    execution_mode: ExecutionMode
    timing_policy_version: str
    simulator_version: str
    materiality_config_ref: str
    assumptions: tuple[str, ...] = ()
    decision_count: int = 0
    evaluated_count: int = 0
    submitted_count: int = 0
    skipped_count: int = 0
    lookahead_violation_count: int = 0
    max_periods: int | None = None
    status: ReplayStatus = ReplayStatus.COMPLETED
    integrity_status: IntegrityStatus = IntegrityStatus.OK
    diagnostic_only: bool = True
    not_executable: bool = True
    trustworthy_for_live_trading: bool = False
    replay_engine_version: str = REPLAY_ENGINE_VERSION


# ---------------------------------------------------------------------------
# Metrics (pure; every metric documents its numerator/denominator in code)
# ---------------------------------------------------------------------------


class CoverageMetrics(_Frozen):
    total_eligible_periods: int
    periods_with_valid_revisions: int
    material_decision_count: int
    submitted_decision_count: int
    filled_count: int
    partial_filled_count: int
    expired_count: int
    evaluated_count: int
    skipped_count: int
    action_rate: float | None  # submitted / material (None if denominator 0)


class PnlMetrics(_Frozen):
    sample_size: int  # evaluated episodes (the denominator for the means)
    total_incremental_pnl_gbp: float
    mean_incremental_pnl_gbp: float | None
    median_incremental_pnl_gbp: float | None
    stdev_incremental_pnl_gbp: float | None
    min_incremental_pnl_gbp: float | None
    max_incremental_pnl_gbp: float | None
    total_realised_cashflow_gbp: float
    total_fees_gbp: float
    total_slippage_gbp: float


class HitRegretMetrics(_Frozen):
    sample_size: int
    pct_outperforming_no_action: float | None
    pct_underperforming_no_action: float | None
    pct_in_line: float | None
    mean_regret_vs_model_gbp: float | None
    mean_regret_vs_perfect_foresight_gbp: float | None
    perfect_foresight_capture_ratio: float | None  # sum(trader incr) / sum(perfect incr)
    capture_ratio_note: str


class RiskMetrics(_Frozen):
    sample_size: int
    max_drawdown_gbp: float | None
    worst_single_period_loss_gbp: float | None
    downside_deviation_gbp: float | None
    fifth_percentile_gbp: float | None
    loss_frequency: float | None


class ExecutionMetrics(_Frozen):
    submitted_count: int
    fill_rate: float | None
    partial_fill_rate: float | None
    average_slippage_gbp: float | None
    average_fee_gbp: float | None
    average_levels_consumed: float | None
    volume_weighted_execution_price_gbp_per_mwh: float | None


class TimingMetrics(_Frozen):
    hedge_now_count: int
    partial_hedge_now_count: int
    wait_count: int
    no_action_count: int
    mean_incremental_pnl_by_verdict: tuple["SegmentMetric", ...] = ()
    mean_incremental_pnl_by_priority: tuple["SegmentMetric", ...] = ()


class SegmentMetric(_Frozen):
    dimension: str
    segment: str
    episode_count: int
    evaluated_count: int
    total_incremental_pnl_gbp: float
    mean_incremental_pnl_gbp: float | None


class CumulativePnlPoint(_Frozen):
    index: int
    settlement_period: int
    episode_id: str
    incremental_pnl_gbp: float
    cumulative_trader_gbp: float
    cumulative_model_gbp: float
    cumulative_no_action_gbp: float          # always 0 (the baseline)
    cumulative_perfect_foresight_gbp: float  # hindsight upper bound (shown separately)


class ReplayMetrics(_Frozen):
    replay_run_id: str
    run_mode: ReplayMode
    sample_size: int
    sample_size_note: str
    coverage: CoverageMetrics
    pnl: PnlMetrics
    hit_regret: HitRegretMetrics
    risk: RiskMetrics
    execution: ExecutionMetrics
    timing: TimingMetrics
    segments: tuple[SegmentMetric, ...] = ()
    diagnostic_only: bool = True
    not_executable: bool = True


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------


class ReplayCreateRequest(BaseModel):
    """Body for POST /replay-runs (API-facing; mutable by design)."""

    dataset_id: str | None = None
    run_mode: ReplayMode = ReplayMode.SAMPLE_REPLAY
    replay_start: datetime | None = None
    replay_end: datetime | None = None
    trader_policy: TraderPolicy = TraderPolicy.TIMING_POLICY
    execution_mode: ExecutionMode = ExecutionMode.REALISTIC
    timing_policy_version: str | None = None
    simulator_version: str | None = None
    materiality_config_ref: str | None = None
    max_periods: int | None = None
    idempotency_key: str | None = None


class ReplayCreateResponse(_Frozen):
    run: ReplayRun
    integrity: IntegrityReport
    diagnostic_only: bool = True
    not_executable: bool = True


# ---------------------------------------------------------------------------
# ID factories
# ---------------------------------------------------------------------------


def new_replay_run_id() -> str:
    return f"replay-{uuid4().hex[:10]}"


def new_episode_id(replay_run_id: str, settlement_period: int) -> str:
    return f"ep-{replay_run_id}-sp{settlement_period}-{uuid4().hex[:6]}"
