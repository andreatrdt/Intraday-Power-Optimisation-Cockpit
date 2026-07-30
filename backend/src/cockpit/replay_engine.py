"""Point-in-time replay event loop and orchestration (Milestone 8).

Drives the **existing production services** — decision orchestrator, hedge-timing,
execution simulator, settlement and evaluation — across many episodes without
look-ahead. Every time-dependent read goes through the injected replay clock and the
:class:`~cockpit.replay_dataset.PointInTimeView` look-ahead guard. Each run uses fully
isolated stores; the global cockpit singletons are never touched.

Reuse seam: the guard produces a point-in-time ``AdapterSnapshot`` for
``DecisionOrchestrator.process`` and a duck-typed rolling adapter for the timing +
execution services, so the replay economics are identical to the live SAMPLE workflow.

Event schedule (deterministic; wall-clock ``now()`` is never read here):

* ``decision_time`` — the latest forecast vintage's publication time. Decisions are
  created for every eligible period and the trader policy acts once.
* ``reassessment checkpoints`` (TIMING_POLICY only) — at fixed time-to-gate offsets
  (:data:`REASSESS_MINUTES_TO_GATE`) after ``decision_time``; a still-waiting decision
  is re-assessed and may submit. A decision that has already submitted is never
  re-submitted (duplicate-order guard).
* ``gate closure`` — a decision that never submitted is rejected (a realised no-trade).
* ``delivery_end`` — the period is delivered, settled and evaluated from realised data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace

from cockpit.decision_models import DecisionStatus, TradeDecision
from cockpit.decision_orchestrator import AdapterSnapshot, DecisionOrchestrator, OptimiserPeriodView
from cockpit.decision_prioritisation import HedgeTimingService, assess_timing, build_timing_market_view
from cockpit.decision_service import DecisionStore, DecisionValidationError, StaleDecisionError
from cockpit.evaluation_service import EvaluationService
from cockpit.execution_models import ExecutionConfig, ExecutionMode
from cockpit.execution_service import ExecutionService
from cockpit.forecast_revision import VintageForecastPoint
from cockpit.decision_models import RunMode
from cockpit.hedge_timing_models import TimingVerdict
from cockpit.replay_dataset import LookAheadViolation, PointInTimeView, ReplayDataset
from cockpit.replay_models import (
    DatasetPeriodRecord,
    EpisodeSkipReason,
    IntegrityReport,
    IntegrityStatus,
    LifecyclePath,
    ReplayEpisodeResult,
    ReplayMode,
    ReplayRun,
    ReplayStatus,
    TraderPolicy,
    new_episode_id,
    new_replay_run_id,
)
from cockpit.settlement_models import ProcessSkipReason, RealisedInputs
from cockpit.settlement_service import SettlementInputError, SettlementService

# TIMING_POLICY reassessment schedule: minutes-to-gate at which a still-waiting
# decision is re-assessed. Documented and deterministic.
REASSESS_MINUTES_TO_GATE = (90, 60, 30, 10)
DEFAULT_MAX_PERIODS = 200
_TOL = 1e-6


class ReplayLimitExceeded(Exception):
    """The dataset/window exceeds the configured bounded-run limit."""


@dataclass(frozen=True)
class ReplayConfig:
    dataset: ReplayDataset
    run_mode: ReplayMode
    trader_policy: TraderPolicy
    execution_mode: ExecutionMode
    replay_start: datetime
    replay_end: datetime
    timing_policy_version: str
    simulator_version: str
    materiality_config_ref: str
    max_periods: int | None = DEFAULT_MAX_PERIODS


@dataclass
class ReplayResult:
    run: ReplayRun
    episodes: list[ReplayEpisodeResult]
    integrity: IntegrityReport


# ---------------------------------------------------------------------------
# Injected replay clock + guard-routed rolling adapter
# ---------------------------------------------------------------------------


class _Clock:
    """A mutable, explicitly-advanced replay clock. Never wall-clock now()."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def set(self, t: datetime) -> None:
        self._t = t


def _level(level) -> SimpleNamespace:
    # Duck-typed order-book level: exposes exactly what the timing + execution
    # services read (side/level/price/volume).
    return SimpleNamespace(side=level.side, level=level.level, price_gbp_per_mwh=level.price_gbp_per_mwh, volume_mwh=level.volume_mwh)


