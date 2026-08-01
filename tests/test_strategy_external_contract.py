from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from market_db.read_api import (
    StrategyExternalReadError,
    create_app,
    strategy_calendar_payload,
)
from market_db.strategy_external_contract import (
    EXPECTED_ROLES,
    StrategyExternalContractError,
    _validate_contract,
    load_strategy_external_contract,
    public_strategy_external_contract,
)


class FakeReader:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def query(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return self.responses.pop(0) if self.responses else []

    def query_atomic(self, queries):
        selected = [(statement, tuple(params)) for statement, params in queries]
        self.calls.append(("ATOMIC", tuple(selected)))
        responses = self.responses[: len(selected)]
        del self.responses[: len(selected)]
        return responses


def test_contract_bundle_is_manifest_verified_and_fail_closed():
    contract, manifest_sha256 = load_strategy_external_contract()
    assert contract["bundle_status"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert tuple(item["dataset_role"] for item in contract["contracts"]) == EXPECTED_ROLES
    assert len(manifest_sha256) == 64
    assert contract["security"] == {
        "read_only_api": True,
        "account_identifiers_in_public_receipts": False,
        "tokens_in_public_receipts": False,
        "orders_allowed": False,
        "prechecks_allowed": False,
    }
    blocked = [
        item
        for item in contract["contracts"]
        if item["availability_state"] == "BLOCKED_EXTERNAL_CONTRACT"
    ]
    assert blocked
    assert all(item["blocker_ids"] for item in blocked)
    assert all(item["quality"]["state"] != "PASS" for item in blocked)
    assert all(item["freshness"]["state"] != "CURRENT" for item in blocked)


def test_openapi_declares_strategy_external_get_surfaces():
    text = Path("specs/read_api_v1_openapi.yaml").read_text(encoding="utf-8")
    for path in (
        "/api/v1/strategy-data/contracts:",
        "/api/v1/strategy-data/status:",
        "/api/v1/strategy-data/receipts:",
        "/api/v1/strategy-data/calendars/{calendar_id}:",
    ):
        assert path in text
    assert "post:" not in text[text.index("/api/v1/strategy-data/contracts:") :]


def test_contract_validation_rejects_false_available_blocked_source():
    payload = public_strategy_external_contract()
    raw, _ = load_strategy_external_contract()
    selected = next(
        item for item in raw["contracts"] if item["dataset_role"] == "SIGNAL_TOTAL_RETURN_DAILY"
    )
    selected["quality"]["state"] = "PASS"
    with pytest.raises(
        StrategyExternalContractError,
        match="STRATEGY_EXTERNAL_CONTRACT_FAIL_CLOSED_INVALID",
    ):
        _validate_contract(raw)
    assert payload["bundle_status"] == "BLOCKED_EXTERNAL_CONTRACT"


def test_contract_and_unapplied_status_endpoints_are_get_only():
    reader = FakeReader([[{"migration_applied": False}]])
    client = create_app(reader).test_client()

    contracts = client.get("/api/v1/strategy-data/contracts")
    assert contracts.status_code == 200
    assert contracts.get_json()["read_only"] is True
    assert contracts.get_json()["bundle_status"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert reader.calls == []

    status = client.get("/api/v1/strategy-data/status")
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["migration_status"] == "NOT_APPLIED"
    assert payload["overall"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert "SIGNAL_TOTAL_RETURN_DAILY" in payload["blocked_roles"]
    assert len(reader.calls) == 1

    rejected = client.post("/api/v1/strategy-data/contracts", json={})
    assert rejected.status_code == 405
    assert rejected.get_json()["error_code"] == "READ_ONLY_API"


def test_receipts_endpoint_is_empty_before_migration_and_validates_role():
    reader = FakeReader([[{"migration_applied": False}]])
    client = create_app(reader).test_client()

    response = client.get(
        "/api/v1/strategy-data/receipts?dataset_role=SIGNAL_TOTAL_RETURN_DAILY&limit=10"
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "api_version": 1,
        "contract_revision": "1.2",
        "dataset_role": "SIGNAL_TOTAL_RETURN_DAILY",
        "migration_status": "NOT_APPLIED",
        "read_only": True,
        "row_count": 0,
        "rows": [],
    }

    invalid = client.get("/api/v1/strategy-data/receipts?dataset_role=UNKNOWN")
    assert invalid.status_code == 400
    assert len(reader.calls) == 1


def test_receipts_endpoint_reads_only_security_barrier_view_after_migration():
    receipt = {
        "receipt_id": "receipt-1",
        "edc_id": "EDC-05",
        "dataset_role": "DISTRIBUTION_CASH_TRANSACTION",
        "contract_id": "c2_edc05_distribution_cash_transaction_v1",
        "availability_state": "BLOCKED_EXTERNAL_CONTRACT",
        "source_observed_at_utc": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "available_at_utc": datetime(2026, 7, 30, tzinfo=timezone.utc),
        "freshness_state": "NOT_EVALUATED_SLA",
        "quality_state": "NOT_EVALUATED",
        "revision_state": "NOT_EVALUATED",
        "cost_confidence": "NOT_APPLICABLE",
        "warning_ids": [],
        "blocker_ids": ["BLOCKED_EXTERNAL_CONTRACT_DISTRIBUTION_TRANSACTION"],
        "values_modified": False,
        "interpolation_performed": False,
        "payload": {},
        "receipt_sha256": "a" * 64,
    }
    reader = FakeReader([[{"migration_applied": True}], [receipt]])
    response = create_app(reader).test_client().get(
        "/api/v1/strategy-data/receipts?dataset_role=DISTRIBUTION_CASH_TRANSACTION&limit=1"
    )
    assert response.status_code == 200
    assert response.get_json()["rows"][0]["receipt_id"] == "receipt-1"
    statement, params = reader.calls[1]
    assert "FROM analytics.v_strategy_external_data_receipt" in statement
    assert "FROM ops.strategy_external_data_receipt" not in statement
    assert params == (
        "DISTRIBUTION_CASH_TRANSACTION",
        "DISTRIBUTION_CASH_TRANSACTION",
        1,
    )


def test_manifests_endpoint_includes_strategy_external_contract_bundle():
    response = create_app(FakeReader([[], []])).test_client().get("/api/v1/manifests")
    assert response.status_code == 200
    external = response.get_json()["strategy_external_data_contract"]
    assert external["bundle_id"] == "c2_strategy_external_data_contract_v1"
    assert external["bundle_status"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert len(external["manifest_sha256"]) == 64


def test_applied_status_preserves_quality_freshness_and_revision_dimensions():
    observed = {
        "edc_id": "EDC-01",
        "dataset_role": "SIGNAL_TOTAL_RETURN_DAILY",
        "contract_id": "c2_edc01_signal_total_return_daily_v1",
        "contract_state": "BLOCKED_EXTERNAL_CONTRACT",
        "availability_state": "BLOCKED_EXTERNAL_CONTRACT",
        "provider_id": None,
        "dataset_id": None,
        "price_basis": "adjusted_total_return_index",
        "horizon_minutes": 1440,
        "target_read_endpoint": "/api/v1/total-return",
        "latest_receipt_id": None,
        "last_good_receipt_id": None,
        "source_as_of": None,
        "source_observed_at_utc": None,
        "available_at_utc": None,
        "accepted_at_utc": None,
        "expected_by_utc": None,
        "published_at_utc": None,
        "freshness_state": "NOT_EVALUATED_SLA",
        "quality_state": "NOT_EVALUATED",
        "revision_state": "NOT_EVALUATED",
        "cost_confidence": "NOT_APPLICABLE",
        "warning_ids": [],
        "blocker_ids": ["BLOCKED_EXTERNAL_CONTRACT_SIGNAL_CURRENT"],
        "decision_required_ids": ["EDR-01"],
        "provider_data_version": None,
        "manifest_sha256": "a" * 64,
        "ordered_content_sha256": None,
        "calendar_id": None,
    }
    reader = FakeReader([[{"migration_applied": True}], [observed]])
    response = create_app(reader).test_client().get("/api/v1/strategy-data/status")
    assert response.status_code == 200
    selected = next(
        item
        for item in response.get_json()["sources"]
        if item["dataset_role"] == "SIGNAL_TOTAL_RETURN_DAILY"
    )
    assert selected["quality"]["state"] == "NOT_EVALUATED"
    assert selected["freshness"]["state"] == "NOT_EVALUATED_SLA"
    assert selected["revision"]["state"] == "NOT_EVALUATED"
    assert selected["availability_state"] == "BLOCKED_EXTERNAL_CONTRACT"


def test_calendar_endpoint_does_not_publish_unaccepted_catalog_seed():
    reader = FakeReader([[]])
    with pytest.raises(StrategyExternalReadError, match="CALENDAR_NOT_FOUND"):
        strategy_calendar_payload(
            reader,
            calendar_id="XNYS_US_EQUITY",
            start=date(2026, 7, 30),
            end=date(2026, 7, 31),
            limit=10,
        )
    assert "analytics.v_strategy_external_data_receipt" in reader.calls[0][0]
    assert "catalog.session_calendar" not in reader.calls[0][0]


def test_calendar_endpoint_rejects_unbounded_and_unknown_calendar():
    client = create_app(FakeReader()).test_client()
    assert client.get("/api/v1/strategy-data/calendars/XNYS_US_EQUITY").status_code == 400

    reader = FakeReader([[]])
    response = create_app(reader).test_client().get(
        "/api/v1/strategy-data/calendars/UNKNOWN?start=2026-07-01&end=2026-08-01"
    )
    assert response.status_code == 404
    assert response.get_json()["error_code"] == "CALENDAR_NOT_FOUND"


def test_calendar_endpoint_prefers_accepted_official_common_receipt():
    session = {
        "session_date": "2026-07-30",
        "open_utc": "2026-07-30T13:30:00Z",
        "close_utc": "2026-07-30T20:00:00Z",
        "session_state": "OPEN",
        "early_close": False,
        "venues": ["ARCX", "XNAS"],
    }
    receipt = {
        "receipt_id": "calendar-receipt",
        "availability_state": "AVAILABLE_WITH_WARNINGS",
        "provider_id": "NYSE_NASDAQ_OFFICIAL_PUBLICATIONS",
        "provider_data_version": "official-hashes",
        "lineage_id": "intersection-v1",
        "ordered_content_sha256": "a" * 64,
        "source_observed_at_utc": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "accepted_at_utc": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "warning_ids": ["SOURCE_PUBLISHED_AT_NOT_EXPOSED"],
        "blocker_ids": [],
        "payload": {
            "calendar_id": "ARCX_XNAS_COMMON_REGULAR_2026",
            "calendar_version": "arcx_xnas_common_2026_v1",
            "tzdb_version": "rule-hash",
            "published_at_utc": None,
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
            "source_urls": ["https://www.nyse.com", "https://www.nasdaqtrader.com"],
            "normalized_sha256": "a" * 64,
            "normalization": "intersection",
            "source_sha256": {"nyse": "b" * 64, "nasdaq": "c" * 64},
            "sessions": [session],
        },
    }
    reader = FakeReader([[receipt]])
    payload = strategy_calendar_payload(
        reader,
        calendar_id="ARCX_XNAS_COMMON_REGULAR_2026",
        start=date(2026, 7, 30),
        end=date(2026, 7, 31),
        limit=10,
    )
    assert payload["common_calendar_verified"] is True
    assert payload["evidence_only"] is False
    assert payload["availability_state"] == "AVAILABLE_WITH_WARNINGS"
    assert payload["row_count"] == 1
    assert payload["sessions"] == [session]
