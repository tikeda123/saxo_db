from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from market_db.data_ui import (
    chart_rows,
    filter_series,
    inventory_series,
    normalize_series,
    overview_payload,
    quality_summary_payload,
    resolve_series,
    series_detail,
    series_role,
)
from market_db.read_api import create_app
import market_db.validate as validate_module
from market_db.validate import manifest_artifact_state


class FakeReader:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def query(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return self.responses.pop(0) if self.responses else []


def inventory_row(**overrides):
    row = {
        "source_dataset_id": "v13_incremental",
        "instrument_id": 6,
        "symbol": "IWM:arcx",
        "category": "equity_reit",
        "layer": "curated",
        "price_basis": "native_ohlc",
        "horizon_minutes": 60,
        "row_count": 28178,
        "min_time_utc": datetime(2010, 6, 21, tzinfo=timezone.utc),
        "max_time_utc": datetime(2026, 7, 16, 19, 30, tzinfo=timezone.utc),
        "latest_complete_time_utc": datetime(2026, 7, 16, 18, 30, tzinfo=timezone.utc),
        "quality_status": "PASS",
        "latest_ingestion_run_id": 105,
        "freshness_seconds": 3600,
        "freshness_status": "PASS",
    }
    row.update(overrides)
    return row


def test_series_roles_separate_canonical_derived_total_return_and_raw():
    assert series_role(inventory_row()) == "CANONICAL_1H"
    assert series_role(inventory_row(layer="derived", horizon_minutes=240)) == "DERIVED_4H"
    assert series_role(inventory_row(layer="derived", horizon_minutes=1440)) == "DERIVED_1D_RISK"
    assert series_role(inventory_row(price_basis="etf_total_return", horizon_minutes=1440)) == "TOTAL_RETURN_DAILY"
    assert series_role(inventory_row(layer="raw", horizon_minutes=240)) == "RAW_ARCHIVE"
    assert series_role(inventory_row(layer="research_metadata", horizon_minutes=None)) == "REFERENCE_METADATA"


def test_series_id_is_stable_opaque_and_resolved_only_from_inventory():
    reader = FakeReader([[inventory_row()]])
    rows = inventory_series(reader)
    selected_id = rows[0]["series_id"]
    assert len(selected_id) == 24
    assert set(selected_id) <= set("0123456789abcdef")
    assert "IWM" not in selected_id

    resolver = FakeReader([[inventory_row()]])
    assert resolve_series(resolver, selected_id)["symbol"] == "IWM:arcx"
    with pytest.raises(ValueError):
        resolve_series(FakeReader(), "curated.market_bar")


def test_inventory_filters_keep_role_status_and_canonical_meaning_explicit():
    rows = [
        normalize_series(inventory_row()),
        normalize_series(inventory_row(layer="raw", horizon_minutes=240, quality_status="WARN")),
        normalize_series(inventory_row(symbol="EURUSD", category="fx", price_basis="bid_ask_mid", freshness_status="NOT_EVALUATED")),
    ]
    assert [row["role"] for row in filter_series(rows, canonical_only=True)] == [
        "CANONICAL_1H", "CANONICAL_1H"
    ]
    assert filter_series(rows, role="RAW_ARCHIVE")[0]["layer"] == "raw"
    assert filter_series(rows, symbol="eur")[0]["symbol"] == "EURUSD"
    with pytest.raises(ValueError):
        filter_series(rows, role="DROP_TABLE")


def test_chart_query_is_parameterized_pinned_and_supports_operator_mode():
    series = normalize_series(inventory_row())
    reader = FakeReader([[{"time_utc": datetime(2026, 7, 16, tzinfo=timezone.utc)}]])
    kind, rows = chart_rows(
        reader,
        series,
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 17, tzinfo=timezone.utc),
        limit=100,
        eligibility="stored_complete",
    )
    assert kind == "ohlc"
    assert len(rows) == 1
    statement, params = reader.calls[0]
    assert "derivation_version" not in statement
    assert "quality_status IN ('PASS','WARN','NOT_EVALUATED')" in statement
    assert "ORDER BY b.time_utc DESC" in statement
    assert "ORDER BY selected.time_utc" in statement
    assert series["symbol"] not in statement
    assert params == (
        6,
        "native_ohlc",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 17, tzinfo=timezone.utc),
        "stored_complete",
        101,
    )
    with pytest.raises(ValueError):
        chart_rows(
            FakeReader(), series,
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 17, tzinfo=timezone.utc),
            limit=10, eligibility="raw",
        )


