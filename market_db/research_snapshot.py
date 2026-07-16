"""Create and freeze the DB2 physical research snapshot without database links."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from .connection import ADMIN_DB, MARKET_DB, RESEARCH_DB, connect, project_root
from .import_legacy import INVENTORY_PATH, sha256_file


CUTOFF_UTC = "2024-06-28T23:59:59Z"
CUTOFF_DATE = "2024-06-28"
CONTENT_MANIFEST = Path("manifests/db2_research_snapshot_content.json")
DUMP_MANIFEST = Path("manifests/db2_research_snapshot_dump.json")
DUMP_PATH = Path("backups/postgres/saxo_research_v13_db2.dump")


COPY_SPECS = (
    ("catalog.source_dataset", "TRUE"),
    ("catalog.session_calendar", "TRUE"),
    ("catalog.session_interval", "TRUE"),
    ("catalog.instrument", "TRUE"),
    ("ops.ingestion_run", "trigger = 'DB2_LEGACY_IMPORT'"),
    ("ops.source_file", "ingestion_run_id IN (SELECT ingestion_run_id FROM ops.ingestion_run WHERE trigger='DB2_LEGACY_IMPORT')"),
    ("ops.watermark", "TRUE"),
    ("raw.market_bar_revision", f"time_utc <= '{CUTOFF_UTC}'::timestamptz"),
    (
        "raw.reference_observation",
        f"observation_time_utc <= '{CUTOFF_UTC}'::timestamptz OR layer = 'research_metadata'",
    ),
    ("curated.market_bar", f"time_utc <= '{CUTOFF_UTC}'::timestamptz"),
    ("curated.etf_total_return_daily", f"date <= '{CUTOFF_DATE}'::date"),
    ("derived.market_bar_4h", f"time_utc <= '{CUTOFF_UTC}'::timestamptz"),
    ("derived.market_bar_1d_risk", f"session_date <= '{CUTOFF_DATE}'::date"),
    ("quality.event", "TRUE"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    absolute = project_root() / path
    absolute.parent.mkdir(parents=True, exist_ok=True)
    temporary = absolute.with_suffix(absolute.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, absolute)


def _columns(cursor: Any, relation: str) -> list[str]:
    schema, table = relation.split(".", 1)
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    columns = [str(row[0]) for row in cursor.fetchall()]
    if not columns:
        raise RuntimeError(f"missing snapshot relation: {relation}")
    return columns


def expected_snapshot_content() -> dict[str, Any]:
    counts: dict[str, int] = {}
    boundaries: dict[str, Any] = {}
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_snapshot_plan") as conn:
        with conn.cursor() as cursor:
            for relation, where in COPY_SPECS:
                cursor.execute(f"SELECT COUNT(*) FROM {relation} WHERE {where}")
                counts[relation] = int(cursor.fetchone()[0])
            for name, query in {
                "raw_max_time_utc": f"SELECT MAX(time_utc) FROM raw.market_bar_revision WHERE time_utc <= '{CUTOFF_UTC}'::timestamptz",
                "curated_max_time_utc": f"SELECT MAX(time_utc) FROM curated.market_bar WHERE time_utc <= '{CUTOFF_UTC}'::timestamptz",
                "total_return_max_date": f"SELECT MAX(date) FROM curated.etf_total_return_daily WHERE date <= '{CUTOFF_DATE}'::date",
                "reference_max_time_utc": f"SELECT MAX(observation_time_utc) FROM raw.reference_observation WHERE observation_time_utc <= '{CUTOFF_UTC}'::timestamptz",
            }.items():
                cursor.execute(query)
                value = cursor.fetchone()[0]
                boundaries[name] = None if value is None else value.isoformat()
            cursor.execute(f"SELECT COUNT(*) FROM raw.market_bar_revision WHERE time_utc > '{CUTOFF_UTC}'::timestamptz")
            boundaries["raw_post_cutoff_rows_excluded"] = int(cursor.fetchone()[0])
            cursor.execute(f"SELECT COUNT(*) FROM curated.market_bar WHERE time_utc > '{CUTOFF_UTC}'::timestamptz")
            boundaries["curated_post_cutoff_rows_excluded"] = int(cursor.fetchone()[0])
            cursor.execute(f"SELECT COUNT(*) FROM curated.etf_total_return_daily WHERE date > '{CUTOFF_DATE}'::date")
            boundaries["total_return_post_cutoff_rows_excluded"] = int(cursor.fetchone()[0])
    return {
        "schema_version": 1,
        "phase": "DB2",
        "plan_id": "category_specific_intraday_strategy_v13",
        "research_line_id": "v13categoryintraday",
        "source_database": MARKET_DB,
        "snapshot_database": RESEARCH_DB,
        "cutoff_utc": CUTOFF_UTC,
        "created_at_utc": _utc_now(),
        "source_inventory_relative_path": str(INVENTORY_PATH),
        "source_inventory_sha256": sha256_file(project_root() / INVENTORY_PATH),
        "table_counts_before_snapshot_registry_row": counts,
        "boundaries": boundaries,
        "FDW_or_dblink_used": False,
    }


def _target_has_data(cursor: Any) -> tuple[int, int]:
    cursor.execute("SELECT COUNT(*) FROM ops.research_snapshot")
    snapshots = int(cursor.fetchone()[0])
    cursor.execute(
        "SELECT (SELECT COUNT(*) FROM raw.market_bar_revision) + "
        "(SELECT COUNT(*) FROM raw.reference_observation) + "
        "(SELECT COUNT(*) FROM curated.market_bar) + "
        "(SELECT COUNT(*) FROM curated.etf_total_return_daily)"
    )
    payload_rows = int(cursor.fetchone()[0])
    return snapshots, payload_rows


def _stream_copy(source_cursor: Any, target_cursor: Any, relation: str, where: str) -> int:
    columns = _columns(source_cursor, relation)
    target_columns = _columns(target_cursor, relation)
    if columns != target_columns:
        raise RuntimeError(f"snapshot column mismatch: {relation}")
    column_sql = ", ".join(f'"{column}"' for column in columns)
    copied = 0
    with source_cursor.copy(
        f"COPY (SELECT {column_sql} FROM {relation} WHERE {where}) TO STDOUT"
    ) as source_copy:
        with target_cursor.copy(f"COPY {relation} ({column_sql}) FROM STDIN") as target_copy:
            for block in source_copy:
                target_copy.write(block)
                copied += bytes(block).count(b"\n")
    return copied


def _set_sequences(cursor: Any) -> None:
    for relation, column in (
        ("catalog.instrument", "instrument_id"),
        ("ops.ingestion_run", "ingestion_run_id"),
        ("ops.source_file", "source_file_id"),
        ("quality.event", "quality_event_id"),
    ):
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{relation}', '{column}'),
                COALESCE((SELECT MAX({column}) FROM {relation}), 1),
                EXISTS (SELECT 1 FROM {relation})
            )
            """
        )