class _PitRollingAdapter:
    """Duck-typed, guard-routed stand-in for the rolling service.

    Exposes only what HedgeTimingService + ExecutionService read, all sourced through
    the point-in-time view (so every market read is look-ahead-guarded)."""

    def __init__(self, view: PointInTimeView) -> None:
        self._view = view

    def live_state(self):
        periods = self._view.active_market_periods()
        snapshot_id = periods[0].market_snapshot_id if periods else None
        return SimpleNamespace(state=SimpleNamespace(
            current_time=self._view.now,
            current_market_snapshot_id=snapshot_id,
            current_forecast_vintage_id=None,
        ))

    def current_optimisation(self):
        trajectory = [
            SimpleNamespace(
                settlement_period=p.settlement_period, delivery_period=p.delivery_period,
                buy_mwh=p.recommended_buy_mwh, sell_mwh=p.recommended_sell_mwh, charge_mw=0.0, discharge_mw=0.0,
                best_bid_gbp_per_mwh=(p.bids[0].price_gbp_per_mwh if p.bids else None),
                best_ask_gbp_per_mwh=(p.asks[0].price_gbp_per_mwh if p.asks else None),
                reference_price_gbp_per_mwh=p.reference_price_gbp_per_mwh, tradeable=p.tradeable,
            )
            for p in self._view.active_market_periods()
        ]
        return SimpleNamespace(run_id="replay-optimisation", projected_trajectory=trajectory)

    @property
    def period_inputs(self):
        return [self._period_input(p) for p in self._view.active_market_periods()]

    @staticmethod
    def _period_input(p: DatasetPeriodRecord) -> SimpleNamespace:
        return SimpleNamespace(
            settlement_period=p.settlement_period, delivery_period=p.delivery_period,
            delivery_start=p.delivery_start, delivery_end=p.delivery_end,
            contracted_q_mwh=p.contracted_q_mwh, gate_closure_at=p.gate_closure_at, tradeable=p.tradeable,
            reference_price_gbp_per_mwh=p.reference_price_gbp_per_mwh,
            bids=[_level(x) for x in p.bids], asks=[_level(x) for x in p.asks],
        )


class _ReplayRealisedInputsProvider:
    """Settlement realised-inputs provider that sources realised generation + dual
    imbalance prices from the DATASET via the look-ahead guard (never before delivery),
    combined with the decision's own executed volumes/price/fees and initial position."""

    def __init__(self, view: PointInTimeView, run_mode: str, *, config: ExecutionConfig) -> None:
        self._view = view
        self._run_mode = run_mode
        self.config = config  # execution config; the evaluation service reads its fee rate

    def realised_for(self, decision: TradeDecision, *, now: datetime, require_period_ended: bool = True) -> RealisedInputs:
        ctx = decision.context
        if require_period_ended and now < ctx.delivery_end:
            raise SettlementInputError(
                ProcessSkipReason.DELIVERY_PERIOD_NOT_ENDED,
                f"Delivery period ends at {ctx.delivery_end.isoformat()}.",
            )
        period = self._view.realised_period(decision.settlement_period)  # guarded read
        execution = decision.execution_result
        executed_buy = execution.executed_buy_mwh if execution else 0.0
        executed_sell = execution.executed_sell_mwh if execution else 0.0
        avg_price = execution.average_execution_price if execution else None
        fees = (execution.execution_fees_gbp or 0.0) if execution else 0.0
        return RealisedInputs(
            decision_id=decision.decision_id, settlement_period=decision.settlement_period,
            delivery_start=ctx.delivery_start, delivery_end=ctx.delivery_end,
            delivered_at=(decision.settlement_result.delivered_at if decision.settlement_result and decision.settlement_result.delivered_at else now),
            realised_generation_mwh=period.realised_generation_mwh,
            initial_contracted_position_mwh=ctx.position_before_mwh if ctx.position_before_mwh is not None else period.contracted_q_mwh,
            executed_buy_mwh=executed_buy, executed_sell_mwh=executed_sell,
            average_execution_price_gbp_per_mwh=avg_price, execution_fees_gbp=fees,
            imbalance_buy_price_gbp_per_mwh=period.imbalance_buy_price_gbp_per_mwh,
            imbalance_sell_price_gbp_per_mwh=period.imbalance_sell_price_gbp_per_mwh,
            reference_market_price_gbp_per_mwh=period.realised_reference_price_gbp_per_mwh,
            source_mode=period.source_mode, quality=period.quality,
            lineage_ids=period.lineage_value_ids, run_mode=self._run_mode,
        )


