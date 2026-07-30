from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_db.data_ui import inventory_series, series_detail
from market_db.instrument_reference import (
    ALLOWED_OFFICIAL_SOURCE_HOSTS,
    EXPECTED_INSTRUMENT_KEYS,
    instrument_catalog_payload,
    reference_catalog_metadata,
    reference_for_key,
)
from market_db.read_api import create_app


class FakeReader:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def query(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return self.responses.pop(0) if self.responses else []


def inventory_row(instrument_id=9, symbol="SPY:arcx"):
    return {
        "source_dataset_id": "v13_incremental",
        "instrument_id": instrument_id,
        "symbol": symbol,
        "category": "equity_reit",
        "layer": "curated",
        "price_basis": "native_ohlc",
        "horizon_minutes": 60,
        "row_count": 123,
        "min_time_utc": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "max_time_utc": datetime(2026, 7, 25, tzinfo=timezone.utc),
        "latest_complete_time_utc": datetime(2026, 7, 24, 18, 30, tzinfo=timezone.utc),
        "quality_status": "PASS",
        "freshness_status": "PASS",
    }


def instrument_row(key, instrument_id):
    reference = reference_for_key(key)
    return {
        "instrument_id": instrument_id,
        "provider": "Saxo OpenAPI",
        "environment": "SIM",
        "instrument_key": key,
        "symbol": reference["short_name"],
        "asset_type": "Etf" if key not in {"audusd", "eurusd", "gold", "us500", "usdcad", "usdchf", "usdjpy", "us_treasury", "wti"} else {
            "audusd": "FxSpot", "eurusd": "FxSpot", "gold": "FxSpot", "us500": "CfdOnIndex",
            "usdcad": "FxSpot", "usdchf": "FxSpot", "usdjpy": "FxSpot",
            "us_treasury": "CfdOnEtf", "wti": "ContractFutures",
        }[key],
        "category": reference["category"],
        "currency": "USD",
        "exchange_id": None,
        "session_calendar_id": None,
    }


def all_instrument_rows():
    ids = {
        "eem": 1, "efa": 2, "gld": 3, "icom": 4, "ief": 5, "iwm": 6,
        "lqd": 7, "shy": 8, "spy": 9, "tip": 10, "tlt": 11, "vnq": 12,
        "eurusd": 13, "gold": 14, "us500": 15, "us_treasury": 16,
        "usdjpy": 17, "wti": 18, "audusd": 19, "usdcad": 20, "usdchf": 21,
    }
    return [instrument_row(key, ids[key]) for key in sorted(EXPECTED_INSTRUMENT_KEYS)]


def test_reference_catalog_is_complete_and_uses_allowlisted_https_sources():
    metadata = reference_catalog_metadata()
    assert metadata["catalog_id"] == "saxo_db_instrument_reference_v1"
    assert metadata["reference_relative_path"] == "specs/instrument_reference_v1.json"
    for key in EXPECTED_INSTRUMENT_KEYS:
        item = reference_for_key(key)
        assert item["instrument_key"] == key
        assert item["quote_interpretation_ja"]
        assert item["data_cautions_ja"]
        for source in item["official_sources"]:
            assert source["url"].startswith("https://")
            assert any(host in source["url"] for host in ALLOWED_OFFICIAL_SOURCE_HOSTS)
    with pytest.raises(LookupError):
        reference_for_key("unknown")


def test_catalog_joins_versioned_definitions_to_current_managed_series():
    rows = inventory_series(FakeReader([[inventory_row()]]))
    reader = FakeReader([all_instrument_rows()])
    payload = instrument_catalog_payload(reader, rows)
    spy = next(item for item in payload["instruments"] if item["instrument_key"] == "spy")
    assert payload["instrument_count"] == 21
    assert spy["managed_instrument"]["provider"] == "Saxo OpenAPI"
    assert spy["managed_series"]["series_count"] == 1
    assert spy["managed_series"]["layers"] == ["1H"]
    assert spy["managed_series"]["default_series_id"]

    total_return = inventory_series(FakeReader([[inventory_row(instrument_id=None, symbol="SPY")]]))
    total_return[0].update({"price_basis": "etf_total_return", "role": "TOTAL_RETURN_DAILY", "layer_label": "1D-TR"})
    payload = instrument_catalog_payload(FakeReader([all_instrument_rows()]), total_return)
    spy = next(item for item in payload["instruments"] if item["instrument_key"] == "spy")
    us_treasury = next(item for item in payload["instruments"] if item["instrument_key"] == "us_treasury")
    assert spy["managed_series"]["series_count"] == 1
    assert us_treasury["managed_series"]["series_count"] == 0


def test_series_detail_includes_product_meaning_separate_from_series_status():
    series = inventory_series(FakeReader([[inventory_row()]]))[0]
    db_instrument = instrument_row("spy", 9)
    reader = FakeReader([[inventory_row()], [], [], [], [db_instrument]])
    payload = series_detail(reader, series["series_id"])
    assert payload["product"]["instrument_key"] == "spy"
    assert "ETF" in payload["product"]["instrument_type_ja"]
    assert payload["product"]["managed_instrument"]["asset_type"] == "Etf"
    assert payload["series"]["status"] == "PASS"


def test_ui_instrument_endpoints_are_get_only_and_return_official_sources():
    reader = FakeReader([[inventory_row()], all_instrument_rows()])
    client = create_app(reader).test_client()
    listing = client.get("/api/v1/ui/instruments")
    assert listing.status_code == 200
    assert listing.get_json()["data"]["instrument_count"] == 21

    detail = client.get("/api/v1/ui/instruments/spy")
    assert detail.status_code == 200
    assert detail.get_json()["data"]["official_sources"]
    assert client.post("/api/v1/ui/instruments", json={}).status_code == 405
    assert client.get("/api/v1/ui/instruments/not-present").status_code == 404


def test_data_console_exposes_catalog_official_links_and_mcp_prompt_without_api_key():
    from market_db.connection import project_root

    root = project_root()
    template = (root / "market_db/templates/data_ui.html").read_text(encoding="utf-8")
    script = (root / "market_db/static/data-ui/data-ui.js").read_text(encoding="utf-8")
    assert 'href="/ui/catalog"' in template
    assert 'target="_blank" rel="noopener noreferrer"' in script
    assert "AIへの質問文をコピー" in script
    assert "saxo_db MCPを使って" in script
    assert "navigator.clipboard.writeText" in script
    assert "OPENAI_API_KEY" not in template + script
    assert 'method: "POST"' not in template + script