def _insert_snapshot_row(
    cursor: Any,
    content: dict[str, Any],
    content_sha: str,
    *,
    dump_sha: str | None = None,
    dump_size: int | None = None,
    restore_list_pass: bool | None = None,
) -> None:
    cursor.execute(
        """
        INSERT INTO ops.research_snapshot (
            plan_id, research_line_id, cutoff_utc, source_database,
            source_manifest_sha256, row_counts_json, snapshot_sha256,
            frozen_at_utc, status, snapshot_manifest_relative_path,
            dump_relative_path, dump_sha256, dump_size_bytes, dump_pg_restore_list_pass
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),'FROZEN',%s,%s,%s,%s,%s)
        """,
        (
            content["plan_id"], content["research_line_id"], content["cutoff_utc"],
            content["source_database"], content["source_inventory_sha256"],
            Jsonb(content["table_counts_before_snapshot_registry_row"]), content_sha,
            str(CONTENT_MANIFEST), str(DUMP_PATH) if dump_sha else None,
            dump_sha, dump_size, restore_list_pass,
        ),
    )


def _freeze_database_default() -> None:
    with connect("postgres", ADMIN_DB, autocommit=True, application_name="saxo_db_snapshot_freeze") as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'ALTER DATABASE "{RESEARCH_DB}" SET default_transaction_read_only TO on')


