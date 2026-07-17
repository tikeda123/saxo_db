from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

import market_db.migrate as migrate_module
from market_db.connection import FORWARD_DB, MARKET_DB, RESEARCH_DB, connect, raw_connection_kwargs
from market_db.inspect import fetch_rows
from market_db.migrate import MigrationError, apply_database_migrations, run_all


pytestmark = pytest.mark.integration


def require_integration() -> None:
    if os.environ.get("SAXO_DB_INTEGRATION") != "1":
        pytest.skip("set SAXO_DB_INTEGRATION=1 for the DB1 runtime gate")


@pytest.fixture(autouse=True)
def _integration_enabled():
    require_integration()


def test_server_version_timezone_and_migration_rerun():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW server_version")
            assert cursor.fetchone()[0].startswith("18.4")
            cursor.execute("SHOW timezone")
            assert cursor.fetchone()[0] == "UTC"
    result = run_all()
    assert result["migrations"]
    assert all(item["status"] == "skipped" for item in result["migrations"])


def test_role_attributes_object_owners_and_forbidden_extensions():
    with connect("postgres", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = 'saxo_db_owner'"
            )
            assert cursor.fetchone() == (False, False, False, False, False, False)
            cursor.execute(
                "SELECT DISTINCT pg_get_userbyid(c.relowner) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','S')",
                (["catalog", "ops", "raw", "staging", "curated", "derived", "quality", "analytics"],),
            )
            assert {row[0] for row in cursor.fetchall()} == {"saxo_db_owner"}
            cursor.execute(
                "SELECT DISTINCT pg_get_userbyid(p.proowner) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname = ANY(%s)",
                (["catalog", "ops", "raw", "staging", "curated", "derived", "quality", "analytics"],),
            )
            assert {row[0] for row in cursor.fetchall()} == {"saxo_db_owner"}
            cursor.execute("SELECT extname FROM pg_extension WHERE extname IN ('dblink','postgres_fdw')")
            assert cursor.fetchall() == []


def test_database_connect_boundaries():
    denied = (
        ("saxo_ingest", RESEARCH_DB),
        ("saxo_ingest", FORWARD_DB),
        ("saxo_app_reader", RESEARCH_DB),
        ("v13_research_reader", MARKET_DB),
        ("v13_forward_writer", MARKET_DB),
    )
    for role, database in denied:
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**raw_connection_kwargs(role, database))


