from __future__ import annotations

import os

import psycopg
import pytest

from market_db.connection import MARKET_DB, connect


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _integration_enabled():
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for DMI1 integration tests")


def test_reader_contract_exposes_stable_identity_and_fail_closed_events():
    with connect("saxo_app_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT instrument_id, instrument_key FROM analytics.v_data_inventory "
                "WHERE instrument_id IS NOT NULL LIMIT 1"
            )
            instrument_id, instrument_key = cursor.fetchone()
            assert instrument_id is not None
            assert instrument_key
            cursor.execute(
                "SELECT scope_kind, applicability, current_blocker "
                "FROM quality.v_open_event WHERE severity IN ('ERROR','CRITICAL') LIMIT 1"
            )
            scope_kind, applicability, current_blocker = cursor.fetchone()
            assert scope_kind in {"INSTRUMENT", "SERIES", "DATASET", "RUN", "LAYER", "GLOBAL", "UNKNOWN"}
            assert applicability in {"CURRENT", "HISTORICAL", "UNKNOWN"}
            assert current_blocker is (applicability in {"CURRENT", "UNKNOWN"})


def test_reader_cannot_read_review_base_tables():
    with connect("saxo_app_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM quality.event_scope")
    with connect("saxo_app_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM quality.event_applicability_review")


