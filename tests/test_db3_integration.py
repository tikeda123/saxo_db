from __future__ import annotations

import os

import psycopg
import pytest

from market_db.connection import MARKET_DB, RESEARCH_DB, connect
from market_db.derive_bars import DERIVATION_VERSION, rebuild
from market_db.incremental_update import incremental_status


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _db3_ready():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DB3 integration tests")
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM ops.schema_migration WHERE migration_number='0012')")
            if not cursor.fetchone()[0]:
                pytest.skip("DB3 migration has not completed")


def test_canonical_calendars_and_watermarks_are_registered():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.session_calendar_id, c.metadata_json->>'verification_status',
                       COUNT(DISTINCT i.instrument_id)
                FROM catalog.session_calendar c
                JOIN catalog.instrument i USING (session_calendar_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                GROUP BY c.session_calendar_id, c.metadata_json
                ORDER BY c.session_calendar_id
                """
            )
            assert cursor.fetchall() == [("SBFX_24X5", "PROVISIONAL", 2), ("XNYS_US_EQUITY", "VERIFIED", 11)]
            cursor.execute(
                """
                SELECT COUNT(*) FROM ops.watermark w JOIN catalog.instrument i USING(instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM' AND w.horizon_minutes=60
                """
            )
            assert int(cursor.fetchone()[0]) == 13


def test_incremental_status_reads_operational_state_without_mutation():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT data_status,COUNT(*) FROM ops.watermark GROUP BY data_status")
            before = {str(status): int(count) for status, count in cursor.fetchall()}
    status = incremental_status()
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT data_status,COUNT(*) FROM ops.watermark GROUP BY data_status")
            after = {str(value): int(count) for value, count in cursor.fetchall()}
    assert status["watermarks"] == before == after
    assert set(status["runs"]) <= {"PASS", "FAILED", "BLOCKED"}


def test_derived_bars_use_only_completed_pass_1h_and_are_idempotent():
    with connect("saxo_ingest", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                first = rebuild(cursor)
                second = rebuild(cursor)
                assert first["inserted_4h"] == second["inserted_4h"] > 0
                assert first["inserted_1d"] == second["inserted_1d"] > 0
                cursor.execute(
                    "SELECT COUNT(*) FROM derived.market_bar_4h WHERE derivation_version=%s AND quality_status='FAIL'",
                    (DERIVATION_VERSION,),
                )
                assert int(cursor.fetchone()[0]) == 0


def test_coverage_separates_missing_and_out_of_session_rows():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT coverage_status, missing_rows, out_of_session_rows
                FROM analytics.v_data_coverage c
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                """
            )
            rows = cursor.fetchall()
            assert len(rows) == 13
            assert all(status != "FAIL" for status, _, _ in rows)
            assert any(out_of_session > 0 for status, _, out_of_session in rows if status == "WARN")


def test_curated_watermark_and_derived_mutations_roll_back_together():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT MAX(close) FROM curated.market_bar")
            original_close = cursor.fetchone()[0]
            cursor.execute("SELECT MAX(updated_at_utc) FROM ops.watermark")
            original_watermark = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM derived.market_bar_4h")
            original_derived = int(cursor.fetchone()[0])
    with connect("saxo_ingest", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE curated.market_bar SET close=close WHERE (instrument_id,horizon_minutes,time_utc,price_basis) IN "
                    "(SELECT instrument_id,horizon_minutes,time_utc,price_basis FROM curated.market_bar LIMIT 1)"
                )
                cursor.execute("UPDATE ops.watermark SET updated_at_utc=clock_timestamp()")
                rebuild(cursor)
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT MAX(close) FROM curated.market_bar")
            assert cursor.fetchone()[0] == original_close
            cursor.execute("SELECT MAX(updated_at_utc) FROM ops.watermark")
            assert cursor.fetchone()[0] == original_watermark
            cursor.execute("SELECT COUNT(*) FROM derived.market_bar_4h")
            assert int(cursor.fetchone()[0]) == original_derived


def test_full_refetch_guard_rejects_active_watermark_without_deleting_data():
    with connect("saxo_ingest", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ops.ingestion_run(trigger,environment,status) "
                    "VALUES ('manual_db3_full_refetch','SIM','RUNNING') RETURNING ingestion_run_id"
                )
                run_id = int(cursor.fetchone()[0])
                cursor.execute("SELECT instrument_id FROM ops.watermark WHERE data_status='ACTIVE' LIMIT 1")
                instrument_id = int(cursor.fetchone()[0])
                with pytest.raises(psycopg.errors.RaiseException, match="guard condition failed"):
                    cursor.execute("CALL curated.prepare_full_refetch(%s,%s)", (run_id, instrument_id))


def test_research_database_remains_frozen_and_has_no_db3_migration():
    with connect("saxo_migrator", RESEARCH_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            assert cursor.fetchone()[0] == "on"
            cursor.execute("SELECT COUNT(*) FROM ops.schema_migration WHERE migration_number IN ('0010','0011','0012')")
            assert int(cursor.fetchone()[0]) == 0