# ---------------------------------------------------------------------------
# Isolated per-run context
# ---------------------------------------------------------------------------


@dataclass
class _ReplayContext:
    store: DecisionStore
    orchestrator: DecisionOrchestrator
    timing: HedgeTimingService
    execution: ExecutionService
    settlement: SettlementService
    evaluation: EvaluationService
    adapter: _PitRollingAdapter
    view: PointInTimeView


def _new_context(dataset: ReplayDataset, config: ReplayConfig, clock: _Clock) -> _ReplayContext:
    view = PointInTimeView(dataset, clock)
    adapter = _PitRollingAdapter(view)
    store = DecisionStore()
    exec_config = ExecutionConfig(simulator_version=config.simulator_version) if config.simulator_version else ExecutionConfig()
    orchestrator = DecisionOrchestrator(decisions=store, rolling=adapter)
    timing = HedgeTimingService(decisions=store, revisions=orchestrator, rolling=adapter)
    execution = ExecutionService(decisions=store, rolling=adapter, config=exec_config)
    provider = _ReplayRealisedInputsProvider(view, config.run_mode.value, config=exec_config)
    settlement = SettlementService(decisions=store, provider=provider)
    evaluation = EvaluationService(decisions=store, settlement=settlement)
    return _ReplayContext(store, orchestrator, timing, execution, settlement, evaluation, adapter, view)


# ---------------------------------------------------------------------------
# Snapshot builder (guard-routed → orchestrator.process)
# ---------------------------------------------------------------------------


def _build_snapshot(view: PointInTimeView) -> AdapterSnapshot:
    vintage_points = view.vintage_points_asof()  # guarded: published_at <= clock
    forecast_points = tuple(
        VintageForecastPoint(
            vintage_id=v.vintage_id, published_at=v.published_at, settlement_period=v.settlement_period,
            delivery_period=v.delivery_period, delivery_start=v.delivery_start, delivery_end=v.delivery_end,
            p10_mwh=v.p10_mwh, p50_mwh=v.p50_mwh, p90_mwh=v.p90_mwh, unit=v.unit,
            source_mode=v.source_mode, quality=v.quality, lineage_value_ids=v.lineage_value_ids,
        )
        for v in vintage_points
    )
    periods = view.active_market_periods()
    q_by_period = {p.delivery_period: p.contracted_q_mwh for p in periods}
    gate_by_period = {p.delivery_period: p.gate_closure_at for p in periods}
    optimiser_by_sp = {
        p.settlement_period: OptimiserPeriodView(
            settlement_period=p.settlement_period, delivery_period=p.delivery_period,
            buy_mwh=p.recommended_buy_mwh, sell_mwh=p.recommended_sell_mwh,
            best_bid_gbp_per_mwh=(p.bids[0].price_gbp_per_mwh if p.bids else None),
            best_ask_gbp_per_mwh=(p.asks[0].price_gbp_per_mwh if p.asks else None),
            reference_price_gbp_per_mwh=p.reference_price_gbp_per_mwh, tradeable=p.tradeable,
        )
        for p in periods
    }
    return AdapterSnapshot(
        as_of=view.now, run_mode=RunMode.SAMPLE_DEMO,
        market_snapshot_id=(periods[0].market_snapshot_id if periods else None),
        optimisation_run_id="replay-optimisation",
        forecast_points=forecast_points, q_by_period=q_by_period,
        gate_closure_by_period=gate_by_period, optimiser_by_sp=optimiser_by_sp,
    )


# ---------------------------------------------------------------------------
# Per-decision episode bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Episode:
    settlement_period: int
    delivery_period: str
    period: DatasetPeriodRecord
    decision_id: str | None = None
    submitted: bool = False
    order_ids: list[str] = field(default_factory=list)
    timing_ids: list[str] = field(default_factory=list)
    delivered: bool = False
    evaluated: bool = False
    skip_reason: EpisodeSkipReason | None = None
    timing_verdict: str | None = None
    timing_priority: str | None = None
    trader_policy_action: str | None = None


