export type SourceMode = "LIVE" | "LATEST_AVAILABLE" | "SAMPLE" | "SYNTHETIC" | "ERROR";
export type SemanticKind = "OBSERVATION" | "FORECAST" | "ESTIMATE" | "ASSUMPTION";
export type Quality = "FRESH" | "STALE" | "PARTIAL" | "MISSING" | "REVISED" | "INVALID";
export type Readiness = "READY" | "DEGRADED" | "BLOCKED";

export interface ValidationCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface DataLineage {
  source_feed: string;
  source_mode: SourceMode;
  semantic_kind: SemanticKind;
  quality: Quality;
  published_at: string | null;
  retrieved_at: string;
  normalised_at: string;
  raw_field_name: string;
  transformations: string[];
  validation_checks: ValidationCheck[];
  warnings: string[];
}

export interface CanonicalDataPoint {
  value_id: string;
  metric: string;
  value: number | string | boolean;
  unit: string;
  delivery_period: string | null;
  delivery_start: string | null;
  lineage: DataLineage;
  previous_value: number | string | boolean | null;
  delta_vs_previous: number | null;
  included_in_current_snapshot: boolean;
  snapshot_id: string | null;
}

export interface FeedHealth {
  feed_id: string;
  feed_name: string;
  description: string;
  source_mode: SourceMode;
  semantic_kind: SemanticKind;
  quality: Quality;
  configured: boolean;
  connected: boolean;
  expected_refresh_cadence_seconds: number;
  freshness_sla_seconds: number;
  last_refresh_attempt: string | null;
  last_successful_refresh: string | null;
  age_seconds: number | null;
  rows_retrieved: number;
  rows_normalised: number;
  validation_errors: string[];
  latest_error_message: string | null;
  retry_status: string;
  included_in_current_snapshot: boolean;
  required_for_snapshot: boolean;
  required_for_optimiser: boolean;
  pipeline_stage: string;
}

export interface CockpitSnapshot {
  snapshot_id: string;
  as_of: string;
  input_hash: string;
  status: Readiness;
  readiness: { status: Readiness; reasons: string[] };
  optimiser_readiness: { status: Readiness; allowed: boolean; reasons: string[] };
  feeds_included: string[];
  feeds_excluded: string[];
  stale_feeds: string[];
  missing_feeds: string[];
  values: CanonicalDataPoint[];
}

export interface DataFlowEvent {
  event_id: string;
  occurred_at: string;
  feed_id: string | null;
  stage: string;
  level: string;
  message: string;
  attempt_id: string | null;
  snapshot_id: string | null;
}

export interface LineageResponse {
  value: CanonicalDataPoint;
  age_seconds: number;
}

export interface ForecastVintage {
  vintage_id: string;
  issued_at: string;
  source_feed: string;
  source_mode: SourceMode;
  semantic_kind: SemanticKind;
  quality: Quality;
  model_name: string;
}

export interface ForecastReliability {
  score: number | null;
  label: string;
  flags: string[];
  model_disagreement_mwh: number | null;
  score_value: CanonicalDataPoint | null;
  disagreement_value: CanonicalDataPoint | null;
}

export interface ForecastDelta {
  versus_previous_mwh: number | null;
  versus_day_ahead_mwh: number | null;
  versus_previous_value: CanonicalDataPoint | null;
  versus_day_ahead_value: CanonicalDataPoint | null;
}

export interface ForecastPoint {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  duration_hours: number;
  p10: CanonicalDataPoint;
  p50: CanonicalDataPoint;
  p90: CanonicalDataPoint;
  previous_p50: CanonicalDataPoint | null;
  day_ahead_p50: CanonicalDataPoint | null;
  delta: ForecastDelta;
  reliability: ForecastReliability;
  warnings: string[];
}

export interface PositionVersion {
  version_id: string;
  as_of: string;
  source_feed: string;
  source_mode: SourceMode;
  semantic_kind: SemanticKind;
  quality: Quality;
}

