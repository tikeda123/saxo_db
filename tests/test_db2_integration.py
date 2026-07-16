from __future__ import annotations

import json
import os

import psycopg
import pytest

from market_db.connection import MARKET_DB, RESEARCH_DB, connect
from market_db.import_legacy import run_import
from market_db.inspect import fetch_rows
from market_db.research_snapshot import CUTOFF_UTC, snapshot_status


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _db2_ready():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DB2 integration tests")
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM ops.source_file")
            if int(cursor.fetchone()[0]) != 69:
                pytest.skip("DB2 import has not completed")


def test_db2_market_counts_and_source_lineage():
    expected = {
        "raw.market_bar_revision": 636_629,
        "raw.reference_observation": 90_894,
        "curated.market_bar": 394_992,
        "curated.etf_total_return_daily": 54_285,
    }
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            for relation, count in expected.items():
                cursor.execute(f"SELECT COUNT(*) FROM {relation}")
                assert int(cursor.fetchone()[0]) == count
            cursor.execute("SELECT COUNT(*), SUM(row_count) FROM ops.source_file")
            assert cursor.fetchone() == (69, 781_808)
            cursor.execute(
                """
                SELECT COUNT(*) FROM ops.source_file sf
                JOIN catalog.source_dataset ds USING (source_dataset_id)
                JOIN analytics.v_data_lineage l USING (source_file_id)
                WHERE CASE WHEN ds.dataset_kind='total_return'
                    THEN l.curated_rows <> sf.row_count OR l.raw_rows <> 0
                    ELSE l.raw_rows <> sf.row_count END
                """
            )
            assert int(cursor.fetchone()[0]) == 0


def test_curated_and_quality_semantics():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FILTER (WHERE is_complete), COUNT(*) FILTER (WHERE NOT is_complete) FROM curated.market_bar")
            assert cursor.fetchone() == (394_979, 13)
            cursor.execute("SELECT quality_status, COUNT(*) FROM curated.etf_total_return_daily GROUP BY quality_status ORDER BY 1")
            assert cursor.fetchall() == [("PASS", 54_283), ("WARN", 2)]
            cursor.execute("SELECT COUNT(*) FROM quality.event WHERE status='OPEN' AND rule_id='source_series_quality_gate'")
            assert int(cursor.fetchone()[0]) == 5
            cursor.execute("SELECT DISTINCT coverage_status FROM analytics.v_data_coverage")
            assert {row[0] for row in cursor.fetchall()} <= {"PASS", "WARN", "NOT_EVALUATED"}


def test_import_is_idempotent():
    result = run_import()
    assert result["imported_files"] == 0
    assert result["skipped_files"] == 69
    assert result["imported_source_rows"] == 0


def test_inspection_returns_real_inventory_and_lineage():
    assert fetch_rows(MARKET_DB, "inventory")
    assert len(fetch_rows(MARKET_DB, "lineage")) == 69
    assert fetch_rows(MARKET_DB, "quality")


def test_research_snapshot_is_cutoff_bounded_and_read_only():
    status = snapshot_status()
    assert status["snapshot_count"] == 1
    assert status["default_transaction_read_only"] == "on"
    assert status["raw_max_time_utc"] <= CUTOFF_UTC
    assert status["curated_max_time_utc"] <= CUTOFF_UTC
    assert status["total_return_max_date"] <= "2024-06-28"
    with connect("v13_research_reader", RESEARCH_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cursor.execute("CREATE TABLE public.db2_reader_must_fail(id integer)")
