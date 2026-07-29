"""Decision evaluation, regret and storage (Milestone 7).

Scores a SETTLED decision against benchmarks. Delegates all arithmetic to the pure
:mod:`cockpit.benchmarks` and :mod:`cockpit.pnl_attribution` modules; owns only the
assembly of the :class:`~cockpit.settlement_models.EvaluationResult`, regret,
cautious quality labelling, storage and idempotency, and the ``SETTLED → EVALUATED``
transition (via the decision service / state machine).

Regret convention (identical for every benchmark):

    regret_vs_benchmark = benchmark_incremental_pnl − realised_incremental_pnl

Positive regret means the benchmark would have done better than the decision. The
realised incremental P&L is the settlement's ``realised_pnl_gbp`` (incremental vs
NO_ACTION). A single decision is never labelled statistically good or bad.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from cockpit.benchmarks import compute_benchmarks
from cockpit.decision_models import BenchmarkResult as DecisionBenchmarkResult
from cockpit.decision_models import DecisionStatus, TradeDecision
from cockpit.decision_service import DECISIONS, DecisionStore
from cockpit.pnl_attribution import compute_attribution
from cockpit.settlement_models import (
    BenchmarkName,
    BenchmarkResult,
    DecisionQualityLabel,
    EvaluationResult,
    ProcessCompletedResult,
    ProcessedDecision,
    ProcessSkipReason,
    SkippedDecision,
    new_evaluation_id,
)
from cockpit.settlement_service import (
    DELIVERABLE_STATES,
    SETTLEMENT,
    IdempotencyConflictError,
    SettlementInputError,
    SettlementService,
    _payload_hash,
)

# Below this |incremental P&L| a single realised outcome is called "in line with
# no action" rather than out/under-performance — a cautious band, not significance.
IN_LINE_TOLERANCE_GBP = 0.01


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class EvaluationStore:
    """In-memory, immutable-out store for evaluations + their benchmark results."""

    def __init__(self) -> None:
        self._evaluations: dict[str, EvaluationResult] = {}
        self._ids: list[str] = []
        self._by_decision: dict[str, str] = {}
        self._benchmarks_by_decision: dict[str, tuple[BenchmarkResult, ...]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}  # client key -> (payload hash, eval id)
        self._lock = RLock()

    def record(self, evaluation: EvaluationResult, *, idempotency_key: str | None, payload_key: str) -> None:
        with self._lock:
            self._evaluations[evaluation.evaluation_id] = evaluation
            self._ids.append(evaluation.evaluation_id)
            self._by_decision[evaluation.decision_id] = evaluation.evaluation_id
            self._benchmarks_by_decision[evaluation.decision_id] = evaluation.benchmark_results
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (payload_key, evaluation.evaluation_id)

    def existing_for_key(self, idempotency_key: str, payload_key: str) -> EvaluationResult | None:
        with self._lock:
            found = self._idempotency.get(idempotency_key)
            if found is None:
                return None
            stored_payload, eval_id = found
            if stored_payload != payload_key:
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was used with a different payload."
                )
            return self._evaluations.get(eval_id)

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        with self._lock:
            return self._evaluations.get(evaluation_id)

    def list(self) -> list[EvaluationResult]:
        with self._lock:
            return [self._evaluations[eid] for eid in reversed(self._ids)]

    def for_decision(self, decision_id: str) -> EvaluationResult | None:
        with self._lock:
            eid = self._by_decision.get(decision_id)
            return self._evaluations.get(eid) if eid else None

    def benchmarks_for_decision(self, decision_id: str) -> tuple[BenchmarkResult, ...]:
        with self._lock:
            return self._benchmarks_by_decision.get(decision_id, ())

    def reset(self) -> None:
        with self._lock:
            self._evaluations.clear()
            self._ids.clear()
            self._by_decision.clear()
            self._benchmarks_by_decision.clear()
            self._idempotency.clear()


def _regret(benchmark: BenchmarkResult | None, realised_incremental: float) -> float | None:
    if benchmark is None:
        return None
    return benchmark.incremental_pnl_vs_no_action_gbp - realised_incremental


def _quality(realised_incremental: float) -> tuple[DecisionQualityLabel, str]:
    if realised_incremental > IN_LINE_TOLERANCE_GBP:
        return (
            DecisionQualityLabel.OUTPERFORMED_NO_ACTION,
            f"This single SAMPLE outcome beat no-action by £{realised_incremental:,.2f}. "
            "One period is not evidence of strategy quality.",
        )
    if realised_incremental < -IN_LINE_TOLERANCE_GBP:
        return (
            DecisionQualityLabel.UNDERPERFORMED_NO_ACTION,
            f"This single SAMPLE outcome trailed no-action by £{-realised_incremental:,.2f}. "
            "One period is not evidence of strategy quality.",
        )
    return (
        DecisionQualityLabel.IN_LINE_WITH_NO_ACTION,
        "This single SAMPLE outcome was in line with no-action. One period is not evidence of strategy quality.",
    )


class EvaluationService:
    """Coordinates evaluation of a SETTLED decision against benchmarks."""

    def __init__(self, *, decisions: DecisionStore | None = None, settlement: SettlementService | None = None) -> None:
        self.decisions = decisions or DECISIONS
        self.settlement = settlement or SETTLEMENT
        self.store = EvaluationStore()

    def evaluate(
        self,
        decision_id: str,
        *,
        now: datetime | None = None,
        expected_status: DecisionStatus | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
    ) -> EvaluationResult:
        at = now or _utcnow()
        payload = _payload_hash(op="evaluate", decision_id=decision_id, expected_status=expected_status, expected_sequence=expected_sequence)
        if idempotency_key is not None:
            existing = self.store.existing_for_key(idempotency_key, payload)
            if existing is not None:
                return existing

        decision = self.decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Unknown decision '{decision_id}'")

        settlement = self.settlement.settlement_for_decision(decision_id)
        if settlement is None:
            # Belt-and-braces: the SETTLED→EVALUATED transition below would also
            # reject an unsettled decision, but this gives a clearer message.
            raise KeyError(f"Decision '{decision_id}' has no settlement to evaluate.")

        inputs = self.settlement.realised_inputs_for(decision, now=at)
        attribution = compute_attribution(inputs)
        benchmarks = compute_benchmarks(
            inputs,
            model_buy_mwh=decision.recommendation.buy_mwh,
            model_sell_mwh=decision.recommendation.sell_mwh,
            fee_gbp_per_mwh=self.settlement.provider.config.fee_gbp_per_mwh,
        )
        by_name = {benchmark.benchmark_name: benchmark for benchmark in benchmarks}
        realised_incremental = settlement.realised_pnl_gbp

        regret_no_action = _regret(by_name.get(BenchmarkName.NO_ACTION), realised_incremental)
        regret_model = _regret(by_name.get(BenchmarkName.MODEL_RECOMMENDATION), realised_incremental)
        regret_perfect = _regret(by_name.get(BenchmarkName.PERFECT_FORESIGHT), realised_incremental)
        # NO_ACTION and PERFECT_FORESIGHT are always present.
        assert regret_no_action is not None and regret_perfect is not None
        regret_best = max(
            benchmark.incremental_pnl_vs_no_action_gbp - realised_incremental for benchmark in benchmarks
        )

        label, note = _quality(realised_incremental)
        warnings: tuple[str, ...] = ()
        if not attribution.reconciled:
            warnings = (f"Attribution reconciliation error £{attribution.reconciliation_error_gbp:.6f}.",)

        evaluation = EvaluationResult(
            evaluation_id=new_evaluation_id(decision_id),
            decision_id=decision_id,
            evaluated_at=at,
            realised_outcome=settlement,
            pnl_attribution=attribution,
            benchmark_results=benchmarks,
            regret_vs_no_action_gbp=round(regret_no_action, 4),
            regret_vs_model_recommendation_gbp=round(regret_model, 4) if regret_model is not None else None,
            regret_vs_perfect_foresight_gbp=round(regret_perfect, 4),
            decision_quality_label=label,
            decision_quality_note=note,
            warnings=warnings,
            lineage_ids=inputs.lineage_ids,
            source_mode=inputs.source_mode,
            quality=inputs.quality,
        )

        # Advance the decision (SETTLED → EVALUATED); non-SETTLED or duplicate → 409.
        self.decisions.evaluate(
            decision_id,
            benchmark_results=[
                DecisionBenchmarkResult(
                    name=benchmark.benchmark_name.value,
                    label=benchmark.description,
                    net_pnl_gbp=round(benchmark.incremental_pnl_vs_no_action_gbp, 4),
                    regret_gbp=round(benchmark.incremental_pnl_vs_no_action_gbp - realised_incremental, 4),
                    tradable=benchmark.attainable,
                    note="; ".join(benchmark.warnings) or None,
                )
                for benchmark in benchmarks
            ],
            regret_vs_best_benchmark_gbp=round(regret_best, 4),
            decision_quality_note=note,
            at=at,
            expected_status=expected_status,
            expected_sequence=expected_sequence,
            reason=f"SAMPLE evaluation vs benchmarks: {label.value}.",
        )
        self.store.record(evaluation, idempotency_key=idempotency_key, payload_key=payload)
        return evaluation

    # -- SAMPLE batch workflow ---------------------------------------------

    def process_completed(self, *, now: datetime | None = None) -> ProcessCompletedResult:
        """Advance every eligible decision whose delivery period has ended through
        deliver → settle → evaluate. Future periods are skipped (never processed);
        already-evaluated decisions are reported as existing. All SAMPLE + diagnostic.
        """
        at = now or _utcnow()
        processed: list[ProcessedDecision] = []
        existing: list[str] = []
        skipped: list[SkippedDecision] = []

        for decision in self.decisions.list(newest_first=False):
            did = decision.decision_id
            status = decision.status
            if status is DecisionStatus.EVALUATED:
                existing.append(did)
                continue
            if status not in DELIVERABLE_STATES and status not in (DecisionStatus.DELIVERED, DecisionStatus.SETTLED):
                skipped.append(
                    SkippedDecision(
                        decision_id=did,
                        settlement_period=decision.settlement_period,
                        reason=ProcessSkipReason.NOT_EXECUTION_COMPLETE,
                        detail=f"Decision is {status.value}; not an execution-complete or no-trade state.",
                    )
                )
                continue
            try:
                current = self.decisions.get(did)
                if current.status in DELIVERABLE_STATES:
                    self.settlement.deliver(did, now=at)
                    current = self.decisions.get(did)
                if current.status is DecisionStatus.DELIVERED:
                    self.settlement.settle(did, now=at)
                    current = self.decisions.get(did)
                if current.status is DecisionStatus.SETTLED:
                    self.evaluate(did, now=at)
            except SettlementInputError as error:
                skipped.append(
                    SkippedDecision(
                        decision_id=did,
                        settlement_period=decision.settlement_period,
                        reason=error.reason,
                        detail=str(error),
                    )
                )
                continue

            delivery = self.settlement.delivery_for_decision(did)
            settlement = self.settlement.settlement_for_decision(did)
            evaluation = self.evaluation_for_decision(did)
            processed.append(
                ProcessedDecision(
                    decision_id=did,
                    settlement_period=decision.settlement_period,
                    delivery_id=delivery.delivery_id,
                    settlement_id=settlement.settlement_id,
                    evaluation_id=evaluation.evaluation_id,
                    decision_quality_label=evaluation.decision_quality_label,
                )
            )
        return ProcessCompletedResult(
            as_of=at,
            processed=tuple(processed),
            existing=tuple(existing),
            skipped=tuple(skipped),
        )

    # -- reads --------------------------------------------------------------

    def list_evaluations(self) -> list[EvaluationResult]:
        return self.store.list()

    def get_evaluation(self, evaluation_id: str) -> EvaluationResult | None:
        return self.store.get(evaluation_id)

    def evaluation_for_decision(self, decision_id: str) -> EvaluationResult | None:
        return self.store.for_decision(decision_id)

    def reset(self) -> None:
        self.store.reset()


EVALUATION = EvaluationService()
"""Process-level singleton evaluation service."""