def test_operator_can_append_scope_and_review_but_has_no_direct_dml():
    with connect("postgres", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO quality.event(rule_id,severity,action) "
                    "VALUES ('dmi1_fixture','CRITICAL','review') RETURNING quality_event_id"
                )
                event_id = cursor.fetchone()[0]
                cursor.execute("SET LOCAL ROLE saxo_ops_operator")
                cursor.execute(
                    "CALL quality.record_event_scope(%s,'GLOBAL',NULL,NULL,NULL,%s::jsonb,%s)",
                    (event_id, '{"evidence":"fixture"}', "dmi1-test"),
                )
                cursor.execute(
                    "CALL quality.review_event_applicability(%s,'HISTORICAL',%s,NULL,%s)",
                    (event_id, "superseded fixture", "dmi1-test"),
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():
                        cursor.execute(
                            "INSERT INTO quality.event_scope(quality_event_id,scope_kind,recorded_by) "
                            "VALUES (%s,'GLOBAL','bypass')",
                            (event_id,),
                        )
                cursor.execute("RESET ROLE")
                cursor.execute(
                    "SELECT scope_kind, applicability, current_blocker "
                    "FROM quality.v_event_status WHERE quality_event_id=%s",
                    (event_id,),
                )
                assert cursor.fetchone() == ("GLOBAL", "HISTORICAL", False)


def test_new_event_gets_fail_closed_defaults_and_known_rule_scope():
    with connect("postgres", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO quality.event(instrument_id,horizon_minutes,rule_id,severity,action) "
                    "VALUES (13,60,'db3_atomic_run_gate','CRITICAL','fixture') "
                    "RETURNING quality_event_id"
                )
                known_id = int(cursor.fetchone()[0])
                cursor.execute(
                    "INSERT INTO quality.event(rule_id,severity,action) "
                    "VALUES ('unrecognized_dmi1_fixture','ERROR','fixture') "
                    "RETURNING quality_event_id"
                )
                unknown_id = int(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT scope_kind,affected_layer,price_basis,applicability,current_blocker "
                    "FROM quality.v_event_status WHERE quality_event_id=%s",
                    (known_id,),
                )
                assert cursor.fetchone() == (
                    "RUN", "curated", "bid_ask_mid", "CURRENT", True
                )
                cursor.execute(
                    "SELECT scope_kind,applicability,current_blocker "
                    "FROM quality.v_event_status WHERE quality_event_id=%s",
                    (unknown_id,),
                )
                assert cursor.fetchone() == ("UNKNOWN", "UNKNOWN", True)


def test_all_series_pass_appends_atomic_supersession_review():
    with connect("postgres", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ops.ingestion_run(trigger,environment,status) "
                    "VALUES ('fixture_failed','sim','BLOCKED') RETURNING ingestion_run_id"
                )
                failed_run_id = int(cursor.fetchone()[0])
                cursor.execute(
                    "INSERT INTO quality.event(ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,action) "
                    "VALUES (%s,9,60,'db3_atomic_run_gate','CRITICAL','fixture') "
                    "RETURNING quality_event_id",
                    (failed_run_id,),
                )
                event_id = int(cursor.fetchone()[0])
                cursor.execute(
                    "INSERT INTO ops.ingestion_run(trigger,environment,status) "
                    "VALUES ('manual_db3','sim','RUNNING') RETURNING ingestion_run_id"
                )
                pass_run_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run_instrument_scope (
                        ingestion_run_id,instrument_id,instrument_key,uic,asset_type,
                        horizon_minutes,price_basis
                    )
                    SELECT %s,instrument_id,lower(market_key),uic,asset_type,60,'native_ohlc'
                    FROM catalog.instrument WHERE instrument_id=9
                    """,
                    (pass_run_id,),
                )
                cursor.execute(
                    "UPDATE ops.ingestion_run SET status='PASS',successful_series=1,"
                    "finished_at_utc=clock_timestamp() WHERE ingestion_run_id=%s",
                    (pass_run_id,),
                )
                cursor.execute(
                    "SELECT applicability,superseded_by_ingestion_run_id,reviewed_by,current_blocker "
                    "FROM quality.v_event_status WHERE quality_event_id=%s",
                    (event_id,),
                )
                assert cursor.fetchone() == (
                    "HISTORICAL", pass_run_id, "system:fx_run_scope_supersession_v1", False
                )


def test_eurusd_scoped_failure_does_not_apply_to_usdjpy_but_unknown_global_does():
    applicability_sql = """
        SELECT COUNT(*) FROM quality.v_open_event e
        WHERE e.quality_event_id=%s AND e.severity IN ('ERROR','CRITICAL')
          AND (
              e.instrument_key=%s
              OR e.scope_kind IN ('GLOBAL','UNKNOWN','DATASET','LAYER')
              OR (
                  e.instrument_id IS NULL AND e.scope_kind='RUN'
                  AND (
                      NOT (e.scope_evidence ? 'selected_instrument_keys')
                      OR (e.scope_evidence->'selected_instrument_keys') ? %s
                  )
              )
          )
    """
    with connect("postgres", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run(trigger,environment,status,requested_series)
                    VALUES ('scheduled_fx_hourly','SIM','BLOCKED','[]'::jsonb)
                    RETURNING ingestion_run_id
                    """
                )
                run_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run_instrument_scope (
                        ingestion_run_id,instrument_id,instrument_key,uic,asset_type,
                        horizon_minutes,price_basis
                    )
                    SELECT %s,instrument_id,lower(market_key),uic,asset_type,60,'bid_ask_mid'
                    FROM catalog.instrument WHERE market_key='eurusd'
                    RETURNING instrument_id
                    """,
                    (run_id,),
                )
                eurusd_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO quality.event(
                        ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,action
                    ) VALUES (%s,%s,60,'db3_atomic_run_gate','CRITICAL','fixture')
                    RETURNING quality_event_id
                    """,
                    (run_id, eurusd_id),
                )
                scoped_id = int(cursor.fetchone()[0])
                cursor.execute(applicability_sql, (scoped_id, "eurusd", "eurusd"))
                assert int(cursor.fetchone()[0]) == 1
                cursor.execute(applicability_sql, (scoped_id, "usdjpy", "usdjpy"))
                assert int(cursor.fetchone()[0]) == 0

                cursor.execute(
                    """
                    INSERT INTO quality.event(rule_id,severity,action)
                    VALUES ('unrecognized_global_fixture','CRITICAL','fixture')
                    RETURNING quality_event_id
                    """
                )
                global_id = int(cursor.fetchone()[0])
                for key in ("eurusd", "usdjpy"):
                    cursor.execute(applicability_sql, (global_id, key, key))
                    assert int(cursor.fetchone()[0]) == 1


def test_legacy_reconciliation_exit_gate_is_satisfied():
    with connect("saxo_app_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM quality.v_open_event "
                "WHERE severity IN ('ERROR','CRITICAL') AND applicability='UNKNOWN'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT applicability,COUNT(*) FROM quality.v_open_event "
                "GROUP BY applicability ORDER BY applicability"
            )
            counts = dict(cursor.fetchall())
            # Live failed runs may add CURRENT evidence. Exact counts are not
            # an invariant; every atomic blocker must instead have explicit
            # immutable run/instrument scope and no UNKNOWN applicability.
            assert counts["CURRENT"] >= 5
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM quality.v_open_event e
                WHERE e.rule_id='db3_atomic_run_gate'
                  AND e.severity IN ('ERROR','CRITICAL')
                  AND e.applicability='CURRENT'
                  AND NOT (
                    e.instrument_id IS NOT NULL
                    OR (
                      e.scope_kind='RUN'
                      AND e.scope_evidence ? 'selected_instrument_keys'
                    )
                  )
                """
            )
            assert cursor.fetchone()[0] == 0
            # New failed runs may add evidence, but a later PASS run must
            # classify it as historical rather than reopen the UNKNOWN gate.
            assert counts["HISTORICAL"] >= 17
