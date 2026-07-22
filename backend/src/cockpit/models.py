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


class ServiceProduct(BaseModel):
    product_id: str
    name: str
    direction: str
    product_kind: str
    description: str


class ServiceCommitment(BaseModel):
    commitment_id: str
    product: ServiceProduct
    delivery_period: str
    reserved_mw: float
    required_duration_hours: float
    obligation_status: str
    reserved_value: CanonicalDataPoint
    duration_value: CanonicalDataPoint


class OptionalityAssumption(BaseModel):
    key: str
    label: str
    value: float
    unit: str
    description: str
    value_point: CanonicalDataPoint


class BMOptionalityEstimate(BaseModel):
    acceptance_probability: float
    expected_activation_mwh: float
    expected_margin_gbp_per_mwh: float
    gross_expected_value_gbp: float
    non_delivery_risk_penalty_gbp: float
    activation_opportunity_cost_gbp: float
    expected_value_gbp: float
    expected_activation_value: CanonicalDataPoint
    gross_expected_value: CanonicalDataPoint
    non_delivery_risk_penalty_value: CanonicalDataPoint
    activation_opportunity_cost_value: CanonicalDataPoint
    expected_value: CanonicalDataPoint
    optional_not_guaranteed: bool = True


class AncillaryServiceEstimate(BaseModel):
    availability_value_gbp: float
    expected_activation_value_gbp: float
    non_delivery_risk_penalty_gbp: float
    expected_service_value_gbp: float
    availability_value: CanonicalDataPoint
    expected_activation_value: CanonicalDataPoint
    non_delivery_risk_penalty_value: CanonicalDataPoint
    expected_service_value: CanonicalDataPoint


class OptionalityViolation(BaseModel):
    code: str
    message: str
    severity: str = "WARNING"
    delivery_period: str | None = None
    direction: str | None = None
    observed_value: CanonicalDataPoint | None = None
    required_value: CanonicalDataPoint | None = None


