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

export type CoordinatorAction = "NO_ACTION" | "MARKET_ONLY" | "BATTERY_ONLY_P50" | "BATTERY_PRESERVE_FLEXIBILITY" | "MARKET_BATTERY_HYBRID" | "OPTIONALITY_PRESERVING";

export interface CoordinatorScenarioResidual {
  scenario: "P10" | "P50" | "P90";
  exposure_before_mwh: number;
  battery_net_export_mwh: number;
  signed_market_trade_mwh: number;
  residual_exposure_mwh: number;
  direction: "LONG" | "SHORT" | "FLAT";
  residual_value: CanonicalDataPoint;
}

export interface CoordinatorCostBreakdown {
  market_execution_cost_gbp: number;
  expected_imbalance_cost_gbp: number;
  tail_risk_penalty_gbp: number;
  battery_opportunity_cost_gbp: number;
  optionality_lost_gbp: number;
  service_risk_penalty_gbp: number;
  total_diagnostic_cost_gbp: number;
  market_execution_cost_value: CanonicalDataPoint;
  expected_imbalance_cost_value: CanonicalDataPoint;
  tail_risk_penalty_value: CanonicalDataPoint;
  battery_opportunity_cost_value: CanonicalDataPoint;
  optionality_lost_value: CanonicalDataPoint;
  service_risk_penalty_value: CanonicalDataPoint;
  total_diagnostic_cost_value: CanonicalDataPoint;
}

export interface CoordinatorPeriodResult {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  exposure_before: ScenarioExposure[];
  market_hedge_side: "BUY" | "SELL" | "NONE";
  signed_market_trade_mwh: number;
  market_trade_volume_mwh: number;
  market_trade_value: CanonicalDataPoint;
  market_wap_gbp_per_mwh: number | null;
  market_wap_value: CanonicalDataPoint | null;
  market_unfilled_mwh: number;
  market_unfilled_value: CanonicalDataPoint;
  battery_charge_mwh: number;
  battery_discharge_mwh: number;
  battery_net_export_mwh: number;
  battery_action_value: CanonicalDataPoint;
  soc_before_mwh: number;
  soc_after_mwh: number;
  soc_before_value: CanonicalDataPoint;
  soc_after_value: CanonicalDataPoint;
  residuals: CoordinatorScenarioResidual[];
  optionality_lost_gbp: number;
  optionality_lost_value: CanonicalDataPoint;
  service_commitment_at_risk: boolean;
  service_coverage_ratio: number;
  service_risk_value: CanonicalDataPoint;
  binding_constraints: string[];
  cost: CoordinatorCostBreakdown;
  warnings: string[];
}

export interface CoordinatorReadiness {
  status: Readiness;
  calculation_allowed: boolean;
  trustworthy_for_live_trading: boolean;
  diagnostic_only: boolean;
  executable_live_ready: boolean;
  reasons: string[];
  critical_blockers: string[];
}

export interface CoordinatorCandidate {
  candidate_id: string;
  action: CoordinatorAction;
  action_name: string;
  market_trade_volume_mwh: number;
  market_trade_volume_value: CanonicalDataPoint;
  market_hedge_side: "BUY" | "SELL" | "MIXED" | "NONE";
  market_wap_gbp_per_mwh: number | null;
  market_wap_value: CanonicalDataPoint | null;
  market_unfilled_mwh: number;
  market_unfilled_value: CanonicalDataPoint;
  battery_path: string;
  battery_charge_mwh: number;
  battery_charge_value: CanonicalDataPoint;
  battery_discharge_mwh: number;
  battery_discharge_value: CanonicalDataPoint;
  residual_p10_mwh: number;
  residual_p10_value: CanonicalDataPoint;
  residual_p50_mwh: number;
  residual_p50_value: CanonicalDataPoint;
  residual_p90_mwh: number;
  residual_p90_value: CanonicalDataPoint;
  optionality_lost_gbp: number;
  optionality_lost_value: CanonicalDataPoint;
  service_commitments_at_risk: number;
  cost: CoordinatorCostBreakdown;
  readiness: CoordinatorReadiness;
  periods: CoordinatorPeriodResult[];
  rank: number;
  explanation: string;
  warning_badges: string[];
}

export interface CoordinatorSensitivity {
  sensitivity_id: string;
  label: string;
  change: string;
  baseline_preferred_action: CoordinatorAction;
  counterfactual_preferred_action: CoordinatorAction;
  baseline_cost_gbp: number;
  counterfactual_cost_gbp: number;
  changed_preference: boolean;
  explanation: string;
}

