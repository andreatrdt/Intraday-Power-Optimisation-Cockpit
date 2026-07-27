"""Milestone 4 — hedge-timing service: dedup, ranking, caps, batch summaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cockpit.decision_models import DecisionContext, ModelRecommendation, RecommendedAction, TriggerType
from cockpit.decision_service import DecisionStore
from cockpit.decision_prioritisation import HedgeTimingService
from cockpit.hedge_timing_models import TimingConfig, TimingMarketView, TimingRevisionSignals, TimingVerdict

AT = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _ctx(sp, *, p50):
    start = AT + timedelta(hours=1) + timedelta(minutes=30 * (sp - 1))
    return DecisionContext(
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        delivery_start=start,
        delivery_end=start + timedelta(minutes=30),
        as_of=AT,
        trigger_type=TriggerType.FORECAST_REVISION,
        trigger_description=f"SP{sp} revised",
        minutes_to_gate_closure=30.0,
        p10_exposure_before_mwh=p50 - 25,
        p50_exposure_before_mwh=p50,
        p90_exposure_before_mwh=p50 + 25,
    )


def _rec(buy):
    return ModelRecommendation(action=RecommendedAction.BUY, buy_mwh=buy, sell_mwh=0.0)


def _market(sp, *, required, executable, snapshot="book-1", run="opt-1"):
    return TimingMarketView(
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        market_snapshot_id=snapshot,
        optimisation_run_id=run,
        available=True,
        tradeable=True,
        recommended_side="BUY",
        required_volume_mwh=required,
        best_bid_gbp_per_mwh=70.0,
        best_ask_gbp_per_mwh=72.0,
        spread_gbp_per_mwh=2.0,
        bid_depth_mwh=60.0,
        ask_depth_mwh=executable,
        executable_volume_mwh=executable,
        wap_gbp_per_mwh=72.3,
        wap_slippage_gbp_per_mwh=0.3,
    )


_SIGNALS = TimingRevisionSignals(revision_magnitude_mwh=30.0, revision_significance_score=0.8, direction_flip=False)


def _service():
    return HedgeTimingService(decisions=DecisionStore())


def test_repeated_assessment_is_idempotent():
    service = _service()
    decision = service.decisions.create(context=_ctx(24, p50=-46.0), recommendation=_rec(32.0))
    first, new1 = service.assess(decision, _market(24, required=32.0, executable=32.0), _SIGNALS, now=AT)
    second, new2 = service.assess(decision, _market(24, required=32.0, executable=32.0), _SIGNALS, now=AT)
    assert new1 is True and new2 is False
    assert first.assessment_id == second.assessment_id
    assert len(service.list_assessments()) == 1


def test_new_market_snapshot_creates_new_assessment():
    service = _service()
    decision = service.decisions.create(context=_ctx(24, p50=-46.0), recommendation=_rec(32.0))
    a1, _ = service.assess(decision, _market(24, required=32.0, executable=32.0, snapshot="book-1"), _SIGNALS, now=AT)
    a2, new2 = service.assess(decision, _market(24, required=32.0, executable=32.0, snapshot="book-2"), _SIGNALS, now=AT)
    assert new2 is True
    assert a1.assessment_id != a2.assessment_id
    assert len(service.list_assessments()) == 2


def test_deterministic_ranking_and_cap():
    config = TimingConfig(top_n_cap=3)
    service = HedgeTimingService(decisions=DecisionStore(), config=config)
    # Larger exposure -> higher priority.
    for sp, p50 in [(24, -60.0), (25, -40.0), (26, -20.0), (27, -8.0)]:
        decision = service.decisions.create(context=_ctx(sp, p50=p50), recommendation=_rec(min(abs(p50), 30.0)))
        service.assess(decision, _market(sp, required=min(abs(p50), 30.0), executable=min(abs(p50), 30.0)), _SIGNALS, now=AT)
    ranked = service.prioritise(service.list_assessments())
    assert len(ranked) == 3  # capped
    scores = [item.priority_score for item in ranked]
    assert scores == sorted(scores, reverse=True)  # descending
    # Deterministic: same inputs -> same order.
    assert [i.decision_id for i in ranked] == [i.decision_id for i in service.prioritise(service.list_assessments())]


def test_batch_of_41_returns_limited_top_list_but_all_retrievable():
    config = TimingConfig(top_n_cap=8)
    store = DecisionStore()
    service = HedgeTimingService(decisions=store, config=config)
    contexts = [_ctx(sp, p50=-(5.0 + sp)) for sp in range(1, 42)]  # 41 periods
    recommendations = [_rec(min(5.0 + sp, 30.0)) for sp in range(1, 42)]
    batch, decisions = store.create_batch(contexts=contexts, recommendations=recommendations)
    assert len(decisions) == 41

    for decision in decisions:
        required = min(5.0 + decision.settlement_period, 30.0)
        service.assess(decision, _market(decision.settlement_period, required=required, executable=required), _SIGNALS, now=AT)

    summary = service.batch_summary(batch.batch_id)
    assert summary.total_decisions == 41
    assert summary.assessed_decisions == 41
    assert len(summary.top_decision_ids) <= 8  # capped presentation
    assert summary.affected_period_range == "SP1–SP41"
    # All underlying decisions remain stored and retrievable.
    assert all(store.get(d.decision_id) is not None for d in decisions)
    assert len(store.list()) == 41


def test_batch_summary_counts_verdicts_and_priorities():
    store = DecisionStore()
    service = HedgeTimingService(decisions=store)
    contexts = [_ctx(24, p50=-46.0), _ctx(25, p50=-3.0)]  # one actionable, one within tolerance
    batch, decisions = store.create_batch(contexts=contexts, recommendations=[_rec(32.0), _rec(2.0)])
    service.assess(decisions[0], _market(24, required=32.0, executable=32.0), _SIGNALS, now=AT)
    service.assess(decisions[1], _market(25, required=2.0, executable=2.0), _SIGNALS, now=AT)
    summary = service.batch_summary(batch.batch_id)
    assert summary.total_decisions == 2
    assert summary.hedge_now_periods + summary.partial_hedge_periods + summary.wait_periods + summary.no_action_periods == 2
    assert summary.no_action_periods >= 1  # the within-tolerance one


def test_unknown_batch_summary_is_none():
    assert _service().batch_summary("batch-nope") is None


def test_latest_for_decision_tracks_newest():
    service = _service()
    decision = service.decisions.create(context=_ctx(24, p50=-46.0), recommendation=_rec(32.0))
    service.assess(decision, _market(24, required=32.0, executable=32.0, snapshot="book-1"), _SIGNALS, now=AT)
    latest, _ = service.assess(decision, _market(24, required=32.0, executable=32.0, snapshot="book-2"), _SIGNALS, now=AT)
    assert service.latest_for_decision(decision.decision_id).assessment_id == latest.assessment_id
