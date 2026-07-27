"""Deterministic, explainable hedge-timing policy (Milestone 4).

`assess_timing` maps one single-period ``TradeDecision`` + observable market and
revision inputs to a :class:`HedgeTimingAssessment` (verdict + decomposed
priority). It is a **timing policy over current observable conditions** — not a
price forecast, an optimal-stopping model, an expected-value calculation, or a
probability of success. It never invents future price movement, future
revisions, fill probability, imbalance-price forecasts or calibrated reliability;
unavailable inputs are stated explicitly in ``warnings``.

Storage, deduplication, ranking and batch summaries live in
:mod:`cockpit.decision_prioritisation`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from cockpit.decision_models import TradeDecision
from cockpit.hedge_timing_models import (
    HedgeTimingAssessment,
    POLICY_VERSION,
    PriorityComponents,
    TimingConfig,
    TimingMarketView,
    TimingPriority,
    TimingRevisionSignals,
    TimingVerdict,
)
from cockpit.models import Quality

_QUALITY_SCORE: dict[Quality, float] = {
    Quality.FRESH: 1.0,
    Quality.REVISED: 0.8,
    Quality.PARTIAL: 0.5,
    Quality.STALE: 0.4,
    Quality.MISSING: 0.0,
    Quality.INVALID: 0.0,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def new_assessment_id(decision_id: str) -> str:
    return f"tim-{decision_id}-{uuid4().hex[:8]}"


def _hedge_side(buy_mwh: float, sell_mwh: float, tolerance: float) -> tuple[str, float]:
    if buy_mwh > sell_mwh + tolerance:
        return "BUY", round(buy_mwh - sell_mwh, 6)
    if sell_mwh > buy_mwh + tolerance:
        return "SELL", round(sell_mwh - buy_mwh, 6)
    return "NONE", 0.0


def _components(
    *,
    p50_exposure: float,
    p10_exposure: float,
    p90_exposure: float,
    minutes_to_gate: float | None,
    market: TimingMarketView,
    signals: TimingRevisionSignals | None,
    required: float,
    quality: Quality,
    calculation_allowed: bool,
    config: TimingConfig,
) -> PriorityComponents:
    if minutes_to_gate is None:
        gate = 0.0
    elif minutes_to_gate <= 0:
        gate = 1.0
    else:
        gate = _clamp(1.0 - minutes_to_gate / config.gate_closure_score_horizon_minutes)

    exposure = _clamp(abs(p50_exposure) / config.exposure_scale_mwh)
    tail = _clamp(max(abs(p10_exposure), abs(p90_exposure)) / config.exposure_scale_mwh)
    significance = signals.revision_significance_score if signals and signals.revision_significance_score is not None else 0.0
    direction = 1.0 if signals and signals.direction_flip else 0.0

    if market.available and required > 0 and market.executable_volume_mwh is not None:
        depth_ratio = _clamp((market.executable_volume_mwh or 0.0) / required)
        spread = market.spread_gbp_per_mwh
        slippage = market.wap_slippage_gbp_per_mwh or 0.0
        price_ok = (spread is not None and spread <= config.max_spread_gbp_per_mwh) and (
            slippage <= config.max_slippage_gbp_per_mwh
        )
        liquidity = depth_ratio * (1.0 if price_ok else 0.5)
        penalty = 0.0
        if spread is not None and spread > config.max_spread_gbp_per_mwh:
            penalty += (spread - config.max_spread_gbp_per_mwh) / config.max_spread_gbp_per_mwh
        if slippage > config.max_slippage_gbp_per_mwh:
            penalty += (slippage - config.max_slippage_gbp_per_mwh) / config.max_slippage_gbp_per_mwh
        penalty = _clamp(penalty)
    else:
        liquidity = 0.0
        penalty = 0.0

    trust = _QUALITY_SCORE.get(quality, 0.0) * (1.0 if calculation_allowed else 0.0)

    numerator = (
        config.weight_gate * gate
        + config.weight_exposure * exposure
        + config.weight_tail * tail
        + config.weight_significance * significance
        + config.weight_direction_flip * direction
        + config.weight_liquidity * liquidity
        + config.weight_trust * trust
        - config.weight_spread_penalty * penalty
    )
    denominator = (
        config.weight_gate
        + config.weight_exposure
        + config.weight_tail
        + config.weight_significance
        + config.weight_direction_flip
        + config.weight_liquidity
        + config.weight_trust
    )
    weighted_total = _clamp(numerator / denominator) if denominator > 0 else 0.0

    return PriorityComponents(
        gate_closure_component=round(gate, 4),
        exposure_component=round(exposure, 4),
        tail_exposure_component=round(tail, 4),
        significance_component=round(significance, 4),
        direction_flip_component=round(direction, 4),
        liquidity_component=round(liquidity, 4),
        spread_slippage_penalty=round(penalty, 4),
        trust_quality_component=round(trust, 4),
        weighted_total=round(weighted_total, 4),
    )


def priority_band(score: float, verdict: TimingVerdict, config: TimingConfig) -> TimingPriority:
    if verdict is TimingVerdict.NO_ACTION:
        return TimingPriority.INFORMATIONAL
    if score >= config.critical_threshold:
        band = TimingPriority.CRITICAL
    elif score >= config.high_threshold:
        band = TimingPriority.HIGH
    elif score >= config.medium_threshold:
        band = TimingPriority.MEDIUM
    elif score >= config.low_threshold:
        band = TimingPriority.LOW
    else:
        band = TimingPriority.INFORMATIONAL
    # An explicit "act now" verdict is never below MEDIUM, so it cannot be buried.
    if verdict is TimingVerdict.HEDGE_NOW and band in (TimingPriority.LOW, TimingPriority.INFORMATIONAL):
        band = TimingPriority.MEDIUM
    return band


def assess_timing(
    decision: TradeDecision,
    market: TimingMarketView,
    signals: TimingRevisionSignals | None = None,
    config: TimingConfig | None = None,
    *,
    now: datetime | None = None,
) -> HedgeTimingAssessment:
    config = config or TimingConfig()
    assessed_at = now or datetime.now(tz=timezone.utc)
    ctx = decision.context
    rec = decision.recommendation

    p50 = ctx.p50_exposure_before_mwh or 0.0
    p10 = ctx.p10_exposure_before_mwh or 0.0
    p90 = ctx.p90_exposure_before_mwh or 0.0
    minutes = ctx.minutes_to_gate_closure
    side, required = _hedge_side(rec.buy_mwh, rec.sell_mwh, config.action_tolerance_mwh)

    components = _components(
        p50_exposure=p50,
        p10_exposure=p10,
        p90_exposure=p90,
        minutes_to_gate=minutes,
        market=market,
        signals=signals,
        required=required,
        quality=ctx.quality,
        calculation_allowed=ctx.calculation_allowed,
        config=config,
    )

    executable = market.executable_volume_mwh or 0.0
    depth_ratio = (executable / required) if (market.available and required > 0) else 0.0
    spread = market.spread_gbp_per_mwh
    slippage = market.wap_slippage_gbp_per_mwh or 0.0
    price_ok = (spread is not None and spread <= config.max_spread_gbp_per_mwh) and (
        slippage <= config.max_slippage_gbp_per_mwh
    )
    gate_near = minutes is not None and minutes <= config.gate_closure_near_minutes

    reasons: list[str] = []
    warnings: list[str] = []
    verdict: TimingVerdict

    # ---- NO_ACTION guards ------------------------------------------------
    if not ctx.calculation_allowed:
        verdict = TimingVerdict.NO_ACTION
        reasons.append("Source/trust state forbids calculation.")
        warnings.append("calculation_allowed is False.")
    elif not market.tradeable or (minutes is not None and minutes < 0):
        verdict = TimingVerdict.NO_ACTION
        reasons.append("Delivery period is no longer tradeable (Gate Closure passed).")
    elif required <= config.action_tolerance_mwh:
        verdict = TimingVerdict.NO_ACTION
        reasons.append("Optimiser recommendation is effectively zero.")
    elif abs(p50) <= config.exposure_tolerance_mwh:
        verdict = TimingVerdict.NO_ACTION
        reasons.append(f"P50 exposure {p50:+.1f} MWh is within tolerance ±{config.exposure_tolerance_mwh:.1f} MWh.")
    else:
        # ---- actionable ---------------------------------------------------
        if signals is None or signals.revision_significance_score is None:
            warnings.append("Revision significance unavailable; significance component set to 0.")
        if minutes is None:
            warnings.append("minutes_to_gate_closure unknown; treated as not near.")
        if not market.available:
            verdict = TimingVerdict.WAIT
            reasons.append("Executable market data is unavailable for this period; cannot justify acting now.")
            warnings.append("Executable market unavailable.")
        elif required < config.small_recommendation_ratio * abs(p50):
            verdict = TimingVerdict.WAIT
            reasons.append(
                f"Recommended {required:.1f} MWh is small versus {abs(p50):.1f} MWh exposure "
                f"(< {config.small_recommendation_ratio:.0%}); defer."
            )
        elif not gate_near:
            verdict = TimingVerdict.WAIT
            reasons.append(
                f"{minutes:.0f} min to Gate Closure > {config.gate_closure_near_minutes:.0f} min threshold; defer."
                if minutes is not None
                else "Gate Closure timing unknown; no transparent reason to act now; defer."
            )
            if signals and signals.uncertainty_width_change_mwh > config.uncertainty_width_increase_mwh:
                reasons.append(
                    f"Forecast uncertainty widened {signals.uncertainty_width_change_mwh:+.1f} MWh; waiting is reasonable."
                )
        elif depth_ratio >= config.depth_sufficiency_ratio and price_ok:
            verdict = TimingVerdict.HEDGE_NOW
            reasons.append(
                f"Gate Closure near ({minutes:.0f} min); visible depth covers "
                f"{depth_ratio:.0%} of {required:.1f} MWh with spread/slippage within limits."
            )
        elif depth_ratio >= config.partial_min_ratio:
            verdict = TimingVerdict.PARTIAL_HEDGE_NOW
            reasons.append(
                f"Gate Closure near; visible depth covers only {depth_ratio:.0%} of {required:.1f} MWh — "
                f"reduce risk partially."
            )
            if not price_ok:
                warnings.append("Spread/slippage exceed configured limits; partial hedge still reduces exposure.")
        else:
            verdict = TimingVerdict.WAIT
            reasons.append(
                f"Gate Closure near but visible depth covers only {depth_ratio:.0%}; insufficient to hedge now."
            )
            warnings.append("Insufficient executable depth to hedge now.")

    # ---- now / deferred split --------------------------------------------
    if verdict is TimingVerdict.HEDGE_NOW:
        now_volume = required
    elif verdict is TimingVerdict.PARTIAL_HEDGE_NOW:
        now_volume = round(min(executable, required), 6)
    else:
        now_volume = 0.0
    deferred_volume = round(max(0.0, required - now_volume), 6)

    now_buy = now_volume if side == "BUY" else 0.0
    now_sell = now_volume if side == "SELL" else 0.0
    deferred_buy = deferred_volume if side == "BUY" else 0.0
    deferred_sell = deferred_volume if side == "SELL" else 0.0

    priority = priority_band(components.weighted_total, verdict, config)
    significance_available = bool(signals and signals.revision_significance_score is not None)

    return HedgeTimingAssessment(
        assessment_id=new_assessment_id(decision.decision_id),
        decision_id=decision.decision_id,
        assessed_at=assessed_at,
        settlement_period=ctx.settlement_period,
        delivery_period=ctx.delivery_period,
        verdict=verdict,
        priority=priority,
        priority_score=components.weighted_total,
        recommended_now_buy_mwh=now_buy,
        recommended_now_sell_mwh=now_sell,
        deferred_buy_mwh=deferred_buy,
        deferred_sell_mwh=deferred_sell,
        urgency_score=components.gate_closure_component,
        liquidity_score=components.liquidity_component,
        exposure_risk_score=round(max(components.exposure_component, components.tail_exposure_component), 4),
        gate_closure_score=components.gate_closure_component,
        confidence_or_significance_component=components.significance_component,
        significance_available=significance_available,
        priority_components=components,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        market=market,
        market_snapshot_id=market.market_snapshot_id,
        optimisation_run_id=market.optimisation_run_id,
        policy_version=config.policy_version or POLICY_VERSION,
        source_mode=ctx.source_mode,
        quality=ctx.quality,
    )