export interface CoordinatorRecommendation {
  label: "Diagnostic recommendation";
  selected_candidate_id: string;
  selected_action: CoordinatorAction;
  selected_action_name: string;
  diagnostic_score_gbp: number;
  diagnostic_score_value: CanonicalDataPoint;
  not_executable: boolean;
  trustworthy_for_live_trading: boolean;
  explanation: string;
  what_would_change: string[];
  warnings: string[];
}

export interface CoordinatorSimulationInput {
  imbalance_price_gbp_per_mwh: number;
  tail_risk_weight: number;
  optionality_loss_weight: number;
  maximum_market_hedge_volume_mwh: number | null;
  selected_battery_path: "NO_ACTION" | "P50_COVERAGE" | "PRESERVE_FLEXIBILITY";
  confidence_scenario: "P10" | "P50" | "P90";
  explicit_sample_market: boolean;
  assumption_source_mode: SourceMode;
}

export interface CoordinatorSnapshot {
  coordinator_snapshot_id: string;
  cockpit_snapshot_id: string;
  as_of: string;
  source_mode: SourceMode;
  quality: Quality;
  readiness: CoordinatorReadiness;
  assumptions: CanonicalDataPoint[];
  candidates: CoordinatorCandidate[];
  recommendation: CoordinatorRecommendation | null;
  sensitivities: CoordinatorSensitivity[];
  warnings: string[];
}

export type SampleRegime = "normal" | "tightening" | "oversupply" | "price_spike" | "wind_forecast_miss" | "demand_surprise";
export type HorizonMode = "next_auction" | "next_8_periods" | "end_of_day";

export interface LiveHistoryPoint {
  observed_at: string;
  renewable_production_mw: number;
  wind_mw: number;
  solar_mw: number;
  demand_mw: number;
  residual_demand_mw: number;
  forecast_p50_mw: number;
  forecast_error_mw: number;
  frequency_hz: number;
  reference_price_gbp_per_mwh: number;
  best_bid_gbp_per_mwh: number;
  best_ask_gbp_per_mwh: number;
  bid_depth_mwh: number;
  ask_depth_mwh: number;
  q_mwh: number;
  exposure_mwh: number;
  soc_mwh: number;
  previous_projected_soc_mwh: number | null;
  reserve_up_mw: number;
  reserve_down_mw: number;
  system_tightness_score: number;
  demand_surprise_mw: number;
  production_surprise_mw: number;
  regime: SampleRegime;
}

export interface ForecastVintageHistoryPoint {
  observed_at: string;
  vintage_id: string;
  delivery_period: string;
  p50_mwh: number;
  previous_p50_mwh: number;
  actual_mwh: number | null;
  error_mwh: number | null;
  source_mode: SourceMode;
}

export interface HistoricalOptimisationPoint {
  as_of: string;
  run_id: string;
  first_action: string;
  starting_soc_mwh: number;
  projected_soc_mwh: number;
  starting_q_mwh: number;
  buy_mwh: number;
  sell_mwh: number;
  diagnostic_value_gbp: number;
}

export interface ForecastVintageChartPoint {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  previous_p50_mwh: number;
  latest_p50_mwh: number;
  p10_mwh: number;
  p90_mwh: number;
  delta_mwh: number;
  confidence_score: number;
  driver: string;
}

export interface ChartPoint { label: string; value: number; timestamp: string | null; settlement_period: number | null; delivery_period: string | null; }
export interface ChartAnnotation { timestamp: string | null; label: string; kind: string; value: number | null; }
export interface ChartSeries { key: string; label: string; unit: string; kind: string; points: ChartPoint[]; flat_explanation?: string | null; region?: string; annotations?: ChartAnnotation[]; }
export interface RiskMeasure { key: string; label: string; value: number; unit: string; status: string; }
export interface DriverContribution { key: string; label: string; score: number; unit: string; explanation: string; }
export interface SensitivityResult { key: string; label: string; stressed_case: string; baseline_value_gbp: number; stressed_value_gbp: number; delta_gbp: number; unit: string; explanation: string; }

export interface RollingTrust {
  readiness: Readiness;
  calculation_allowed: boolean;
  trustworthy_for_live_trading: boolean;
  diagnostic_only: boolean;
  reasons: string[];
  critical_missing_inputs: string[];
}

