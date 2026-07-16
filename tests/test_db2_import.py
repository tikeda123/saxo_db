from __future__ import annotations

import hashlib
import json

import pytest

from market_db.import_legacy import (
    EXPECTED_FILES,
    EXPECTED_ROWS,
    InventoryRecord,
    canonical_payload,
    classify,
    load_inventory,
    observation_time,
    parse_bool,
    reference_key,
    verify_inventory,
)


def record(group: str, path: str) -> InventoryRecord:
    return InventoryRecord(group, path, 1, 1, "0" * 64)


def test_db2_file_classification_is_exhaustive():
    records = load_inventory()
    classes = {selected: 0 for selected in ("raw_market_bar", "curated_total_return", "reference_observation")}
    rows = {selected: 0 for selected in classes}
    for item in records:
        selected = classify(item)
        classes[selected] += 1
        rows[selected] += item.row_count
    assert classes == {"raw_market_bar": 44, "curated_total_return": 1, "reference_observation": 24}
    assert rows == {"raw_market_bar": 636_629, "curated_total_return": 54_285, "reference_observation": 90_894}
    assert len(records) == EXPECTED_FILES
    assert sum(item.row_count for item in records) == EXPECTED_ROWS


def test_classification_rejects_metadata_as_market_bar():
    assert classify(record("saxo_intraday", "data/import/intraday/collection_summary.csv")) == "reference_observation"
    assert classify(record("saxo_ETF_daily_raw", "data/import/daily/saxo_etf_raw/instrument_master.csv")) == "reference_observation"
    assert classify(record("saxo_multi_asset_daily", "data/import/daily/saxo_multi_asset/eurusd_daily.csv")) == "raw_market_bar"


def test_inventory_hash_and_size_gate_passes():
    result = verify_inventory(load_inventory())
    assert result["status"] == "PASS"
    assert result["errors"] == []


def test_boolean_parser_is_strict():
    assert parse_bool("True") is True
    assert parse_bool("false") is False
    with pytest.raises(ValueError):
        parse_bool("1")


def test_canonical_payload_is_stable():
    payload, digest = canonical_payload({"b": "2", "a": "1"})
    assert payload == {"b": "2", "a": "1"}
    assert digest == hashlib.sha256(b'{"a":"1","b":"2"}').hexdigest()


def test_reference_metadata_helpers():
    assert reference_key({"ticker": "SPY"}) == "SPY"
    assert observation_time({"date": "2024-06-28"}) == "2024-06-28T00:00:00Z"
    assert observation_time({"end_utc": "2024-06-28T00:00:00Z"}) is None