_DELIVERABLE = frozenset({
    DecisionStatus.FILLED, DecisionStatus.PARTIALLY_FILLED, DecisionStatus.EXPIRED,
    DecisionStatus.REJECTED, DecisionStatus.CANCELLED,
})
_PATH_BY_STATUS = {
    DecisionStatus.FILLED: LifecyclePath.FILLED,
    DecisionStatus.PARTIALLY_FILLED: LifecyclePath.PARTIALLY_FILLED,
    DecisionStatus.EXPIRED: LifecyclePath.EXPIRED,
    DecisionStatus.REJECTED: LifecyclePath.REJECTED,
    DecisionStatus.CANCELLED: LifecyclePath.REJECTED,
}


def _lifecycle_path(decision: TradeDecision | None) -> LifecyclePath | None:
    """The pre-delivery execution-terminal path (FILLED/PARTIALLY_FILLED/EXPIRED/
    REJECTED/NO_TRADE), read from the transition history — not the current status,
    which after evaluation is EVALUATED."""
    if decision is None:
        return None
    for transition in decision.transitions:
        if transition.to_status is DecisionStatus.DELIVERED and transition.from_status is not None:
            return _PATH_BY_STATUS.get(transition.from_status)
    if decision.execution_result is not None:
        return _PATH_BY_STATUS.get(DecisionStatus(decision.execution_result.status.value), LifecyclePath.NO_TRADE)
    if any(t.to_status is DecisionStatus.REJECTED for t in decision.transitions):
        return LifecyclePath.REJECTED
    return LifecyclePath.NO_TRADE


def _has_volume(rec) -> bool:
    return (rec.buy_mwh or 0.0) > _TOL or (rec.sell_mwh or 0.0) > _TOL


def _accept_and_submit(ctx: _ReplayContext, config: ReplayConfig, ep: _Episode, now: datetime) -> None:
    try:
        if ctx.store.get(ep.decision_id).status is DecisionStatus.PROPOSED:
            ctx.store.accept(ep.decision_id, at=now)
        outcome = ctx.execution.submit_simulated(ep.decision_id, mode=config.execution_mode, now=now)
        ep.submitted = True
        ep.order_ids.append(outcome.order.order_id)
    except (DecisionValidationError, StaleDecisionError, ValueError):
        pass  # zero volume / gate passed → left for gate enforcement (no-trade)


def _modify_and_submit(ctx: _ReplayContext, config: ReplayConfig, ep: _Episode, now: datetime, buy: float, sell: float) -> None:
    rec = ctx.store.get(ep.decision_id).recommendation
    same = abs((buy or 0.0) - rec.buy_mwh) <= _TOL and abs((sell or 0.0) - rec.sell_mwh) <= _TOL
    try:
        if same:
            ctx.store.accept(ep.decision_id, at=now)
        else:
            ctx.store.modify(ep.decision_id, buy_mwh=buy, sell_mwh=sell, rationale="Replay TIMING_POLICY partial-now volume.", at=now)
        outcome = ctx.execution.submit_simulated(ep.decision_id, mode=config.execution_mode, now=now)
        ep.submitted = True
        ep.order_ids.append(outcome.order.order_id)
    except (DecisionValidationError, StaleDecisionError, ValueError):
        pass


def _timing_action(ctx: _ReplayContext, config: ReplayConfig, ep: _Episode, now: datetime) -> None:
    decision = ctx.store.get(ep.decision_id)
    # Reassess against the CURRENT time-to-gate. The stored decision's frozen
    # minutes_to_gate_closure was set at creation; assess a copy carrying the current
    # value so urgency rises as the clock advances (WAIT → HEDGE_NOW). The stored
    # decision is never mutated.
    gate = decision.context.gate_closure_at
    assess_decision = decision
    if gate is not None:
        minutes = round((gate - now).total_seconds() / 60.0, 2)
        assess_decision = decision.model_copy(update={"context": decision.context.model_copy(update={"minutes_to_gate_closure": minutes})})
    market_view = build_timing_market_view(assess_decision, ctx.adapter)
    signals = ctx.timing.signals_for(decision)
    stored, _ = ctx.timing.assess(assess_decision, market_view, signals, now=now)
    ep.timing_ids.append(stored.assessment_id)
    ep.timing_verdict = stored.verdict.value
    ep.timing_priority = stored.priority.value
    verdict = stored.verdict
    if verdict is TimingVerdict.HEDGE_NOW:
        _accept_and_submit(ctx, config, ep, now)
        ep.trader_policy_action = "ACCEPT_SUBMIT" if ep.submitted else "WAIT"
    elif verdict is TimingVerdict.PARTIAL_HEDGE_NOW:
        now_buy = stored.recommended_now_buy_mwh
        now_sell = stored.recommended_now_sell_mwh
        if (now_buy or 0.0) > _TOL or (now_sell or 0.0) > _TOL:
            _modify_and_submit(ctx, config, ep, now, now_buy, now_sell)
            ep.trader_policy_action = "MODIFY_SUBMIT" if ep.submitted else "WAIT"
        else:
            ep.trader_policy_action = "WAIT"
    elif verdict is TimingVerdict.WAIT:
        ep.trader_policy_action = "WAIT"
    else:  # NO_ACTION
        ctx.store.reject(ep.decision_id, rationale="Replay TIMING_POLICY: timing verdict NO_ACTION.", at=now)
        ep.trader_policy_action = "REJECT"


