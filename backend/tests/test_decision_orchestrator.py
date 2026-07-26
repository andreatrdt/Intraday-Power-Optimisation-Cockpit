"""Milestone 3 — trader-in-the-loop backend integration.

Unit tests drive the orchestrator with hand-built AdapterSnapshots (no rolling
service needed); one integration test exercises the real rolling adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cockpit.decision_models import DecisionStatus, RecommendedAction, RunMode
from cockpit.decision_service import DecisionStore
from cockpit.decision_orchestrator import (
    AdapterSnapshot,
    DecisionOrchestrator,
    InconsistentActionError,
    OptimiserPeriodView,
    map_recommended_action,
)
from cockpit.forecast_revision import VintageForecastPoint

AT0 = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DSTART = AT0 + timedelta(minutes=90)


def _vp(vintage_id, minutes_before, *, sp=24, p10, p50, p90, delivery_start=DSTART):
    return VintageForecastPoint(
        vintage_id=vintage_id,
        published_at=AT0 - timedelta(minutes=minutes_before),
        settlement_period=sp,
        delivery_period=f"SP{sp}",
        delivery_start=delivery_start,
        delivery_end=delivery_start + timedelta(minutes=30),
        p10_mwh=p10,
        p50_mwh=p50,
        p90_mwh=p90,
    )


def _view(sp, buy, sell, **kw):
    return OptimiserPeriodView(settlement_period=sp, delivery_period=f"SP{sp}", buy_mwh=buy, sell_mwh=sell, **kw)


def _material_snapshot(
    *,
    latest_vintage="v2",
    previous_vintage="v1",
    buy=32.0,
    sell=0.0,
    q=210.0,
    sp=24,
    include_optimiser=True,
    gate_at=None,
    run_mode=RunMode.SAMPLE_DEMO,
    extra_points=(),
):
    points = [
        _vp(previous_vintage, 60, sp=sp, p10=180, p50=200, p90=220),
        _vp(latest_vintage, 10, sp=sp, p10=150, p50=164, p90=196),  # delta_p50 = -36 (material)
        *extra_points,
    ]
    return AdapterSnapshot(
        as_of=AT0,
        run_mode=run_mode,
        market_snapshot_id="book-1",
        optimisation_run_id="opt-1",
        forecast_points=tuple(points),
        q_by_period={f"SP{sp}": q},
        gate_closure_by_period={f"SP{sp}": gate_at if gate_at is not None else AT0 + timedelta(minutes=85)},
        optimiser_by_sp={sp: _view(sp, buy, sell)} if include_optimiser else {},
    )


@pytest.fixture()
def orchestrator():
    return DecisionOrchestrator(decisions=DecisionStore(), rolling=None)


# ---------------------------------------------------------------------------
# Action mapping
# ---------------------------------------------------------------------------


def test_action_mapping_buy_sell_no_action():
    assert map_recommended_action(32.0, 0.0) is RecommendedAction.BUY
    assert map_recommended_action(0.0, 12.0) is RecommendedAction.SELL
    assert map_recommended_action(0.0, 0.0) is RecommendedAction.NO_ACTION


def test_action_mapping_rejects_simultaneous_buy_and_sell():
    with pytest.raises(InconsistentActionError):
        map_recommended_action(5.0, 5.0)


# ---------------------------------------------------------------------------
# Decision creation
# ---------------------------------------------------------------------------


def test_material_revision_creates_one_decision(orchestrator):
    result = orchestrator.process(_material_snapshot(), now=AT0)
    assert len(result.created_decision_ids) == 1
    assert result.batch_id is not None
    decision = orchestrator.decisions.get(result.created_decision_ids[0])
    assert decision.status is DecisionStatus.PROPOSED
    assert decision.not_executable and decision.diagnostic_only


def test_non_material_revision_creates_none(orchestrator):
    points = (
        _vp("v1", 60, p10=180, p50=200, p90=220),
        _vp("v2", 10, p10=178, p50=198, p90=218),  # delta -2
    )
    snap = AdapterSnapshot(
        as_of=AT0,
        market_snapshot_id="book-1",
        optimisation_run_id="opt-1",
        forecast_points=points,
        q_by_period={"SP24": 0.0},
        gate_closure_by_period={"SP24": AT0 + timedelta(minutes=85)},
        optimiser_by_sp={24: _view(24, 0.0, 0.0)},
    )
    result = orchestrator.process(snap, now=AT0)
    assert result.created_decision_ids == ()
    assert any(s.code == "NON_MATERIAL" for s in result.skipped)


def test_context_matches_the_revision(orchestrator):
    result = orchestrator.process(_material_snapshot(q=210.0), now=AT0)
    decision = orchestrator.decisions.get(result.created_decision_ids[0])
    ctx = decision.context
    assert ctx.settlement_period == 24
    assert ctx.delivery_period == "SP24"
    assert ctx.forecast_vintage_id == "v2"
    assert ctx.previous_forecast_vintage_id == "v1"
    assert ctx.forecast_revision_id is not None
    assert ctx.market_snapshot_id == "book-1"
    assert ctx.optimisation_run_id == "opt-1"
    assert ctx.forecast_revision_mwh == -36.0
    assert ctx.p50_exposure_before_mwh == -46.0  # latest 164 - Q 210
    assert ctx.position_before_mwh == 210.0
    assert ctx.minutes_to_gate_closure == 85.0
    # The linked revision is retrievable and carries the previous exposures.
    revision = orchestrator.revision(ctx.forecast_revision_id)
    assert revision is not None
    assert revision.portfolio.previous_p50_exposure_mwh == -10.0  # 200 - 210


def test_recommendation_matches_the_optimiser_period(orchestrator):
    result = orchestrator.process(_material_snapshot(buy=32.0, sell=0.0), now=AT0)
    rec = orchestrator.decisions.get(result.created_decision_ids[0]).recommendation
    assert rec.action is RecommendedAction.BUY
    assert rec.buy_mwh == 32.0
    assert rec.sell_mwh == 0.0
    # Fields that are not credibly available are left null / UNAVAILABLE.
    assert rec.limit_price is None
    assert rec.confidence_score is None
    assert rec.risk_if_no_action_gbp is None
    assert any("Optimiser action" in r for r in rec.reasoning)


@pytest.mark.parametrize(
    "buy,sell,expected",
    [(32.0, 0.0, RecommendedAction.BUY), (0.0, 18.0, RecommendedAction.SELL), (0.0, 0.0, RecommendedAction.NO_ACTION)],
)
def test_recommendation_action_reflects_optimiser(orchestrator, buy, sell, expected):
    result = orchestrator.process(_material_snapshot(buy=buy, sell=sell), now=AT0)
    rec = orchestrator.decisions.get(result.created_decision_ids[0]).recommendation
    assert rec.action is expected


def test_inconsistent_buy_and_sell_is_rejected(orchestrator):
    result = orchestrator.process(_material_snapshot(buy=5.0, sell=5.0), now=AT0)
    assert result.created_decision_ids == ()
    assert any(s.code == "INCONSISTENT_BUY_SELL" for s in result.skipped)


def test_missing_optimiser_period_is_skipped(orchestrator):
    result = orchestrator.process(_material_snapshot(include_optimiser=False), now=AT0)
    assert result.created_decision_ids == ()
    assert any(s.code == "NO_MATCHING_OPTIMISER_PERIOD" for s in result.skipped)


def test_gate_closure_already_passed_is_skipped(orchestrator):
    result = orchestrator.process(
        _material_snapshot(gate_at=AT0 - timedelta(minutes=5)), now=AT0
    )
    assert result.created_decision_ids == ()
    assert any(s.code == "GATE_CLOSURE_PASSED" for s in result.skipped)


# ---------------------------------------------------------------------------
# Deduplication / idempotency
# ---------------------------------------------------------------------------


def test_repeated_refresh_is_idempotent(orchestrator):
    first = orchestrator.process(_material_snapshot(), now=AT0)
    assert len(first.created_decision_ids) == 1
    second = orchestrator.process(_material_snapshot(), now=AT0)
    assert second.created_decision_ids == ()
    assert second.duplicate_decision_ids == first.created_decision_ids


def test_new_vintage_creates_a_new_decision(orchestrator):
    first = orchestrator.process(_material_snapshot(previous_vintage="v1", latest_vintage="v2"), now=AT0)
    assert len(first.created_decision_ids) == 1
    # A new latest vintage (v3 over v2) is a different dedupe key -> new decision.
    newer = AdapterSnapshot(
        as_of=AT0,
        market_snapshot_id="book-2",
        optimisation_run_id="opt-2",
        forecast_points=(
            _vp("v2", 60, p10=150, p50=164, p90=196),
            _vp("v3", 10, p10=130, p50=150, p90=190),
        ),
        q_by_period={"SP24": 210.0},
        gate_closure_by_period={"SP24": AT0 + timedelta(minutes=85)},
        optimiser_by_sp={24: _view(24, 20.0, 0.0)},
    )
    second = orchestrator.process(newer, now=AT0)
    assert len(second.created_decision_ids) == 1
    assert second.created_decision_ids != first.created_decision_ids


# ---------------------------------------------------------------------------
# Multi-period batch
# ---------------------------------------------------------------------------


def test_multi_period_update_creates_one_batch(orchestrator):
    points = (
        _vp("v1", 60, sp=24, p10=180, p50=200, p90=220),
        _vp("v2", 10, sp=24, p10=150, p50=164, p90=196),  # material
        _vp("v1", 60, sp=25, p10=90, p50=110, p90=130),
        _vp("v2", 10, sp=25, p10=70, p50=84, p90=116),  # material
    )
    snap = AdapterSnapshot(
        as_of=AT0,
        market_snapshot_id="book-1",
        optimisation_run_id="opt-1",
        forecast_points=points,
        q_by_period={"SP24": 210.0, "SP25": 120.0},
        gate_closure_by_period={"SP24": AT0 + timedelta(minutes=85), "SP25": AT0 + timedelta(minutes=115)},
        optimiser_by_sp={24: _view(24, 32.0, 0.0), 25: _view(25, 24.0, 0.0)},
    )
    result = orchestrator.process(snap, now=AT0)
    assert len(result.created_decision_ids) == 2
    assert result.batch_id is not None
    batch = orchestrator.decisions.get_batch(result.batch_id)
    assert set(batch.decision_ids) == set(result.created_decision_ids)
    for decision_id in result.created_decision_ids:
        assert orchestrator.decisions.get(decision_id).batch_id == result.batch_id
    assert set(batch.affected_delivery_periods) == {"SP24", "SP25"}


# ---------------------------------------------------------------------------
# Point-in-time & missing previous quantiles
# ---------------------------------------------------------------------------


def test_missing_previous_quantiles_is_skipped(orchestrator):
    # Only a LATEST full point exists; no previous vintage -> the service skips.
    snap = AdapterSnapshot(
        as_of=AT0,
        forecast_points=(_vp("v_latest", 10, p10=150, p50=164, p90=196),),
        q_by_period={"SP24": 210.0},
        gate_closure_by_period={"SP24": AT0 + timedelta(minutes=85)},
        optimiser_by_sp={24: _view(24, 32.0, 0.0)},
    )
    result = orchestrator.process(snap, now=AT0)
    assert result.created_decision_ids == ()
    assert any(s.stage == "FORECAST" and s.code == "MISSING_PREVIOUS_VINTAGE" for s in result.skipped)


def test_no_future_vintage_enters_the_calculation(orchestrator):
    snap = _material_snapshot(
        previous_vintage="v1",
        latest_vintage="v2",
        extra_points=(_vp("v_future", -30, p10=100, p50=120, p90=140),),  # published after as_of
    )
    result = orchestrator.process(snap, now=AT0)
    assert len(result.created_decision_ids) == 1
    ctx = orchestrator.decisions.get(result.created_decision_ids[0]).context
    assert ctx.forecast_vintage_id == "v2"  # not the future v3
    assert ctx.previous_forecast_vintage_id == "v1"


# ---------------------------------------------------------------------------
# Trust / run-mode / immutability
# ---------------------------------------------------------------------------


def test_trust_and_run_mode_preserved(orchestrator):
    result = orchestrator.process(_material_snapshot(run_mode=RunMode.HISTORICAL_REPLAY), now=AT0)
    decision = orchestrator.decisions.get(result.created_decision_ids[0])
    ctx = decision.context
    assert ctx.run_mode is RunMode.HISTORICAL_REPLAY
    assert ctx.trustworthy_for_live_trading is False
    assert ctx.calculation_allowed is True
    assert decision.diagnostic_only is True and decision.not_executable is True
    assert result.trustworthy_for_live_trading is False and result.diagnostic_only is True


def test_stored_decision_is_frozen(orchestrator):
    result = orchestrator.process(_material_snapshot(), now=AT0)
    decision = orchestrator.decisions.get(result.created_decision_ids[0])
    with pytest.raises(ValidationError):
        decision.status = DecisionStatus.FILLED


# ---------------------------------------------------------------------------
# Rolling adapter — end-to-end with the real SAMPLE environment (isolated)
# ---------------------------------------------------------------------------


def _isolated_rolling():
    """A fresh RollingService + environment + pipeline, so tests do not mutate
    or depend on the global ROLLING singleton."""
    from cockpit.pipeline import DataFlowPipeline
    from cockpit.rolling_service import RollingService
    from cockpit.simulated_environment import SimulatedEnvironment

    rolling = RollingService(DataFlowPipeline(), SimulatedEnvironment())
    rolling.initialise()
    return rolling


def test_first_vintage_only_skips_missing_previous():
    rolling = _isolated_rolling()
    assert len(rolling.forecast_vintage_snapshots()) == 1
    orchestrator = DecisionOrchestrator(decisions=DecisionStore(), rolling=rolling)
    snap = orchestrator.build_rolling_snapshot()
    # One eligible vintage -> one point per settlement period, no previous.
    per_sp: dict[int, int] = {}
    for point in snap.forecast_points:
        per_sp[point.settlement_period] = per_sp.get(point.settlement_period, 0) + 1
        assert point.published_at <= snap.as_of
        assert point.source_mode.value == "SAMPLE"
    assert per_sp and all(count == 1 for count in per_sp.values())
    result = orchestrator.process(snap)
    assert result.created_decision_ids == ()
    assert any(s.code == "MISSING_PREVIOUS_VINTAGE" for s in result.skipped)


def test_material_sample_refresh_creates_decisions_end_to_end():
    from cockpit.models import SampleRegime

    rolling = _isolated_rolling()
    store = DecisionStore()
    orchestrator = DecisionOrchestrator(decisions=store, rolling=rolling)
    assert orchestrator.refresh().created_decision_ids == ()  # only one vintage yet

    rolling.set_regime(SampleRegime.WIND_FORECAST_MISS)  # material forecast change -> 2nd vintage
    assert len(rolling.forecast_vintage_snapshots()) == 2
    result = orchestrator.refresh()

    assert len(result.created_decision_ids) >= 1
    assert result.batch_id is not None
    batch = store.get_batch(result.batch_id)
    assert set(batch.decision_ids) == set(result.created_decision_ids)
    decision = store.get(result.created_decision_ids[0])
    # The decision links to a retrievable supporting revision.
    assert orchestrator.revision(decision.context.forecast_revision_id) is not None
    assert decision.context.run_mode is RunMode.SAMPLE_DEMO
    assert decision.context.trustworthy_for_live_trading is False


def test_non_material_sample_refresh_creates_none():
    rolling = _isolated_rolling()
    orchestrator = DecisionOrchestrator(decisions=DecisionStore(), rolling=rolling)
    orchestrator.refresh()  # one vintage
    rolling.refresh()  # second vintage, same regime/step -> no material change
    assert len(rolling.forecast_vintage_snapshots()) == 2
    result = orchestrator.refresh()
    assert result.created_decision_ids == ()
    assert any(s.code == "NON_MATERIAL" for s in result.skipped)


def test_repeated_sample_refresh_is_idempotent():
    from cockpit.models import SampleRegime

    rolling = _isolated_rolling()
    rolling.set_regime(SampleRegime.WIND_FORECAST_MISS)
    store = DecisionStore()
    orchestrator = DecisionOrchestrator(decisions=store, rolling=rolling)
    first = orchestrator.refresh()
    assert len(first.created_decision_ids) >= 1
    second = orchestrator.refresh()  # rolling state unchanged
    assert second.created_decision_ids == ()
    assert set(second.duplicate_decision_ids) == set(first.created_decision_ids)


def test_environment_reset_clears_vintage_state():
    from cockpit.models import SampleRegime

    rolling = _isolated_rolling()
    rolling.set_regime(SampleRegime.WIND_FORECAST_MISS)
    assert len(rolling.forecast_vintage_snapshots()) == 2
    rolling.reset()
    # Reset clears retained vintages and re-seeds a clean deterministic initial one.
    assert len(rolling.forecast_vintage_snapshots()) == 1
