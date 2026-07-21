from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cockpit.api import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