def _apply_policy(ctx: _ReplayContext, config: ReplayConfig, ep: _Episode, now: datetime) -> None:
    policy = config.trader_policy
    if policy is TraderPolicy.NO_ACTION:
        ctx.store.reject(ep.decision_id, rationale="Replay NO_ACTION policy: recommendation left unexecuted.", at=now)
        ep.trader_policy_action = "REJECT"
        return
    if policy is TraderPolicy.MODEL_FOLLOW:
        decision = ctx.store.get(ep.decision_id)
        if _has_volume(decision.recommendation):
            _accept_and_submit(ctx, config, ep, now)
            ep.trader_policy_action = "ACCEPT_SUBMIT" if ep.submitted else "NO_TRADE"
        else:
            ep.trader_policy_action = "NO_TRADE"  # zero recommendation → gate rejects as no-trade
        return
    _timing_action(ctx, config, ep, now)  # TIMING_POLICY


def _create_and_act(ctx: _ReplayContext, config: ReplayConfig, episodes: dict[int, _Episode], *, now: datetime) -> None:
    result = ctx.orchestrator.process(_build_snapshot(ctx.view), now=now)
    for did in result.created_decision_ids:
        decision = ctx.store.get(did)
        ep = episodes.get(decision.settlement_period)
        if ep is not None:
            ep.decision_id = did
    for ep in episodes.values():
        if ep.decision_id is None:
            ep.skip_reason = EpisodeSkipReason.NO_MATERIAL_REVISION
        else:
            _apply_policy(ctx, config, ep, now)


def _reassess(ctx: _ReplayContext, config: ReplayConfig, episodes: dict[int, _Episode], *, now: datetime) -> None:
    if config.trader_policy is not TraderPolicy.TIMING_POLICY:
        return
    for ep in episodes.values():
        if ep.decision_id is None or ep.submitted:
            continue
        decision = ctx.store.get(ep.decision_id)
        if decision.status is not DecisionStatus.PROPOSED:
            continue  # already rejected/actioned
        gate = decision.context.gate_closure_at
        if gate is not None and now >= gate:
            continue  # gate enforcement handles this
        _timing_action(ctx, config, ep, now)  # duplicate-order safe: only acts on un-submitted PROPOSED


def _enforce_gate(ctx: _ReplayContext, episodes: dict[int, _Episode], *, now: datetime) -> None:
    for ep in episodes.values():
        if ep.decision_id is None or ep.submitted:
            continue
        decision = ctx.store.get(ep.decision_id)
        if decision.status not in (DecisionStatus.PROPOSED, DecisionStatus.ACCEPTED, DecisionStatus.MODIFIED):
            continue
        gate = decision.context.gate_closure_at
        if gate is not None and now >= gate:
            ctx.store.reject(ep.decision_id, rationale="Gate Closure reached without a submission (replay no-trade).", at=now)