class OptionalityPeriodDiagnostic(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    risk_rank: int = 0
    starting_soc_mwh: float
    ending_soc_mwh: float
    starting_soc_value: CanonicalDataPoint
    ending_soc_value: CanonicalDataPoint
    upward_power_available_before_mw: float
    downward_power_available_before_mw: float
    upward_power_available_after_mw: float
    downward_power_available_after_mw: float
    upward_power_available_before_value: CanonicalDataPoint
    downward_power_available_before_value: CanonicalDataPoint
    upward_power_available_after_value: CanonicalDataPoint
    downward_power_available_after_value: CanonicalDataPoint
    upward_duration_available_hours: float
    downward_duration_available_hours: float
    upward_duration_available_value: CanonicalDataPoint
    downward_duration_available_value: CanonicalDataPoint
    committed_upward_mw: float
    committed_downward_mw: float
    optional_upward_before_mw: float
    optional_downward_before_mw: float
    optional_upward_after_mw: float
    optional_downward_after_mw: float
    optional_upward_after_value: CanonicalDataPoint
    optional_downward_after_value: CanonicalDataPoint
    commitment_coverage_ratio: float
    commitment_coverage_value: CanonicalDataPoint
    bm_estimate: BMOptionalityEstimate
    service_estimate: AncillaryServiceEstimate
    optionality_value_before_gbp: float
    optionality_value_after_gbp: float
    optionality_lost_gbp: float
    optionality_value_before_value: CanonicalDataPoint
    optionality_value_after_value: CanonicalDataPoint
    optionality_lost_value: CanonicalDataPoint
    commitment_at_risk: bool
    violations: list[OptionalityViolation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OptionalityPathImpact(BaseModel):
    path_name: str
    path_label: str
    diagnostic_only: bool = True
    optionality_value_before_gbp: float
    optionality_value_after_gbp: float
    optionality_lost_gbp: float
    optionality_value_before_value: CanonicalDataPoint | None = None
    optionality_value_after_value: CanonicalDataPoint | None = None
    optionality_lost_value: CanonicalDataPoint | None = None
    commitments_at_risk: int
    worst_affected_period: str | None = None
    periods: list[OptionalityPeriodDiagnostic] = Field(default_factory=list)
    violations: list[OptionalityViolation] = Field(default_factory=list)
    explanation: str


class OptionalityReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    reasons: list[str] = Field(default_factory=list)


class OptionalitySnapshot(BaseModel):
    optionality_snapshot_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    source_mode: SourceMode
    quality: Quality
    readiness: OptionalityReadiness
    commitments: list[ServiceCommitment] = Field(default_factory=list)
    assumptions: list[OptionalityAssumption] = Field(default_factory=list)
    path_impacts: list[OptionalityPathImpact] = Field(default_factory=list)
    optional_not_guaranteed: bool = True
    warnings: list[str] = Field(default_factory=list)


class CoordinatorAction(StrEnum):
    NO_ACTION = "NO_ACTION"
    MARKET_ONLY = "MARKET_ONLY"
    BATTERY_ONLY_P50 = "BATTERY_ONLY_P50"
    BATTERY_PRESERVE_FLEXIBILITY = "BATTERY_PRESERVE_FLEXIBILITY"
    MARKET_BATTERY_HYBRID = "MARKET_BATTERY_HYBRID"
    OPTIONALITY_PRESERVING = "OPTIONALITY_PRESERVING"


class CoordinatorScenarioResidual(BaseModel):
    scenario: str
    exposure_before_mwh: float
    battery_net_export_mwh: float
    signed_market_trade_mwh: float
    residual_exposure_mwh: float
    direction: str
    residual_value: CanonicalDataPoint


class CoordinatorCostBreakdown(BaseModel):
    market_execution_cost_gbp: float
    expected_imbalance_cost_gbp: float
    tail_risk_penalty_gbp: float
    battery_opportunity_cost_gbp: float
    optionality_lost_gbp: float
    service_risk_penalty_gbp: float
    total_diagnostic_cost_gbp: float
    market_execution_cost_value: CanonicalDataPoint
    expected_imbalance_cost_value: CanonicalDataPoint
    tail_risk_penalty_value: CanonicalDataPoint
    battery_opportunity_cost_value: CanonicalDataPoint
    optionality_lost_value: CanonicalDataPoint
    service_risk_penalty_value: CanonicalDataPoint
    total_diagnostic_cost_value: CanonicalDataPoint


class CoordinatorPeriodResult(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    exposure_before: list[ScenarioExposure]
    market_hedge_side: str
    signed_market_trade_mwh: float
    market_trade_volume_mwh: float
    market_trade_value: CanonicalDataPoint
    market_wap_gbp_per_mwh: float | None = None
    market_wap_value: CanonicalDataPoint | None = None
    market_unfilled_mwh: float
    market_unfilled_value: CanonicalDataPoint
    battery_charge_mwh: float
    battery_discharge_mwh: float
    battery_net_export_mwh: float
    battery_action_value: CanonicalDataPoint
    soc_before_mwh: float
    soc_after_mwh: float
    soc_before_value: CanonicalDataPoint
    soc_after_value: CanonicalDataPoint
    residuals: list[CoordinatorScenarioResidual]
    optionality_lost_gbp: float
    optionality_lost_value: CanonicalDataPoint
    service_commitment_at_risk: bool
    service_coverage_ratio: float
    service_risk_value: CanonicalDataPoint
    binding_constraints: list[str] = Field(default_factory=list)
    cost: CoordinatorCostBreakdown
    warnings: list[str] = Field(default_factory=list)


class CoordinatorReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    diagnostic_only: bool = True
    executable_live_ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    critical_blockers: list[str] = Field(default_factory=list)


class CoordinatorCandidate(BaseModel):
    candidate_id: str
    action: CoordinatorAction
    action_name: str
    market_trade_volume_mwh: float
    market_trade_volume_value: CanonicalDataPoint
    market_hedge_side: str
    market_wap_gbp_per_mwh: float | None = None
    market_wap_value: CanonicalDataPoint | None = None
    market_unfilled_mwh: float
    market_unfilled_value: CanonicalDataPoint
    battery_path: str
    battery_charge_mwh: float
    battery_charge_value: CanonicalDataPoint
    battery_discharge_mwh: float
    battery_discharge_value: CanonicalDataPoint
    residual_p10_mwh: float
    residual_p10_value: CanonicalDataPoint
    residual_p50_mwh: float
    residual_p50_value: CanonicalDataPoint
    residual_p90_mwh: float
    residual_p90_value: CanonicalDataPoint
    optionality_lost_gbp: float
    optionality_lost_value: CanonicalDataPoint
    service_commitments_at_risk: int
    cost: CoordinatorCostBreakdown
    readiness: CoordinatorReadiness
    periods: list[CoordinatorPeriodResult] = Field(default_factory=list)
    rank: int = 0
    explanation: str
    warning_badges: list[str] = Field(default_factory=list)


class CoordinatorSensitivity(BaseModel):
    sensitivity_id: str
    label: str
    change: str
    baseline_preferred_action: CoordinatorAction
    counterfactual_preferred_action: CoordinatorAction
    baseline_cost_gbp: float
    counterfactual_cost_gbp: float
    changed_preference: bool
    explanation: str


class CoordinatorRecommendation(BaseModel):
    label: str = "Diagnostic recommendation"
    selected_candidate_id: str
    selected_action: CoordinatorAction
    selected_action_name: str
    diagnostic_score_gbp: float
    diagnostic_score_value: CanonicalDataPoint
    not_executable: bool = True
    trustworthy_for_live_trading: bool
    explanation: str
    what_would_change: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CoordinatorSimulationInput(BaseModel):
    imbalance_price_gbp_per_mwh: float = Field(default=125.0, ge=0)
    tail_risk_weight: float = Field(default=0.35, ge=0)
    optionality_loss_weight: float = Field(default=1.0, ge=0)
    maximum_market_hedge_volume_mwh: float | None = Field(default=None, ge=0)
    selected_battery_path: str = "PRESERVE_FLEXIBILITY"
    confidence_scenario: str = "P50"
    explicit_sample_market: bool = True
    assumption_source_mode: SourceMode = SourceMode.SAMPLE


class CoordinatorSnapshot(BaseModel):
    coordinator_snapshot_id: str
    cockpit_snapshot_id: str
    as_of: datetime
    source_mode: SourceMode
    quality: Quality
    readiness: CoordinatorReadiness
    assumptions: list[CanonicalDataPoint] = Field(default_factory=list)
    candidates: list[CoordinatorCandidate] = Field(default_factory=list)
    recommendation: CoordinatorRecommendation | None = None
    sensitivities: list[CoordinatorSensitivity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SampleRegime(StrEnum):
    NORMAL = "normal"
    TIGHTENING = "tightening"
    OVERSUPPLY = "oversupply"
    PRICE_SPIKE = "price_spike"
    WIND_FORECAST_MISS = "wind_forecast_miss"
    DEMAND_SURPRISE = "demand_surprise"


class HorizonMode(StrEnum):
    NEXT_AUCTION = "next_auction"
    NEXT_8_PERIODS = "next_8_periods"
    END_OF_DAY = "end_of_day"


class HorizonRequest(BaseModel):
    mode: HorizonMode


class LiveHistoryPoint(BaseModel):
    observed_at: datetime
    renewable_production_mw: float
    wind_mw: float
    solar_mw: float
    demand_mw: float
    residual_demand_mw: float
    forecast_p50_mw: float
    forecast_error_mw: float
    frequency_hz: float
    reference_price_gbp_per_mwh: float
    best_bid_gbp_per_mwh: float
    best_ask_gbp_per_mwh: float
    bid_depth_mwh: float
    ask_depth_mwh: float
    q_mwh: float
    exposure_mwh: float
    soc_mwh: float
    previous_projected_soc_mwh: float | None = None
    reserve_up_mw: float
    reserve_down_mw: float
    system_tightness_score: float
    demand_surprise_mw: float
    production_surprise_mw: float


class ForecastVintageChartPoint(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    previous_p50_mwh: float
    latest_p50_mwh: float
    p10_mwh: float
    p90_mwh: float
    delta_mwh: float
    confidence_score: float
    driver: str


class ChartPoint(BaseModel):
    label: str
    value: float
    timestamp: datetime | None = None
    settlement_period: int | None = None
    delivery_period: str | None = None


class ChartSeries(BaseModel):
    key: str
    label: str
    unit: str
    kind: str = "line"
    points: list[ChartPoint] = Field(default_factory=list)


class RiskMeasure(BaseModel):
    key: str
    label: str
    value: float
    unit: str
    status: str = "INFO"


class DriverContribution(BaseModel):
    key: str
    label: str
    score: float
    unit: str = "score"
    explanation: str


class SensitivityResult(BaseModel):
    key: str
    label: str
    stressed_case: str
    baseline_value_gbp: float
    stressed_value_gbp: float
    delta_gbp: float
    unit: str = "GBP"
    explanation: str


class RollingTrust(BaseModel):
    readiness: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    diagnostic_only: bool = True
    reasons: list[str] = Field(default_factory=list)
    critical_missing_inputs: list[str] = Field(default_factory=list)


class RollingEvent(BaseModel):
    event_id: str
    occurred_at: datetime
    event_type: str
    message: str
    source_mode: SourceMode
    quality: Quality
    step: int
    value_id: str | None = None


class RollingState(BaseModel):
    current_time: datetime
    current_settlement_period: int
    current_settlement_label: str
    next_settlement_period: int
    next_settlement_label: str
    next_gate_closure_at: datetime
    minutes_to_gate_closure: float
    current_soc_mwh: float
    previous_projected_soc_mwh: float | None = None
    current_q_mwh_by_period: dict[str, float] = Field(default_factory=dict)
    previous_run_id: str | None = None
    latest_optimisation_run_id: str | None = None
    current_forecast_vintage_id: str
    previous_forecast_vintage_id: str | None = None
    current_market_snapshot_id: str
    previous_market_snapshot_id: str | None = None
    current_regime: SampleRegime
    current_step: int
    refresh_sequence: int
    state_source_mode: SourceMode
    quality: Quality
    trust: RollingTrust
    snapshot_id: str
    last_soc_change_mwh: float = 0.0
    last_q_change_mwh: float = 0.0
    horizon_mode: HorizonMode = HorizonMode.NEXT_8_PERIODS
    effective_horizon_mode: HorizonMode = HorizonMode.NEXT_8_PERIODS
    optimisation_horizon_start: datetime
    optimisation_horizon_end: datetime
    horizon_warning: str | None = None
    auction_calendar_configured: bool = False
    simulation_assumption: str = (
        "SAMPLE simulation assumes previous model actions are followed. This is not real execution or live control."
    )


class RollingProductionDemand(BaseModel):
    renewable_production_mw: float
    wind_mw: float
    solar_mw: float
    demand_mw: float
    residual_demand_mw: float
    production_delta_mw: float
    demand_delta_mw: float
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class RollingOrderBookLevel(BaseModel):
    side: str
    level: int
    price_gbp_per_mwh: float
    volume_mwh: float
    price_value: CanonicalDataPoint
    volume_value: CanonicalDataPoint


class RollingMarketState(BaseModel):
    reference_price_gbp_per_mwh: float
    best_bid_gbp_per_mwh: float
    best_ask_gbp_per_mwh: float
    spread_gbp_per_mwh: float
    bid_depth_mwh: float
    ask_depth_mwh: float
    sell_wap_5_mwh: float | None = None
    sell_wap_10_mwh: float | None = None
    buy_wap_5_mwh: float | None = None
    buy_wap_10_mwh: float | None = None
    frequency_hz: float
    system_tightness_score: float
    market_regime: SampleRegime
    bids: list[RollingOrderBookLevel] = Field(default_factory=list)
    asks: list[RollingOrderBookLevel] = Field(default_factory=list)
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class RollingPortfolioBattery(BaseModel):
    current_q_mwh: float
    current_forecast_generation_mwh: float
    exposure_before_action_mwh: float
    current_soc_mwh: float
    previous_projected_soc_mwh: float | None = None
    reserve_up_held_mw: float
    reserve_down_held_mw: float
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class LiveStateSnapshot(BaseModel):
    state: RollingState
    production_demand: RollingProductionDemand
    market: RollingMarketState
    portfolio_battery: RollingPortfolioBattery
    events: list[RollingEvent] = Field(default_factory=list)
    lineage_values: list[CanonicalDataPoint] = Field(default_factory=list)
    history: list[LiveHistoryPoint] = Field(default_factory=list)
    forecast_vintage_series: list[ForecastVintageChartPoint] = Field(default_factory=list)
    chart_series: dict[str, list[ChartSeries]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RegimeRequest(BaseModel):
    regime: SampleRegime


class OptimisationReadiness(BaseModel):
    status: SnapshotStatus
    calculation_allowed: bool
    trustworthy_for_live_trading: bool
    diagnostic_only: bool = True
    executable_live_ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    critical_blockers: list[str] = Field(default_factory=list)


class OptimisationStartingState(BaseModel):
    current_time: datetime
    current_settlement_period: int
    starting_soc_mwh: float
    starting_q_mwh: float
    forecast_vintage_id: str
    market_snapshot_id: str
    regime: SampleRegime
    source_mode: SourceMode
    horizon_mode: HorizonMode = HorizonMode.NEXT_8_PERIODS
    effective_horizon_mode: HorizonMode = HorizonMode.NEXT_8_PERIODS
    horizon_start: datetime
    horizon_end: datetime


class OptimisationPeriodInput(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    duration_hours: float
    generation_p10_mwh: float
    generation_p50_mwh: float
    generation_p90_mwh: float
    demand_mw: float
    system_tightness_score: float
    reference_price_gbp_per_mwh: float
    contracted_q_mwh: float
    bids: list[RollingOrderBookLevel] = Field(default_factory=list)
    asks: list[RollingOrderBookLevel] = Field(default_factory=list)
    gate_closure_at: datetime
    tradeable: bool
    upward_commitment_mw: float
    downward_commitment_mw: float
    residual_demand_mw: float = 0.0
    previous_p50_mwh: float = 0.0
    forecast_confidence_score: float = 0.0
    forecast_driver: str = "normal"
    demand_surprise_mw: float = 0.0
    production_surprise_mw: float = 0.0
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class OptimisationObjectiveBreakdown(BaseModel):
    market_execution_value_gbp: float = 0.0
    imbalance_expected_cost_gbp: float = 0.0
    tail_risk_penalty_gbp: float = 0.0
    degradation_cost_gbp: float = 0.0
    upward_availability_value_gbp: float = 0.0
    downward_availability_value_gbp: float = 0.0
    bm_expected_activation_value_gbp: float = 0.0
    service_non_delivery_risk_gbp: float = 0.0
    optionality_preservation_value_gbp: float = 0.0
    terminal_soc_value_gbp: float = 0.0
    total_diagnostic_value_gbp: float = 0.0
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class OptimisationExplanationDrivers(BaseModel):
    forecast_driver: str
    demand_system_driver: str
    price_order_book_driver: str
    battery_soc_driver: str
    reserve_bm_driver: str
    terminal_soc_driver: str
    imbalance_tail_risk_driver: str
    binding_constraint_driver: str


class OptimisationPeriodResult(BaseModel):
    settlement_period: int
    delivery_period: str
    delivery_start: datetime
    delivery_end: datetime
    generation_p10_mwh: float
    generation_p50_mwh: float
    generation_p90_mwh: float
    demand_mw: float
    system_tightness_score: float
    reference_price_gbp_per_mwh: float
    best_bid_gbp_per_mwh: float
    best_ask_gbp_per_mwh: float
    market_wap_gbp_per_mwh: float | None = None
    visible_depth_consumed_mwh: float
    q_before_action_mwh: float
    buy_mwh: float
    sell_mwh: float
    charge_mw: float
    discharge_mw: float
    battery_net_export_mw: float
    reserve_up_mw: float
    reserve_down_mw: float
    soc_before_mwh: float
    projected_soc_mwh: float
    residual_p10_mwh: float
    residual_p50_mwh: float
    residual_p90_mwh: float
    residual_long_mwh: float
    residual_short_mwh: float
    imbalance_risk_cost_gbp: float
    market_execution_value_gbp: float
    degradation_cost_gbp: float
    reserve_bm_service_value_gbp: float
    terminal_soc_contribution_gbp: float
    total_period_contribution_gbp: float
    binding_constraints: list[str] = Field(default_factory=list)
    why_action: str
    residual_demand_mw: float = 0.0
    exposure_before_p10_mwh: float = 0.0
    exposure_before_p50_mwh: float = 0.0
    exposure_before_p90_mwh: float = 0.0
    gate_closure_at: datetime
    tradeable: bool
    bid_depth_mwh: float = 0.0
    ask_depth_mwh: float = 0.0
    unfilled_market_volume_mwh: float = 0.0
    wap_slippage_gbp_per_mwh: float = 0.0
    upward_commitment_mw: float = 0.0
    downward_commitment_mw: float = 0.0
    upward_headroom_mw: float = 0.0
    downward_headroom_mw: float = 0.0
    upward_duration_coverage_h: float = 0.0
    downward_duration_coverage_h: float = 0.0
    values: dict[str, CanonicalDataPoint] = Field(default_factory=dict)


class OptimisationChangeSummary(BaseModel):
    forecast_change_mwh: float
    demand_change_mw: float
    price_change_gbp_per_mwh: float
    depth_change_mwh: float
    q_change_mwh: float
    soc_change_mwh: float
    reserve_optionality_change_gbp: float
    trajectory_change_reason: str


class OptimisationRun(BaseModel):
    run_id: str
    as_of: datetime
    snapshot_id: str
    solver: str
    solver_status: str
    horizon_length: int
    starting_state: OptimisationStartingState
    inputs: list[OptimisationPeriodInput] = Field(default_factory=list)
    projected_trajectory: list[OptimisationPeriodResult] = Field(default_factory=list)
    objective_breakdown: OptimisationObjectiveBreakdown
    objective_value_gbp: float
    terminal_soc_mwh: float
    full_cycle_equivalents: float
    explanation_drivers: OptimisationExplanationDrivers
    change_since_previous: OptimisationChangeSummary
    readiness: OptimisationReadiness
    lineage_values: list[CanonicalDataPoint] = Field(default_factory=list)
    chart_series: dict[str, list[ChartSeries]] = Field(default_factory=dict)
    risk_measures: list[RiskMeasure] = Field(default_factory=list)
    driver_contributions: list[DriverContribution] = Field(default_factory=list)
    sensitivities: list[SensitivityResult] = Field(default_factory=list)
    sanity_warnings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    immutable: bool = True
    not_executable: bool = True
