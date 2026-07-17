from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from market_db.export_parquet import ExportError, export_bars, output_path


class FakeReader:
    def query(self, statement, params=()):
        return [
            {
                "instrument_key": "iwm",
                "symbol": "IWM:xnys",
                "layer": "1h",
                "time_utc": datetime(2026, 7, 16, 20, tzinfo=timezone.utc),
                "session_date": None,
                "price_basis": "bid_ask_mid",
                "open": Decimal("225.100000000000"),
                "high": Decimal("226.100000000000"),
                "low": Decimal("224.500000000000"),
                "close": Decimal("225.900000000000"),
                "volume": Decimal("12345.00000000"),
                "is_complete": True,
                "quality_status": "PASS",
            }
        ]


def test_export_writes_verified_typed_parquet_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr("market_db.export_parquet.project_root", lambda: tmp_path)
    monkeypatch.setattr("market_db.backup.project_root", lambda: tmp_path)
    result = export_bars(
        instrument_key="IWM",
        layer="1h",
        start=datetime(2026, 7, 16, tzinfo=timezone.utc),
        end=datetime(2026, 7, 17, tzinfo=timezone.utc),
        name="iwm_sample.parquet",
        reader=FakeReader(),
    )

    parquet = tmp_path / result["parquet_relative_path"]
    manifest = parquet.with_suffix(".manifest.json")
    assert result["status"] == "PASS"
    assert result["row_count"] == result["readback_row_count"] == 1
    assert json.loads(manifest.read_text(encoding="utf-8")) == result
    with duckdb.connect(":memory:") as connection:
        row = connection.execute(
            "SELECT instrument_key, layer, open::VARCHAR, is_complete FROM read_parquet(?)",
            [str(parquet)],
        ).fetchone()
    assert row == ("iwm", "1h", "225.100000000000", True)


def test_export_output_is_confined_to_simple_parquet_name(monkeypatch, tmp_path):
    monkeypatch.setattr("market_db.export_parquet.project_root", lambda: tmp_path)
    assert output_path("iwm_1h.parquet").parent == (tmp_path / "exports/parquet").resolve()
    for invalid in ("../escape.parquet", "/tmp/escape.parquet", "bad.json", "spaces here.parquet"):
        with pytest.raises(ExportError):
            output_path(invalid)
