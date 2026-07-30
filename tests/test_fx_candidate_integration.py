from __future__ import annotations

import os

import pytest

from market_db.connection import MARKET_DB, connect


pytestmark = pytest.mark.skipif(
    os.environ.get("SAXO_DB_INTEGRATION") != "1",
    reason="set SAXO_DB_INTEGRATION=1 for PostgreSQL integration tests",
)


def test_candidate_catalog_publication_and_reader_privileges_are_fail_closed():
    with connect(
        "saxo_app_reader", MARKET_DB, application_name="saxo_db_candidate_integration"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.market_key,i.symbol,i.uic,i.asset_type,i.session_calendar_id,
                       p.publication_status,p.consecutive_normal_passes,
                       p.consumer_availability_status,p.research_policy_id,
                       p.provider_advertised_start_utc,
                       p.effective_coverage_start_utc,
                       p.warning_metadata_json
                FROM catalog.instrument i
                JOIN catalog.series_publication_state p USING (instrument_id)
                WHERE i.market_key=ANY(%s) AND p.horizon_minutes=60
                  AND p.price_basis='bid_ask_mid'
                ORDER BY i.market_key
                """,
                (["audusd", "usdcad", "usdchf"],),
            )
            rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT has_table_privilege(current_user,'catalog.series_publication_state','SELECT'),
                       has_table_privilege(current_user,'catalog.series_publication_state','INSERT'),
                       current_setting('transaction_read_only')
                """
            )
            privileges = cursor.fetchone()

    assert [(row[0], row[1], int(row[2]), row[3], row[4]) for row in rows] == [
        ("audusd", "AUDUSD", 4, "FxSpot", "SBFX_24X5"),
        ("usdcad", "USDCAD", 38, "FxSpot", "SBFX_24X5"),
        ("usdchf", "USDCHF", 39, "FxSpot", "SBFX_24X5"),
    ]
    assert all(row[5] in {"CANDIDATE", "STAGING", "PUBLISHED", "BLOCKED"} for row in rows)
    assert all(0 <= int(row[6]) <= 2 for row in rows)
    assert all(row[7] in {"BLOCKED", "AVAILABLE_WITH_WARNINGS"} for row in rows)
    assert all(row[8] == "fx_research_candidate_user_approved_warnings_v1" for row in rows)
    assert all(row[9] is not None and row[10] is not None for row in rows)
    assert all(row[11].get("values_modified") is False for row in rows)
    assert privileges == (True, False, "on")


def test_candidate_gate_ingest_role_has_only_required_quality_reads():
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_candidate_gate_privileges"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT
                    has_schema_privilege(current_user,'analytics','USAGE'),
                    has_table_privilege(current_user,'analytics.v_data_coverage','SELECT'),
                    has_table_privilege(current_user,'analytics.v_data_freshness','SELECT'),
                    has_table_privilege(current_user,'quality.v_open_event','SELECT'),
                    has_table_privilege(current_user,'analytics.v_data_coverage','INSERT'),
                    has_table_privilege(current_user,'quality.v_open_event','UPDATE'),
                    current_setting('transaction_read_only')
                """
            )
            privileges = cursor.fetchone()

    assert privileges == (True, True, True, True, False, False, "on")
