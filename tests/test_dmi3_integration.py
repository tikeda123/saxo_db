from __future__ import annotations

import os

import pytest

from market_db.connection import MARKET_DB, connect
from market_db.read_api import DatabaseReader, create_app


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _integration_enabled():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DMI3 integration tests")


@pytest.fixture
def client():
    reader = DatabaseReader()
    try:
        yield create_app(reader).test_client()
    finally:
        reader.close()


def _query(eligibility: str = "eligible", source_dataset_id: str | None = None) -> str:
    source = "" if source_dataset_id is None else f"&source_dataset_id={source_dataset_id}"
    return (
        "/api/v1/total-return?instrument_key=iwm"
        f"{source}&start=2024-01-01T00:00:00Z&end=2024-07-01T00:00:00Z"
        f"&limit=5&eligibility={eligibility}"
    )


def test_stable_total_return_uses_explicit_mapping_and_total_return_basis(client):
    response = client.get(_query())
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_revision"] == "1.2"
    assert payload["series"]["instrument_key"] == "iwm"
    assert payload["series"]["source_dataset_id"] == "20260712T135236Z"
    assert payload["series"]["external_series_key"] == "IWM"
    assert payload["series"]["price_basis"] == "etf_total_return"
    assert payload["mapping"]["approved_by"] == "codex-dmi3-20260720"
    assert payload["source"]["parity_status"] == "PASS"
    assert payload["row_count"] == 5
    assert payload["truncated"] is True
    assert payload["warnings"] == []
    assert payload["rows"][0]["session_date"] == "2024-01-02"
    assert payload["rows"][0]["price_basis"] == "etf_total_return"
    assert len(payload["ordered_content_sha256"]) == 64
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_dmi3_parity") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT date, total_return_index, volume, quality_status
                FROM curated.etf_total_return_daily
                WHERE source_dataset_id=%s AND ticker=%s
                  AND date >= %s AND date < %s AND quality_status='PASS'
                ORDER BY date
                LIMIT 5
                """,
                ("20260712T135236Z", "IWM", "2024-01-01", "2024-07-01"),
            )
            source_rows = cursor.fetchall()
    assert [
        (row["session_date"], row["value"], row["volume"], row["quality_status"])
        for row in payload["rows"]
    ] == [
        (row[0].isoformat(), str(row[1]), str(row[2]), row[3])
        for row in source_rows
    ]


def test_stored_complete_is_explicitly_warned(client):
    response = client.get(_query("stored_complete", "20260712T135236Z"))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["warnings"] == [
        "NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED"
    ]
    assert payload["series"]["price_basis"] == "etf_total_return"


def test_total_return_fails_closed_for_unknown_series_and_invalid_requests(client):
    unknown = client.get(_query().replace("instrument_key=iwm", "instrument_key=eurusd"))
    assert unknown.status_code == 404
    assert unknown.get_json() == {
        "error_code": "TOTAL_RETURN_MAPPING_NOT_FOUND",
        "status": "FAILED",
    }
    invalid = client.get(_query("all"))
    assert invalid.status_code == 400
    assert invalid.get_json() == {"error_code": "INVALID_REQUEST", "status": "FAILED"}
    write = client.post(_query())
    assert write.status_code == 405
    assert write.get_json() == {"error_code": "READ_ONLY_API", "status": "FAILED"}