def test_total_return_chart_uses_line_value_and_not_synthetic_ohlc():
    series = normalize_series(inventory_row(
        source_dataset_id="total_return",
        instrument_id=None,
        symbol="IWM",
        price_basis="etf_total_return",
        horizon_minutes=1440,
    ))
    reader = FakeReader([[]])
    kind, _ = chart_rows(
        reader, series,
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 7, 1, tzinfo=timezone.utc),
        limit=10, eligibility="eligible",
    )
    statement, params = reader.calls[0]
    assert kind == "line"
    assert "total_return_index AS value" in statement
    assert "NULL::NUMERIC AS open" in statement
    assert params[0:2] == ("total_return", "IWM")


def test_series_detail_filters_expensive_views_in_sql_before_reading():
    selected = normalize_series(inventory_row())
    coverage = [{"instrument_id": 6, "coverage_status": "WARN"}]
    freshness = [{"instrument_id": 6, "freshness_status": "PASS"}]
    lineage = [{"source_dataset_id": "v13_incremental", "relative_path": "data/source.json"}]
    reader = FakeReader([[inventory_row()], coverage, freshness, lineage])

    payload = series_detail(reader, selected["series_id"])

    assert payload["coverage"] == coverage
    assert payload["freshness"] == freshness
    assert payload["lineage"] == lineage
    detail_calls = reader.calls[1:]
    assert all("WHERE" in statement for statement, _ in detail_calls)
    assert detail_calls[0][1] == (6,)
    assert detail_calls[1][1] == (6,)
    assert detail_calls[2][1] == ("v13_incremental", 100)


def test_overview_cards_reconcile_from_one_inventory_model():
    rows = [
        inventory_row(),
        inventory_row(layer="derived", horizon_minutes=240, row_count=8000),
        inventory_row(layer="derived", horizon_minutes=1440, row_count=4000),
        inventory_row(layer="raw", horizon_minutes=240, row_count=9000),
    ]
    reader = FakeReader([
        rows,
        [{"source_dataset_id": "active", "active": True}, {"source_dataset_id": "inactive", "active": False}],
        [{"ingestion_run_id": 105, "status": "PASS"}],
        [{"database_name": "saxo_market", "restore_smoke_test_status": "PASS"}],
    ])
    payload = overview_payload(reader)
    assert payload["cards"] == {
        "active_dataset_count": 1,
        "canonical_instrument_count": 1,
        "canonical_1h_rows": 28178,
        "derived_4h_rows": 8000,
        "derived_1d_rows": 4000,
        "attention_series_count": 0,
    }
    assert payload["role_totals"]["RAW_ARCHIVE"]["row_count"] == 9000
    assert len(payload["canonical_guardrails"]) == 1
    assert payload["canonical_guardrails"][0]["symbol"] == "IWM:arcx"


def test_quality_summary_keeps_current_status_separate_from_historical_events():
    coverage = [{
        "instrument_id": 6, "symbol": "IWM:arcx", "coverage_status": "WARN",
        "missing_rows": 3, "out_of_session_rows": 1,
    }]
    freshness = [{
        "instrument_id": 6, "symbol": "IWM:arcx", "category": "equity_reit",
        "freshness_status": "PASS", "latest_complete_time_utc": datetime(2026, 7, 16, tzinfo=timezone.utc),
    }]
    events = [{"severity": "CRITICAL", "status": "OPEN", "rule_id": "old_failed_run"}]
    payload = quality_summary_payload(FakeReader([coverage, freshness, events]))
    assert payload["current"][0]["status"] == "WARN"
    assert payload["current_status_totals"] == {"WARN": 1}
    assert payload["historical_severity_totals"] == {"CRITICAL": 1}
    assert payload["historical_open_events"][0]["rule_id"] == "old_failed_run"