def _deliver_settle_evaluate(ctx: _ReplayContext, episodes: dict[int, _Episode], *, now: datetime) -> None:
    for ep in episodes.values():
        if ep.decision_id is None or ep.evaluated:
            continue
        decision = ctx.store.get(ep.decision_id)
        if now < decision.context.delivery_end or decision.status not in _DELIVERABLE:
            continue
        try:
            ctx.settlement.deliver(ep.decision_id, now=now)
            ctx.settlement.settle(ep.decision_id, now=now)
            ctx.evaluation.evaluate(ep.decision_id, now=now)
            ep.delivered = True
            ep.evaluated = True
        except SettlementInputError as error:
            ep.skip_reason = (
                EpisodeSkipReason.MISSING_REALISED_GENERATION
                if error.reason is ProcessSkipReason.MISSING_REALISED_GENERATION
                else EpisodeSkipReason.MISSING_SETTLEMENT_PRICES
            )
        except LookAheadViolation:
            ep.skip_reason = EpisodeSkipReason.MISSING_REALISED_GENERATION


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_replay(config: ReplayConfig) -> ReplayResult:
    """Execute one deterministic point-in-time replay; returns run + episodes + integrity."""
    dataset = config.dataset
    validation = dataset.validate()
    run_id = new_replay_run_id()

    window = sorted(
        (p for p in dataset.periods if p.delivery_start >= config.replay_start and p.delivery_end <= config.replay_end),
        key=lambda p: p.settlement_period,
    )
    limit = config.max_periods or DEFAULT_MAX_PERIODS
    if len(window) > limit:
        raise ReplayLimitExceeded(f"{len(window)} periods in window exceeds max_periods={limit}.")

    if validation.status is IntegrityStatus.DATASET_INVALID:
        integrity = IntegrityReport(
            status=IntegrityStatus.DATASET_INVALID, lookahead_violation_count=0,
            skipped_data_reasons=validation.issues, dataset_validation_status=IntegrityStatus.DATASET_INVALID,
        )
        return ReplayResult(_build_run(config, run_id, [], integrity, status=ReplayStatus.FAILED), [], integrity)

    if not window:
        integrity = IntegrityReport(status=IntegrityStatus.OK, lookahead_violation_count=0)
        return ReplayResult(_build_run(config, run_id, [], integrity), [], integrity)

    window_sps = {p.settlement_period for p in window}
    decision_time = max(v.published_at for v in dataset.vintages if v.settlement_period in window_sps)

    clock = _Clock(decision_time)
    ctx = _new_context(dataset, config, clock)
    episodes: dict[int, _Episode] = {p.settlement_period: _Episode(p.settlement_period, p.delivery_period, p) for p in window}

    events: set[datetime] = {decision_time}
    for p in window:
        events.add(p.gate_closure_at)
        events.add(p.delivery_end)
        if config.trader_policy is TraderPolicy.TIMING_POLICY:
            for minutes in REASSESS_MINUTES_TO_GATE:
                checkpoint = p.gate_closure_at - timedelta(minutes=minutes)
                if checkpoint > decision_time:
                    events.add(checkpoint)

    for t in sorted(events):
        clock.set(t)
        if t == decision_time:
            _create_and_act(ctx, config, episodes, now=t)
        else:
            _reassess(ctx, config, episodes, now=t)
        _enforce_gate(ctx, episodes, now=t)
        _deliver_settle_evaluate(ctx, episodes, now=t)

    results = sorted(
        (_build_episode_result(ctx, run_id, ep) for ep in episodes.values()),
        key=lambda e: e.settlement_period,
    )
    integrity = _build_integrity(ctx, validation)
    return ReplayResult(_build_run(config, run_id, results, integrity), results, integrity)


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _build_episode_result(ctx: _ReplayContext, run_id: str, ep: _Episode) -> ReplayEpisodeResult:
    decision = ctx.store.get(ep.decision_id) if ep.decision_id else None
    settlement = ctx.settlement.settlement_for_decision(ep.decision_id) if ep.decision_id else None
    delivery = ctx.settlement.delivery_for_decision(ep.decision_id) if ep.decision_id else None
    evaluation = ctx.evaluation.evaluation_for_decision(ep.decision_id) if ep.decision_id else None
    outcome = ctx.execution.latest_outcome_for_decision(ep.decision_id) if ep.decision_id else None

    by_name = {b.benchmark_name.value: b for b in (evaluation.benchmark_results if evaluation else ())}
    execution_result = decision.execution_result if decision else None
    lifecycle_path = _lifecycle_path(decision)

    horizon = None
    if decision is not None:
        horizon = round((decision.context.delivery_start - decision.context.as_of).total_seconds() / 60.0, 2)

    return ReplayEpisodeResult(
        episode_id=new_episode_id(run_id, ep.settlement_period),
        replay_run_id=run_id,
        settlement_period=ep.settlement_period,
        delivery_period=ep.delivery_period,
        decision_ids=(ep.decision_id,) if ep.decision_id else (),
        revision_ids=((decision.context.forecast_revision_id,) if decision and decision.context.forecast_revision_id else ()),
        timing_assessment_ids=tuple(ep.timing_ids),
        simulated_order_ids=tuple(ep.order_ids),
        delivery_id=delivery.delivery_id if delivery else None,
        settlement_id=settlement.settlement_id if settlement else None,
        evaluation_id=evaluation.evaluation_id if evaluation else None,
        lifecycle_path=lifecycle_path,
        skip_reason=ep.skip_reason,
        realised_incremental_pnl_gbp=settlement.realised_pnl_gbp if settlement else None,
        total_realised_cashflow_gbp=settlement.total_realised_cashflow_gbp if settlement else None,
        no_action_pnl_gbp=(_bench(by_name, "NO_ACTION") if evaluation else None),
        model_pnl_gbp=_bench(by_name, "MODEL_RECOMMENDATION") if evaluation else None,
        trader_pnl_gbp=_bench(by_name, "TRADER_INSTRUCTION") if evaluation else None,
        perfect_foresight_pnl_gbp=_bench(by_name, "PERFECT_FORESIGHT") if evaluation else None,
        regret_vs_model_gbp=evaluation.regret_vs_model_recommendation_gbp if evaluation else None,
        regret_vs_perfect_foresight_gbp=evaluation.regret_vs_perfect_foresight_gbp if evaluation else None,
        executed_buy_mwh=execution_result.executed_buy_mwh if execution_result else 0.0,
        executed_sell_mwh=execution_result.executed_sell_mwh if execution_result else 0.0,
        unfilled_mwh=execution_result.unfilled_volume_mwh if execution_result else 0.0,
        average_execution_price_gbp_per_mwh=execution_result.average_execution_price if execution_result else None,
        fees_gbp=outcome.total_fees_gbp if outcome else 0.0,
        slippage_gbp=outcome.total_slippage_gbp if outcome else 0.0,
        levels_consumed=outcome.levels_consumed if outcome else 0,
        timing_verdict=ep.timing_verdict,
        timing_priority=ep.timing_priority,
        recommended_action=decision.recommendation.action.value if decision else None,
        forecast_horizon_minutes=horizon,
        trader_policy_action=ep.trader_policy_action,
        warnings=(settlement.warnings if settlement else ()),
        lineage_ids=ep.period.lineage_value_ids,
    )