export interface RollingEvent {
  event_id: string;
  occurred_at: string;
  event_type: string;
  message: string;
  source_mode: SourceMode;
  quality: Quality;
  step: number;
  value_id: string | null;
}

export interface RollingState {
  current_time: string;
  current_settlement_period: number;
  current_settlement_label: string;
  next_settlement_period: number;
  next_settlement_label: string;
  next_gate_closure_at: string;
  minutes_to_gate_closure: number;
  current_soc_mwh: number;
  previous_projected_soc_mwh: number | null;
  current_q_mwh_by_period: Record<string, number>;
  previous_run_id: string | null;
  latest_optimisation_run_id: string | null;
  current_forecast_vintage_id: string;
  previous_forecast_vintage_id: string | null;
  current_market_snapshot_id: string;
  previous_market_snapshot_id: string | null;
  current_regime: SampleRegime;
  current_step: number;
  refresh_sequence: number;
  state_source_mode: SourceMode;
  quality: Quality;
  trust: RollingTrust;
  snapshot_id: string;
  last_soc_change_mwh: number;
  last_q_change_mwh: number;
  horizon_mode: HorizonMode;
  effective_horizon_mode: HorizonMode;
  optimisation_horizon_start: string;
  optimisation_horizon_end: string;
  horizon_warning: string | null;
  auction_calendar_configured: boolean;
  simulation_assumption: string;
}

export interface RollingProductionDemand {
  renewable_production_mw: number;
  wind_mw: number;
  solar_mw: number;
  demand_mw: number;
  residual_demand_mw: number;
  production_delta_mw: number;
  demand_delta_mw: number;
  values: Record<string, CanonicalDataPoint>;
}

export interface RollingOrderBookLevel {
  side: "BID" | "ASK";
  level: number;
  price_gbp_per_mwh: number;
  volume_mwh: number;
  price_value: CanonicalDataPoint;
  volume_value: CanonicalDataPoint;
}

export interface RollingMarketState {
  reference_price_gbp_per_mwh: number;
  best_bid_gbp_per_mwh: number;
  best_ask_gbp_per_mwh: number;
  spread_gbp_per_mwh: number;
  bid_depth_mwh: number;
  ask_depth_mwh: number;
  sell_wap_5_mwh: number | null;
  sell_wap_10_mwh: number | null;
  buy_wap_5_mwh: number | null;
  buy_wap_10_mwh: number | null;
  frequency_hz: number;
  system_tightness_score: number;
  market_regime: SampleRegime;
  bids: RollingOrderBookLevel[];
  asks: RollingOrderBookLevel[];
  values: Record<string, CanonicalDataPoint>;
}

export interface RollingPortfolioBattery {
  current_q_mwh: number;
  current_forecast_generation_mwh: number;
  exposure_before_action_mwh: number;
  current_soc_mwh: number;
  previous_projected_soc_mwh: number | null;
  reserve_up_held_mw: number;
  reserve_down_held_mw: number;
  values: Record<string, CanonicalDataPoint>;
}

export interface LiveStateSnapshot {
  state: RollingState;
  production_demand: RollingProductionDemand;
  market: RollingMarketState;
  portfolio_battery: RollingPortfolioBattery;
  events: RollingEvent[];
  lineage_values: CanonicalDataPoint[];
  history: LiveHistoryPoint[];
  forecast_vintage_series: ForecastVintageChartPoint[];
  forecast_vintage_history: ForecastVintageHistoryPoint[];
  optimisation_history: HistoricalOptimisationPoint[];
  available_history_windows: string[];
  chart_series: Record<string, ChartSeries[]>;
  chart_insights: Record<string, string>;
  context_risk_measures: RiskMeasure[];
  warnings: string[];
}

export interface OptimisationReadiness {
  status: Readiness;
  calculation_allowed: boolean;
  trustworthy_for_live_trading: boolean;
  diagnostic_only: boolean;
  executable_live_ready: boolean;
  reasons: string[];
  critical_blockers: string[];
}

export interface OptimisationPeriodInput {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  duration_hours: number;
  generation_p10_mwh: number;
  generation_p50_mwh: number;
  generation_p90_mwh: number;
  demand_mw: number;
  system_tightness_score: number;
  reference_price_gbp_per_mwh: number;
  contracted_q_mwh: number;
  bids: RollingOrderBookLevel[];
  asks: RollingOrderBookLevel[];
  gate_closure_at: string;
  tradeable: boolean;
  upward_commitment_mw: number;
  downward_commitment_mw: number;
  residual_demand_mw: number;
  previous_p50_mwh: number;
  forecast_confidence_score: number;
  forecast_driver: string;
  demand_surprise_mw: number;
  production_surprise_mw: number;
  values: Record<string, CanonicalDataPoint>;
}

