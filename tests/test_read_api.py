from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from market_db.read_api import (
    MAX_BAR_ROWS,
    bar_rows,
    create_app,
    operation_rows,
    ordered_content_sha256,
    parse_limit,
    parse_utc,
    series_status_payload,
)
from market_db.validate import (
    db4_manifest_baseline_is_valid,
    dmi1_manifest_baseline_is_valid,
    dmi2a_manifest_baseline_is_valid,
    dmi2b_manifest_baseline_is_valid,
    dmi3_manifest_baseline_is_valid,
    dmi4_manifest_baseline_is_valid,
    dmi5_manifest_baseline_is_valid,
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


def test_read_api_root_has_security_headers_and_write_methods_are_rejected():
    app = create_app(FakeReader())
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200
    assert response.get_json()["read_only"] is True
    assert response.headers["Cache-Control"].startswith("no-store")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Frame-Options"] == "DENY"

    rejected = client.post("/api/v1/bars", json={"sql": "DELETE FROM curated.market_bar"})
    assert rejected.status_code == 405
    assert rejected.get_json() == {"error_code": "READ_ONLY_API", "status": "FAILED"}


def test_data_management_ui_is_same_origin_and_uses_only_local_assets():
    app = create_app(FakeReader())
    client = app.test_client()
    response = client.get("/ui/overview")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "saxo_db Data Console" in page
    assert "/static/vendor/lightweight-charts-5.2.0/" in page
    assert "https://cdn" not in page
    assert "SAXO_ACCESS_TOKEN" not in page
    assert "localStorage" not in page
    assert "sessionStorage" not in page


def test_health_requires_app_reader_and_read_only_transaction():
    reader = FakeReader(
        [[{
            "database_name": "saxo_market",
            "role_name": "saxo_app_reader",
            "transaction_read_only": "on",
            "statement_timeout": "30s",
        }]]
    )
    response = create_app(reader).test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "PASS"

    unhealthy = FakeReader(
        [[{
            "database_name": "saxo_market",
            "role_name": "saxo_migrator",
            "transaction_read_only": "off",
            "statement_timeout": "0",
        }]]
    )
    response = create_app(unhealthy).test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json()["status"] == "FAIL"


def test_bars_require_period_use_parameters_and_normalize_values():
    row = {
        "instrument_key": "iwm",
        "layer": "1h",
        "time_utc": datetime(2026, 7, 16, 20, tzinfo=timezone.utc),
        "session_date": None,
        "open": Decimal("1.250000000000"),
    }
    reader = FakeReader([[row]])
    client = create_app(reader).test_client()

    missing = client.get("/api/v1/bars?instrument_key=iwm&layer=1h")
    assert missing.status_code == 400
    assert reader.calls == []

    response = client.get(
        "/api/v1/bars?instrument_key=IWM&layer=1h"
        "&start=2026-07-16T00:00:00Z&end=2026-07-17T00:00:00Z&limit=10"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["api_version"] == 1
    assert payload["contract_revision"] == "1.2"
    assert payload["generated_at_utc"].endswith("Z")
    assert payload["instrument_key"] == "iwm"
    assert payload["rows"][0]["time_utc"] == "2026-07-16T20:00:00Z"
    assert payload["rows"][0]["open"] == "1.250000000000"
    statement, params = reader.calls[0]
    assert "i.market_key=%s" in statement
    assert "iwm" not in statement
    assert params[0] == "iwm"
    assert params[-1] == 11


def test_bar_query_and_limits_reject_unbounded_or_unknown_input():
    start = datetime(2026, 7, 17, tzinfo=timezone.utc)
    end = datetime(2026, 7, 16, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        bar_rows(
            FakeReader(), instrument_key="iwm", layer="1h", start=start, end=end, limit=10
        )
    with pytest.raises(ValueError):
        bar_rows(
            FakeReader(), instrument_key="iwm", layer="raw", start=end, end=start, limit=10
        )
    with pytest.raises(ValueError):
        parse_limit(str(MAX_BAR_ROWS + 1), MAX_BAR_ROWS)
    with pytest.raises(ValueError):
        parse_utc("2026-07-17T00:00:00", "start")


def test_operation_endpoint_uses_only_the_fixed_view_allow_list():
    reader = FakeReader([[{"layer": "1h", "row_count": 10}]])
    client = create_app(reader).test_client()

    response = client.get("/api/v1/operations/inventory?limit=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["row_count"] == 1
    assert payload["api_version"] == 1
    assert payload["contract_revision"] == "1.2"
    assert payload["generated_at_utc"].endswith("Z")
    statement, params = reader.calls[0]
    assert statement == (
        "SELECT * FROM analytics.v_data_inventory "
        "ORDER BY layer, symbol, horizon_minutes LIMIT %s"
    )
    assert params == (1,)

    rejected = client.get("/api/v1/operations/curated.market_bar")
    assert rejected.status_code == 400
    assert len(reader.calls) == 1
    with pytest.raises(ValueError):
        operation_rows(reader, "DROP TABLE", 1)


def test_daily_bar_query_uses_date_bounds():
    reader = FakeReader()
    bar_rows(
        reader,
        instrument_key="efa",
        layer="1d",
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 17, tzinfo=timezone.utc),
        limit=5,
    )
    statement, params = reader.calls[0]
    assert "derivation_version='db3_accepted_1h_calendar_v1'" in statement
    assert params[1].isoformat() == "2026-07-01"
    assert params[2].isoformat() == "2026-07-17"


def _snapshot_manifest():
    return {
        "phase": "DB2",
        "plan_id": "category_specific_intraday_strategy_v13",
        "research_line_id": "v13categoryintraday",
        "source_database": "saxo_market",
        "snapshot_database": "saxo_research_v13",
        "cutoff_utc": "2024-06-28T23:59:59Z",
        "source_inventory_sha256": "a" * 64,
        "table_counts_before_snapshot_registry_row": {"curated.market_bar": 2},
        "boundaries": {"curated_max_time_utc": "2024-06-28T20:00:00+00:00"},
        "FDW_or_dblink_used": False,
    }


def _snapshot_responses():
    cutoff = datetime(2024, 6, 28, 23, 59, 59, tzinfo=timezone.utc)
    max_time = datetime(2024, 6, 28, 20, tzinfo=timezone.utc)
    return [
        [{
            "read_at_utc": datetime(2026, 7, 20, tzinfo=timezone.utc),
            "snapshot_marker": "10:20:",
            "database_name": "saxo_research_v13",
            "role_name": "v13_research_reader",
            "transaction_read_only": "on",
            "statement_timeout": "30s",
        }],
        [{
            "snapshot_id": 1,
            "plan_id": "category_specific_intraday_strategy_v13",
            "research_line_id": "v13categoryintraday",
            "cutoff_utc": cutoff,
            "source_database": "saxo_market",
            "source_manifest_sha256": "a" * 64,
            "row_counts_json": {"curated.market_bar": 2},
            "snapshot_sha256": "c" * 64,
            "status": "FROZEN",
            "snapshot_manifest_relative_path": (
                "manifests/db2_research_snapshot_content.json"
            ),
        }],
        [{
            "instrument_id": 9,
            "instrument_key": "spy",
            "symbol": "SPY:arcx",
            "category": "equity_reit",
            "layer": "1h",
            "horizon_minutes": 60,
            "price_basis": "native_ohlc",
        }],
        [{
            "curated_market_bar_rows": 2,
            "curated_min_time_utc": datetime(2024, 6, 28, 19, tzinfo=timezone.utc),
            "curated_max_time_utc": max_time,
            "post_cutoff_rows": 0,
        }],
        [{
            "instrument_key": "spy",
            "instrument_id": 9,
            "symbol": "SPY:arcx",
            "category": "equity_reit",
            "layer": "1h",
            "time_utc": max_time,
            "price_basis": "native_ohlc",
            "open": Decimal("545.100000000000"),
            "high": Decimal("546.000000000000"),
            "low": Decimal("544.500000000000"),
            "close": Decimal("545.500000000000"),
            "volume": Decimal("1000.000000000000"),
            "is_complete": True,
            "quality_status": "PASS",
        }],
    ]


def test_snapshot_bars_use_dedicated_atomic_reader_and_verified_manifest():
    market_reader = FakeReader()
    snapshot_reader = FakeReader(_snapshot_responses())
    manifest = _snapshot_manifest()
    client = create_app(
        market_reader,
        snapshot_reader,
        snapshot_manifest_loader=lambda path: (manifest, "c" * 64),
    ).test_client()

    response = client.get(
        "/api/v1/snapshots/1/bars?instrument_key=SPY&layer=1h"
        "&price_basis=native_ohlc&start=2024-06-28T19:00:00Z"
        "&end=2024-06-29T00:00:00Z&limit=10"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_revision"] == "1.2"
    assert payload["snapshot"]["requested_snapshot_id"] == 1
    assert payload["snapshot"]["resolved_snapshot_id"] == 1
    assert payload["snapshot"]["snapshot_database"] == "saxo_research_v13"
    assert payload["snapshot"]["snapshot_sha256"] == "c" * 64
    assert payload["integrity"]["status"] == "PASS"
    assert payload["row_count"] == 1
    assert payload["ordered_content_sha256"] == ordered_content_sha256(payload["rows"])
    assert market_reader.calls == []
    assert len(snapshot_reader.calls) == 1
    assert snapshot_reader.calls[0][0] == "ATOMIC"
    atomic_queries = snapshot_reader.calls[0][1]
    assert len(atomic_queries) == 5
    assert "FROM ops.research_snapshot" in atomic_queries[1][0]
    assert "FROM curated.market_bar" in atomic_queries[4][0]
    assert atomic_queries[4][1][-1] == 11


def test_snapshot_bars_fail_closed_for_layer_unknown_snapshot_and_integrity():
    valid_query = (
        "?instrument_key=spy&layer=1h&price_basis=native_ohlc"
        "&start=2024-06-28T19:00:00Z&end=2024-06-29T00:00:00Z&limit=10"
    )
    reader = FakeReader()
    response = create_app(FakeReader(), reader).test_client().get(
        "/api/v1/snapshots/1/bars" + valid_query.replace("layer=1h", "layer=4h")
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "SNAPSHOT_LAYER_NOT_AVAILABLE"
    assert reader.calls == []

    missing_responses = _snapshot_responses()
    missing_responses[1] = []
    response = create_app(FakeReader(), FakeReader(missing_responses)).test_client().get(
        "/api/v1/snapshots/999/bars" + valid_query
    )
    assert response.status_code == 404
    assert response.get_json()["error_code"] == "SNAPSHOT_NOT_FOUND"

    mismatch_responses = _snapshot_responses()
    response = create_app(
        FakeReader(),
        FakeReader(mismatch_responses),
        snapshot_manifest_loader=lambda path: (_snapshot_manifest(), "d" * 64),
    ).test_client().get("/api/v1/snapshots/1/bars" + valid_query)
    assert response.status_code == 503
    assert response.get_json()["error_code"] == "SNAPSHOT_INTEGRITY_FAILED"

    missing_series_responses = _snapshot_responses()
    missing_series_responses[2] = []
    response = create_app(
        FakeReader(), FakeReader(missing_series_responses)
    ).test_client().get("/api/v1/snapshots/1/bars" + valid_query)
    assert response.status_code == 404
    assert response.get_json()["error_code"] == "SNAPSHOT_SERIES_NOT_FOUND"


def _total_return_responses(*, mapping_count=1, quality_status="PASS"):
    read_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    return [
        [{
            "read_at_utc": read_at,
            "snapshot_marker": "30:40:",
            "database_name": "saxo_market",
            "role_name": "saxo_app_reader",
            "transaction_read_only": "on",
        }],
        [{
            "source_dataset_id": "20260712T135236Z",
            "external_series_key": "IWM",
            "instrument_id": 6,
            "mapping_kind": "TICKER_EXACT",
            "mapping_reason": "explicit review",
            "approved_at_utc": read_at,
            "approved_by": "codex-dmi3-20260720",
            "instrument_key": "iwm",
            "symbol": "IWM:arcx",
            "category": "equity_reit",
            "dataset_name": "ETF11 curated total-return daily",
            "provider": "Yahoo Finance and FRED",
            "price_basis": "etf_total_return",
            "research_eligibility": "development_cutoff_only",
            "mapping_count": mapping_count,
            "session_date": datetime(2024, 6, 28, tzinfo=timezone.utc).date(),
            "value": Decimal("205.125000000000"),
            "volume": Decimal("1000000.00000000"),
            "quality_status": quality_status,
            "row_price_basis": "etf_total_return",
        }],
    ]


def test_total_return_endpoint_uses_explicit_mapping_and_separate_price_basis():
    reader = FakeReader(_total_return_responses())
    response = create_app(reader).test_client().get(
        "/api/v1/total-return?instrument_key=IWM"
        "&start=2024-06-01T00:00:00Z&end=2024-07-01T00:00:00Z&limit=10"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_revision"] == "1.2"
    assert payload["series"]["instrument_key"] == "iwm"
    assert payload["series"]["source_dataset_id"] == "20260712T135236Z"
    assert payload["series"]["price_basis"] == "etf_total_return"
    assert payload["source"]["parity_status"] == "PASS"
    assert payload["row_count"] == 1
    assert payload["rows"][0]["value"] == "205.125000000000"
    assert payload["rows"][0]["price_basis"] == "etf_total_return"
    assert len(payload["ordered_content_sha256"]) == 64
    statement, params = reader.calls[0][1][1]
    assert "catalog.series_instrument_mapping" in statement
    assert "i.symbol=%s" not in statement
    assert params[0] == "iwm"
    assert params[-1] == 11


def test_total_return_endpoint_requires_dataset_when_mapping_is_ambiguous():
    reader = FakeReader(_total_return_responses(mapping_count=2))
    response = create_app(reader).test_client().get(
        "/api/v1/total-return?instrument_key=iwm"
        "&start=2024-06-01T00:00:00Z&end=2024-07-01T00:00:00Z"
    )
    assert response.status_code == 409
    assert response.get_json() == {
        "error_code": "SOURCE_DATASET_REQUIRED",
        "status": "FAILED",
    }


def test_total_return_stored_complete_is_explicitly_warned_and_invalid_eligibility_rejected():
    reader = FakeReader(_total_return_responses(quality_status="WARN"))
    client = create_app(reader).test_client()
    response = client.get(
        "/api/v1/total-return?instrument_key=iwm&source_dataset_id=20260712T135236Z"
        "&start=2024-06-01T00:00:00Z&end=2024-07-01T00:00:00Z"
        "&eligibility=stored_complete"
    )
    assert response.status_code == 200
    assert response.get_json()["warnings"] == [
        "NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED"
    ]
    invalid = client.get(
        "/api/v1/total-return?instrument_key=iwm"
        "&start=2024-06-01T00:00:00Z&end=2024-07-01T00:00:00Z"
        "&eligibility=all"
    )
    assert invalid.status_code == 400
    assert invalid.get_json() == {"error_code": "INVALID_REQUEST", "status": "FAILED"}


def _series_status_responses(events=None):
    read_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    return [
        [{"read_at_utc": read_at, "snapshot_marker": "10:20:"}],
        [{
            "instrument_id": 9, "instrument_key": "spy", "symbol": "SPY:arcx",
            "category": "equity_reit", "layer": "1h", "horizon_minutes": 60,
            "price_basis": "native_ohlc",
        }],
        [{"coverage_status": "PASS", "actual_rows": 100}],
        [{
            "freshness_status": "PASS", "data_status": "ACTIVE", "data_version": 42,
            "last_ingestion_run_id": 105, "latest_complete_time_utc": read_at,
        }],
        list(events or []),
        [{"ingestion_run_id": 105, "status": "PASS"}],
        [{"quality_event_high_watermark": 395032}],
    ]


def test_series_status_uses_one_atomic_read_and_exposes_component_revisions():
    historical = {
        "quality_event_id": 33, "status": "OPEN", "severity": "CRITICAL",
        "scope_kind": "RUN", "affected_layer": "curated", "horizon_minutes": 60,
        "price_basis": "native_ohlc", "applicability": "HISTORICAL",
        "current_blocker": False,
    }
    reader = FakeReader(_series_status_responses([historical]))
    response = create_app(reader).test_client().get(
        "/api/v1/series-status?instrument_key=SPY&layer=1h&price_basis=native_ohlc"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["series"]["instrument_key"] == "spy"
    assert payload["consistency"] == {
        "read_at_utc": "2026-07-20T00:00:00Z",
        "snapshot_marker": "10:20:",
        "watermark_data_version": 42,
        "latest_ingestion_run_id": 105,
        "quality_event_high_watermark": 395032,
    }
    assert payload["state"]["eligibility_status"] == "ELIGIBLE"
    assert payload["state"]["historical_unresolved_event_count"] == 1
    assert len(reader.calls) == 1
    assert reader.calls[0][0] == "ATOMIC"
    atomic_queries = reader.calls[0][1]
    assert len(atomic_queries) == 7
    assert "e.scope_kind='UNKNOWN'" in atomic_queries[4][0]
    assert "e.affected_layer='curated'" in atomic_queries[4][0]


def test_series_status_unknown_event_and_stale_data_fail_closed():
    unknown = {
        "quality_event_id": 400000, "status": "OPEN", "severity": "ERROR",
        "scope_kind": "UNKNOWN", "applicability": "UNKNOWN", "current_blocker": True,
    }
    responses = _series_status_responses([unknown])
    responses[2][0]["coverage_status"] = "WARN"
    responses[3][0]["freshness_status"] = "STALE"
    payload = series_status_payload(
        FakeReader(responses), instrument_key="spy", layer="1h", price_basis="native_ohlc"
    )
    assert payload is not None
    assert payload["state"]["quality_status"] == "FAIL"
    assert payload["state"]["eligibility_status"] == "BLOCKED"
    assert payload["state"]["unknown_blocker_count"] == 1
    assert payload["state"]["eligibility_reasons"] == [
        "FRESHNESS_STALE", "QUALITY_CURRENT_OR_UNKNOWN_BLOCKER"
    ]
    assert payload["state"]["eligibility_warnings"] == ["COVERAGE_WARN"]


def test_series_status_rejects_unsupported_layer_and_returns_not_found():
    reader = FakeReader()
    client = create_app(reader).test_client()
    rejected = client.get(
        "/api/v1/series-status?instrument_key=spy&layer=4h&price_basis=native_ohlc"
    )
    assert rejected.status_code == 400
    assert reader.calls == []

    missing = FakeReader(_series_status_responses())
    missing.responses[1] = []
    response = create_app(missing).test_client().get(
        "/api/v1/series-status?instrument_key=missing&layer=1h&price_basis=native_ohlc"
    )
    assert response.status_code == 404
    assert response.get_json()["error_code"] == "SERIES_NOT_FOUND"


def test_derived_layer_count_endpoint_is_pinned_to_the_canonical_version():
    reader = FakeReader([[{"layer": "1h", "row_count": 1}]])
    response = create_app(reader).test_client().get("/api/v1/layer-counts")
    assert response.status_code == 200
    statement, _ = reader.calls[0]
    assert statement.count("derivation_version='db3_accepted_1h_calendar_v1'") == 2


def test_db4_manifest_baseline_requires_all_operational_and_security_evidence():
    comparisons = {
        command: True
        for command in (
            "inventory", "coverage", "freshness", "runs",
            "quality", "lineage", "storage", "backups",
        )
    }
    payload = {
        "phase": "DB4",
        "status": "PASS",
        "read_api": {
            "health_status": "PASS",
            "bind_host": "127.0.0.1",
            "role_name": "saxo_app_reader",
            "transaction_read_only": "on",
            "api_cli_comparisons": comparisons,
        },
        "backups": {
            "verified_databases": ["saxo_market", "saxo_research_v13", "saxo_forward_v13"],
            "restore_smoke_database": "saxo_market",
            "restore_smoke_status": "PASS",
        },
        "retention": {"dry_run_status": "PASS", "apply_status": "PASS", "deleted": []},
        "parquet": {"status": "PASS", "row_count": 1, "readback_row_count": 1},
        "security": {
            "access_token_persisted": False,
            "account_identifier_persisted": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert db4_manifest_baseline_is_valid(payload)
    payload["security"]["database_write_routes"] = 1
    assert not db4_manifest_baseline_is_valid(payload)


def test_dmi1_manifest_separates_contract_pass_from_reconciliation_block():
    payload = {
        "phase": "DMI1",
        "status": "BLOCKED_DATA_RECONCILIATION",
        "contract_status": "PASS",
        "migration": {"number": "0015", "status": "APPLIED"},
        "reconciliation": {
            "status": "BLOCKED_DATA_RECONCILIATION",
            "unknown_event_count": 22,
        },
        "security": {
            "access_token_saved": False,
            "account_identifier_saved": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert dmi1_manifest_baseline_is_valid(payload)
    payload["reconciliation"]["unknown_event_count"] = 0
    assert not dmi1_manifest_baseline_is_valid(payload)


def test_dmi1_manifest_accepts_completed_reconciliation_with_preserved_base_events():
    payload = {
        "phase": "DMI1",
        "status": "PASS",
        "contract_status": "PASS",
        "migration": {"number": "0017", "status": "APPLIED"},
        "reconciliation": {
            "status": "PASS",
            "unknown_event_count": 0,
            "current_event_count": 5,
            "historical_event_count": 17,
            "base_event_unchanged": True,
        },
        "security": {
            "access_token_saved": False,
            "account_identifier_saved": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert dmi1_manifest_baseline_is_valid(payload)
    payload["reconciliation"]["base_event_unchanged"] = False
    assert not dmi1_manifest_baseline_is_valid(payload)


def test_dmi2a_manifest_requires_atomic_read_only_preflight_contract():
    payload = {
        "phase": "DMI2A",
        "status": "PASS",
        "contract_revision": "1.1",
        "endpoint": {
            "method": "GET", "path": "/api/v1/series-status", "supported_layers": ["1h"]
        },
        "transaction": {
            "read_only": True, "isolation": "REPEATABLE READ", "single_snapshot": True
        },
        "runtime_evidence": {
            "unknown_blocker_count": 0, "quality_event_high_watermark": 395032
        },
        "migration": {"number": "0018", "status": "APPLIED"},
        "security": {
            "access_token_saved": False,
            "account_identifier_saved": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert dmi2a_manifest_baseline_is_valid(payload)
    payload["transaction"]["single_snapshot"] = False
    assert not dmi2a_manifest_baseline_is_valid(payload)


def test_dmi3_manifest_requires_explicit_mapping_and_parity_contract():
    payload = {
        "phase": "DMI3",
        "status": "PASS",
        "contract_revision": "1.2",
        "endpoint": {
            "method": "GET", "path": "/api/v1/total-return", "supported_price_basis": ["etf_total_return"]
        },
        "mapping": {
            "table": "catalog.series_instrument_mapping",
            "approved_mapping_count": 11,
            "unapproved_mapping_count": 0,
            "ambiguous_mapping_count": 0,
        },
        "transaction": {"read_only": True, "isolation": "REPEATABLE READ", "single_snapshot": True},
        "runtime_evidence": {
            "instrument_key": "iwm", "source_dataset_id": "20260712T135236Z",
            "row_count": 20, "parity_status": "PASS", "ordered_content_sha256": "a" * 64,
        },
        "security": {
            "access_token_saved": False, "account_identifier_saved": False,
            "arbitrary_sql_enabled": False, "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert dmi3_manifest_baseline_is_valid(payload)
    payload["mapping"]["unapproved_mapping_count"] = 1
    assert not dmi3_manifest_baseline_is_valid(payload)


def test_dmi4_manifest_requires_cursor_binding_parity_and_contract_evidence():
    payload = {
        "phase": "DMI4",
        "status": "PASS",
        "contract_revision": "1.2",
        "cursor": {
            "codec": "HMAC-SHA256",
            "query_bound": True,
            "snapshot_bound": True,
            "state_revision_bound": True,
            "composite_key": ["time_utc", "instrument_id", "price_basis"],
            "total_return_key": ["session_date"],
            "restart_expiry": True,
        },
        "contract": {
            "openapi_relative_path": "specs/read_api_v1_openapi.yaml",
            "compatibility_status": "PASS",
        },
        "runtime_evidence": {
            "snapshot_direct_parity": "PASS",
            "total_return_direct_parity": "PASS",
            "snapshot_missing_count": 0,
            "snapshot_duplicate_count": 0,
            "snapshot_order_reversal_count": 0,
            "total_return_missing_count": 0,
            "total_return_duplicate_count": 0,
            "total_return_order_reversal_count": 0,
        },
        "fail_closed": {
            "tampered_cursor": "CURSOR_INVALID",
            "query_mismatch": "CURSOR_QUERY_MISMATCH",
            "state_revision_change": "CURSOR_EXPIRED",
        },
        "security": {
            "access_token_saved": False,
            "account_identifier_saved": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
    }
    assert dmi4_manifest_baseline_is_valid(payload)
    payload["cursor"]["query_bound"] = False
    assert not dmi4_manifest_baseline_is_valid(payload)


def test_dmi5_manifest_requires_non_data_lifecycle_and_zero_mutation_evidence():
    payload = {
        "phase": "DMI5",
        "phase_name": "DMI5_READ_API_OPERATIONAL_READINESS",
        "status": "PASS",
        "migration": {"status": "NOT_REQUIRED"},
        "lifecycle": {
            "start": "PASS", "status": "PASS", "stop": "PASS",
            "second_start_idempotent": True, "postgres_healthy_after_stop": True,
        },
        "incident_reproduction": {
            "status": "BLOCKED_READ_API_NOT_RUNNING", "exit_code": 2,
        },
        "preflight": {
            "status": "PASS", "exit_code": 0,
            "request_paths": [
                "/", "/health", "/api/v1/bars", "/api/v1/total-return"
            ],
            "market_rows_received": 0, "metadata_rows_received": 0,
        },
        "contract": {
            "host": "127.0.0.1", "port": 8766, "api_version": 1,
            "contract_revision": "1.2", "role_name": "saxo_app_reader",
            "transaction_read_only": "on", "statement_timeout": "30s",
        },
        "mutation_invariant": {
            "data_mutation_commands": 0, "market_table_dml_counter_delta": 0,
            "migration_history_unchanged": True,
        },
        "security": {
            "bind_host": "127.0.0.1", "access_token_saved": False,
            "account_identifier_saved": False, "database_write_routes": 0,
            "saxo_write_requests": 0,
        },
        "test_evidence": {
            "integration_smoke": "PASS", "full_regression": "PASS",
            "passed": 1, "total": 1,
        },
    }
    assert dmi5_manifest_baseline_is_valid(payload)
    payload["preflight"]["market_rows_received"] = 1
    assert not dmi5_manifest_baseline_is_valid(payload)


def test_dmi2b_manifest_requires_verified_immutable_snapshot_read_contract():
    payload = {
        "phase": "DMI2B",
        "status": "PASS",
        "contract_revision": "1.2",
        "endpoint": {
            "method": "GET",
            "path": "/api/v1/snapshots/{snapshot_id}/bars",
            "supported_layers": ["1h"],
        },
        "source": {
            "database": "saxo_research_v13",
            "role": "v13_research_reader",
            "separate_connection_pool": True,
        },
        "transaction": {
            "read_only": True,
            "isolation": "REPEATABLE READ",
            "single_snapshot": True,
        },
        "runtime_evidence": {
            "snapshot_id": 1,
            "integrity_status": "PASS",
            "row_count": 7,
            "snapshot_sha256": "a" * 64,
            "ordered_content_sha256": "b" * 64,
            "current_database_update_invariant": True,
        },
        "fail_closed_evidence": {
            "unknown_snapshot": True,
            "unavailable_layer": True,
            "write_method": True,
        },
        "migration": {"status": "NOT_REQUIRED"},
        "security": {
            "access_token_saved": False,
            "account_identifier_saved": False,
            "arbitrary_sql_enabled": False,
            "database_write_routes": 0,
            "fdw_or_dblink_added": False,
            "saxo_write_requests": 0,
        },
    }
    assert dmi2b_manifest_baseline_is_valid(payload)
    payload["runtime_evidence"]["current_database_update_invariant"] = False
    assert not dmi2b_manifest_baseline_is_valid(payload)
