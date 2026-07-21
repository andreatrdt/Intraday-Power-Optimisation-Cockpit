"""Executable intraday order-book diagnostics integrated with forecast exposure."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from cockpit.forecast_layer import combined_quality, combined_source_mode
from cockpit.liquidity import executable_price, hedge_side, liquidity_score, ordered_levels
from cockpit.models import (
    CanonicalDataPoint,
    CockpitSnapshot,
    DataLineage,
    ExecutablePrice,
    GateClosureStatus,
    HedgeCostDiagnostic,
    LiquidityAssessment,
    MarketPeriodSnapshot,
    MarketReadiness,
    MarketSnapshot,
    OrderBookLevel,
    Quality,
    SemanticKind,
    SnapshotStatus,
    SourceMode,
    ValidationCheck,
)
from cockpit.position_layer import build_forecast_position


GATE_CLOSURE_MINUTES = 60
DEFAULT_LEVELS = 3
_LEVEL_PATTERN = re.compile(r"^market_(bid|ask)_(price|volume)_l(\d+)$")


@dataclass
class MarketLayerResult:
    snapshot: MarketSnapshot
    derived_values: list[CanonicalDataPoint]


def gate_closure_status(
    delivery_start: datetime,
    delivery_end: datetime,
    as_of: datetime,
    offset_minutes: int = GATE_CLOSURE_MINUTES,
) -> GateClosureStatus:
    closure = delivery_start - timedelta(minutes=offset_minutes)
    minutes = (closure - as_of).total_seconds() / 60
    if minutes <= 0:
        status = "CLOSED"
        warning = "Gate Closure has passed for this settlement period."
    elif minutes <= 30:
        status = "APPROACHING"
        warning = "Gate Closure is within 30 minutes."
    else:
        status = "OPEN"
        warning = None
    return GateClosureStatus(
        delivery_start=delivery_start,
        delivery_end=delivery_end,
        gate_closure_at=closure,
        minutes_to_gate_closure=round(minutes, 2),
        status=status,
        warning=warning,
    )


def build_market_snapshot(
    cockpit_snapshot: CockpitSnapshot,
    *,
    live_provider_status: SourceMode = SourceMode.ERROR,
    levels_considered: int = DEFAULT_LEVELS,
    active_provider_quality: Quality | None = None,
    active_provider_mode: SourceMode | None = None,
) -> MarketLayerResult:
    forecast_position = build_forecast_position(cockpit_snapshot)
    exposures_by_period = {
        period.delivery_period: period for period in forecast_position.snapshot.periods
    }
    candidates: dict[str, list[CanonicalDataPoint]] = {}
    for point in cockpit_snapshot.values:
        match = _LEVEL_PATTERN.match(point.metric)
        if not match or not point.delivery_period:
            continue
        candidates.setdefault(point.lineage.source_feed, []).append(point)

    active_provider = _select_provider(candidates)
    market_inputs = [point.model_copy(deep=True) for point in candidates.get(active_provider, [])]
    if active_provider_quality == Quality.STALE:
        for point in market_inputs:
            point.lineage.quality = Quality.STALE
            point.lineage.warnings.append("Market feed freshness SLA has been exceeded.")
    if active_provider_mode is not None:
        for point in market_inputs:
            point.lineage.source_mode = active_provider_mode

    raw: dict[str, dict[str, dict[int, dict[str, CanonicalDataPoint]]]] = {}
    for point in market_inputs:
        match = _LEVEL_PATTERN.match(point.metric)
        assert match and point.delivery_period
        side, kind, level_text = match.groups()
        raw.setdefault(point.delivery_period, {}).setdefault(side, {}).setdefault(
            int(level_text), {}
        )[kind] = point

    derived = list(forecast_position.derived_values)
    periods: list[MarketPeriodSnapshot] = []
    warnings: list[str] = []
    invalid_periods: list[str] = []
    for delivery_period, exposure_period in exposures_by_period.items():
        period_book = raw.get(delivery_period)
        if not period_book:
            invalid_periods.append(delivery_period)
            warnings.append(f"{delivery_period}: executable order book is missing")
            continue
        bids = _levels(period_book.get("bid", {}), "BID")
        asks = _levels(period_book.get("ask", {}), "ASK")
        if not bids or not asks:
            invalid_periods.append(delivery_period)
            warnings.append(f"{delivery_period}: valid bid and ask depth are required")
            continue

        sorted_bids = sorted(bids, key=lambda level: (-level.price_gbp_per_mwh, level.level))
        sorted_asks = sorted(asks, key=lambda level: (level.price_gbp_per_mwh, level.level))
        best_bid = sorted_bids[0].price_value
        best_ask = sorted_asks[0].price_value
        depth_bids = sorted_bids[:levels_considered]
        depth_asks = sorted_asks[:levels_considered]
        spread = _derived(
            cockpit_snapshot,
            "market_spread",
            delivery_period,
            exposure_period.delivery_start,
            float(best_ask.value) - float(best_bid.value),
            "GBP/MWh",
            [best_ask, best_bid],
            "best ask - best bid",
        )
        bid_depth = _derived(
            cockpit_snapshot,
            "market_bid_depth",
            delivery_period,
            exposure_period.delivery_start,
            sum(level.volume_mwh for level in depth_bids),
            "MWh",
            [level.volume_value for level in depth_bids],
            f"sum bid volume over first {levels_considered} price levels",
        )
        ask_depth = _derived(
            cockpit_snapshot,
            "market_ask_depth",
            delivery_period,
            exposure_period.delivery_start,
            sum(level.volume_mwh for level in depth_asks),
            "MWh",
            [level.volume_value for level in depth_asks],
            f"sum ask volume over first {levels_considered} price levels",
        )
        score_number = liquidity_score(
            float(bid_depth.value), float(ask_depth.value), float(spread.value)
        )
        score = _derived(
            cockpit_snapshot,
            "market_liquidity_score",
            delivery_period,
            exposure_period.delivery_start,
            score_number,
            "score",
            [bid_depth, ask_depth, spread],
            "0.65 * capped two-sided depth score + 0.35 * spread score",
        )
        depth_warning = (
            "Thin displayed depth within configured price levels."
            if min(float(bid_depth.value), float(ask_depth.value)) < 10
            else None
        )
        liquidity = LiquidityAssessment(
            spread_gbp_per_mwh=float(spread.value),
            bid_depth_mwh=float(bid_depth.value),
            ask_depth_mwh=float(ask_depth.value),
            liquidity_score=score_number,
            warning=depth_warning,
            spread_value=spread,
            bid_depth_value=bid_depth,
            ask_depth_value=ask_depth,
            liquidity_score_value=score,
        )
        exposure_map = {item.scenario: item for item in exposure_period.exposures}
        p50_hedge, p50_values = _hedge(
            cockpit_snapshot,
            delivery_period,
            exposure_period.delivery_start,
            bids + asks,
            exposure_map["P50"],
            levels_considered,
        )
        downside_hedge, downside_values = _hedge(
            cockpit_snapshot,
            delivery_period,
            exposure_period.delivery_start,
            bids + asks,
            exposure_map["P10"],
            levels_considered,
        )
        period_warnings = list(
            dict.fromkeys(
                [
                    *(point.lineage.warnings[0] for point in (best_bid, best_ask) if point.lineage.warnings),
                    *(warning for warning in (depth_warning, p50_hedge.liquidity_warning, downside_hedge.liquidity_warning) if warning),
                ]
            )
        )
        gate = gate_closure_status(
            exposure_period.delivery_start,
            exposure_period.delivery_end,
            cockpit_snapshot.as_of,
        )
        if gate.warning:
            period_warnings.append(gate.warning)
        periods.append(
            MarketPeriodSnapshot(
                settlement_period=exposure_period.settlement_period,
                delivery_period=delivery_period,
                delivery_start=exposure_period.delivery_start,
                delivery_end=exposure_period.delivery_end,
                bids=sorted_bids,
                asks=sorted_asks,
                best_bid=best_bid,
                best_ask=best_ask,
                liquidity=liquidity,
                gate_closure=gate,
                p10_exposure_mwh=exposure_map["P10"].residual_position_mwh,
                p50_exposure_mwh=exposure_map["P50"].residual_position_mwh,
                p90_exposure_mwh=exposure_map["P90"].residual_position_mwh,
                p50_hedge=p50_hedge,
                downside_hedge=downside_hedge,
                warnings=list(dict.fromkeys(period_warnings)),
            )
        )
        derived.extend([spread, bid_depth, ask_depth, score, *p50_values, *downside_values])

    periods.sort(key=lambda period: period.delivery_start)
    source_mode = combined_source_mode(market_inputs) if market_inputs else SourceMode.ERROR
    quality = combined_quality(market_inputs) if market_inputs else Quality.MISSING
    readiness = _readiness(
        market_inputs,
        invalid_periods,
        expected_periods=len(exposures_by_period),
        actual_periods=len(periods),
    )
    if live_provider_status == SourceMode.ERROR and source_mode == SourceMode.SAMPLE:
        warnings.append(
            "Licensed executable market provider is ERROR/MISSING; the active sample book is a separate demonstration feed."
        )
    if source_mode == SourceMode.SAMPLE:
        warnings.append("Sample order-book data is not live executable market data.")
    provider = active_provider or "unavailable"
    input_hash = hashlib.sha256(
        f"{cockpit_snapshot.input_hash}:market-liquidity-v1:{levels_considered}".encode()
    ).hexdigest()
    result = MarketSnapshot(
        market_snapshot_id=f"market-{cockpit_snapshot.snapshot_id}-{input_hash[:8]}",
        cockpit_snapshot_id=cockpit_snapshot.snapshot_id,
        as_of=cockpit_snapshot.as_of,
        input_hash=input_hash,
        active_provider=provider,
        live_provider_status=live_provider_status,
        source_mode=source_mode,
        quality=quality,
        readiness=readiness,
        levels_considered=levels_considered,
        periods=periods,
        warnings=list(dict.fromkeys(warnings)),
    )
    return MarketLayerResult(snapshot=result, derived_values=derived)


def _select_provider(candidates: dict[str, list[CanonicalDataPoint]]) -> str | None:
    if not candidates:
        return None
    precedence = {
        SourceMode.LIVE: 0,
        SourceMode.LATEST_AVAILABLE: 1,
        SourceMode.SAMPLE: 2,
        SourceMode.SYNTHETIC: 3,
        SourceMode.ERROR: 4,
    }
    return min(
        candidates,
        key=lambda feed: (
            precedence.get(candidates[feed][0].lineage.source_mode, 5),
            feed,
        ),
    )


def _levels(
    raw_levels: dict[int, dict[str, CanonicalDataPoint]], side: str
) -> list[OrderBookLevel]:
    levels: list[OrderBookLevel] = []
    for level_number, values in raw_levels.items():
        price = values.get("price")
        volume = values.get("volume")
        if (
            price is None
            or volume is None
            or price.lineage.quality in (Quality.MISSING, Quality.INVALID)
            or volume.lineage.quality in (Quality.MISSING, Quality.INVALID)
            or float(volume.value) <= 0
        ):
            continue
        levels.append(
            OrderBookLevel(
                side=side,
                level=level_number,
                price_gbp_per_mwh=float(price.value),
                volume_mwh=float(volume.value),
                price_value=price,
                volume_value=volume,
            )
        )
    return levels


def _hedge(
    snapshot: CockpitSnapshot,
    delivery_period: str,
    delivery_start: datetime,
    levels: list[OrderBookLevel],
    exposure,
    max_levels: int,
) -> tuple[HedgeCostDiagnostic, list[CanonicalDataPoint]]:
    side = hedge_side(exposure.residual_position_mwh)
    required = abs(exposure.residual_position_mwh) if side != "NONE" else 0.0
    calculation = executable_price(levels, required, side, max_levels)
    values: list[CanonicalDataPoint] = []
    if side == "NONE":
        execution = ExecutablePrice(
            side=side,
            required_volume_mwh=0,
            executable_volume_mwh=0,
            unfilled_volume_mwh=0,
            levels_considered=max_levels,
            levels_used=0,
        )
        return (
            HedgeCostDiagnostic(
                scenario=exposure.scenario,
                exposure_mwh=exposure.residual_position_mwh,
                exposure_value=exposure.exposure_value,
                hedge_side="NONE",
                required_volume_mwh=0,
                execution=execution,
                estimated_cashflow_gbp=0,
                explanation=f"{exposure.scenario} exposure is flat; no hedge volume is required.",
            ),
            values,
        )

    relevant = ordered_levels(levels, side)[:max_levels]
    inputs = [exposure.exposure_value] + [
        value for level in relevant for value in (level.price_value, level.volume_value)
    ]
    wap_value = None
    if calculation.wap_gbp_per_mwh is not None:
        wap_value = _derived(
            snapshot,
            f"hedge_{exposure.scenario.lower()}_wap",
            delivery_period,
            delivery_start,
            calculation.wap_gbp_per_mwh,
            "GBP/MWh",
            inputs,
            f"volume-weighted {side.lower()} execution across up to {max_levels} levels",
        )
        values.append(wap_value)
    executable_value = _derived(
        snapshot,
        f"hedge_{exposure.scenario.lower()}_executable_volume",
        delivery_period,
        delivery_start,
        calculation.executable_volume_mwh,
        "MWh",
        inputs,
        "sum filled order-book volume",
    )
    unfilled_value = _derived(
        snapshot,
        f"hedge_{exposure.scenario.lower()}_unfilled_volume",
        delivery_period,
        delivery_start,
        calculation.unfilled_volume_mwh,
        "MWh",
        inputs,
        "required hedge volume - executable volume",
    )
    values.extend([executable_value, unfilled_value])
    cashflow = (
        calculation.executable_volume_mwh * (calculation.wap_gbp_per_mwh or 0.0)
    ) * (1 if side == "SELL" else -1)
    cashflow_value = _derived(
        snapshot,
        f"hedge_{exposure.scenario.lower()}_cashflow",
        delivery_period,
        delivery_start,
        cashflow,
        "GBP",
        [exposure.exposure_value, *( [wap_value] if wap_value else []), executable_value],
        "signed executable volume * WAP; sell positive, buy negative",
    )
    values.append(cashflow_value)
    warning = (
        f"Insufficient depth: {calculation.unfilled_volume_mwh:.2f} MWh remains unfilled."
        if calculation.unfilled_volume_mwh > 0.001
        else None
    )
    execution = ExecutablePrice(
        side=side,
        required_volume_mwh=required,
        executable_volume_mwh=calculation.executable_volume_mwh,
        unfilled_volume_mwh=calculation.unfilled_volume_mwh,
        wap_gbp_per_mwh=calculation.wap_gbp_per_mwh,
        levels_considered=max_levels,
        levels_used=calculation.levels_used,
        wap_value=wap_value,
        executable_volume_value=executable_value,
        unfilled_volume_value=unfilled_value,
    )
    explanation = (
        f"{exposure.scenario} is {abs(exposure.residual_position_mwh):.1f} MWh "
        f"{'short' if side == 'BUY' else 'long'}, requiring a {side.lower()} hedge. "
        f"The first {max_levels} executable levels fill {calculation.executable_volume_mwh:.1f} MWh"
        + (
            f" at a WAP of £{calculation.wap_gbp_per_mwh:.2f}/MWh."
            if calculation.wap_gbp_per_mwh is not None
            else "."
        )
    )
    return (
        HedgeCostDiagnostic(
            scenario=exposure.scenario,
            exposure_mwh=exposure.residual_position_mwh,
            exposure_value=exposure.exposure_value,
            hedge_side=side,
            required_volume_mwh=required,
            execution=execution,
            estimated_cashflow_gbp=cashflow,
            cashflow_value=cashflow_value,
            liquidity_warning=warning,
            explanation=explanation,
        ),
        values,
    )


def _readiness(
    market_inputs: list[CanonicalDataPoint],
    invalid_periods: list[str],
    *,
    expected_periods: int,
    actual_periods: int,
) -> MarketReadiness:
    if not market_inputs or invalid_periods or actual_periods != expected_periods:
        reasons = ["Executable bid, ask or depth is missing or invalid"]
        if invalid_periods:
            reasons.append("Affected periods: " + ", ".join(invalid_periods))
        return MarketReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=reasons,
        )
    modes = {point.lineage.source_mode for point in market_inputs}
    qualities = {point.lineage.quality for point in market_inputs}
    if Quality.MISSING in qualities or Quality.INVALID in qualities:
        return MarketReadiness(
            status=SnapshotStatus.BLOCKED,
            calculation_allowed=False,
            trustworthy_for_live_trading=False,
            reasons=["Executable market values contain missing or invalid inputs"],
        )
    reasons: list[str] = []
    if Quality.STALE in qualities:
        reasons.append("Executable market data is stale but remains calculable")
    non_live = modes - {SourceMode.LIVE}
    if non_live:
        reasons.append(
            "Calculation uses non-live market modes: "
            + ", ".join(sorted(mode.value for mode in non_live))
        )
    if reasons:
        reasons.append("Market diagnostics are not trustworthy for live trading")
        return MarketReadiness(
            status=SnapshotStatus.DEGRADED,
            calculation_allowed=True,
            trustworthy_for_live_trading=False,
            reasons=reasons,
        )
    return MarketReadiness(
        status=SnapshotStatus.READY,
        calculation_allowed=True,
        trustworthy_for_live_trading=True,
        reasons=["Executable bid/ask and depth are fresh, live, and internally valid"],
    )


def _derived(
    snapshot: CockpitSnapshot,
    metric: str,
    delivery_period: str,
    delivery_start: datetime,
    value: float,
    unit: str,
    inputs: list[CanonicalDataPoint],
    expression: str,
) -> CanonicalDataPoint:
    source_mode = combined_source_mode(inputs)
    quality = combined_quality(inputs)
    warnings = list(
        dict.fromkeys(warning for point in inputs for warning in point.lineage.warnings)
    )
    if source_mode in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
        warnings.append(f"Derived from {source_mode.value} inputs; not live trading data.")
    published = [point.lineage.published_at for point in inputs if point.lineage.published_at]
    identifier = uuid5(
        NAMESPACE_URL,
        f"{snapshot.snapshot_id}:{metric}:{delivery_period}:{','.join(point.value_id for point in inputs)}",
    )
    return CanonicalDataPoint(
        value_id=str(identifier),
        metric=metric,
        value=round(value, 6),
        unit=unit,
        delivery_period=delivery_period,
        delivery_start=delivery_start,
        lineage=DataLineage(
            source_feed="market_liquidity_calculation",
            source_mode=source_mode,
            semantic_kind=SemanticKind.ESTIMATE,
            quality=quality,
            published_at=max(published) if published else None,
            retrieved_at=max(point.lineage.retrieved_at for point in inputs),
            normalised_at=snapshot.as_of,
            raw_field_name=expression,
            transformations=[expression],
            validation_checks=[
                ValidationCheck(
                    name="finite_result",
                    passed=value == value and abs(value) != float("inf"),
                    detail="calculated value is finite",
                ),
                ValidationCheck(
                    name="traceable_inputs",
                    passed=bool(inputs),
                    detail=f"derived from {len(inputs)} canonical input values",
                ),
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        included_in_current_snapshot=True,
        snapshot_id=snapshot.snapshot_id,
    )
