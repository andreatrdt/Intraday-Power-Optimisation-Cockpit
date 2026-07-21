"""Typed contracts for observable feed ingestion and cockpit snapshots."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceMode(StrEnum):
    LIVE = "LIVE"
    LATEST_AVAILABLE = "LATEST_AVAILABLE"
    SAMPLE = "SAMPLE"
    SYNTHETIC = "SYNTHETIC"
    ERROR = "ERROR"


class SemanticKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    FORECAST = "FORECAST"
    ESTIMATE = "ESTIMATE"
    ASSUMPTION = "ASSUMPTION"


class Quality(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    REVISED = "REVISED"
    INVALID = "INVALID"


class SnapshotStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class OptimiserStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class DataLineage(BaseModel):
    source_feed: str
    source_mode: SourceMode
    semantic_kind: SemanticKind
    quality: Quality
    published_at: datetime | None = None
    retrieved_at: datetime
    normalised_at: datetime
    raw_field_name: str
    transformations: list[str] = Field(default_factory=list)
    validation_checks: list[ValidationCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CanonicalDataPoint(BaseModel):
    value_id: str
    metric: str
    value: float | int | str | bool
    unit: str
    delivery_period: str | None = None
    delivery_start: datetime | None = None
    lineage: DataLineage
    previous_value: float | int | str | bool | None = None
    delta_vs_previous: float | None = None
    included_in_current_snapshot: bool = False
    snapshot_id: str | None = None


class IngestionAttempt(BaseModel):
    attempt_id: str
    feed_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: AttemptStatus
    rows_retrieved: int = 0
    rows_normalised: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    error_message: str | None = None
    retry_count: int = 0


class FeedHealth(BaseModel):
    feed_id: str
    feed_name: str
    description: str
    source_mode: SourceMode
    semantic_kind: SemanticKind
    quality: Quality
    configured: bool
    connected: bool
    expected_refresh_cadence_seconds: int
    freshness_sla_seconds: int
    last_refresh_attempt: datetime | None = None
    last_successful_refresh: datetime | None = None
    age_seconds: float | None = None
    rows_retrieved: int = 0
    rows_normalised: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    latest_error_message: str | None = None
    retry_status: str = "IDLE"
    included_in_current_snapshot: bool = False
    required_for_snapshot: bool = False
    required_for_optimiser: bool = False
    pipeline_stage: str = "SOURCE"


class SnapshotReadiness(BaseModel):
    status: SnapshotStatus
    reasons: list[str] = Field(default_factory=list)


class OptimiserReadiness(BaseModel):
    status: OptimiserStatus
    allowed: bool
    reasons: list[str] = Field(default_factory=list)


class CockpitSnapshot(BaseModel):
    snapshot_id: str
    as_of: datetime
    input_hash: str
    status: SnapshotStatus
    readiness: SnapshotReadiness
    optimiser_readiness: OptimiserReadiness
    feeds_included: list[str]
    feeds_excluded: list[str]
    stale_feeds: list[str]
    missing_feeds: list[str]
    values: list[CanonicalDataPoint]


class DataFlowEvent(BaseModel):
    event_id: str
    occurred_at: datetime
    feed_id: str | None = None
    stage: str
    level: str
    message: str
    attempt_id: str | None = None
    snapshot_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RefreshRequest(BaseModel):
    include_in_snapshot: bool | None = None


class ForecastVintage(BaseModel):
    vintage_id: str
    issued_at: datetime
    source_feed: str
    source_mode: SourceMode
    semantic_kind: SemanticKind = SemanticKind.FORECAST
    quality: Quality
    model_name: str


class ForecastReliability(BaseModel):
    score: float | None = Field(default=None, ge=0, le=1)
    label: str
    flags: list[str] = Field(default_factory=list)
    model_disagreement_mwh: float | None = None
    score_value: CanonicalDataPoint | None = None
    disagreement_value: CanonicalDataPoint | None = None


class ForecastDelta(BaseModel):
    versus_previous_mwh: float | None = None
    versus_day_ahead_mwh: float | None = None
    versus_previous_value: CanonicalDataPoint | None = None
    versus_day_ahead_value: CanonicalDataPoint | None = None


class ForecastPoint(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    duration_hours: float
    p10: CanonicalDataPoint
    p50: CanonicalDataPoint
    p90: CanonicalDataPoint
    previous_p50: CanonicalDataPoint | None = None
    day_ahead_p50: CanonicalDataPoint | None = None
    delta: ForecastDelta
    reliability: ForecastReliability
    warnings: list[str] = Field(default_factory=list)


class PositionVersion(BaseModel):
    version_id: str
    as_of: datetime
    source_feed: str
    source_mode: SourceMode
    semantic_kind: SemanticKind
    quality: Quality


class PositionPoint(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    contracted_position: CanonicalDataPoint
    warnings: list[str] = Field(default_factory=list)


class ScenarioExposure(BaseModel):
    scenario: str
    generation_mwh: float
    contracted_position_mwh: float
    residual_position_mwh: float
    direction: str
    generation_value: CanonicalDataPoint
    exposure_value: CanonicalDataPoint


class PositionReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    reasons: list[str] = Field(default_factory=list)


class ForecastPositionPeriod(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    forecast: ForecastPoint
    position: PositionPoint
    exposures: list[ScenarioExposure]
    base_case_direction: str
    downside_exposure_mwh: float
    upside_exposure_mwh: float
    risk_magnitude_mwh: float
    risk_rank: int = 0
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class ForecastPositionSnapshot(BaseModel):
    forecast_position_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    input_hash: str
    readiness: PositionReadiness
    latest_vintage: ForecastVintage | None = None
    previous_vintage: ForecastVintage | None = None
    position_version: PositionVersion | None = None
    periods: list[ForecastPositionPeriod] = Field(default_factory=list)
    most_exposed_periods: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OrderBookLevel(BaseModel):
    side: str
    level: int
    price_gbp_per_mwh: float
    volume_mwh: float
    price_value: CanonicalDataPoint
    volume_value: CanonicalDataPoint


class ExecutablePrice(BaseModel):
    side: str
    required_volume_mwh: float
    executable_volume_mwh: float
    unfilled_volume_mwh: float
    wap_gbp_per_mwh: float | None = None
    levels_considered: int
    levels_used: int
    wap_value: CanonicalDataPoint | None = None
    executable_volume_value: CanonicalDataPoint | None = None
    unfilled_volume_value: CanonicalDataPoint | None = None


class LiquidityAssessment(BaseModel):
    spread_gbp_per_mwh: float
    bid_depth_mwh: float
    ask_depth_mwh: float
    liquidity_score: float = Field(ge=0, le=1)
    warning: str | None = None
    spread_value: CanonicalDataPoint
    bid_depth_value: CanonicalDataPoint
    ask_depth_value: CanonicalDataPoint
    liquidity_score_value: CanonicalDataPoint


class GateClosureStatus(BaseModel):
    delivery_start: datetime
    delivery_end: datetime
    gate_closure_at: datetime
    minutes_to_gate_closure: float
    status: str
    warning: str | None = None


class MarketReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    reasons: list[str] = Field(default_factory=list)


class HedgeCostDiagnostic(BaseModel):
    scenario: str
    exposure_mwh: float
    exposure_value: CanonicalDataPoint
    hedge_side: str
    required_volume_mwh: float
    execution: ExecutablePrice
    estimated_cashflow_gbp: float
    cashflow_value: CanonicalDataPoint | None = None
    liquidity_warning: str | None = None
    explanation: str


class MarketPeriodSnapshot(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    best_bid: CanonicalDataPoint
    best_ask: CanonicalDataPoint
    liquidity: LiquidityAssessment
    gate_closure: GateClosureStatus
    p10_exposure_mwh: float
    p50_exposure_mwh: float
    p90_exposure_mwh: float
    p50_hedge: HedgeCostDiagnostic
    downside_hedge: HedgeCostDiagnostic
    warnings: list[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    market_snapshot_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    input_hash: str
    active_provider: str
    live_provider_status: SourceMode
    source_mode: SourceMode
    quality: Quality
    readiness: MarketReadiness
    levels_considered: int
    periods: list[MarketPeriodSnapshot] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BatteryAssetLimits(BaseModel):
    e_min: CanonicalDataPoint
    e_max: CanonicalDataPoint
    charge_power_max: CanonicalDataPoint
    discharge_power_max: CanonicalDataPoint
    charge_efficiency: CanonicalDataPoint
    discharge_efficiency: CanonicalDataPoint
    reserve_duration: CanonicalDataPoint


class BatteryOpportunityCost(BaseModel):
    discharge_cost_gbp_per_mwh: float
    charge_cost_gbp_per_mwh: float
    discharge_cost_value: CanonicalDataPoint
    charge_cost_value: CanonicalDataPoint
    degradation_cost: CanonicalDataPoint
    terminal_soc_penalty: CanonicalDataPoint
    future_flexibility_penalty: CanonicalDataPoint
    terminal_soc_target: CanonicalDataPoint
    assumptions: list[str] = Field(default_factory=list)


class BatteryExposureCoverage(BaseModel):
    scenario: str
    exposure_mwh: float
    support_direction: str
    maximum_support_mwh: float
    covered_mwh: float
    residual_after_support_mwh: float
    coverage_percent: float
    exposure_value: CanonicalDataPoint
    covered_value: CanonicalDataPoint
    residual_value: CanonicalDataPoint


class BatteryFeasibilityPoint(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    duration_hours: float
    current_soc: CanonicalDataPoint
    upward_reserved: CanonicalDataPoint
    downward_reserved: CanonicalDataPoint
    max_charge_mwh: float
    max_discharge_mwh: float
    upward_power_headroom_mw: float
    downward_power_headroom_mw: float
    upward_energy_duration_hours: float
    downward_space_duration_hours: float
    projected_soc_after_max_charge_mwh: float
    projected_soc_after_max_discharge_mwh: float
    max_charge_value: CanonicalDataPoint
    max_discharge_value: CanonicalDataPoint
    upward_power_headroom_value: CanonicalDataPoint
    downward_power_headroom_value: CanonicalDataPoint
    upward_energy_duration_value: CanonicalDataPoint
    downward_space_duration_value: CanonicalDataPoint
    projected_soc_after_max_charge_value: CanonicalDataPoint
    projected_soc_after_max_discharge_value: CanonicalDataPoint
    binding_constraints: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BatteryPeriodSnapshot(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    feasibility: BatteryFeasibilityPoint
    coverage: list[BatteryExposureCoverage]
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class BatteryReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    reasons: list[str] = Field(default_factory=list)


class BatteryFlexibilitySnapshot(BaseModel):
    battery_snapshot_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    input_hash: str
    source_mode: SourceMode
    quality: Quality
    readiness: BatteryReadiness
    current_soc: CanonicalDataPoint | None = None
    limits: BatteryAssetLimits | None = None
    opportunity_cost: BatteryOpportunityCost | None = None
    periods: list[BatteryPeriodSnapshot] = Field(default_factory=list)
    most_useful_periods: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BatteryPathPeriodAction(BaseModel):
    delivery_period: str
    charge_mw: float = 0.0
    discharge_mw: float = 0.0


class BatteryPathInput(BaseModel):
    path_name: str = "CUSTOM"
    actions: list[BatteryPathPeriodAction] = Field(default_factory=list)


class BatteryPathViolation(BaseModel):
    code: str
    message: str
    severity: str = "ERROR"
    delivery_period: str | None = None
    observed_value: CanonicalDataPoint | None = None
    limit_value: CanonicalDataPoint | None = None


class BatteryPathPeriodResult(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    duration_hours: float
    starting_soc_mwh: float
    charge_mw: float
    charge_mwh: float
    discharge_mw: float
    discharge_mwh: float
    net_export_mw: float
    ending_soc_mwh: float
    upward_power_headroom_mw: float
    downward_power_headroom_mw: float
    upward_energy_duration_hours: float
    downward_energy_duration_hours: float
    max_feasible_charge_mwh: float
    max_feasible_discharge_mwh: float
    starting_soc_value: CanonicalDataPoint
    charge_power_value: CanonicalDataPoint
    charge_energy_value: CanonicalDataPoint
    discharge_power_value: CanonicalDataPoint
    discharge_energy_value: CanonicalDataPoint
    net_export_value: CanonicalDataPoint
    ending_soc_value: CanonicalDataPoint
    upward_power_headroom_value: CanonicalDataPoint
    downward_power_headroom_value: CanonicalDataPoint
    upward_energy_duration_value: CanonicalDataPoint
    downward_energy_duration_value: CanonicalDataPoint
    max_feasible_charge_value: CanonicalDataPoint
    max_feasible_discharge_value: CanonicalDataPoint
    exposure_before: list[ScenarioExposure]
    residual_exposure: list[ScenarioExposure]
    binding_constraints: list[str] = Field(default_factory=list)
    violations: list[BatteryPathViolation] = Field(default_factory=list)


class BatteryPathReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    reasons: list[str] = Field(default_factory=list)


class BatteryPathSimulation(BaseModel):
    simulation_id: str
    cockpit_snapshot_id: str
    path_name: str
    path_label: str
    path_kind: str
    diagnostic_only: bool = True
    as_of: datetime
    source_mode: SourceMode
    quality: Quality
    readiness: BatteryPathReadiness
    valid: bool
    periods: list[BatteryPathPeriodResult] = Field(default_factory=list)
    e_min_mwh: float | None = None
    e_max_mwh: float | None = None
    e_min_value: CanonicalDataPoint | None = None
    e_max_value: CanonicalDataPoint | None = None
    terminal_soc_mwh: float | None = None
    terminal_soc_value: CanonicalDataPoint | None = None
    terminal_target_mwh: float | None = None
    terminal_target_value: CanonicalDataPoint | None = None
    terminal_shortfall_mwh: float | None = None
    terminal_shortfall_value: CanonicalDataPoint | None = None
    total_absolute_p50_residual_mwh: float | None = None
    total_absolute_p50_residual_value: CanonicalDataPoint | None = None
    first_binding_constraint: str | None = None
    violations: list[BatteryPathViolation] = Field(default_factory=list)
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class BatteryPathComparison(BaseModel):
    comparison_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    readiness: BatteryPathReadiness
    no_action: BatteryPathSimulation
    p50_coverage: BatteryPathSimulation
    preserve_flexibility: BatteryPathSimulation
    p50_terminal_soc_delta_mwh: float
    preserve_terminal_soc_delta_mwh: float
    p50_residual_reduction_mwh: float
    preserve_residual_reduction_mwh: float
    explanation: str
