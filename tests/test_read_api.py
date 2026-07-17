from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from market_db.read_api import (
    MAX_BAR_ROWS,
    bar_rows,
    create_app,
    operation_rows,
    parse_limit,
    parse_utc,
)
from market_db.validate import db4_manifest_baseline_is_valid


class FakeReader:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def query(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return self.responses.pop(0) if self.responses else []


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
    assert response.get_json()["row_count"] == 1
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