export interface PositionPoint {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  contracted_position: CanonicalDataPoint;
  warnings: string[];
}

export interface ScenarioExposure {
  scenario: "P10" | "P50" | "P90";
  generation_mwh: number;
  contracted_position_mwh: number;
  residual_position_mwh: number;
  direction: "LONG" | "SHORT" | "FLAT";
  generation_value: CanonicalDataPoint;
  exposure_value: CanonicalDataPoint;
}

export interface PositionReadiness {
  status: Readiness;
  calculation_allowed: boolean;
  trustworthy_for_live_trading: boolean;
  reasons: string[];
}

export interface ForecastPositionPeriod {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  forecast: ForecastPoint;
  position: PositionPoint;
  exposures: ScenarioExposure[];
  base_case_direction: "LONG" | "SHORT" | "FLAT";
  downside_exposure_mwh: number;
  upside_exposure_mwh: number;
  risk_magnitude_mwh: number;
  risk_rank: number;
  explanation: string;
  warnings: string[];
}

export interface ForecastPositionSnapshot {
  forecast_position_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  input_hash: string;
  readiness: PositionReadiness;
  latest_vintage: ForecastVintage | null;
  previous_vintage: ForecastVintage | null;
  position_version: PositionVersion | null;
  periods: ForecastPositionPeriod[];
  most_exposed_periods: string[];
  warnings: string[];
}

export interface OrderBookLevel {
  side: "BID" | "ASK";
  level: number;
  price_gbp_per_mwh: number;
  volume_mwh: number;
  price_value: CanonicalDataPoint;
  volume_value: CanonicalDataPoint;
}

export interface ExecutablePrice {
  side: "BUY" | "SELL" | "NONE";
  required_volume_mwh: number;
  executable_volume_mwh: number;
  unfilled_volume_mwh: number;
  wap_gbp_per_mwh: number | null;
  levels_considered: number;
  levels_used: number;
  wap_value: CanonicalDataPoint | null;
  executable_volume_value: CanonicalDataPoint | null;
  unfilled_volume_value: CanonicalDataPoint | null;
}

export interface LiquidityAssessment {
  spread_gbp_per_mwh: number;
  bid_depth_mwh: number;
  ask_depth_mwh: number;
  liquidity_score: number;
  warning: string | null;
  spread_value: CanonicalDataPoint;
  bid_depth_value: CanonicalDataPoint;
  ask_depth_value: CanonicalDataPoint;
  liquidity_score_value: CanonicalDataPoint;
}

export interface GateClosureStatus {
  delivery_start: string;
  delivery_end: string;
  gate_closure_at: string;
  minutes_to_gate_closure: number;
  status: "OPEN" | "APPROACHING" | "CLOSED";
  warning: string | null;
}

export interface MarketReadiness {
  status: Readiness;
  calculation_allowed: boolean;
  trustworthy_for_live_trading: boolean;
  reasons: string[];
}

export interface HedgeCostDiagnostic {
  scenario: "P10" | "P50" | "P90";
  exposure_mwh: number;
  exposure_value: CanonicalDataPoint;
  hedge_side: "BUY" | "SELL" | "NONE";
  required_volume_mwh: number;
  execution: ExecutablePrice;
  estimated_cashflow_gbp: number;
  cashflow_value: CanonicalDataPoint | null;
  liquidity_warning: string | null;
  explanation: string;
}

export interface MarketPeriodSnapshot {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  bids: OrderBookLevel[];
  asks: OrderBookLevel[];
  best_bid: CanonicalDataPoint;
  best_ask: CanonicalDataPoint;
  liquidity: LiquidityAssessment;
  gate_closure: GateClosureStatus;
  p10_exposure_mwh: number;
  p50_exposure_mwh: number;
  p90_exposure_mwh: number;
  p50_hedge: HedgeCostDiagnostic;
  downside_hedge: HedgeCostDiagnostic;
  warnings: string[];
}

