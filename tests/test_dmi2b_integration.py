from __future__ import annotations

import os

import pytest

from market_db.connection import MARKET_DB, connect
from market_db.read_api import DatabaseReader, SnapshotDatabaseReader, create_app


pytestmark = pytest.mark.integration


SNAPSHOT_QUERY = (
    "/api/v1/snapshots/1/bars?instrument_key=spy&layer=1h"
    "&price_basis=native_ohlc&start=2024-06-28T13:00:00Z"
    "&end=2024-06-29T00:00:00Z&limit=100"
)


@pytest.fixture(autouse=True)
def _integration_enabled():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DMI2B integration tests")


@pytest.fixture
def snapshot_client():
    market_reader = DatabaseReader()
    snapshot_reader = SnapshotDatabaseReader()
    try:
        yield create_app(market_reader, snapshot_reader).test_client()
    finally:
        market_reader.close()
        snapshot_reader.close()


def test_snapshot_bound_bars_use_verified_frozen_research_database(snapshot_client):
    response = snapshot_client.get(SNAPSHOT_QUERY)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_revision"] == "1.2"
    assert payload["snapshot"]["requested_snapshot_id"] == 1
    assert payload["snapshot"]["resolved_snapshot_id"] == 1
    assert payload["snapshot"]["snapshot_database"] == "saxo_research_v13"
    assert payload["snapshot"]["source_database"] == "saxo_market"
    assert payload["snapshot"]["cutoff_utc"] == "2024-06-28T23:59:59Z"
    assert payload["snapshot"]["snapshot_sha256"] == (
        "c275d078dcdff418ff2d34eb8e2e38a8d790510881556341f6bafbf0c8b63d6b"
    )
    assert payload["integrity"] == {
        "status": "PASS",
        "manifest_sha256": payload["snapshot"]["snapshot_sha256"],
        "curated_market_bar_rows": 329745,
        "curated_max_time_utc": "2024-06-28T20:00:00Z",
        "post_cutoff_rows": 0,
    }
    assert payload["row_count"] == 7
    assert payload["truncated"] is False
    assert len(payload["ordered_content_sha256"]) == 64


def test_snapshot_content_is_unchanged_across_current_database_update(snapshot_client):
    before = snapshot_client.get(SNAPSHOT_QUERY).get_json()
    with connect(
        "saxo_migrator", MARKET_DB, application_name="saxo_db_dmi2b_update_probe"
    ) as current:
        with current.cursor() as cursor:
            cursor.execute(
                "CREATE TEMP TABLE dmi2b_current_update_probe "
                "(probe_id INTEGER PRIMARY KEY, note TEXT NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO dmi2b_current_update_probe VALUES (%s, %s)",
                (1, "committed current database write"),
            )
        current.commit()
        after = snapshot_client.get(SNAPSHOT_QUERY).get_json()

    assert after["snapshot"]["snapshot_sha256"] == before["snapshot"]["snapshot_sha256"]
    assert after["row_count"] == before["row_count"]
    assert after["ordered_content_sha256"] == before["ordered_content_sha256"]
    assert after["rows"] == before["rows"]


def test_snapshot_endpoint_fails_closed_without_current_database_fallback(snapshot_client):
    unavailable = snapshot_client.get(SNAPSHOT_QUERY.replace("layer=1h", "layer=4h"))
    assert unavailable.status_code == 409
    assert unavailable.get_json() == {
        "error_code": "SNAPSHOT_LAYER_NOT_AVAILABLE",
        "status": "FAILED",
    }

    unknown = snapshot_client.get(SNAPSHOT_QUERY.replace("snapshots/1", "snapshots/999"))
    assert unknown.status_code == 404
    assert unknown.get_json() == {
        "error_code": "SNAPSHOT_NOT_FOUND",
        "status": "FAILED",
    }

    write = snapshot_client.post(SNAPSHOT_QUERY)
    assert write.status_code == 405
    assert write.get_json() == {"error_code": "READ_ONLY_API", "status": "FAILED"}
