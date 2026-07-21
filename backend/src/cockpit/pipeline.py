"""In-memory observable data pipeline for Milestone 1A.

The store is intentionally simple for the first vertical slice, but the contracts
are persistence-ready. Each refresh emits stage events, produces traceable
canonical points, and rebuilds a content-hashed cockpit snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from uuid import uuid4

from cockpit.adapters import FeedAdapter, adapters
from cockpit.models import (
    AttemptStatus,
    CanonicalDataPoint,
    CockpitSnapshot,
    DataFlowEvent,
    DataLineage,
    FeedHealth,
    IngestionAttempt,
    OptimiserReadiness,
    OptimiserStatus,
    Quality,
    SnapshotReadiness,
    SnapshotStatus,
    SourceMode,
)
from cockpit.settlement import UTC


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class DataFlowPipeline:
    def __init__(self, feed_adapters: list[FeedAdapter] | None = None) -> None:
        supplied = feed_adapters or adapters()
        self.adapters = {adapter.feed_id: adapter for adapter in supplied}
        self.feed_health: dict[str, FeedHealth] = {
            adapter.feed_id: self._initial_health(adapter) for adapter in supplied
        }
        self.attempts: list[IngestionAttempt] = []
        self.events: list[DataFlowEvent] = []
        self.current_points: dict[str, list[CanonicalDataPoint]] = {
            adapter.feed_id: [] for adapter in supplied
        }
        self.lineage_index: dict[str, CanonicalDataPoint] = {}
        self.snapshots: dict[str, CockpitSnapshot] = {}
        self.current_snapshot: CockpitSnapshot | None = None
        self._lock = asyncio.Lock()
        self._bootstrapped = False

    @staticmethod
    def _initial_health(adapter: FeedAdapter) -> FeedHealth:
        mode = adapter.source_mode
        if mode not in (SourceMode.SAMPLE, SourceMode.SYNTHETIC):
            mode = SourceMode.ERROR
        message = (
            "Provider is not configured"
            if not adapter.configured
            else "No refresh has been attempted in this process"
        )
        return FeedHealth(
            feed_id=adapter.feed_id,
            feed_name=adapter.feed_name,
            description=adapter.description,
            source_mode=mode,
            semantic_kind=adapter.semantic_kind,
            quality=Quality.MISSING,
            configured=adapter.configured,
            connected=False,
            expected_refresh_cadence_seconds=adapter.cadence_seconds,
            freshness_sla_seconds=adapter.freshness_sla_seconds,
            latest_error_message=message,
            included_in_current_snapshot=False,
            required_for_snapshot=adapter.required_for_snapshot,
            required_for_optimiser=adapter.required_for_optimiser,
        )

    async def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        self._bootstrapped = True
        for feed_id in (
            "forecast_sample",
            "portfolio_position_sample",
            "battery_telemetry_sample",
            "battery_config_sample",
            "service_commitments_sample",
            "market_intraday",
            "market_order_book_sample",
        ):
            await self.refresh(feed_id)

    async def refresh(
        self, feed_id: str, *, include_in_snapshot: bool | None = None
    ) -> tuple[IngestionAttempt, FeedHealth, CockpitSnapshot]:
        if feed_id not in self.adapters:
            raise KeyError(feed_id)
        async with self._lock:
            adapter = self.adapters[feed_id]
            now = utc_now()
            attempt = IngestionAttempt(
                attempt_id=str(uuid4()),
                feed_id=feed_id,
                started_at=now,
                status=AttemptStatus.RUNNING,
            )
            self.attempts.append(attempt)
            health = self.feed_health[feed_id]
            health.last_refresh_attempt = now
            health.retry_status = "RUNNING"
            health.pipeline_stage = "INGESTION"
            self._event(
                feed_id=feed_id,
                stage="INGESTION",
                message=f"{adapter.feed_name} refresh started",
                attempt_id=attempt.attempt_id,
            )

            try:
                result = await adapter.fetch(now)
                attempt.rows_retrieved = len(result.rows)
                self._event(
                    feed_id=feed_id,
                    stage="RAW_PAYLOAD",
                    message=f"{adapter.feed_name} returned {len(result.rows)} rows/items",
                    attempt_id=attempt.attempt_id,
                    metadata={"rows_retrieved": len(result.rows)},
                )

                normalised = adapter.normalise(result)
                attempt.rows_normalised = len(normalised)
                self._event(
                    feed_id=feed_id,
                    stage="NORMALISATION",
                    message=f"Normalised {len(normalised)} canonical values from {adapter.feed_name}",
                    attempt_id=attempt.attempt_id,
                    metadata={"rows_normalised": len(normalised)},
                )

                errors = [
                    f"{value.metric}: {check.name} - {check.detail}"
                    for value in normalised
                    for check in value.checks
                    if not check.passed
                ]
                attempt.validation_errors = errors
                quality = Quality.INVALID if errors else Quality.FRESH
                self._event(
                    feed_id=feed_id,
                    stage="VALIDATION",
                    level="ERROR" if errors else "INFO",
                    message=(
                        f"Validation failed with {len(errors)} errors"
                        if errors
                        else f"Validation passed for {len(normalised)} canonical values"
                    ),
                    attempt_id=attempt.attempt_id,
                )

                previous = {
                    (point.metric, point.delivery_period): point
                    for point in self.current_points.get(feed_id, [])
                }
                points: list[CanonicalDataPoint] = []
                normalised_at = utc_now()
                for value in normalised:
                    prior = previous.get((value.metric, value.delivery_period))
                    delta = None
                    if prior and isinstance(prior.value, (int, float)) and isinstance(value.value, (int, float)):
                        delta = round(float(value.value) - float(prior.value), 6)
                    point_quality = (
                        Quality.INVALID if any(not check.passed for check in value.checks) else quality
                    )
                    point = CanonicalDataPoint(
                        value_id=str(uuid4()),
                        metric=value.metric,
                        value=value.value,
                        unit=value.unit,
                        delivery_period=value.delivery_period,
                        delivery_start=value.delivery_start,
                        lineage=DataLineage(
                            source_feed=feed_id,
                            source_mode=adapter.source_mode,
                            semantic_kind=adapter.semantic_kind,
                            quality=point_quality,
                            published_at=value.published_at,
                            retrieved_at=result.retrieved_at,
                            normalised_at=normalised_at,
                            raw_field_name=value.raw_field_name,
                            transformations=value.transformations,
                            validation_checks=value.checks,
                            warnings=value.warnings,
                        ),
                        previous_value=prior.value if prior else None,
                        delta_vs_previous=delta,
                    )
                    points.append(point)
                    self.lineage_index[point.value_id] = point

                self.current_points[feed_id] = points
                attempt.status = AttemptStatus.SUCCEEDED
                attempt.finished_at = utc_now()
                health.source_mode = adapter.source_mode
                health.quality = quality
                health.connected = True
                health.last_successful_refresh = result.retrieved_at
                health.rows_retrieved = len(result.rows)
                health.rows_normalised = len(points)
                health.validation_errors = errors
                health.latest_error_message = None if not errors else "; ".join(errors[:3])
                health.retry_status = "SUCCEEDED"
                health.pipeline_stage = "CANONICAL"
                self._event(
                    feed_id=feed_id,
                    stage="CANONICAL",
                    message=f"Published {len(points)} canonical values for {adapter.feed_name}",
                    attempt_id=attempt.attempt_id,
                )
            except Exception as exc:  # explicit failure: never call a fallback adapter
                attempt.status = AttemptStatus.FAILED
                attempt.finished_at = utc_now()
                attempt.error_message = str(exc)
                health.source_mode = SourceMode.ERROR
                health.quality = Quality.STALE if self.current_points.get(feed_id) else Quality.MISSING
                health.connected = False
                health.latest_error_message = str(exc)
                health.retry_status = "FAILED"
                health.pipeline_stage = "ERROR"
                if self.current_points.get(feed_id):
                    for point in self.current_points[feed_id]:
                        point.lineage.source_mode = SourceMode.LATEST_AVAILABLE
                        point.lineage.quality = Quality.STALE
                        point.lineage.warnings.append(f"Latest refresh failed: {exc}")
                self._event(
                    feed_id=feed_id,
                    stage="ERROR",
                    level="ERROR",
                    message=f"{adapter.feed_name} ERROR: {exc}",
                    attempt_id=attempt.attempt_id,
                )

            if include_in_snapshot is not None:
                adapter.include_by_default = include_in_snapshot
            snapshot = self._build_snapshot()
            return attempt, self.health_for(feed_id), snapshot

    def health_for(self, feed_id: str) -> FeedHealth:
        health = self.feed_health[feed_id].model_copy(deep=True)
        if health.last_successful_refresh:
            health.age_seconds = max(
                0.0, (utc_now() - health.last_successful_refresh).total_seconds()
            )
            if health.age_seconds > health.freshness_sla_seconds:
                if health.source_mode == SourceMode.LIVE:
                    health.source_mode = SourceMode.LATEST_AVAILABLE
                if health.quality not in (Quality.INVALID, Quality.MISSING):
                    health.quality = Quality.STALE
        return health

    def all_health(self) -> list[FeedHealth]:
        return [self.health_for(feed_id) for feed_id in self.adapters]

    def _build_snapshot(self) -> CockpitSnapshot:
        now = utc_now()
        for indexed_point in self.lineage_index.values():
            indexed_point.included_in_current_snapshot = False
            indexed_point.snapshot_id = None
        included: list[str] = []
        excluded: list[str] = []
        values: list[CanonicalDataPoint] = []
        stale: list[str] = []
        missing: list[str] = []

        for feed_id, adapter in self.adapters.items():
            health = self.health_for(feed_id)
            points = self.current_points.get(feed_id, [])
            valid_points = [point for point in points if point.lineage.quality != Quality.INVALID]
            use_feed = bool(valid_points) and adapter.include_by_default
            if use_feed:
                included.append(feed_id)
                for point in valid_points:
                    snapshot_point = point.model_copy(deep=True)
                    if health.quality == Quality.STALE:
                        snapshot_point.lineage.quality = Quality.STALE
                        if adapter.source_mode == SourceMode.LIVE:
                            snapshot_point.lineage.source_mode = SourceMode.LATEST_AVAILABLE
                    values.append(snapshot_point)
            else:
                excluded.append(feed_id)
            if health.quality == Quality.STALE:
                stale.append(feed_id)
            if health.quality in (Quality.MISSING, Quality.INVALID):
                missing.append(feed_id)

        snapshot_blockers = [
            adapter.feed_id
            for adapter in self.adapters.values()
            if adapter.required_for_snapshot and adapter.feed_id not in included
        ]
        snapshot_reasons: list[str] = []
        if snapshot_blockers:
            snapshot_status = SnapshotStatus.BLOCKED
            snapshot_reasons.append(
                "Critical cockpit feeds missing or invalid: " + ", ".join(snapshot_blockers)
            )
        else:
            degraded = bool(stale) or any(
                adapter.required_for_optimiser and adapter.feed_id not in included
                for adapter in self.adapters.values()
            )
            snapshot_status = SnapshotStatus.DEGRADED if degraded else SnapshotStatus.READY
            if stale:
                snapshot_reasons.append("Stale feeds included or available: " + ", ".join(stale))
            missing_context = [
                adapter.feed_id
                for adapter in self.adapters.values()
                if adapter.required_for_optimiser and adapter.feed_id not in included
            ]
            if missing_context:
                snapshot_reasons.append(
                    "Cockpit context is visible but optimiser inputs are incomplete: "
                    + ", ".join(missing_context)
                )
            if not snapshot_reasons:
                snapshot_reasons.append("All required cockpit feeds are fresh and internally valid")

        optimiser_blockers = [
            adapter.feed_id
            for adapter in self.adapters.values()
            if adapter.required_for_optimiser and adapter.feed_id not in included
        ]
        if snapshot_status == SnapshotStatus.BLOCKED:
            optimiser_blockers.extend(snapshot_blockers)
        optimiser_blockers = list(dict.fromkeys(optimiser_blockers))
        if optimiser_blockers:
            optimiser = OptimiserReadiness(
                status=OptimiserStatus.BLOCKED,
                allowed=False,
                reasons=[
                    "Optimiser blocked because required inputs are unavailable: "
                    + ", ".join(optimiser_blockers)
                ],
            )
        elif stale:
            optimiser = OptimiserReadiness(
                status=OptimiserStatus.DEGRADED,
                allowed=False,
                reasons=["Stale input review is required before optimisation: " + ", ".join(stale)],
            )
        else:
            optimiser = OptimiserReadiness(
                status=OptimiserStatus.READY,
                allowed=True,
                reasons=["All required optimiser inputs are available, fresh, and valid"],
            )

        hash_payload = [
            {
                "id": point.value_id,
                "metric": point.metric,
                "value": point.value,
                "retrieved_at": point.lineage.retrieved_at.isoformat(),
            }
            for point in sorted(values, key=lambda point: point.value_id)
        ]
        input_hash = hashlib.sha256(
            json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        snapshot_id = f"snap-{now.strftime('%Y%m%dT%H%M%S%f')}-{input_hash[:8]}"
        for point in values:
            point.included_in_current_snapshot = True
            point.snapshot_id = snapshot_id
            indexed = self.lineage_index.get(point.value_id)
            if indexed:
                indexed.included_in_current_snapshot = True
                indexed.snapshot_id = snapshot_id

        snapshot = CockpitSnapshot(
            snapshot_id=snapshot_id,
            as_of=now,
            input_hash=input_hash,
            status=snapshot_status,
            readiness=SnapshotReadiness(status=snapshot_status, reasons=snapshot_reasons),
            optimiser_readiness=optimiser,
            feeds_included=included,
            feeds_excluded=excluded,
            stale_feeds=stale,
            missing_feeds=missing,
            values=values,
        )
        self.snapshots[snapshot_id] = snapshot
        self.current_snapshot = snapshot
        for feed_id, health in self.feed_health.items():
            health.included_in_current_snapshot = feed_id in included
            if feed_id in included:
                health.pipeline_stage = "SNAPSHOT"

        self._event(
            stage="SNAPSHOT",
            level="WARN" if snapshot_status != SnapshotStatus.READY else "INFO",
            message=(
                f"Snapshot {snapshot_id} built as {snapshot_status.value}; "
                f"optimiser {optimiser.status.value}"
            ),
            snapshot_id=snapshot_id,
            metadata={"feeds_included": included, "feeds_excluded": excluded},
        )
        if optimiser.status == OptimiserStatus.BLOCKED:
            self._event(
                stage="OPTIMISER_READINESS",
                level="ERROR",
                message=optimiser.reasons[0],
                snapshot_id=snapshot_id,
            )
        return snapshot

    def _event(
        self,
        *,
        stage: str,
        message: str,
        level: str = "INFO",
        feed_id: str | None = None,
        attempt_id: str | None = None,
        snapshot_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.events.append(
            DataFlowEvent(
                event_id=str(uuid4()),
                occurred_at=utc_now(),
                feed_id=feed_id,
                stage=stage,
                level=level,
                message=message,
                attempt_id=attempt_id,
                snapshot_id=snapshot_id,
                metadata=metadata or {},
            )
        )

    def recent_events(self, limit: int = 100) -> list[DataFlowEvent]:
        return list(reversed(self.events[-limit:]))

    def recent_attempts(self, limit: int = 100) -> list[IngestionAttempt]:
        return list(reversed(self.attempts[-limit:]))


PIPELINE = DataFlowPipeline()
