"""Trader-in-the-loop backend integration (Milestone 3).

Coordinates the existing services — rolling state, the Forecast Revision Service
and the TradeDecision lifecycle — so that a *material* single-period forecast
revision creates a corresponding single-period ``TradeDecision`` with an initial
model recommendation:

    rolling snapshot
      → forecast revision run (per settlement period)
      → material trigger candidates
      → one TradeDecision per period, grouped in a DecisionBatch
      → recommendation populated from the current optimiser result
      → stored in DECISIONS, retrievable via the API

This module owns **coordination only**. It does not own forecast mathematics
(``forecast_revision``), optimisation mathematics (``full_action_optimiser``) or
lifecycle rules (``decision_service`` / ``decision_state_machine``). It invents
no values: fields that are not credibly available are left null / ``UNAVAILABLE``.

Nothing here submits an order or controls an asset. Every created decision is
``diagnostic_only`` / ``not_executable`` and ``trustworthy_for_live_trading =
False``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from cockpit.decision_models import (
    ConfidenceBasis,
    DecisionContext,
    ModelRecommendation,
    RecommendedAction,
    RunMode,
    TradeDecision,
    TriggerType,
    Urgency,
)
from cockpit.decision_service import DECISIONS, DecisionStore
from cockpit.forecast_revision import (
    ForecastRevision,
    ForecastRevisionRun,
    ForecastRevisionService,
    VintageForecastPoint,
)
from cockpit.models import Quality

_ACTION_TOLERANCE_MWH = 1e-6


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Action mapping
# ---------------------------------------------------------------------------


class InconsistentActionError(ValueError):
    """Raised when the optimiser reports simultaneous buy and sell."""

    code = "INCONSISTENT_BUY_SELL"


def map_recommended_action(
    buy_mwh: float, sell_mwh: float, *, tolerance_mwh: float = _ACTION_TOLERANCE_MWH
) -> RecommendedAction:
    """Map an optimiser market decision to a recommended action.

    Only the market hedge is mapped — battery charge/discharge is never collapsed
    into buy/sell.

    * ``buy > tol`` and ``sell ≈ 0``  → ``BUY``
    * ``sell > tol`` and ``buy ≈ 0``  → ``SELL``
    * both ≈ 0                         → ``NO_ACTION`` (the optimiser chose not to
      trade this snapshot; ``WAIT`` is a hedge-timing verdict, produced later)
    * both > tol                      → ``InconsistentActionError`` (rejected)
    """
    buying = buy_mwh > tolerance_mwh
    selling = sell_mwh > tolerance_mwh
    if buying and selling:
        raise InconsistentActionError(
            f"Optimiser reported simultaneous buy {buy_mwh:.3f} and sell {sell_mwh:.3f} MWh."
        )
    if buying:
        return RecommendedAction.BUY
    if selling:
        return RecommendedAction.SELL
    return RecommendedAction.NO_ACTION


# ---------------------------------------------------------------------------
# Adapter DTOs
# ---------------------------------------------------------------------------


class OptimiserPeriodView(_Frozen):
    """The subset of one optimiser period result the orchestrator needs."""

    settlement_period: int
    delivery_period: str
    buy_mwh: float
    sell_mwh: float
    charge_mw: float = 0.0
    discharge_mw: float = 0.0
    best_bid_gbp_per_mwh: float | None = None
    best_ask_gbp_per_mwh: float | None = None
    reference_price_gbp_per_mwh: float | None = None
    tradeable: bool = True


class AdapterSnapshot(_Frozen):
    """Everything the orchestrator needs for one refresh, gathered from rolling
    structures (or hand-built in tests). Forecast points are genuine full-quantile
    vintages only — never manufactured."""

    as_of: datetime
    run_mode: RunMode = RunMode.SAMPLE_DEMO
    market_snapshot_id: str | None = None
    optimisation_run_id: str | None = None
    forecast_points: tuple[VintageForecastPoint, ...] = ()
    q_by_period: dict[str, float] = {}
    gate_closure_by_period: dict[str, datetime] = {}
    optimiser_by_sp: dict[int, OptimiserPeriodView] = {}
    adapter_skips: tuple["DecisionSkip", ...] = ()


# ---------------------------------------------------------------------------
# Result DTOs
# ---------------------------------------------------------------------------


class DecisionSkip(_Frozen):
    settlement_period: int | None
    delivery_period: str | None
    stage: str
    code: str
    message: str


class DecisionRefreshResult(_Frozen):
    as_of: datetime
    run_mode: RunMode
    forecast_run_id: str
    created_decision_ids: tuple[str, ...]
    batch_id: str | None
    duplicate_decision_ids: tuple[str, ...]
    skipped: tuple[DecisionSkip, ...]
    diagnostic_only: bool = True
    trustworthy_for_live_trading: bool = False


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

# The deduplication key. A decision is a duplicate when a stored decision already
# has the same (latest vintage, previous vintage, settlement period, trigger).
DedupeKey = tuple[str | None, str | None, int, TriggerType]


def dedupe_key(decision: TradeDecision) -> DedupeKey:
    context = decision.context
    return (
        context.forecast_vintage_id,
        context.previous_forecast_vintage_id,
        context.settlement_period,
        context.trigger_type,
    )


# ---------------------------------------------------------------------------
# Pure builders (context + recommendation from a revision + optimiser view)
# ---------------------------------------------------------------------------


def _calculation_allowed(quality: Quality) -> bool:
    return quality not in (Quality.INVALID, Quality.MISSING)


def build_context(
    revision: ForecastRevision,
    snap: AdapterSnapshot,
    *,
    minutes_to_gate: float | None,
    gate_closure_at: datetime | None = None,
) -> DecisionContext:
    reason = revision.materiality.materiality_reasons[0] if revision.materiality.materiality_reasons else "material revision"
    description = (
        f"{revision.delivery_period}: P50 forecast revised "
        f"{revision.comparison.delta_p50_mwh:+.1f} MWh "
        f"({revision.significance.calibration_basis.value}); {reason}"
    )
    return DecisionContext(
        settlement_period=revision.settlement_period,
        delivery_period=revision.delivery_period,
        delivery_start=revision.delivery_start,
        delivery_end=revision.delivery_end,
        as_of=revision.as_of,
        trigger_type=TriggerType.FORECAST_REVISION,
        trigger_description=description,
        run_mode=snap.run_mode,
        source_mode=revision.source_mode,
        quality=revision.quality,
        calculation_allowed=_calculation_allowed(revision.quality),
        trustworthy_for_live_trading=False,
        forecast_vintage_id=revision.latest_forecast_vintage_id,
        previous_forecast_vintage_id=revision.previous_forecast_vintage_id,
        forecast_revision_id=revision.revision_id,
        market_snapshot_id=snap.market_snapshot_id,
        optimisation_run_id=snap.optimisation_run_id,
        minutes_to_gate_closure=minutes_to_gate,
        gate_closure_at=gate_closure_at,
        position_before_mwh=revision.portfolio.contracted_position_q_mwh,
        forecast_revision_mwh=revision.comparison.delta_p50_mwh,
        # LATEST (post-revision, pre-hedge) exposures; previous exposures are on
        # the linked ForecastRevision.
        p10_exposure_before_mwh=revision.portfolio.latest_p10_exposure_mwh,
        p50_exposure_before_mwh=revision.portfolio.latest_p50_exposure_mwh,
        p90_exposure_before_mwh=revision.portfolio.latest_p90_exposure_mwh,
    )


def build_recommendation(
    revision: ForecastRevision,
    view: OptimiserPeriodView,
    action: RecommendedAction,
) -> ModelRecommendation:
    """Populate the recommendation from what the optimiser genuinely provides.

    Only ``action`` / ``buy_mwh`` / ``sell_mwh`` and transparent reasoning are
    populated. Limit price, urgency, confidence, and expected/risk £-values are
    left null / ``UNAVAILABLE`` — they are not produced credibly yet.
    """
    reasoning = (
        *revision.materiality.materiality_reasons,
        f"Optimiser action for {revision.delivery_period}: "
        f"buy {view.buy_mwh:.1f} / sell {view.sell_mwh:.1f} MWh → {action.value}.",
    )
    return ModelRecommendation(
        action=action,
        buy_mwh=round(view.buy_mwh, 6),
        sell_mwh=round(view.sell_mwh, 6),
        limit_price=None,  # optimiser provides an executed WAP, not a limit price
        urgency=Urgency.NONE,  # deferred to the hedge-timing milestone
        confidence_score=None,
        confidence_basis=ConfidenceBasis.UNAVAILABLE,
        risk_if_no_action_gbp=None,
        expected_action_value_gbp=None,
        expected_wait_value_gbp=None,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class DecisionOrchestrator:
    """Coordinates rolling state, forecast revisions and decision creation."""

    def __init__(
        self,
        *,
        decisions: DecisionStore | None = None,
        revision_service: ForecastRevisionService | None = None,
        rolling=None,
    ) -> None:
        self.decisions = decisions or DECISIONS
        self.revision_service = revision_service or ForecastRevisionService()
        self._rolling = rolling  # resolved lazily so importing is cheap
        self._revisions: dict[str, ForecastRevision] = {}
        self._runs: list[ForecastRevisionRun] = []
        self._max_runs = 50

    # -- revision / run access ----------------------------------------------

    def revision(self, revision_id: str) -> ForecastRevision | None:
        return self._revisions.get(revision_id)

    def revisions(self) -> list[ForecastRevision]:
        return list(self._revisions.values())

    def runs(self) -> list[ForecastRevisionRun]:
        return list(self._runs)

    def reset(self) -> None:
        """Clear retained revisions/runs. Does not touch the decision store or
        the rolling environment — see docs/forecast-vintages.md on isolation."""
        self._revisions.clear()
        self._runs.clear()

    # -- rolling-driven refresh --------------------------------------------

    def refresh(self, *, now: datetime | None = None) -> DecisionRefreshResult:
        snapshot = self.build_rolling_snapshot()
        return self.process(snapshot, now=now)

    def build_rolling_snapshot(self) -> AdapterSnapshot:
        """Adapt the current rolling/cockpit structures to an AdapterSnapshot.

        Forecast points come from the retained complete vintages: the two newest
        eligible (``published_at <= as_of``) full vintages, so the revision
        service has a genuine latest and previous per period. No quantile is ever
        reconstructed. Q, Gate Closure and the optimiser recommendation come from
        the current rolling/optimiser state.
        """
        rolling = self._resolve_rolling()
        live = rolling.live_state()
        run = rolling.current_optimisation()
        periods = list(rolling.period_inputs)
        as_of = live.state.current_time

        eligible = sorted(
            (vintage for vintage in rolling.forecast_vintage_snapshots() if vintage.published_at <= as_of),
            key=lambda vintage: (vintage.published_at, vintage.vintage_id),
        )
        selected = eligible[-2:]  # latest + immediate previous eligible vintage
        points: list[VintageForecastPoint] = [
            point for vintage in selected for point in self._vintage_to_points(vintage)
        ]

        q_by_period: dict[str, float] = {}
        gate_by_period: dict[str, datetime] = {}
        for period in periods:
            q_by_period[period.delivery_period] = period.contracted_q_mwh
            gate_by_period[period.delivery_period] = period.gate_closure_at

        optimiser_by_sp = {
            result.settlement_period: OptimiserPeriodView(
                settlement_period=result.settlement_period,
                delivery_period=result.delivery_period,
                buy_mwh=result.buy_mwh,
                sell_mwh=result.sell_mwh,
                charge_mw=result.charge_mw,
                discharge_mw=result.discharge_mw,
                best_bid_gbp_per_mwh=result.best_bid_gbp_per_mwh,
                best_ask_gbp_per_mwh=result.best_ask_gbp_per_mwh,
                reference_price_gbp_per_mwh=result.reference_price_gbp_per_mwh,
                tradeable=result.tradeable,
            )
            for result in run.projected_trajectory
        }

        return AdapterSnapshot(
            as_of=live.state.current_time,
            run_mode=RunMode.SAMPLE_DEMO,
            market_snapshot_id=live.state.current_market_snapshot_id,
            optimisation_run_id=run.run_id,
            forecast_points=tuple(points),
            q_by_period=q_by_period,
            gate_closure_by_period=gate_by_period,
            optimiser_by_sp=optimiser_by_sp,
        )

    @staticmethod
    def _vintage_to_points(vintage) -> list[VintageForecastPoint]:
        """Convert a retained complete vintage into per-period forecast points,
        preserving each quantile's original lineage IDs verbatim."""
        return [
            VintageForecastPoint(
                vintage_id=vintage.vintage_id,
                published_at=vintage.published_at,
                settlement_period=period.settlement_period,
                delivery_period=period.delivery_period,
                delivery_start=period.delivery_start,
                delivery_end=period.delivery_end,
                p10_mwh=period.p10_mwh,
                p50_mwh=period.p50_mwh,
                p90_mwh=period.p90_mwh,
                unit=period.unit,
                source_mode=vintage.source_mode,
                quality=vintage.quality,
                lineage_value_ids=period.lineage_value_ids,
            )
            for period in vintage.periods
        ]

    # -- core processing ----------------------------------------------------

    def process(self, snapshot: AdapterSnapshot, *, now: datetime | None = None) -> DecisionRefreshResult:
        """Compute revisions and create only new material decisions (idempotent)."""
        created_at = now or datetime.now(tz=timezone.utc)
        run: ForecastRevisionRun = self.revision_service.compute_run(
            snapshot.forecast_points,
            as_of=snapshot.as_of,
            q_by_period=snapshot.q_by_period,
            gate_closure_by_period=snapshot.gate_closure_by_period,
            run_mode=snapshot.run_mode,
            now=created_at,
        )
        for revision in run.revisions:
            self._revisions[revision.revision_id] = revision
        self._runs.append(run)
        if len(self._runs) > self._max_runs:
            del self._runs[0]

        skips: list[DecisionSkip] = list(snapshot.adapter_skips)
        skips.extend(
            DecisionSkip(
                settlement_period=skip.settlement_period,
                delivery_period=skip.delivery_period,
                stage="FORECAST",
                code=skip.error_code,
                message=skip.message,
            )
            for skip in run.skipped
        )

        existing_keys = {dedupe_key(decision): decision.decision_id for decision in self.decisions.list()}
        duplicate_ids: list[str] = []
        contexts: list[DecisionContext] = []
        recommendations: list[ModelRecommendation] = []

        for revision in run.revisions:
            sp = revision.settlement_period
            dp = revision.delivery_period

            if not revision.is_material:
                skips.append(
                    DecisionSkip(
                        settlement_period=sp, delivery_period=dp, stage="MATERIALITY",
                        code="NON_MATERIAL", message="Revision is not material; no trigger.",
                    )
                )
                continue

            if not _calculation_allowed(revision.quality):
                skips.append(
                    DecisionSkip(
                        settlement_period=sp, delivery_period=dp, stage="TRUST",
                        code="SOURCE_TRUST_NOT_CALCULABLE",
                        message=f"Quality {revision.quality} is not calculable.",
                    )
                )
                continue

            key = (
                revision.latest_forecast_vintage_id,
                revision.previous_forecast_vintage_id,
                sp,
                TriggerType.FORECAST_REVISION,
            )
            if key in existing_keys:
                duplicate_ids.append(existing_keys[key])
                continue

            view = snapshot.optimiser_by_sp.get(sp)
            if view is None:
                skips.append(
                    DecisionSkip(
                        settlement_period=sp, delivery_period=dp, stage="OPTIMISER",
                        code="NO_MATCHING_OPTIMISER_PERIOD", message=f"No optimiser result for SP{sp}.",
                    )
                )
                continue

            gate = snapshot.gate_closure_by_period.get(dp)
            minutes_to_gate = round((gate - snapshot.as_of).total_seconds() / 60.0, 2) if gate is not None else None
            if minutes_to_gate is not None and minutes_to_gate < 0:
                skips.append(
                    DecisionSkip(
                        settlement_period=sp, delivery_period=dp, stage="GATE",
                        code="GATE_CLOSURE_PASSED", message=f"Gate Closure already passed for {dp}.",
                    )
                )
                continue

            try:
                action = map_recommended_action(view.buy_mwh, view.sell_mwh)
            except InconsistentActionError as error:
                skips.append(
                    DecisionSkip(
                        settlement_period=sp, delivery_period=dp, stage="OPTIMISER",
                        code=InconsistentActionError.code, message=str(error),
                    )
                )
                continue

            contexts.append(build_context(revision, snapshot, minutes_to_gate=minutes_to_gate, gate_closure_at=gate))
            recommendations.append(build_recommendation(revision, view, action))

        created_ids: list[str] = []
        batch_id: str | None = None
        if contexts:
            batch, decisions = self.decisions.create_batch(
                contexts=contexts, recommendations=recommendations, created_at=created_at
            )
            created_ids = [decision.decision_id for decision in decisions]
            batch_id = batch.batch_id

        return DecisionRefreshResult(
            as_of=snapshot.as_of,
            run_mode=snapshot.run_mode,
            forecast_run_id=run.run_id,
            created_decision_ids=tuple(created_ids),
            batch_id=batch_id,
            duplicate_decision_ids=tuple(dict.fromkeys(duplicate_ids)),
            skipped=tuple(skips),
        )

    # -- internals ----------------------------------------------------------

    def _resolve_rolling(self):
        if self._rolling is not None:
            return self._rolling
        from cockpit.rolling_service import ROLLING

        self._rolling = ROLLING
        return self._rolling


ORCHESTRATOR = DecisionOrchestrator()
"""Process-level singleton bound to DECISIONS and the rolling service."""