export interface OptimisationObjectiveBreakdown {
  market_execution_value_gbp: number;
  imbalance_expected_cost_gbp: number;
  tail_risk_penalty_gbp: number;
  degradation_cost_gbp: number;
  upward_availability_value_gbp: number;
  downward_availability_value_gbp: number;
  bm_expected_activation_value_gbp: number;
  service_non_delivery_risk_gbp: number;
  optionality_preservation_value_gbp: number;
  terminal_soc_value_gbp: number;
  total_diagnostic_value_gbp: number;
  values: Record<string, CanonicalDataPoint>;
}

export interface OptimisationExplanationDrivers {
  forecast_driver: string;
  demand_system_driver: string;
  price_order_book_driver: string;
  battery_soc_driver: string;
  reserve_bm_driver: string;
  terminal_soc_driver: string;
  imbalance_tail_risk_driver: string;
  binding_constraint_driver: string;
}

export interface OptimisationPeriodResult {
  settlement_period: number;
  delivery_period: string;
  delivery_start: string;
  delivery_end: string;
  generation_p10_mwh: number;
  generation_p50_mwh: number;
  generation_p90_mwh: number;
  demand_mw: number;
  system_tightness_score: number;
  reference_price_gbp_per_mwh: number;
  best_bid_gbp_per_mwh: number;
  best_ask_gbp_per_mwh: number;
  market_wap_gbp_per_mwh: number | null;
  visible_depth_consumed_mwh: number;
  q_before_action_mwh: number;
  buy_mwh: number;
  sell_mwh: number;
  charge_mw: number;
  discharge_mw: number;
  battery_net_export_mw: number;
  reserve_up_mw: number;
  reserve_down_mw: number;
  soc_before_mwh: number;
  projected_soc_mwh: number;
  residual_p10_mwh: number;
  residual_p50_mwh: number;
  residual_p90_mwh: number;
  residual_long_mwh: number;
  residual_short_mwh: number;
  imbalance_risk_cost_gbp: number;
  market_execution_value_gbp: number;
  degradation_cost_gbp: number;
  reserve_bm_service_value_gbp: number;
  terminal_soc_contribution_gbp: number;
  total_period_contribution_gbp: number;
  binding_constraints: string[];
  why_action: string;
  residual_demand_mw: number;
  exposure_before_p10_mwh: number;
  exposure_before_p50_mwh: number;
  exposure_before_p90_mwh: number;
  gate_closure_at: string;
  tradeable: boolean;
  bid_depth_mwh: number;
  ask_depth_mwh: number;
  unfilled_market_volume_mwh: number;
  wap_slippage_gbp_per_mwh: number;
  upward_commitment_mw: number;
  downward_commitment_mw: number;
  upward_headroom_mw: number;
  downward_headroom_mw: number;
  upward_duration_coverage_h: number;
  downward_duration_coverage_h: number;
  imbalance_expected_cost_gbp: number;
  tail_risk_penalty_gbp: number;
  optionality_preservation_value_gbp: number;
  service_non_delivery_risk_gbp: number;
  values: Record<string, CanonicalDataPoint>;
}

export type AuctionPathPhase = "historical" | "current" | "optimised_future";

export interface BatteryPathPoint {
  settlement_period: number; delivery_period: string; timestamp: string; delivery_end: string; phase: AuctionPathPhase;
  charge_mw: number; charge_mwh: number; discharge_mw: number; discharge_mwh: number;
  soc_start_mwh: number; soc_end_mwh: number; reserve_up_mw: number; reserve_down_mw: number;
  upward_headroom_mw: number; downward_headroom_mw: number; upward_duration_coverage_h: number; downward_duration_coverage_h: number;
  soc_min_mwh: number; soc_max_mwh: number; terminal_soc_target_mwh: number; terminal_soc_minimum_mwh: number;
  binding_constraints: string[]; flat_path_explanation: string | null;
}