export interface MarketSnapshot {
  market_snapshot_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  input_hash: string;
  active_provider: string;
  live_provider_status: SourceMode;
  source_mode: SourceMode;
  quality: Quality;
  readiness: MarketReadiness;
  levels_considered: number;
  periods: MarketPeriodSnapshot[];
  warnings: string[];
}

export interface BatteryAssetLimits {
  e_min: CanonicalDataPoint;
  e_max: CanonicalDataPoint;
  charge_power_max: CanonicalDataPoint;
  discharge_power_max: CanonicalDataPoint;
  charge_efficiency: CanonicalDataPoint;
  discharge_efficiency: CanonicalDataPoint;
  reserve_duration: CanonicalDataPoint;
}

export interface BatteryOpportunityCost {
  discharge_cost_gbp_per_mwh: number;
  charge_cost_gbp_per_mwh: number;
  discharge_cost_value: CanonicalDataPoint;
  charge_cost_value: CanonicalDataPoint;
  degradation_cost: CanonicalDataPoint;
  terminal_soc_penalty: CanonicalDataPoint;
  future_flexibility_penalty: CanonicalDataPoint;
  terminal_soc_target: CanonicalDataPoint;
  assumptions: string[];
}

export interface BatteryExposureCoverage {
  scenario: "P10" | "P50" | "P90";
  exposure_mwh: number;
  support_direction: "CHARGE" | "DISCHARGE" | "NONE";
  maximum_support_mwh: number;
  covered_mwh: number;
  residual_after_support_mwh: number;
  coverage_percent: number;
  exposure_value: CanonicalDataPoint;
  covered_value: CanonicalDataPoint;
  residual_value: CanonicalDataPoint;
}

export interface BatteryFeasibilityPoint {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  duration_hours: number;
  current_soc: CanonicalDataPoint;
  upward_reserved: CanonicalDataPoint;
  downward_reserved: CanonicalDataPoint;
  max_charge_mwh: number;
  max_discharge_mwh: number;
  upward_power_headroom_mw: number;
  downward_power_headroom_mw: number;
  upward_energy_duration_hours: number;
  downward_space_duration_hours: number;
  projected_soc_after_max_charge_mwh: number;
  projected_soc_after_max_discharge_mwh: number;
  max_charge_value: CanonicalDataPoint;
  max_discharge_value: CanonicalDataPoint;
  upward_power_headroom_value: CanonicalDataPoint;
  downward_power_headroom_value: CanonicalDataPoint;
  upward_energy_duration_value: CanonicalDataPoint;
  downward_space_duration_value: CanonicalDataPoint;
  projected_soc_after_max_charge_value: CanonicalDataPoint;
  projected_soc_after_max_discharge_value: CanonicalDataPoint;
  binding_constraints: string[];
  warnings: string[];
}

export interface BatteryPeriodSnapshot {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  feasibility: BatteryFeasibilityPoint;
  coverage: BatteryExposureCoverage[];
  explanation: string;
  warnings: string[];
}

export interface BatteryFlexibilitySnapshot {
  battery_snapshot_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  input_hash: string;
  source_mode: SourceMode;
  quality: Quality;
  readiness: PositionReadiness;
  current_soc: CanonicalDataPoint | null;
  limits: BatteryAssetLimits | null;
  opportunity_cost: BatteryOpportunityCost | null;
  periods: BatteryPeriodSnapshot[];
  most_useful_periods: string[];
  warnings: string[];
}

export interface BatteryPathPeriodAction {
  delivery_period: string;
  charge_mw: number;
  discharge_mw: number;
}

export interface BatteryPathViolation {
  code: string;
  message: string;
  severity: string;
  delivery_period: string | null;
  observed_value: CanonicalDataPoint | null;
  limit_value: CanonicalDataPoint | null;
}