def _create_dump(content_sha: str) -> dict[str, Any]:
    absolute = project_root() / DUMP_PATH
    absolute.parent.mkdir(parents=True, exist_ok=True)
    with absolute.open("wb") as output:
        process = subprocess.run(
            [
                "docker", "compose", "-p", "saxo-market-data", "exec", "-T", "postgres",
                "pg_dump", "-U", "postgres", "-d", RESEARCH_DB, "-Fc",
            ],
            cwd=project_root(),
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if process.returncode != 0:
        raise RuntimeError("research snapshot pg_dump failed")
    with absolute.open("rb") as dump_stream:
        restore_check = subprocess.run(
            [
                "docker", "compose", "-p", "saxo-market-data", "exec", "-T", "postgres",
                "pg_restore", "--list",
            ],
            cwd=project_root(),
            stdin=dump_stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    result = {
        "schema_version": 1,
        "phase": "DB2",
        "database": RESEARCH_DB,
        "dump_relative_path": str(DUMP_PATH),
        "dump_sha256": sha256_file(absolute),
        "dump_size_bytes": absolute.stat().st_size,
        "format": "pg_dump_custom",
        "pg_restore_list_pass": restore_check.returncode == 0,
        "research_snapshot_content_manifest": str(CONTENT_MANIFEST),
        "research_snapshot_content_sha256": content_sha,
        "restore_smoke_test_status": "LOCKED_UNTIL_DB4",
        "created_at_utc": _utc_now(),
    }
    if not result["pg_restore_list_pass"]:
        raise RuntimeError("research snapshot pg_restore --list failed")
    _write_json(DUMP_MANIFEST, result)
    return result


def _register_market_snapshot(content: dict[str, Any], content_sha: str, dump: dict[str, Any]) -> None:
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_snapshot_registry") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM ops.research_snapshot WHERE snapshot_sha256=%s",
                (content_sha,),
            )
            if int(cursor.fetchone()[0]) == 0:
                _insert_snapshot_row(
                    cursor,
                    content,
                    content_sha,
                    dump_sha=dump["dump_sha256"],
                    dump_size=dump["dump_size_bytes"],
                    restore_list_pass=True,
                )
        conn.commit()


def _existing_snapshot() -> tuple[int, int, str | None]:
    with connect("saxo_migrator", RESEARCH_DB, application_name="saxo_db_snapshot_preflight") as conn:
        with conn.cursor() as cursor:
            snapshots, payload_rows = _target_has_data(cursor)
            cursor.execute("SELECT MAX(snapshot_sha256) FROM ops.research_snapshot")
            snapshot_sha = cursor.fetchone()[0]
    return snapshots, payload_rows, None if snapshot_sha is None else str(snapshot_sha).strip()


def _load_existing_content(expected_sha: str) -> dict[str, Any]:
    path = project_root() / CONTENT_MANIFEST
    if not path.is_file():
        raise RuntimeError("research snapshot exists but its content manifest is missing")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise RuntimeError("research snapshot content manifest checksum mismatch")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_existing_dump(content_sha: str) -> dict[str, Any] | None:
    manifest_path = project_root() / DUMP_MANIFEST
    if not manifest_path.is_file():
        return None
    dump = json.loads(manifest_path.read_text(encoding="utf-8"))
    dump_path = project_root() / dump["dump_relative_path"]
    if dump.get("research_snapshot_content_sha256") != content_sha:
        raise RuntimeError("research snapshot dump manifest points to different content")
    if not dump_path.is_file() or sha256_file(dump_path) != dump.get("dump_sha256"):
        raise RuntimeError("research snapshot dump checksum mismatch")
    if dump.get("pg_restore_list_pass") is not True:
        raise RuntimeError("research snapshot dump has not passed pg_restore --list")
    return dump


def create_snapshot() -> dict[str, Any]:
    snapshots, payload_rows, existing_sha = _existing_snapshot()
    if snapshots or payload_rows:
        if snapshots != 1 or existing_sha is None:
            raise RuntimeError("research database is not empty and has no complete DB2 snapshot")
        content = _load_existing_content(existing_sha)
        dump = _load_existing_dump(existing_sha)
        if dump is None:
            _freeze_database_default()
            dump = _create_dump(existing_sha)
            _register_market_snapshot(content, existing_sha, dump)
            return {
                "content_sha256": existing_sha,
                "dump": dump,
                "status": "resumed_dump",
            }
        _register_market_snapshot(content, existing_sha, dump)
        return {"content_sha256": existing_sha, "dump": dump, "status": "skipped_existing"}

    content = expected_snapshot_content()
    _write_json(CONTENT_MANIFEST, content)
    content_sha = sha256_file(project_root() / CONTENT_MANIFEST)

    with connect("saxo_migrator", RESEARCH_DB, application_name="saxo_db_snapshot_target") as target:
        with target.cursor() as target_cursor:
            snapshots, payload_rows = _target_has_data(target_cursor)
        if snapshots or payload_rows:
            raise RuntimeError("research database changed after the DB2 snapshot preflight")

        copied: dict[str, int] = {}
        with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_snapshot_source") as source:
            with target.transaction():
                with source.cursor() as source_cursor, target.cursor() as target_cursor:
                    for relation, where in COPY_SPECS:
                        copied[relation] = _stream_copy(source_cursor, target_cursor, relation, where)
                    _set_sequences(target_cursor)
                    _insert_snapshot_row(target_cursor, content, content_sha)
        expected = content["table_counts_before_snapshot_registry_row"]
        if copied != expected:
            raise RuntimeError("snapshot copied-row counts do not match expected counts")

    _freeze_database_default()
    dump = _create_dump(content_sha)
    _register_market_snapshot(content, content_sha, dump)
    return {"content_sha256": content_sha, "copied": copied, "dump": dump, "status": "created"}


def snapshot_status() -> dict[str, Any]:
    with connect("saxo_migrator", RESEARCH_DB, application_name="saxo_db_snapshot_status") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            read_only = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*), MAX(cutoff_utc), MAX(snapshot_sha256) FROM ops.research_snapshot")
            count, cutoff, snapshot_sha = cursor.fetchone()
            cursor.execute("SELECT MAX(time_utc) FROM raw.market_bar_revision")
            raw_max = cursor.fetchone()[0]
            cursor.execute("SELECT MAX(time_utc) FROM curated.market_bar")
            curated_max = cursor.fetchone()[0]
            cursor.execute("SELECT MAX(date) FROM curated.etf_total_return_daily")
            total_return_max = cursor.fetchone()[0]
    return {
        "curated_max_time_utc": None if curated_max is None else curated_max.isoformat(),
        "cutoff_utc": None if cutoff is None else cutoff.isoformat(),
        "default_transaction_read_only": read_only,
        "raw_max_time_utc": None if raw_max is None else raw_max.isoformat(),
        "snapshot_count": int(count),
        "snapshot_sha256": None if snapshot_sha is None else str(snapshot_sha).strip(),
        "total_return_max_date": None if total_return_max is None else total_return_max.isoformat(),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or inspect the frozen DB2 research snapshot")
    parser.add_argument("command", choices=("create", "status"), nargs="?", default="status")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = create_snapshot() if args.command == "create" else snapshot_status()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
