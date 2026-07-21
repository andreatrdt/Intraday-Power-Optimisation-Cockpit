from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cockpit.adapters import UnconfiguredMarketAdapter
from cockpit.liquidity import executable_price, hedge_side
from cockpit.market_layer import build_market_snapshot, gate_closure_status
from cockpit.models import (
    CanonicalDataPoint,
    DataLineage,
    OrderBookLevel,
    Quality,
    SemanticKind,
    SourceMode,
)
from cockpit.pipeline import DataFlowPipeline
from cockpit.settlement import UTC


def canonical(metric: str, value: float, unit: str) -> CanonicalDataPoint:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    return CanonicalDataPoint(
        value_id=f"{metric}-{value}",
        metric=metric,
        value=value,
        unit=unit,
        delivery_period="2026-07-21 SP25",
        delivery_start=now,
        lineage=DataLineage(
            source_feed="test_order_book",
            source_mode=SourceMode.LIVE,
            semantic_kind=SemanticKind.OBSERVATION,
            quality=Quality.FRESH,
            published_at=now,
            retrieved_at=now,
            normalised_at=now,
            raw_field_name=metric,
        ),
    )


def level(side: str, number: int, price: float, volume: float) -> OrderBookLevel:
    return OrderBookLevel(
        side=side,
        level=number,
        price_gbp_per_mwh=price,
        volume_mwh=volume,
        price_value=canonical(f"{side.lower()}_price_{number}", price, "GBP/MWh"),
        volume_value=canonical(f"{side.lower()}_volume_{number}", volume, "MWh"),
    )


async def sample_market_result():
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    result = build_market_snapshot(
        pipeline.current_snapshot.model_copy(deep=True),
        live_provider_status=pipeline.health_for("market_intraday").source_mode,
    )
    return pipeline, result


@pytest.mark.asyncio
async def test_best_bid_and_best_ask_are_extracted_economically() -> None:
    _, result = await sample_market_result()
    period = result.snapshot.periods[0]
    assert float(period.best_bid.value) == max(level.price_gbp_per_mwh for level in period.bids)
    assert float(period.best_ask.value) == min(level.price_gbp_per_mwh for level in period.asks)


@pytest.mark.asyncio
async def test_spread_is_best_ask_minus_best_bid() -> None:
    _, result = await sample_market_result()
    period = result.snapshot.periods[0]
    assert period.liquidity.spread_gbp_per_mwh == pytest.approx(
        float(period.best_ask.value) - float(period.best_bid.value)
    )


def test_wap_buy_sweeps_multiple_ask_levels() -> None:
    levels = [level("ASK", 1, 100, 3), level("ASK", 2, 101, 4)]
    result = executable_price(levels, 5, "BUY", max_levels=3)
    assert result.executable_volume_mwh == 5
    assert result.wap_gbp_per_mwh == pytest.approx((3 * 100 + 2 * 101) / 5)
    assert result.levels_used == 2


def test_wap_sell_sweeps_multiple_bid_levels() -> None:
    levels = [level("BID", 1, 99, 2), level("BID", 2, 98, 5)]
    result = executable_price(levels, 4, "SELL", max_levels=3)
    assert result.executable_volume_mwh == 4
    assert result.wap_gbp_per_mwh == pytest.approx((2 * 99 + 2 * 98) / 4)


def test_insufficient_depth_reports_residual_unfilled_volume() -> None:
    levels = [level("ASK", 1, 100, 3), level("ASK", 2, 101, 4)]
    result = executable_price(levels, 10, "BUY", max_levels=3)
    assert result.executable_volume_mwh == 7
    assert result.unfilled_volume_mwh == 3


def test_exposure_sign_maps_to_correct_hedge_side() -> None:
    assert hedge_side(6.0) == "SELL"
    assert hedge_side(-6.0) == "BUY"
    assert hedge_side(0.0) == "NONE"