export interface BatteryPathPeriodResult {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  duration_hours: number;
  starting_soc_mwh: number;
  charge_mw: number;
  charge_mwh: number;
  discharge_mw: number;
  discharge_mwh: number;
  net_export_mw: number;
  ending_soc_mwh: number;
  upward_power_headroom_mw: number;
  downward_power_headroom_mw: number;
  upward_energy_duration_hours: number;
  downward_energy_duration_hours: number;
  max_feasible_charge_mwh: number;
  max_feasible_discharge_mwh: number;
  starting_soc_value: CanonicalDataPoint;
  charge_power_value: CanonicalDataPoint;
  charge_energy_value: CanonicalDataPoint;
  discharge_power_value: CanonicalDataPoint;
  discharge_energy_value: CanonicalDataPoint;
  net_export_value: CanonicalDataPoint;
  ending_soc_value: CanonicalDataPoint;
  upward_power_headroom_value: CanonicalDataPoint;
  downward_power_headroom_value: CanonicalDataPoint;
  upward_energy_duration_value: CanonicalDataPoint;
  downward_energy_duration_value: CanonicalDataPoint;
  max_feasible_charge_value: CanonicalDataPoint;
  max_feasible_discharge_value: CanonicalDataPoint;
  exposure_before: ScenarioExposure[];
  residual_exposure: ScenarioExposure[];
  binding_constraints: string[];
  violations: BatteryPathViolation[];
}

export interface BatteryPathSimulation {
  simulation_id: string;
  cockpit_snapshot_id: string;
  path_name: "NO_ACTION" | "P50_COVERAGE" | "PRESERVE_FLEXIBILITY" | "CUSTOM";
  path_label: string;
  path_kind: string;
  diagnostic_only: boolean;
  as_of: string;
  source_mode: SourceMode;
  quality: Quality;
  readiness: PositionReadiness;
  valid: boolean;
  periods: BatteryPathPeriodResult[];
  e_min_mwh: number | null;
  e_max_mwh: number | null;
  e_min_value: CanonicalDataPoint | null;
  e_max_value: CanonicalDataPoint | null;
  terminal_soc_mwh: number | null;
  terminal_soc_value: CanonicalDataPoint | null;
  terminal_target_mwh: number | null;
  terminal_target_value: CanonicalDataPoint | null;
  terminal_shortfall_mwh: number | null;
  terminal_shortfall_value: CanonicalDataPoint | null;
  total_absolute_p50_residual_mwh: number | null;
  total_absolute_p50_residual_value: CanonicalDataPoint | null;
  first_binding_constraint: string | null;
  violations: BatteryPathViolation[];
  explanation: string;
  warnings: string[];
}

export interface BatteryPathComparison {
  comparison_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  readiness: PositionReadiness;
  no_action: BatteryPathSimulation;
  p50_coverage: BatteryPathSimulation;
  preserve_flexibility: BatteryPathSimulation;
  p50_terminal_soc_delta_mwh: number;
  preserve_terminal_soc_delta_mwh: number;
  p50_residual_reduction_mwh: number;
  preserve_residual_reduction_mwh: number;
  explanation: string;
}

export interface ServiceProduct {
  product_id: string;
  name: string;
  direction: "UPWARD" | "DOWNWARD";
  product_kind: "COMMITTED";
  description: string;
}

export interface ServiceCommitment {
  commitment_id: string;
  product: ServiceProduct;
  delivery_period: string;
  reserved_mw: number;
  required_duration_hours: number;
  obligation_status: string;
  reserved_value: CanonicalDataPoint;
  duration_value: CanonicalDataPoint;
}

export interface OptionalityAssumption {
  key: string;
  label: string;
  value: number;
  unit: string;
  description: string;
  value_point: CanonicalDataPoint;
}

