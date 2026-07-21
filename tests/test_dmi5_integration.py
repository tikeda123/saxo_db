from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from market_db.read_api_preflight import (
    BLOCKED_READ_API_NOT_RUNNING,
    PASS,
    SystemReadinessProbe,
    check_readiness,
)
from market_db.read_api_service import postgres_healthy, start_service, stop_service


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def require_integration():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1")


def test_dmi5_local_process_stop_blocked_start_idempotent_status_stop():
    initial = check_readiness(SystemReadinessProbe())
    if initial["status"] == PASS and not initial["service"]["managed"]:
        pytest.skip("unmanaged Read API is already listening")
    cleanup = stop_service()
    assert cleanup["status"] == PASS
    assert postgres_healthy() is True

    blocked = check_readiness(SystemReadinessProbe())
    assert blocked["status"] == BLOCKED_READ_API_NOT_RUNNING
    assert blocked["data_inspection"]["market_rows_received"] == 0

    try:
        started = start_service()
        assert started["status"] == PASS
        assert started["readiness"]["service"]["managed"] is True
        repeated = start_service()
        assert repeated["status"] == PASS
        assert repeated["idempotent"] is True

        status = check_readiness(SystemReadinessProbe())
        assert status["status"] == PASS
        assert status["data_inspection"] == {
            "performed": False,
            "market_rows_received": 0,
            "metadata_rows_received": 0,
            "request_paths": ["/", "/health", "/api/v1/bars", "/api/v1/total-return"],
        }

        request = Request(
            "http://127.0.0.1:8766/api/v1/bars",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as captured:
            urlopen(request, timeout=2)
        assert captured.value.code == 405
        assert json.loads(captured.value.read())["error_code"] == "READ_ONLY_API"
    finally:
        stopped = stop_service()
    assert stopped["status"] == PASS
    assert stopped["postgres_healthy"] is True
    assert check_readiness(SystemReadinessProbe())["status"] == BLOCKED_READ_API_NOT_RUNNING