def test_ui_series_and_chart_endpoints_are_get_only_and_warn_for_noneligible_data():
    inventory = inventory_row()
    selected_id = normalize_series(inventory)["series_id"]
    chart = [{
        "time_utc": datetime(2026, 7, 16, tzinfo=timezone.utc),
        "session_date": None,
        "open": "1", "high": "2", "low": "1", "close": "2",
        "volume": "10", "value": None, "price_basis": "native_ohlc",
        "quality_status": "NOT_EVALUATED",
    }]
    reader = FakeReader([[inventory], [inventory], chart])
    client = create_app(reader).test_client()

    listing = client.get("/api/v1/ui/series?canonical_only=true&limit=10")
    assert listing.status_code == 200
    assert listing.get_json()["data"][0]["series_id"] == selected_id

    response = client.get(
        "/api/v1/ui/chart-bars"
        f"?series_id={selected_id}&start=2026-07-01T00:00:00Z&end=2026-07-17T00:00:00Z"
        "&limit=10&eligibility=stored_complete"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["series_kind"] == "ohlc"
    assert payload["warnings"] == ["NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED"]

    rejected = client.post("/api/v1/ui/chart-bars", json={"sql": "DELETE"})
    assert rejected.status_code == 405
    assert rejected.get_json()["error_code"] == "READ_ONLY_API"


def test_chart_endpoint_truncation_keeps_latest_rows_in_ascending_order():
    inventory = inventory_row()
    selected_id = normalize_series(inventory)["series_id"]
    chart = [{
        "time_utc": datetime(2026, 7, day, tzinfo=timezone.utc),
        "session_date": None,
        "open": "1", "high": "2", "low": "1", "close": "2",
        "volume": None, "value": None, "price_basis": "native_ohlc",
        "quality_status": "PASS",
    } for day in range(1, 12)]
    client = create_app(FakeReader([[inventory], chart])).test_client()

    response = client.get(
        "/api/v1/ui/chart-bars"
        f"?series_id={selected_id}&start=2026-07-01T00:00:00Z&end=2026-07-20T00:00:00Z"
        "&limit=10&eligibility=eligible"
    )

    payload = response.get_json()
    assert payload["paging"]["truncated"] is True
    assert payload["warnings"] == ["RESULT_TRUNCATED"]
    assert [row["time_utc"][:10] for row in payload["data"]] == [
        f"2026-07-{day:02d}" for day in range(2, 12)
    ]


def test_frontend_assets_have_no_secret_storage_cdn_or_write_requests():
    from market_db.connection import project_root

    root = project_root()
    html = (root / "market_db/templates/data_ui.html").read_text(encoding="utf-8")
    script = (root / "market_db/static/data-ui/data-ui.js").read_text(encoding="utf-8")
    combined = html + script
    assert "localStorage" not in combined
    assert "sessionStorage" not in combined
    assert "SAXO_ACCESS_TOKEN" not in combined
    assert "cdn.jsdelivr" not in combined
    assert "unpkg.com" not in combined
    assert "method: \"POST\"" not in combined
    assert "new AbortController()" in script
    assert 'limit: "1000"' in script
    assert "DUPLICATE_CHART_TIME" in script
    assert "/static/vendor/lightweight-charts-5.2.0/" in html


def test_extension_manifest_artifact_state_attests_only_exact_current_files(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("current", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(validate_module, "project_root", lambda: tmp_path)

    payload = {"artifacts": {"artifact.txt": {"size_bytes": 7, "sha256": digest}}}
    assert manifest_artifact_state(payload) == ([], {"artifact.txt"})

    payload["artifacts"]["artifact.txt"]["sha256"] = "0" * 64
    assert manifest_artifact_state(payload) == (["sha256:artifact.txt"], set())