export interface BMOptionalityEstimate {
  acceptance_probability: number;
  expected_activation_mwh: number;
  expected_margin_gbp_per_mwh: number;
  gross_expected_value_gbp: number;
  non_delivery_risk_penalty_gbp: number;
  activation_opportunity_cost_gbp: number;
  expected_value_gbp: number;
  expected_activation_value: CanonicalDataPoint;
  gross_expected_value: CanonicalDataPoint;
  non_delivery_risk_penalty_value: CanonicalDataPoint;
  activation_opportunity_cost_value: CanonicalDataPoint;
  expected_value: CanonicalDataPoint;
  optional_not_guaranteed: boolean;
}

export interface AncillaryServiceEstimate {
  availability_value_gbp: number;
  expected_activation_value_gbp: number;
  non_delivery_risk_penalty_gbp: number;
  expected_service_value_gbp: number;
  availability_value: CanonicalDataPoint;
  expected_activation_value: CanonicalDataPoint;
  non_delivery_risk_penalty_value: CanonicalDataPoint;
  expected_service_value: CanonicalDataPoint;
}

export interface OptionalityViolation {
  code: string;
  message: string;
  severity: string;
  delivery_period: string | null;
  direction: string | null;
  observed_value: CanonicalDataPoint | null;
  required_value: CanonicalDataPoint | null;
}

export interface OptionalityPeriodDiagnostic {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  risk_rank: number;
  starting_soc_mwh: number;
  ending_soc_mwh: number;
  starting_soc_value: CanonicalDataPoint;
  ending_soc_value: CanonicalDataPoint;
  upward_power_available_before_mw: number;
  downward_power_available_before_mw: number;
  upward_power_available_after_mw: number;
  downward_power_available_after_mw: number;
  upward_power_available_before_value: CanonicalDataPoint;
  downward_power_available_before_value: CanonicalDataPoint;
  upward_power_available_after_value: CanonicalDataPoint;
  downward_power_available_after_value: CanonicalDataPoint;
  upward_duration_available_hours: number;
  downward_duration_available_hours: number;
  upward_duration_available_value: CanonicalDataPoint;
  downward_duration_available_value: CanonicalDataPoint;
  committed_upward_mw: number;
  committed_downward_mw: number;
  optional_upward_before_mw: number;
  optional_downward_before_mw: number;
  optional_upward_after_mw: number;
  optional_downward_after_mw: number;
  optional_upward_after_value: CanonicalDataPoint;
  optional_downward_after_value: CanonicalDataPoint;
  commitment_coverage_ratio: number;
  commitment_coverage_value: CanonicalDataPoint;
  bm_estimate: BMOptionalityEstimate;
  service_estimate: AncillaryServiceEstimate;
  optionality_value_before_gbp: number;
  optionality_value_after_gbp: number;
  optionality_lost_gbp: number;
  optionality_value_before_value: CanonicalDataPoint;
  optionality_value_after_value: CanonicalDataPoint;
  optionality_lost_value: CanonicalDataPoint;
  commitment_at_risk: boolean;
  violations: OptionalityViolation[];
  warnings: string[];
}

export interface OptionalityPathImpact {
  path_name: "NO_ACTION" | "P50_COVERAGE" | "PRESERVE_FLEXIBILITY" | "CUSTOM";
  path_label: string;
  diagnostic_only: boolean;
  optionality_value_before_gbp: number;
  optionality_value_after_gbp: number;
  optionality_lost_gbp: number;
  optionality_value_before_value: CanonicalDataPoint | null;
  optionality_value_after_value: CanonicalDataPoint | null;
  optionality_lost_value: CanonicalDataPoint | null;
  commitments_at_risk: number;
  worst_affected_period: string | null;
  periods: OptionalityPeriodDiagnostic[];
  violations: OptionalityViolation[];
  explanation: string;
}

export interface OptionalitySnapshot {
  optionality_snapshot_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  source_mode: SourceMode;
  quality: Quality;
  readiness: PositionReadiness;
  commitments: ServiceCommitment[];
  assumptions: OptionalityAssumption[];
  path_impacts: OptionalityPathImpact[];
  optional_not_guaranteed: boolean;
  warnings: string[];
}
