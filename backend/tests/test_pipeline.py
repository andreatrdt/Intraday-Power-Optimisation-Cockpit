from __future__ import annotations

from datetime import datetime

import pytest

from cockpit.adapters import FeedAdapter, NormalisedValue, RawFeedResult
from cockpit.models import Quality, SemanticKind, SourceMode
from cockpit.pipeline import DataFlowPipeline
from cockpit.settlement import UTC


class FailingLiveAdapter(FeedAdapter):
    feed_id = "live_failure"
    feed_name = "Failing live feed"
    description = "Test adapter"
    source_mode = SourceMode.LIVE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 30
    freshness_sla_seconds = 60

    async def fetch(self, now: datetime) -> RawFeedResult:
        raise RuntimeError("upstream unavailable")

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        raise AssertionError("normalise must not run after a fetch failure")


class IntermittentLiveAdapter(FeedAdapter):
    feed_id = "intermittent_live"
    feed_name = "Intermittent live feed"
    description = "Test adapter with one success then one failure"
    source_mode = SourceMode.LIVE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 30
    freshness_sla_seconds = 60

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, now: datetime) -> RawFeedResult:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("second refresh failed")
        return RawFeedResult(rows=[{"reading": 12.5}], retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        return [
            NormalisedValue(
                metric="test_reading",
                value=12.5,
                unit="MW",
                raw_field_name="reading",
                published_at=datetime.now(tz=UTC),
            )
        ]


@pytest.mark.asyncio
async def test_live_failure_is_error_and_never_synthetic() -> None:
    pipeline = DataFlowPipeline([FailingLiveAdapter()])
    attempt, health, snapshot = await pipeline.refresh("live_failure")
    assert attempt.status == "FAILED"
    assert health.source_mode == SourceMode.ERROR
    assert health.quality == Quality.MISSING
    assert "upstream unavailable" in health.latest_error_message
    assert snapshot.values == []
    assert all(event.feed_id != "synthetic_demo" for event in pipeline.events)


@pytest.mark.asyncio
async def test_failed_refresh_marks_prior_live_value_latest_available_and_stale() -> None:
    pipeline = DataFlowPipeline([IntermittentLiveAdapter()])
    await pipeline.refresh("intermittent_live")
    _, health, snapshot = await pipeline.refresh("intermittent_live")
    assert health.source_mode == SourceMode.ERROR
    assert health.quality == Quality.STALE
    point = snapshot.values[0]
    assert point.lineage.source_mode == SourceMode.LATEST_AVAILABLE
    assert point.lineage.quality == Quality.STALE
    assert any("second refresh failed" in warning for warning in point.lineage.warnings)


@pytest.mark.asyncio
async def test_initial_sample_snapshot_is_degraded_and_optimiser_blocked() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    snapshot = pipeline.current_snapshot
    assert snapshot is not None
    assert snapshot.status == "DEGRADED"
    assert snapshot.optimiser_readiness.status == "BLOCKED"
    assert snapshot.optimiser_readiness.allowed is False
    assert "market_intraday" in snapshot.feeds_excluded
    assert all(
        point.lineage.source_mode != SourceMode.SYNTHETIC for point in snapshot.values
    )


@pytest.mark.asyncio
async def test_synthetic_data_requires_explicit_refresh_and_remains_excluded_by_default() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_points["synthetic_demo"] == []
    _, health, snapshot = await pipeline.refresh("synthetic_demo")
    assert health.source_mode == SourceMode.SYNTHETIC
    assert pipeline.current_points["synthetic_demo"]
    assert "synthetic_demo" in snapshot.feeds_excluded


@pytest.mark.asyncio
async def test_every_snapshot_value_has_resolvable_lineage() -> None:
    pipeline = DataFlowPipeline()
    await pipeline.bootstrap()
    assert pipeline.current_snapshot
    for point in pipeline.current_snapshot.values:
        indexed = pipeline.lineage_index[point.value_id]
        assert indexed.lineage.raw_field_name
        assert indexed.snapshot_id == pipeline.current_snapshot.snapshot_id
