from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from market_db.read_api import create_app


class PagingReader:
    def __init__(self, context_rows, metadata_rows, series_rows, integrity_rows, bars, total_rows):
        self.context_rows = context_rows
        self.metadata_rows = metadata_rows
        self.series_rows = series_rows
        self.integrity_rows = integrity_rows
        self.bars = bars
        self.total_rows = total_rows
        self.calls = []

    def query(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return []

    def query_atomic(self, queries):
        selected = [(statement, tuple(params)) for statement, params in queries]
        self.calls.append(("ATOMIC", tuple(selected)))
        if len(selected) == 5:
            after_time = selected[4][1][5]
            rows = self.bars[1:] if after_time is not None else self.bars
            return [
                self.context_rows,
                self.metadata_rows,
                self.series_rows,
                self.integrity_rows,
                rows,
            ]
        after_date = selected[1][1][8]
        rows = self.total_rows[1:] if after_date is not None else self.total_rows
        return [self.context_rows[:1], rows]


def _snapshot_fixture():
    cutoff = datetime(2024, 6, 28, 23, 59, 59, tzinfo=timezone.utc)
    first = datetime(2024, 6, 28, 19, tzinfo=timezone.utc)
    second = datetime(2024, 6, 28, 20, tzinfo=timezone.utc)
    rows = [
        {
            "instrument_key": "spy", "instrument_id": 9, "symbol": "SPY:arcx",
            "category": "equity_reit", "layer": "1h", "time_utc": first,
            "price_basis": "native_ohlc", "open": Decimal("1"), "high": Decimal("2"),
            "low": Decimal("1"), "close": Decimal("2"), "volume": Decimal("10"),
            "is_complete": True, "quality_status": "PASS",
        },
        {
            "instrument_key": "spy", "instrument_id": 9, "symbol": "SPY:arcx",
            "category": "equity_reit", "layer": "1h", "time_utc": second,
            "price_basis": "native_ohlc", "open": Decimal("2"), "high": Decimal("3"),
            "low": Decimal("2"), "close": Decimal("3"), "volume": Decimal("11"),
            "is_complete": True, "quality_status": "PASS",
        },
    ]
    responses = {
        "context": [{
            "read_at_utc": datetime(2026, 7, 20, tzinfo=timezone.utc),
            "snapshot_marker": "1:2:", "database_name": "saxo_research_v13",
            "role_name": "v13_research_reader", "transaction_read_only": "on",
        }],
        "metadata": [{
            "snapshot_id": 1, "plan_id": "plan", "research_line_id": "line",
            "cutoff_utc": cutoff, "source_database": "saxo_market",
            "source_manifest_sha256": "a" * 64, "row_counts_json": {"curated.market_bar": 2},
            "snapshot_sha256": "c" * 64, "status": "FROZEN",
            "snapshot_manifest_relative_path": "manifests/db2_research_snapshot_content.json",
        }],
        "series": [{
            "instrument_id": 9, "instrument_key": "spy", "symbol": "SPY:arcx",
            "category": "equity_reit", "layer": "1h", "horizon_minutes": 60,
            "price_basis": "native_ohlc",
        }],
        "integrity": [{
            "curated_market_bar_rows": 2, "curated_min_time_utc": first,
            "curated_max_time_utc": second, "post_cutoff_rows": 0,
        }],
    }
    manifest = {
        "phase": "DB2", "plan_id": "plan", "research_line_id": "line",
        "source_database": "saxo_market", "snapshot_database": "saxo_research_v13",
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "source_inventory_sha256": "a" * 64,
        "table_counts_before_snapshot_registry_row": {"curated.market_bar": 2},
        "boundaries": {"curated_max_time_utc": second.isoformat()},
        "FDW_or_dblink_used": False,
    }
    return rows, responses, manifest


def _total_fixture():
    read_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    common = {
        "source_dataset_id": "dataset-1", "external_series_key": "IWM",
        "instrument_id": 6, "mapping_kind": "TICKER_EXACT", "mapping_reason": "review",
        "approved_at_utc": read_at, "approved_by": "test", "instrument_key": "iwm",
        "symbol": "IWM:arcx", "category": "equity_reit", "dataset_name": "ETF",
        "provider": "test", "price_basis": "etf_total_return",
        "research_eligibility": "eligible", "mapping_count": 1,
        "volume": Decimal("10"), "quality_status": "PASS", "row_price_basis": "etf_total_return",
        "state_revision": "b" * 64,
    }
    rows = [
        {**common, "session_date": date(2024, 6, 3), "value": Decimal("100")},
        {**common, "session_date": date(2024, 6, 4), "value": Decimal("101")},
    ]
    context = [{
        "read_at_utc": read_at, "snapshot_marker": "3:4:",
        "database_name": "saxo_market", "role_name": "saxo_app_reader",
        "transaction_read_only": "on",
    }]
    return rows, context


def test_dmi4_snapshot_cursor_pages_are_query_bound_and_tamper_evident():
    bars, responses, manifest = _snapshot_fixture()
    reader = PagingReader(
        responses["context"], responses["metadata"], responses["series"],
        responses["integrity"], bars, [],
    )
    client = create_app(
        reader, reader,
        snapshot_manifest_loader=lambda path: (manifest, "c" * 64),
        cursor_secret=b"dmi4-test-secret",
    ).test_client()
    query = (
        "instrument_key=spy&layer=1h&price_basis=native_ohlc"
        "&start=2024-06-28T18:00:00Z&end=2024-06-28T23:00:00Z&limit=1"
    )
    first = client.get("/api/v1/snapshots/1/bars?" + query).get_json()
    assert first["row_count"] == 1
    assert first["truncated"] is True
    assert first["next_cursor"]

    second = client.get("/api/v1/snapshots/1/bars?" + query + "&cursor=" + first["next_cursor"])
    assert second.status_code == 200
    assert [row["time_utc"] for row in second.get_json()["rows"]] == ["2024-06-28T20:00:00Z"]

    mismatch = client.get(
        "/api/v1/snapshots/1/bars?" + query.replace("instrument_key=spy", "instrument_key=efa")
        + "&cursor=" + first["next_cursor"]
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error_code"] == "CURSOR_QUERY_MISMATCH"

    token = first["next_cursor"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    rejected = client.get("/api/v1/snapshots/1/bars?" + query + "&cursor=" + tampered)
    assert rejected.status_code == 400
    assert rejected.get_json()["error_code"] == "CURSOR_INVALID"


def test_dmi4_total_return_cursor_matches_direct_query_and_resolves_dataset():
    total_rows, context = _total_fixture()
    reader = PagingReader(context, [], [], [], [], total_rows)
    client = create_app(reader, cursor_secret=b"dmi4-test-secret").test_client()
    query = (
        "instrument_key=iwm&start=2024-06-01T00:00:00Z"
        "&end=2024-06-10T00:00:00Z&limit=1&eligibility=eligible"
    )
    first = client.get("/api/v1/total-return?" + query).get_json()
    assert first["row_count"] == 1
    assert first["truncated"] is True
    assert first["next_cursor"]
    second = client.get("/api/v1/total-return?" + query + "&cursor=" + first["next_cursor"])
    assert second.status_code == 200
    assert [row["session_date"] for row in second.get_json()["rows"]] == ["2024-06-04"]
    assert second.get_json()["query"]["source_dataset_id"] == "dataset-1"

    mismatch = client.get(
        "/api/v1/total-return?" + query.replace("eligibility=eligible", "eligibility=stored_complete")
        + "&cursor=" + first["next_cursor"]
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error_code"] == "CURSOR_QUERY_MISMATCH"

    for row in reader.total_rows:
        row["state_revision"] = "c" * 64
    expired = client.get(
        "/api/v1/total-return?" + query + "&cursor=" + first["next_cursor"]
    )
    assert expired.status_code == 409
    assert expired.get_json()["error_code"] == "CURSOR_EXPIRED"
