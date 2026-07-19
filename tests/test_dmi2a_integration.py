from __future__ import annotations

import os

import pytest

from market_db.read_api import DatabaseReader, create_app


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _integration_enabled():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DMI2A integration tests")


def test_atomic_series_status_matches_current_spy_components():
    reader = DatabaseReader()
    try:
        response = create_app(reader).test_client().get(
            "/api/v1/series-status?instrument_key=spy&layer=1h&price_basis=native_ohlc"
        )
    finally:
        reader.close()
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["generated_at_utc"] == payload["consistency"]["read_at_utc"]
    assert payload["consistency"]["snapshot_marker"]
    assert payload["series"]["instrument_key"] == "spy"
    assert payload["series"]["layer"] == "1h"
    assert payload["series"]["price_basis"] == "native_ohlc"
    assert payload["consistency"]["latest_ingestion_run_id"] == 105
    assert payload["consistency"]["quality_event_high_watermark"] >= 395032
    assert payload["components"]["latest_ingestion_run"]["status"] == "PASS"
    assert payload["state"]["quality_status"] == "PASS"
    assert payload["state"]["current_blockers"] == []
    assert payload["state"]["unknown_blocker_count"] == 0
    assert payload["state"]["historical_unresolved_event_count"] == 3
    assert payload["state"]["eligibility_status"] == "BLOCKED"
    assert "COVERAGE_WARN" in payload["state"]["eligibility_warnings"]
    assert "FRESHNESS_STALE" in payload["state"]["eligibility_reasons"]


def test_series_status_rejects_wrong_price_basis_without_fallback():
    reader = DatabaseReader()
    try:
        response = create_app(reader).test_client().get(
            "/api/v1/series-status?instrument_key=eurusd&layer=1h&price_basis=native_ohlc"
        )
    finally:
        reader.close()
    assert response.status_code == 404
    assert response.get_json() == {"error_code": "SERIES_NOT_FOUND", "status": "FAILED"}