export interface PositionPathPoint {
  settlement_period: number; delivery_period: string; timestamp: string; delivery_end: string; phase: AuctionPathPhase;
  generation_p10_mwh: number; generation_p50_mwh: number; generation_p90_mwh: number; demand_mw: number; residual_demand_mw: number;
  q_before_mwh: number; buy_mwh: number; sell_mwh: number; q_after_mwh: number;
  exposure_before_p10_mwh: number; exposure_before_p50_mwh: number; exposure_before_p90_mwh: number;
  residual_p10_mwh: number; residual_p50_mwh: number; residual_p90_mwh: number;
  market_action_allowed: boolean; gate_closure_at: string; gate_closure_status: string;
  binding_constraints: string[]; one_line_reason: string;
}

export interface MarketExecutionPathPoint {
  settlement_period: number; delivery_period: string; timestamp: string; phase: AuctionPathPhase;
  bid_price_gbp_per_mwh: number; ask_price_gbp_per_mwh: number; wap_used_gbp_per_mwh: number | null; spread_gbp_per_mwh: number;
  bid_depth_mwh: number; ask_depth_mwh: number; consumed_bid_depth_mwh: number; consumed_ask_depth_mwh: number; unfilled_volume_mwh: number;
  executable_data_mode: SourceMode; reference_price_gbp_per_mwh: number; reference_price_mode: SourceMode;
  gate_closure_at: string; market_action_allowed: boolean;
}

export interface RiskValuePathPoint {
  settlement_period: number; delivery_period: string; timestamp: string; phase: AuctionPathPhase;
  market_value_or_cost_gbp: number; imbalance_cost_gbp: number; tail_risk_penalty_gbp: number; degradation_cost_gbp: number;
  terminal_soc_value_gbp: number; reserve_bm_service_value_gbp: number; optionality_lost_gbp: number;
  total_period_contribution_gbp: number; worst_case_residual_mwh: number; binding_constraint_count: number;
}

export interface OptimisationInteractionPoint {
  stable_sp_id: string;
  delivery_period: string;
  settlement_period: number;
  display_label: string;
  uk_delivery_time: string;
  phase: AuctionPathPhase;
  linked_trajectory_row_id: string;
  tooltip_payload: Record<string, string | number | boolean | null>;
  annotation_payload: string[];
  source_mode: SourceMode;
  source_provenance: string[];
  explanation_text: string;
}

export interface OptimisationChangeSummary {
  forecast_change_mwh: number;
  demand_change_mw: number;
  price_change_gbp_per_mwh: number;
  depth_change_mwh: number;
  q_change_mwh: number;
  soc_change_mwh: number;
  reserve_optionality_change_gbp: number;
  trajectory_change_reason: string;
  headroom_change_mw: number;
  largest_new_risk: string;
}

export interface OptimisationRun {
  run_id: string;
  as_of: string;
  snapshot_id: string;
  solver: string;
  solver_status: string;
  horizon_length: number;
  starting_state: {
    current_time: string;
    current_settlement_period: number;
    starting_soc_mwh: number;
    starting_q_mwh: number;
    forecast_vintage_id: string;
    market_snapshot_id: string;
    regime: SampleRegime;
    source_mode: SourceMode;
    horizon_mode: HorizonMode;
    effective_horizon_mode: HorizonMode;
    horizon_start: string;
    horizon_end: string;
  };
  inputs: OptimisationPeriodInput[];
  projected_trajectory: OptimisationPeriodResult[];
  objective_breakdown: OptimisationObjectiveBreakdown;
  objective_value_gbp: number;
  terminal_soc_mwh: number;
  full_cycle_equivalents: number;
  explanation_drivers: OptimisationExplanationDrivers;
  change_since_previous: OptimisationChangeSummary;
  readiness: OptimisationReadiness;
  lineage_values: CanonicalDataPoint[];
  chart_series: Record<string, ChartSeries[]>;
  chart_insights: Record<string, string>;
  auction_boundary_time: string;
  previous_auction_time: string;
  next_auction_time: string;
  now_marker_time: string;
  current_sp: number;
  visual_window_start: string;
  visual_window_end: string;
  optimisation_window_start: string;
  optimisation_window_end: string;
  number_of_sps_shown: number;
  number_of_sps_optimised: number;
  battery_path_series: BatteryPathPoint[];
  position_path_series: PositionPathPoint[];
  market_execution_series: MarketExecutionPathPoint[];
  risk_value_series: RiskValuePathPoint[];
  interaction_points: OptimisationInteractionPoint[];
  whole_path_explanation: string;
  risk_measures: RiskMeasure[];
  driver_contributions: DriverContribution[];
  sensitivities: SensitivityResult[];
  sanity_warnings: string[];
  warnings: string[];
  immutable: boolean;
  not_executable: boolean;
}