def test_gate_closure_is_one_hour_before_delivery() -> None:
    delivery_start = datetime(2026, 7, 21, 12, tzinfo=UTC)
    status = gate_closure_status(
        delivery_start,
        delivery_start + timedelta(minutes=30),
        datetime(2026, 7, 21, 10, 30, tzinfo=UTC),
    )
    assert status.gate_closure_at == datetime(2026, 7, 21, 11, tzinfo=UTC)
    assert status.minutes_to_gate_closure == 30
    assert status.status == "APPROACHING"


@pytest.mark.asyncio
async def test_stale_market_data_is_degraded_but_calculable() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    snapshot = pipeline.current_snapshot.model_copy(deep=True)
    for point in snapshot.values:
        if point.metric.startswith("market_"):
            point.lineage.quality = Quality.STALE
    result = build_market_snapshot(snapshot).snapshot
    assert result.readiness.status == "DEGRADED"
    assert result.readiness.calculation_allowed is True
    assert result.readiness.trustworthy_for_live_trading is False
    assert any("stale" in reason.lower() for reason in result.readiness.reasons)


@pytest.mark.asyncio
async def test_sample_market_data_and_derived_wap_remain_sample() -> None:
    _, result = await sample_market_result()
    assert result.snapshot.source_mode == SourceMode.SAMPLE
    assert result.snapshot.readiness.status == "DEGRADED"
    wap_values = [
        period.p50_hedge.execution.wap_value
        for period in result.snapshot.periods
        if period.p50_hedge.execution.wap_value
    ]
    assert wap_values
    assert all(value.lineage.source_mode == SourceMode.SAMPLE for value in wap_values)


@pytest.mark.asyncio
async def test_elexon_mid_or_reference_data_is_never_executable_order_book_data() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot is not None
    assert all(
        not point.metric.startswith("market_bid_") and not point.metric.startswith("market_ask_")
        for point in pipeline.current_snapshot.values
        if point.lineage.source_feed == "elexon_system"
    )
    with pytest.raises(RuntimeError, match="Elexon MID is not treated as executable"):
        await UnconfiguredMarketAdapter().fetch(datetime.now(tz=UTC))


@pytest.mark.asyncio
async def test_live_market_error_does_not_silently_fallback_to_sample_feed() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    live = pipeline.health_for("market_intraday")
    sample = pipeline.health_for("market_order_book_sample")
    assert live.source_mode == SourceMode.ERROR
    assert live.quality == Quality.MISSING
    assert pipeline.current_points["market_intraday"] == []
    assert sample.source_mode == SourceMode.SAMPLE
    assert sample.connected is True
    assert pipeline.current_points["market_order_book_sample"]


@pytest.mark.asyncio
async def test_wap_and_cashflow_have_traceable_calculation_lineage() -> None:
    _, result = await sample_market_result()
    hedge = next(
        period.p50_hedge
        for period in result.snapshot.periods
        if period.p50_hedge.execution.wap_value is not None
    )
    assert hedge.execution.wap_value is not None
    assert hedge.cashflow_value is not None
    for point in (hedge.execution.wap_value, hedge.cashflow_value):
        assert point.lineage.source_feed == "market_liquidity_calculation"
        assert point.lineage.transformations
        assert all(check.passed for check in point.lineage.validation_checks)
        assert point.value_id in {value.value_id for value in result.derived_values}


@pytest.mark.asyncio
async def test_missing_order_book_depth_blocks_market_readiness() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    snapshot = pipeline.current_snapshot.model_copy(deep=True)
    first_period = next(
        point.delivery_period
        for point in snapshot.values
        if point.metric == "market_bid_volume_l1"
    )
    snapshot.values = [
        point
        for point in snapshot.values
        if not (
            point.delivery_period == first_period
            and point.metric.startswith("market_bid_volume_")
        )
    ]
    result = build_market_snapshot(snapshot).snapshot
    assert result.readiness.status == "BLOCKED"
    assert result.readiness.calculation_allowed is False
