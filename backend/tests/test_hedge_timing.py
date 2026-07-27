"""Milestone 4 — pure hedge-timing policy (verdicts, priority, decomposition)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.decision_models import DecisionContext, ModelRecommendation, RecommendedAction, TradeDecision, TriggerType
from cockpit.hedge_timing import assess_timing, priority_band
from cockpit.hedge_timing_models import (
    TimingConfig,
    TimingMarketView,
    TimingPriority,
    TimingRevisionSignals,
    TimingVerdict,
)

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _decision(*, buy=32.0, sell=0.0, p50=-46.0, p10=-72.0, p90=-20.0, minutes=30.0, calc=True, quality=None):
    from cockpit.models import Quality

    ctx = DecisionContext(
        settlement_period=24,
        delivery_period="SP24",
        delivery_start=AT + timedelta(hours=1),
        delivery_end=AT + timedelta(hours=1, minutes=30),
        as_of=AT,
        trigger_type=TriggerType.FORECAST_REVISION,
        trigger_description="P50 revised down",
        minutes_to_gate_closure=minutes,
        calculation_allowed=calc,
        quality=quality or Quality.FRESH,
        p10_exposure_before_mwh=p10,
        p50_exposure_before_mwh=p50,
        p90_exposure_before_mwh=p90,
    )
    action = RecommendedAction.BUY if buy >= sell else RecommendedAction.SELL
    return TradeDecision(
        decision_id="dec-1",
        created_at=AT,
        context=ctx,
        recommendation=ModelRecommendation(action=action, buy_mwh=buy, sell_mwh=sell),
    )


def _market(*, executable=32.0, spread=2.0, slippage=0.5, available=True, tradeable=True, required=32.0):
    return TimingMarketView(
        settlement_period=24,
        delivery_period="SP24",
        market_snapshot_id="book-1",
        optimisation_run_id="opt-1",
        available=available,
        tradeable=tradeable,
        recommended_side="BUY",
        required_volume_mwh=required,
        best_bid_gbp_per_mwh=70.0,
        best_ask_gbp_per_mwh=72.0,
        spread_gbp_per_mwh=spread,
        bid_depth_mwh=50.0,
        ask_depth_mwh=executable,
        executable_volume_mwh=executable,
        wap_gbp_per_mwh=72.0 + slippage,
        wap_slippage_gbp_per_mwh=slippage,
    )


_SIGNALS = TimingRevisionSignals(revision_magnitude_mwh=36.0, revision_significance_score=0.9, direction_flip=True)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_hedge_now():
    a = assess_timing(_decision(minutes=30), _market(executable=32.0), _SIGNALS)
    assert a.verdict is TimingVerdict.HEDGE_NOW
    assert a.recommended_now_buy_mwh == 32.0
    assert a.deferred_buy_mwh == 0.0
    assert a.priority in (TimingPriority.CRITICAL, TimingPriority.HIGH)


def test_partial_hedge_now_on_thin_depth():
    a = assess_timing(_decision(minutes=30), _market(executable=16.0), _SIGNALS)
    assert a.verdict is TimingVerdict.PARTIAL_HEDGE_NOW
    assert a.recommended_now_buy_mwh == 16.0
    assert a.deferred_buy_mwh == 16.0


def test_wait_when_gate_far():
    a = assess_timing(_decision(minutes=200), _market(), _SIGNALS)
    assert a.verdict is TimingVerdict.WAIT
    assert a.recommended_now_buy_mwh == 0.0
    assert a.deferred_buy_mwh == 32.0


def test_wait_on_insufficient_depth_near_gate():
    a = assess_timing(_decision(minutes=30), _market(executable=1.0), _SIGNALS)
    assert a.verdict is TimingVerdict.WAIT
    assert any("insufficient" in w.lower() for w in a.warnings)


def test_wait_when_recommendation_small_versus_exposure():
    a = assess_timing(_decision(buy=5.0, p50=-46.0, minutes=30), _market(required=5.0, executable=5.0), _SIGNALS)
    assert a.verdict is TimingVerdict.WAIT
    assert any("small versus" in r for r in a.reasons)


def test_wait_when_market_unavailable():
    a = assess_timing(_decision(minutes=30), _market(available=False), _SIGNALS)
    assert a.verdict is TimingVerdict.WAIT
    assert any("unavailable" in w.lower() for w in a.warnings)


def test_no_action_on_zero_recommendation():
    a = assess_timing(_decision(buy=0.0, sell=0.0), _market(executable=0.0, required=0.0), _SIGNALS)
    assert a.verdict is TimingVerdict.NO_ACTION
    assert a.priority is TimingPriority.INFORMATIONAL


def test_no_action_when_exposure_within_tolerance():
    a = assess_timing(_decision(buy=32.0, p50=-3.0), _market(), _SIGNALS)
    assert a.verdict is TimingVerdict.NO_ACTION


def test_no_action_when_not_tradeable():
    a = assess_timing(_decision(minutes=-5), _market(tradeable=False), _SIGNALS)
    assert a.verdict is TimingVerdict.NO_ACTION
    assert any("tradeable" in r.lower() for r in a.reasons)


def test_no_action_when_trust_blocks_calculation():
    a = assess_timing(_decision(calc=False), _market(), _SIGNALS)
    assert a.verdict is TimingVerdict.NO_ACTION
    assert any("forbids calculation" in r for r in a.reasons)


# ---------------------------------------------------------------------------
# Priority & decomposition
# ---------------------------------------------------------------------------


def test_near_gate_closure_raises_priority():
    near = assess_timing(_decision(minutes=10), _market(), _SIGNALS)
    far = assess_timing(_decision(minutes=220), _market(), _SIGNALS)
    assert near.gate_closure_score > far.gate_closure_score
    assert near.priority_score >= far.priority_score


def test_priority_score_is_decomposed_and_bounded():
    a = assess_timing(_decision(minutes=30), _market(), _SIGNALS)
    components = a.priority_components
    assert 0.0 <= a.priority_score <= 1.0
    assert components.weighted_total == a.priority_score
    for value in (
        components.gate_closure_component,
        components.exposure_component,
        components.tail_exposure_component,
        components.significance_component,
        components.direction_flip_component,
        components.liquidity_component,
        components.trust_quality_component,
    ):
        assert 0.0 <= value <= 1.0


def test_wide_spread_forces_partial_and_penalty():
    a = assess_timing(_decision(minutes=30), _market(executable=32.0, spread=12.0, slippage=6.0), _SIGNALS)
    assert a.verdict is TimingVerdict.PARTIAL_HEDGE_NOW
    assert a.priority_components.spread_slippage_penalty > 0.0


def test_significance_unavailable_is_flagged_not_confidence():
    a = assess_timing(_decision(minutes=30), _market(), signals=None)
    assert a.significance_available is False
    assert a.confidence_or_significance_component == 0.0
    assert any("significance unavailable" in w.lower() for w in a.warnings)


def test_hedge_now_is_never_below_medium():
    # Small exposure/significance would score low, but an act-now verdict is floored at MEDIUM.
    thin_signals = TimingRevisionSignals(revision_magnitude_mwh=6.0, revision_significance_score=0.0, direction_flip=False)
    a = assess_timing(_decision(minutes=44, p50=-6.0, p10=-8.0, p90=-4.0), _market(executable=32.0), thin_signals)
    if a.verdict is TimingVerdict.HEDGE_NOW:
        assert a.priority not in (TimingPriority.LOW, TimingPriority.INFORMATIONAL)


def test_priority_band_helper():
    config = TimingConfig()
    assert priority_band(0.9, TimingVerdict.HEDGE_NOW, config) is TimingPriority.CRITICAL
    assert priority_band(0.1, TimingVerdict.WAIT, config) is TimingPriority.INFORMATIONAL
    assert priority_band(0.9, TimingVerdict.NO_ACTION, config) is TimingPriority.INFORMATIONAL


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_assessment_is_frozen():
    a = assess_timing(_decision(minutes=30), _market(), _SIGNALS)
    with pytest.raises(ValidationError):
        a.verdict = TimingVerdict.WAIT
    assert a.diagnostic_only is True and a.not_executable is True
