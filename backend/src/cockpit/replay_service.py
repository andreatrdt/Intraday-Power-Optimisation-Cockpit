"""Replay run storage, idempotency, dataset registry and API coordination (M8).

Thin coordination over the pure/engine modules: resolves the dataset, enforces the
run-mode/dataset boundary (SAMPLE_REPLAY ≠ historical), runs the deterministic engine,
stores immutable results and serves metrics/cumulative-P&L on demand. No replay logic
lives here — that is :mod:`cockpit.replay_engine`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from threading import RLock

from cockpit.replay_dataset import ReplayDataset, build_sample_dataset
from cockpit.replay_engine import DEFAULT_MAX_PERIODS, ReplayConfig, ReplayLimitExceeded, ReplayResult, run_replay
from cockpit.replay_metrics import build_metrics, cumulative_pnl_series
from cockpit.replay_models import (
    CumulativePnlPoint,
    ReplayCreateRequest,
    ReplayEpisodeResult,
    ReplayMetrics,
    ReplayMode,
    ReplayRun,
)

DEFAULT_TIMING_POLICY_VERSION = "hedge-timing-v1"
DEFAULT_SIMULATOR_VERSION = "execution-sim-v1"
DEFAULT_MATERIALITY_CONFIG_REF = "forecast-revision-v1"
SAMPLE_DATASET_ID = "sample-replay-v1"


class ReplayValidationError(ValueError):
    """Invalid request: unknown dataset, or run-mode/dataset-mode mismatch (HTTP 422)."""


class ReplayIdempotencyConflictError(Exception):
    """Same idempotency key re-used with a different canonical request (HTTP 409)."""


def _payload_hash(request: ReplayCreateRequest, dataset_id: str) -> str:
    canonical = json.dumps(
        {
            "dataset_id": dataset_id,
            "run_mode": request.run_mode.value,
            "replay_start": request.replay_start.isoformat() if request.replay_start else None,
            "replay_end": request.replay_end.isoformat() if request.replay_end else None,
            "trader_policy": request.trader_policy.value,
            "execution_mode": request.execution_mode.value,
            "timing_policy_version": request.timing_policy_version or DEFAULT_TIMING_POLICY_VERSION,
            "simulator_version": request.simulator_version or DEFAULT_SIMULATOR_VERSION,
            "materiality_config_ref": request.materiality_config_ref or DEFAULT_MATERIALITY_CONFIG_REF,
            "max_periods": request.max_periods if request.max_periods is not None else DEFAULT_MAX_PERIODS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReplayService:
    """Process-level replay run registry. Runs never mutate the live cockpit state."""

    def __init__(self) -> None:
        self._results: dict[str, ReplayResult] = {}
        self._order: list[str] = []
        self._idempotency: dict[str, tuple[str, str]] = {}  # client key -> (payload hash, run id)
        self._lock = RLock()

    # -- dataset registry ---------------------------------------------------

    def available_datasets(self) -> list[dict]:
        """Datasets the service can replay. Only a SAMPLE dataset ships today; there
        is no bundled historical dataset, so HISTORICAL_REPLAY is unavailable until a
        genuine historical dataset is supplied."""
        return [{"dataset_id": SAMPLE_DATASET_ID, "run_mode": ReplayMode.SAMPLE_REPLAY.value, "source_mode": "SAMPLE", "quality": "FRESH"}]

    def _resolve_dataset(self, request: ReplayCreateRequest) -> ReplayDataset:
        dataset_id = request.dataset_id or SAMPLE_DATASET_ID
        if request.run_mode is ReplayMode.HISTORICAL_REPLAY:
            raise ReplayValidationError(
                "HISTORICAL_REPLAY requires a genuine historical dataset; none is registered. "
                "SAMPLE data must not be labelled historical."
            )
        if dataset_id != SAMPLE_DATASET_ID:
            raise ReplayValidationError(f"Unknown dataset '{dataset_id}'.")
        if request.run_mode is not ReplayMode.SAMPLE_REPLAY:
            raise ReplayValidationError(f"Dataset '{dataset_id}' is a SAMPLE dataset; run_mode must be SAMPLE_REPLAY.")
        return build_sample_dataset(dataset_id=dataset_id)

    # -- create -------------------------------------------------------------

    def create(self, request: ReplayCreateRequest) -> ReplayResult:
        dataset = self._resolve_dataset(request)
        dataset_id = dataset.dataset_id
        payload = _payload_hash(request, dataset_id)

        if request.idempotency_key is not None:
            with self._lock:
                found = self._idempotency.get(request.idempotency_key)
                if found is not None:
                    stored_payload, run_id = found
                    if stored_payload != payload:
                        raise ReplayIdempotencyConflictError(
                            f"Idempotency key '{request.idempotency_key}' was used with a different request."
                        )
                    return self._results[run_id]  # identical retry

        lo, hi = dataset.bounds()
        config = ReplayConfig(
            dataset=dataset,
            run_mode=request.run_mode,
            trader_policy=request.trader_policy,
            execution_mode=request.execution_mode,
            replay_start=request.replay_start or lo,
            replay_end=request.replay_end or hi,
            timing_policy_version=request.timing_policy_version or DEFAULT_TIMING_POLICY_VERSION,
            simulator_version=request.simulator_version or DEFAULT_SIMULATOR_VERSION,
            materiality_config_ref=request.materiality_config_ref or DEFAULT_MATERIALITY_CONFIG_REF,
            max_periods=request.max_periods if request.max_periods is not None else DEFAULT_MAX_PERIODS,
        )
        result = run_replay(config)  # raises ReplayLimitExceeded if the window is too large

        with self._lock:
            self._results[result.run.replay_run_id] = result
            self._order.append(result.run.replay_run_id)
            if request.idempotency_key is not None:
                self._idempotency[request.idempotency_key] = (payload, result.run.replay_run_id)
        return result

    # -- reads --------------------------------------------------------------

    def get(self, replay_run_id: str) -> ReplayResult | None:
        with self._lock:
            return self._results.get(replay_run_id)

    def list_runs(self) -> list[ReplayRun]:
        with self._lock:
            return [self._results[rid].run for rid in reversed(self._order)]

    def episodes(self, replay_run_id: str) -> list[ReplayEpisodeResult] | None:
        result = self.get(replay_run_id)
        return list(result.episodes) if result else None

    def metrics(self, replay_run_id: str) -> ReplayMetrics | None:
        result = self.get(replay_run_id)
        if result is None:
            return None
        return build_metrics(result.run.replay_run_id, result.run.run_mode, result.episodes)

    def cumulative_pnl(self, replay_run_id: str) -> list[CumulativePnlPoint] | None:
        result = self.get(replay_run_id)
        if result is None:
            return None
        return cumulative_pnl_series(result.episodes)

    def reset(self) -> None:
        with self._lock:
            self._results.clear()
            self._order.clear()
            self._idempotency.clear()


REPLAY = ReplayService()
"""Process-level singleton replay run registry."""
