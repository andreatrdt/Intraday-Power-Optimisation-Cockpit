from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cockpit.api import app
from cockpit.evaluation_service import EVALUATION
from cockpit.rolling_service import ROLLING
from cockpit.settlement_service import SETTLEMENT

# The SAMPLE environment is anchored to real ``datetime.now(UTC)`` and only yields a
# non-empty tradeable window during roughly 00:00–13:00 UTC; outside that window the
# horizon empties and API decision/period tests fail non-deterministically depending
# on the wall-clock moment the suite runs. Pin the global cockpit's clock to a fixed
# time well inside the favourable window so every API test is deterministic. This
# mirrors the pinned-clock convention already used by the isolated rolling tests
# (test_auction_window, test_time_driven_rolling, test_rolling_history_bootstrap).
SAMPLE_AS_OF = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
# Delivery/settlement/evaluation happen after the SAMPLE periods (all on 2026-07-26)
# have completed; pin their clock to the following midnight so every period has ended.
SETTLEMENT_NOW = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ROLLING.environment, "clock", lambda: SAMPLE_AS_OF)
    # Execution submits carry no explicit ``now``; default them to the pinned as-of
    # so simulated orders land inside the pinned (still-open) tradeable window.
    monkeypatch.setattr("cockpit.execution_service._utcnow", lambda: SAMPLE_AS_OF)
    # Delivery/settlement/evaluation default their ``now`` to a time after the SAMPLE
    # periods end, so the completed-period guards pass deterministically.
    monkeypatch.setattr("cockpit.settlement_service._utcnow", lambda: SETTLEMENT_NOW)
    monkeypatch.setattr("cockpit.evaluation_service._utcnow", lambda: SETTLEMENT_NOW)
    SETTLEMENT.reset()
    EVALUATION.reset()
    with TestClient(app) as test_client:
        ROLLING.reset()  # re-anchor the rolling state to the pinned clock
        yield test_client