def test_readers_can_read_only_allowlisted_views():
    market_views = {
        command: fetch_rows(MARKET_DB, command)
        for command in ("inventory", "coverage", "freshness", "runs", "quality", "lineage")
    }
    market_views["lineage"] = fetch_rows(MARKET_DB, "lineage", limit=10_000)
    assert all(isinstance(rows, list) for rows in market_views.values())
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM ops.source_file")
            source_files = int(cursor.fetchone()[0])
    if source_files == 0:
        assert all(rows == [] for rows in market_views.values())
    else:
        # DB2 contributes the immutable 69-file baseline. DB3 live acquisition
        # appends auditable source files, so the operational total can grow.
        assert source_files >= 69
        assert all(market_views[command] for command in market_views)
        assert len(market_views["lineage"]) == source_files
    assert isinstance(fetch_rows(MARKET_DB, "storage"), list)
    assert len(fetch_rows(MARKET_DB, "backups")) == 3
    for command in ("inventory", "coverage", "lineage", "storage"):
        assert isinstance(fetch_rows(RESEARCH_DB, command), list)

    with connect("saxo_app_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM raw.market_bar_revision")
    with connect("saxo_analyst_reader", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cursor.execute("CREATE TABLE public.reader_must_fail (id integer)")


def test_ingest_is_dml_only_and_fixture_rolls_back():
    with connect("saxo_ingest", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO catalog.source_dataset "
                    "(source_dataset_id,dataset_name,provider,environment,dataset_kind,price_basis,authoritative_layer,research_eligibility) "
                    "VALUES ('db1_fixture','fixture','test','sim','raw_market','mid','raw','not_eligible')"
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute("CREATE TABLE public.ingest_must_fail (id integer)")


def test_ingest_cannot_bypass_quality_lifecycle_procedures():
    with connect("saxo_ingest", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO quality.event(rule_id,severity,action) "
                    "VALUES ('db1_ingest_fixture','WARN','review') RETURNING quality_event_id"
                )
                event_id = cursor.fetchone()[0]
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute(
                        "UPDATE quality.event SET status='ACKNOWLEDGED' WHERE quality_event_id=%s",
                        (event_id,),
                    )


def test_ops_procedures_work_without_direct_dml_and_roll_back():
    # The superuser is used only as a fixture harness; SET ROLE enforces the
    # operator's effective privileges while keeping the fixture rollbackable.
    with connect("postgres", MARKET_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO quality.event(rule_id,severity,action) "
                    "VALUES ('db1_fixture','WARN','review') RETURNING quality_event_id"
                )
                event_id = cursor.fetchone()[0]
                cursor.execute("SET LOCAL ROLE saxo_ops_operator")
                cursor.execute("CALL ops.start_backup_run(%s, %s, NULL)", (MARKET_DB, "backups/db1-test.dump"))
                backup_id = cursor.fetchone()[0]
                cursor.execute(
                    "CALL ops.finish_backup_run(%s, 'PASS', %s, 1, TRUE, NULL)",
                    (backup_id, "0" * 64),
                )
                cursor.execute(
                    "CALL quality.acknowledge_event(%s, %s, %s)",
                    (event_id, "db1-test", "acknowledged in rollback fixture"),
                )
                cursor.execute(
                    "CALL quality.resolve_event(%s, %s, %s)",
                    (event_id, "db1-test", "resolved in rollback fixture"),
                )
                cursor.execute("RESET ROLE")
                cursor.execute("SELECT status FROM quality.event WHERE quality_event_id = %s", (event_id,))
                assert cursor.fetchone()[0] == "RESOLVED"
                cursor.execute("SET LOCAL ROLE saxo_ops_operator")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute("INSERT INTO ops.backup_run(database_name,status,relative_path) VALUES ('saxo_market','RUNNING','x')")


def test_forward_writer_has_procedure_only_access():
    with connect("postgres", FORWARD_DB) as conn:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO catalog.source_dataset(source_dataset_id,dataset_name,provider,environment,dataset_kind,price_basis) "
                    "VALUES ('db1_fixture','fixture','test','sim','raw_market','mid')"
                )
                cursor.execute(
                    "INSERT INTO catalog.instrument(provider,environment,market_key,symbol,uic,asset_type,category,currency,active_from_utc) "
                    "VALUES ('test','sim','fixture','FIXTURE',1,'FxSpot','fx','USD',clock_timestamp()) "
                    "RETURNING instrument_id"
                )
                instrument_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO ops.ingestion_run(trigger,environment,status) VALUES ('test','sim','RUNNING') "
                    "RETURNING ingestion_run_id"
                )
                ingestion_run_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO ops.source_file(ingestion_run_id,relative_path,sha256,size_bytes,row_count,source_dataset_id) "
                    "VALUES (%s,'data/import/db1-fixture.csv',%s,1,1,'db1_fixture') RETURNING source_file_id",
                    (ingestion_run_id, "0" * 64),
                )
                source_file_id = cursor.fetchone()[0]
                cursor.execute("SET LOCAL ROLE v13_forward_writer")
                cursor.execute(
                    "CALL raw.append_forward_market_bar(%s::bigint,%s::bigint,%s::bigint,"
                    "60::smallint,clock_timestamp(),'mid'::text,%s::jsonb)",
                    (
                        ingestion_run_id,
                        source_file_id,
                        instrument_id,
                        '{"open":1,"high":1,"low":1,"close":1,"is_complete":true,'
                        '"retrieved_at_utc":"2026-07-16T00:00:00Z",'
                        '"payload_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}',
                    ),
                )
                cursor.execute("RESET ROLE")
                cursor.execute("SELECT count(*) FROM raw.market_bar_revision")
                assert cursor.fetchone()[0] == 1
                cursor.execute("SET LOCAL ROLE v13_forward_writer")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute("SELECT * FROM raw.market_bar_revision")
    with connect("v13_forward_writer", FORWARD_DB) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("INSERT INTO raw.market_bar_revision DEFAULT VALUES")


def test_checksum_mismatch_is_refused_before_sql(tmp_path):
    source = Path(__file__).resolve().parents[1] / "db" / "migrations"
    for path in source.glob("*.sql"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    path = tmp_path / "0008_quality_privilege_hardening.sql"
    path.write_text(path.read_text(encoding="utf-8") + "\n-- changed test copy\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="checksum mismatch"):
        apply_database_migrations(MARKET_DB, directory=tmp_path)


def test_failed_migration_rolls_back_ddl_and_history(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "db" / "migrations"
    for path in source.glob("*.sql"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "0015_rollback_probe.sql").write_text(
        "SET LOCAL ROLE saxo_db_owner; "
        "CREATE TABLE ops.db1_rollback_probe(id integer); "
        "SELECT 1 / 0;",
        encoding="utf-8",
    )
    monkeypatch.setitem(migrate_module.MIGRATION_TARGETS, "0015", (MARKET_DB,))
    with pytest.raises(psycopg.errors.DivisionByZero):
        apply_database_migrations(MARKET_DB, directory=tmp_path)
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('ops.db1_rollback_probe')")
            assert cursor.fetchone()[0] is None
            cursor.execute("SELECT count(*) FROM ops.schema_migration WHERE migration_number='0015'")
            assert cursor.fetchone()[0] == 0


def test_market_state_matches_the_active_database_phase():
    with connect("saxo_migrator", MARKET_DB) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ops.source_file")
            source_files = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT (SELECT count(*) FROM catalog.instrument) + "
                "(SELECT count(*) FROM raw.market_bar_revision) + "
                "(SELECT count(*) FROM curated.market_bar) + "
                "(SELECT count(*) FROM derived.market_bar_4h) + "
                "(SELECT count(*) FROM derived.market_bar_1d_risk) + "
                "(SELECT count(*) FROM quality.event) + "
                "(SELECT count(*) FROM ops.backup_run)"
            )
            payload_rows = int(cursor.fetchone()[0])
            if source_files == 0:
                assert payload_rows == 0
            else:
                assert source_files >= 69
                assert payload_rows > 0
