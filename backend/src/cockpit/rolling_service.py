"""In-memory rolling state and optimisation lifecycle."""

from __future__ import annotations

from threading import RLock

from cockpit.full_action_optimiser import optimise_full_action
from cockpit.models import (
    HorizonMode,
    LiveStateSnapshot,
    OptimisationRun,
    OptimisationStartingState,
    SampleRegime,
    SourceMode,
)
from cockpit.pipeline import DataFlowPipeline, PIPELINE
from cockpit.simulated_environment import SimulatedEnvironment


class RollingService:
    def __init__(self, pipeline: DataFlowPipeline, environment: SimulatedEnvironment | None = None) -> None:
        self.pipeline = pipeline
        self.environment = environment or SimulatedEnvironment()
        self.period_inputs = []
        self.runs: dict[str, OptimisationRun] = {}
        self.run_order: list[str] = []
        self.current_run: OptimisationRun | None = None
        self._lock = RLock()
        self._initialised = False

    def initialise(self) -> None:
        with self._lock:
            if self._initialised:
                return
            self._initialised = True
            live, periods, cockpit = self.environment.reset()
            self._publish(live, periods, cockpit)
            self.run()

    def live_state(self) -> LiveStateSnapshot:
        self._ensure()
        self.ensure_published()
        assert self.environment.live_state is not None
        return self.environment.live_state

    def ensure_published(self) -> None:
        """Restore the rolling snapshot after a lower-level diagnostic feed refresh."""
        if (
            self.environment.live_state is not None
            and self.environment.cockpit_snapshot is not None
            and (
                self.pipeline.current_snapshot is None
                or self.pipeline.current_snapshot.snapshot_id
                != self.environment.cockpit_snapshot.snapshot_id
            )
        ):
            self._publish(
                self.environment.live_state,
                self.period_inputs,
                self.environment.cockpit_snapshot,
            )

    def refresh(self) -> LiveStateSnapshot:
        with self._lock:
            self._ensure()
            applied = self.environment.reconcile_previous_run(self.current_run)
            reason = "Backend-time refresh"
            if applied:
                reason += f"; applied completed SAMPLE path for {', '.join(applied)}"
            live, periods, cockpit = self.environment.refresh(reason=reason)
            self._publish(live, periods, cockpit)
            self.run()
            return self.live_state()

    def set_regime(self, regime: SampleRegime) -> LiveStateSnapshot:
        with self._lock:
            self._ensure()
            live, periods, cockpit = self.environment.set_regime(regime)
            self._publish(live, periods, cockpit)
            self.run()
            return self.live_state()

    def set_horizon_mode(self, mode: HorizonMode) -> LiveStateSnapshot:
        with self._lock:
            self._ensure()
            live, periods, cockpit = self.environment.set_horizon_mode(mode)
            self._publish(live, periods, cockpit)
            self.run()
            return self.live_state()

    def reset(self) -> tuple[LiveStateSnapshot, OptimisationRun]:
        with self._lock:
            self._ensure()
            live, periods, cockpit = self.environment.reset()
            self._publish(live, periods, cockpit)
            run = self.run()
            return self.live_state(), run

    def advance(self) -> tuple[LiveStateSnapshot, OptimisationRun]:
        with self._lock:
            self._ensure()
            if self.current_run is None or not self.current_run.projected_trajectory:
                raise RuntimeError("No prior optimisation action is available to advance")
            action = self.current_run.projected_trajectory[0]
            live, periods, cockpit = self.environment.advance_from_action(
                delivery_period=action.delivery_period,
                projected_soc_mwh=action.projected_soc_mwh,
                buy_mwh=action.buy_mwh,
                sell_mwh=action.sell_mwh,
                run_id=self.current_run.run_id,
            )
            self._publish(live, periods, cockpit)
            run = self.run()
            return self.live_state(), run

    def run(self) -> OptimisationRun:
        with self._lock:
            self._ensure()
            live = self.live_state()
            if not self.period_inputs:
                raise RuntimeError("Rolling environment has no optimisation periods")
            first = self.period_inputs[0]
            starting = OptimisationStartingState(
                current_time=live.state.current_time,
                current_settlement_period=live.state.current_settlement_period,
                starting_soc_mwh=live.state.current_soc_mwh,
                starting_q_mwh=first.contracted_q_mwh,
                forecast_vintage_id=live.state.current_forecast_vintage_id,
                market_snapshot_id=live.state.current_market_snapshot_id,
                regime=live.state.current_regime,
                source_mode=SourceMode.SAMPLE,
                horizon_mode=live.state.horizon_mode,
                effective_horizon_mode=live.state.effective_horizon_mode,
                horizon_start=live.state.optimisation_horizon_start,
                horizon_end=live.state.optimisation_horizon_end,
            )
            previous = self.current_run.model_copy(deep=True) if self.current_run else None
            run = optimise_full_action(
                [period.model_copy(deep=True) for period in self.period_inputs],
                starting,
                live.state.snapshot_id,
                previous_run=previous,
            )
            immutable = run.model_copy(deep=True)
            self.runs[run.run_id] = immutable
            self.run_order.append(run.run_id)
            self.current_run = immutable.model_copy(deep=True)
            for point in run.lineage_values:
                self.pipeline.lineage_index[point.value_id] = point
            self.environment.mark_optimisation(run.run_id, run.as_of)
            return self.current_run.model_copy(deep=True)

    def current_optimisation(self) -> OptimisationRun:
        self._ensure()
        if self.current_run is None:
            raise RuntimeError("No rolling optimisation run exists")
        return self.current_run.model_copy(deep=True)

    def list_runs(self) -> list[OptimisationRun]:
        self._ensure()
        return [self.runs[run_id].model_copy(deep=True) for run_id in reversed(self.run_order)]

    def get_run(self, run_id: str) -> OptimisationRun | None:
        self._ensure()
        run = self.runs.get(run_id)
        return run.model_copy(deep=True) if run else None

    def _publish(self, live, periods, cockpit) -> None:
        self.period_inputs = [period.model_copy(deep=True) for period in periods]
        self.pipeline.current_snapshot = cockpit
        self.pipeline.snapshots[cockpit.snapshot_id] = cockpit
        for point in cockpit.values:
            self.pipeline.lineage_index[point.value_id] = point
        for point in live.lineage_values:
            self.pipeline.lineage_index[point.value_id] = point

    def _ensure(self) -> None:
        if not self._initialised:
            self.initialise()


ROLLING = RollingService(PIPELINE)