def _bench(by_name: dict, name: str) -> float | None:
    b = by_name.get(name)
    return b.incremental_pnl_vs_no_action_gbp if b else None


def _build_integrity(ctx: _ReplayContext, validation) -> IntegrityReport:
    violations = tuple(ctx.view.violations)
    status = IntegrityStatus.VIOLATIONS_DETECTED if violations else IntegrityStatus.OK
    return IntegrityReport(
        status=status,
        lookahead_violation_count=len(violations),
        violations=violations,
        dataset_validation_status=validation.status,
    )


def _build_run(config: ReplayConfig, run_id: str, episodes: list[ReplayEpisodeResult], integrity: IntegrityReport, *, status: ReplayStatus = ReplayStatus.COMPLETED) -> ReplayRun:
    evaluated = [e for e in episodes if e.evaluation_id is not None]
    submitted = [e for e in episodes if e.simulated_order_ids]
    return ReplayRun(
        replay_run_id=run_id,
        created_at=config.replay_start,
        dataset_id=config.dataset.dataset_id,
        run_mode=config.run_mode,
        source_mode=config.dataset.source_mode,
        quality=config.dataset.quality,
        replay_start=config.replay_start,
        replay_end=config.replay_end,
        trader_policy=config.trader_policy,
        execution_mode=config.execution_mode,
        timing_policy_version=config.timing_policy_version,
        simulator_version=config.simulator_version,
        materiality_config_ref=config.materiality_config_ref,
        assumptions=(
            "SAMPLE deterministic dataset; realised generation from the dataset, not live." if config.run_mode is ReplayMode.SAMPLE_REPLAY
            else "Historical dataset inputs.",
            f"Reassessment checkpoints (min-to-gate): {REASSESS_MINUTES_TO_GATE}.",
        ),
        decision_count=sum(1 for e in episodes if e.decision_ids),
        evaluated_count=len(evaluated),
        submitted_count=len(submitted),
        skipped_count=sum(1 for e in episodes if e.skip_reason is not None),
        lookahead_violation_count=integrity.lookahead_violation_count,
        max_periods=config.max_periods,
        status=status,
        integrity_status=integrity.status,
    )
