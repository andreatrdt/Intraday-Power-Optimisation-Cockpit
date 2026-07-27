"""Hedge-timing storage, deduplication, ranking and batch summaries (M4).

Coordinates the pure policy in :mod:`cockpit.hedge_timing` with the decision
store and forecast revisions. Provides a **prioritised, capped** view so a trader
is never shown dozens of equal-priority items, plus lightweight
:class:`DecisionBatchSummary` records for presentation.

No timing logic lives here — only building the market view, deduplication,
ranking and summarisation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from cockpit.decision_models import TradeDecision
from cockpit.decision_orchestrator import ORCHESTRATOR
from cockpit.decision_service import DECISIONS, DecisionStore
from cockpit.hedge_timing import assess_timing
from cockpit.hedge_timing_models import (
    AssessTimingResult,
    DecisionBatchSummary,
    HedgeTimingAssessment,
    PrioritisedItem,
    TimingConfig,
    TimingMarketView,
    TimingPriority,
    TimingRevisionSignals,
    TimingVerdict,
    priority_rank,
)
from cockpit.liquidity import executable_price

_ACTION_TOLERANCE_MWH = 1e-6


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Market adapter (reads rolling state; kept out of the pure policy)
# ---------------------------------------------------------------------------


def build_timing_market_view(decision: TradeDecision, rolling) -> TimingMarketView:
    """Build the observable executable-market inputs for a decision's period.

    Returns an ``available=False`` view when the decision's settlement period is
    not in the current horizon — the policy then explicitly declines to justify
    acting now, rather than guessing.
    """
    live = rolling.live_state()
    run = rolling.current_optimisation()
    ctx = decision.context
    rec = decision.recommendation
    market_snapshot_id = live.state.current_market_snapshot_id
    optimisation_run_id = run.run_id

    if rec.buy_mwh > rec.sell_mwh + _ACTION_TOLERANCE_MWH:
        side, required = "BUY", round(rec.buy_mwh - rec.sell_mwh, 6)
    elif rec.sell_mwh > rec.buy_mwh + _ACTION_TOLERANCE_MWH:
        side, required = "SELL", round(rec.sell_mwh - rec.buy_mwh, 6)
    else:
        side, required = "NONE", 0.0

    period = next((p for p in rolling.period_inputs if p.settlement_period == ctx.settlement_period), None)
    if period is None:
        return TimingMarketView(
            settlement_period=ctx.settlement_period,
            delivery_period=ctx.delivery_period,
            market_snapshot_id=market_snapshot_id,
            optimisation_run_id=optimisation_run_id,
            available=False,
            tradeable=False,
            recommended_side=side,
            required_volume_mwh=required,
        )

    best_bid = period.bids[0].price_gbp_per_mwh if period.bids else None
    best_ask = period.asks[0].price_gbp_per_mwh if period.asks else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    bid_depth = round(sum(level.volume_mwh for level in period.bids), 6)
    ask_depth = round(sum(level.volume_mwh for level in period.asks), 6)

    executable_volume = None
    wap = None
    slippage = None
    if side in ("BUY", "SELL") and required > 0:
        result = executable_price([*period.bids, *period.asks], required, side, max_levels=max(len(period.bids), len(period.asks)))
        executable_volume = round(result.executable_volume_mwh, 6)
        wap = result.wap_gbp_per_mwh
        if wap is not None:
            slippage = round((wap - best_ask) if side == "BUY" else (best_bid - wap), 6)

    return TimingMarketView(
        settlement_period=ctx.settlement_period,
        delivery_period=ctx.delivery_period,
        market_snapshot_id=market_snapshot_id,
        optimisation_run_id=optimisation_run_id,
        available=True,
        tradeable=period.tradeable,
        recommended_side=side,
        required_volume_mwh=required,
        best_bid_gbp_per_mwh=best_bid,
        best_ask_gbp_per_mwh=best_ask,
        spread_gbp_per_mwh=round(spread, 6) if spread is not None else None,
        bid_depth_mwh=bid_depth,
        ask_depth_mwh=ask_depth,
        executable_volume_mwh=executable_volume,
        wap_gbp_per_mwh=round(wap, 6) if wap is not None else None,
        wap_slippage_gbp_per_mwh=slippage,
    )


# ---------------------------------------------------------------------------
# Store + service
# ---------------------------------------------------------------------------

# Assessment deduplication key: an assessment is a duplicate when a stored one
# already exists for the same decision under the same market snapshot, optimiser
# run and timing-policy version. A new market snapshot or optimiser run (or a
# policy-version bump) yields a new assessment.
AssessmentDedupeKey = tuple[str, str | None, str | None, str]


def assessment_dedupe_key(assessment: HedgeTimingAssessment) -> AssessmentDedupeKey:
    return (
        assessment.decision_id,
        assessment.market_snapshot_id,
        assessment.optimisation_run_id,
        assessment.policy_version,
    )


class HedgeTimingService:
    """Assess, deduplicate, store, rank and summarise hedge-timing assessments."""

    def __init__(
        self,
        *,
        decisions: DecisionStore | None = None,
        revisions=None,
        config: TimingConfig | None = None,
        rolling=None,
    ) -> None:
        self.decisions = decisions or DECISIONS
        self.revisions = revisions or ORCHESTRATOR
        self.config = config or TimingConfig()
        self._rolling = rolling
        self._assessments: dict[str, HedgeTimingAssessment] = {}
        self._order: list[str] = []
        self._by_key: dict[AssessmentDedupeKey, str] = {}
        self._by_decision: dict[str, list[str]] = {}
        self._lock = RLock()

    # -- assessment ---------------------------------------------------------

    def assess(
        self,
        decision: TradeDecision,
        market_view: TimingMarketView,
        signals: TimingRevisionSignals | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[HedgeTimingAssessment, bool]:
        """Assess one decision. Returns (assessment, is_new); idempotent by key."""
        with self._lock:
            candidate = assess_timing(decision, market_view, signals, self.config, now=now)
            key = assessment_dedupe_key(candidate)
            if key in self._by_key:
                return self._assessments[self._by_key[key]], False
            self._store(candidate)
            return candidate, True

    def signals_for(self, decision: TradeDecision) -> TimingRevisionSignals | None:
        revision_id = decision.context.forecast_revision_id
        revision = self.revisions.revision(revision_id) if revision_id else None
        if revision is None:
            return None
        return TimingRevisionSignals(
            revision_magnitude_mwh=revision.comparison.absolute_revision_mwh,
            revision_significance_score=revision.significance.revision_significance_score,
            revision_z_score=revision.significance.revision_z_score,
            calibration_basis=revision.significance.calibration_basis.value,
            uncertainty_width_change_mwh=revision.comparison.uncertainty_width_change_mwh,
            direction_flip=revision.portfolio.crossed_zero_exposure,
            materiality_reasons=revision.materiality.materiality_reasons,
        )

    def assess_from_rolling(
        self, decision_ids: list[str] | None = None, *, rolling=None, now: datetime | None = None
    ) -> AssessTimingResult:
        rolling = rolling or self._resolve_rolling()
        assessed_at = now or _utcnow()
        if decision_ids is None:
            decisions = self.decisions.list()
        else:
            decisions = [d for d in (self.decisions.get(i) for i in decision_ids) if d is not None]

        created: list[str] = []
        existing: list[str] = []
        skipped: list[str] = []
        for decision in decisions:
            try:
                market_view = build_timing_market_view(decision, rolling)
            except Exception:  # noqa: BLE001 - never let one decision abort the batch
                skipped.append(decision.decision_id)
                continue
            assessment, is_new = self.assess(decision, market_view, self.signals_for(decision), now=assessed_at)
            (created if is_new else existing).append(assessment.assessment_id)

        prioritised = self.prioritise(self.list_assessments(), self.config.top_n_cap)
        return AssessTimingResult(
            assessed_at=assessed_at,
            policy_version=self.config.policy_version,
            created_assessment_ids=tuple(created),
            existing_assessment_ids=tuple(existing),
            prioritised=tuple(prioritised),
            batch_summaries=tuple(self.batch_summaries()),
            skipped_decision_ids=tuple(skipped),
        )

    # -- reads --------------------------------------------------------------

    def list_assessments(self, *, newest_first: bool = True) -> list[HedgeTimingAssessment]:
        with self._lock:
            order = list(reversed(self._order)) if newest_first else list(self._order)
            return [self._assessments[a] for a in order]

    def get_assessment(self, assessment_id: str) -> HedgeTimingAssessment | None:
        with self._lock:
            return self._assessments.get(assessment_id)

    def latest_for_decision(self, decision_id: str) -> HedgeTimingAssessment | None:
        with self._lock:
            ids = self._by_decision.get(decision_id, [])
            return self._assessments[ids[-1]] if ids else None

    # -- ranking ------------------------------------------------------------

    def prioritise(self, assessments: list[HedgeTimingAssessment], cap: int | None = None) -> list[PrioritisedItem]:
        """Deterministically rank assessments and cap the top list."""
        ranked = sorted(
            assessments,
            key=lambda a: (
                priority_rank(a.priority),
                -a.priority_score,
                -a.gate_closure_score,
                a.decision_id,
            ),
        )
        limit = self.config.top_n_cap if cap is None else cap
        selected = ranked[:limit] if limit and limit > 0 else ranked
        return [
            PrioritisedItem(
                assessment_id=a.assessment_id,
                decision_id=a.decision_id,
                settlement_period=a.settlement_period,
                delivery_period=a.delivery_period,
                verdict=a.verdict,
                priority=a.priority,
                priority_score=a.priority_score,
            )
            for a in selected
        ]

    # -- batch summaries ----------------------------------------------------

    def batch_summary(self, batch_id: str) -> DecisionBatchSummary | None:
        batch = self.decisions.get_batch(batch_id)
        if batch is None:
            return None
        return self._summarise(batch_id, batch.decision_ids)

    def batch_summaries(self) -> list[DecisionBatchSummary]:
        return [self._summarise(batch.batch_id, batch.decision_ids) for batch in self.decisions.list_batches()]

    def _summarise(self, batch_id: str, decision_ids: tuple[str, ...]) -> DecisionBatchSummary:
        assessments = [a for a in (self.latest_for_decision(i) for i in decision_ids) if a is not None]
        decisions = [d for d in (self.decisions.get(i) for i in decision_ids) if d is not None]
        by_priority = {band: 0 for band in TimingPriority}
        by_verdict = {verdict: 0 for verdict in TimingVerdict}
        for assessment in assessments:
            by_priority[assessment.priority] += 1
            by_verdict[assessment.verdict] += 1
        top = self.prioritise(assessments, self.config.top_n_cap)
        sps = [d.settlement_period for d in decisions]
        period_range = f"SP{min(sps)}–SP{max(sps)}" if sps else None
        return DecisionBatchSummary(
            batch_id=batch_id,
            total_decisions=len(decision_ids),
            assessed_decisions=len(assessments),
            critical_count=by_priority[TimingPriority.CRITICAL],
            high_count=by_priority[TimingPriority.HIGH],
            medium_count=by_priority[TimingPriority.MEDIUM],
            low_count=by_priority[TimingPriority.LOW],
            informational_count=by_priority[TimingPriority.INFORMATIONAL],
            hedge_now_periods=by_verdict[TimingVerdict.HEDGE_NOW],
            partial_hedge_periods=by_verdict[TimingVerdict.PARTIAL_HEDGE_NOW],
            wait_periods=by_verdict[TimingVerdict.WAIT],
            no_action_periods=by_verdict[TimingVerdict.NO_ACTION],
            top_decision_ids=tuple(item.decision_id for item in top),
            affected_period_range=period_range,
        )

    # -- internals ----------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._assessments.clear()
            self._order.clear()
            self._by_key.clear()
            self._by_decision.clear()

    def _store(self, assessment: HedgeTimingAssessment) -> None:
        self._assessments[assessment.assessment_id] = assessment
        self._order.append(assessment.assessment_id)
        self._by_key[assessment_dedupe_key(assessment)] = assessment.assessment_id
        self._by_decision.setdefault(assessment.decision_id, []).append(assessment.assessment_id)

    def _resolve_rolling(self):
        if self._rolling is not None:
            return self._rolling
        from cockpit.rolling_service import ROLLING

        self._rolling = ROLLING
        return self._rolling


HEDGE_TIMING = HedgeTimingService()
"""Process-level singleton hedge-timing service."""
