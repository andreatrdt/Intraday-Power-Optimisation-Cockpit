from __future__ import annotations

import pytest

from cockpit.models import BatteryPathInput, BatteryPathPeriodAction, Quality, SourceMode
from cockpit.optionality_layer import build_optionality_snapshot
from cockpit.pipeline import DataFlowPipeline


async def sample_snapshot():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    return pipeline.current_snapshot.model_copy(deep=True)


def impact(snapshot, name):
    return next(item for item in snapshot.path_impacts if item.path_name == name)


@pytest.mark.asyncio
async def test_upward_and_downward_power_availability_follow_net_export() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    period = impact(result, "P50_COVERAGE").periods[0]
    baseline = impact(result, "NO_ACTION").periods[0]
    assert baseline.upward_power_available_after_mw == pytest.approx(20)
    assert baseline.downward_power_available_after_mw == pytest.approx(20)
    net_export = (
        period.upward_power_available_before_mw
        - period.upward_power_available_after_mw
    )
    assert period.upward_power_available_after_mw == pytest.approx(20 - net_export)
    assert period.downward_power_available_after_mw == pytest.approx(20 + net_export)


@pytest.mark.asyncio
async def test_upward_and_downward_duration_calculation() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    period = impact(result, "NO_ACTION").periods[0]
    assert period.upward_duration_available_hours == pytest.approx((54.2 - 10) * 0.92 / 8)
    assert period.downward_duration_available_hours == pytest.approx((100 - 54.2) / (0.94 * 5))


@pytest.mark.asyncio
async def test_commitment_coverage_ratio_and_non_delivery_warning() -> None:
    snapshot = await sample_snapshot()
    standard = build_optionality_snapshot(snapshot).snapshot
    periods = impact(standard, "NO_ACTION").periods
    actions = [
        BatteryPathPeriodAction(delivery_period=period.delivery_period, discharge_mw=20)
        for period in periods[:4]
    ]
    result = build_optionality_snapshot(
        snapshot, BatteryPathInput(path_name="CUSTOM", actions=actions)
    ).snapshot
    custom = impact(result, "CUSTOM")
    at_risk = next(period for period in custom.periods if period.commitment_at_risk)
    assert at_risk.commitment_coverage_ratio < 1
    assert any("COMMITMENT_AT_RISK" in item.code for item in at_risk.violations)
    assert custom.commitments_at_risk > 0


@pytest.mark.asyncio
async def test_bm_expected_value_formula_is_transparent() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    estimate = impact(result, "NO_ACTION").periods[0].bm_estimate
    assert estimate.expected_value_gbp == pytest.approx(
        estimate.gross_expected_value_gbp
        - estimate.non_delivery_risk_penalty_gbp
        - estimate.activation_opportunity_cost_gbp
    )
    assert estimate.optional_not_guaranteed is True


@pytest.mark.asyncio
async def test_service_availability_value_formula_is_transparent() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    estimate = impact(result, "NO_ACTION").periods[0].service_estimate
    assert estimate.availability_value_gbp == pytest.approx(6.5 * (8 + 5) * 0.5)
    assert estimate.expected_service_value_gbp == pytest.approx(
        estimate.availability_value_gbp
        + estimate.expected_activation_value_gbp
        - estimate.non_delivery_risk_penalty_gbp
    )


@pytest.mark.asyncio
async def test_standard_path_integration_reports_optionality_lost() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    no_action = impact(result, "NO_ACTION")
    coverage = impact(result, "P50_COVERAGE")
    preserve = impact(result, "PRESERVE_FLEXIBILITY")
    assert no_action.optionality_lost_gbp == pytest.approx(0)
    assert coverage.optionality_lost_gbp == pytest.approx(
        coverage.optionality_value_before_gbp - coverage.optionality_value_after_gbp
    )
    assert coverage.optionality_lost_gbp > 0
    assert abs(preserve.optionality_lost_gbp) < abs(coverage.optionality_lost_gbp)
    assert sorted(period.risk_rank for period in coverage.periods) == list(range(1, len(coverage.periods) + 1))
    assert no_action.worst_affected_period is None
    assert "largest value impact" not in no_action.explanation.lower()


@pytest.mark.asyncio
async def test_sample_assumptions_remain_sample_and_degrade_readiness() -> None:
    result = build_optionality_snapshot(await sample_snapshot()).snapshot
    assert result.source_mode == SourceMode.SAMPLE
    assert result.readiness.status == "DEGRADED"
    assert result.readiness.calculation_allowed is True
    assert result.readiness.trustworthy_for_live_trading is False
    assert all(item.value_point.lineage.source_mode == SourceMode.SAMPLE for item in result.assumptions)
    assert all(item.value_point.lineage.semantic_kind == "ASSUMPTION" for item in result.assumptions)


@pytest.mark.asyncio
async def test_stale_commitment_degrades_readiness() -> None:
    snapshot = await sample_snapshot()
    commitment = next(point for point in snapshot.values if point.metric == "upward_service_commitment")
    commitment.lineage.quality = Quality.STALE
    result = build_optionality_snapshot(snapshot).snapshot
    assert result.readiness.status == "DEGRADED"
    assert any("stale" in reason.lower() for reason in result.readiness.reasons)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_metric", ["upward_service_commitment", "bm_acceptance_probability"])
async def test_missing_commitment_or_assumption_blocks(missing_metric) -> None:
    snapshot = await sample_snapshot()
    snapshot.values = [point for point in snapshot.values if point.metric != missing_metric]
    result = build_optionality_snapshot(snapshot).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.readiness.calculation_allowed is False
    assert result.path_impacts == []


@pytest.mark.asyncio
async def test_optionality_values_have_lineage_and_non_guaranteed_label() -> None:
    result = build_optionality_snapshot(await sample_snapshot())
    period = impact(result.snapshot, "P50_COVERAGE").periods[0]
    point = period.optionality_lost_value
    assert point.lineage.source_feed == "optionality_diagnostic"
    assert point.lineage.semantic_kind == "ESTIMATE"
    assert any("not guaranteed" in warning.lower() for warning in point.lineage.warnings)
    assert any(check.name == "optional_not_guaranteed" for check in point.lineage.validation_checks)
    assert point.value_id in {item.value_id for item in result.derived_values}
    assert result.snapshot.optional_not_guaranteed is True


@pytest.mark.asyncio
async def test_synthetic_assumption_is_never_silently_relabelled() -> None:
    snapshot = await sample_snapshot()
    assumption = next(point for point in snapshot.values if point.metric == "bm_expected_margin")
    assumption.lineage.source_mode = SourceMode.SYNTHETIC
    result = build_optionality_snapshot(snapshot).snapshot
    assert result.source_mode == SourceMode.SYNTHETIC
    assert result.readiness.status == "DEGRADED"
    point = impact(result, "NO_ACTION").periods[0].bm_estimate.expected_value
    assert point.lineage.source_mode == SourceMode.SYNTHETIC
