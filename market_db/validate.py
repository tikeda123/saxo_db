"""Evidence-oriented DB1/DB2 validators with no state-changing operations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from .connection import FORWARD_DB, MARKET_DB, RESEARCH_DB, connect, project_root
from .migrate import MIGRATION_TARGETS, list_migrations, migration_number, migration_sha256, validate_applied_checksums


MARKET_TABLES = (
    "catalog.source_dataset",
    "catalog.session_calendar",
    "catalog.session_interval",
    "catalog.instrument",
    "ops.ingestion_run",
    "ops.source_file",
    "ops.watermark",
    "ops.research_snapshot",
    "ops.backup_run",
    "raw.market_bar_revision",
    "raw.reference_observation",
    "curated.market_bar",
    "curated.etf_total_return_daily",
    "derived.market_bar_4h",
    "derived.market_bar_1d_risk",
    "quality.event",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db3_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate immutable DB3 implementation evidence without freezing live row counts."""
    derived = payload.get("derived")
    if not isinstance(derived, dict):
        return False

    def positive_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    return (
        positive_int(derived.get("market_bar_4h_rows"))
        and positive_int(derived.get("market_bar_4h_analysis_eligible_rows"))
        and positive_int(derived.get("market_bar_1d_rows"))
        and positive_int(derived.get("market_bar_1d_analysis_eligible_rows"))
        and derived.get("quality_fail_rows") == 0
    )


def validate_import_inventory() -> dict[str, Any]:
    root = project_root()
    inventory_path = root / "manifests" / "import_file_inventory.csv"
    errors: list[str] = []
    count = rows = size = 0
    with inventory_path.open(newline="", encoding="utf-8") as stream:
        for record in csv.DictReader(stream):
            path = root / record["target_relative_path"]
            count += 1
            rows += int(record["row_count"])
            size += int(record["size_bytes"])
            if not path.is_file():
                errors.append(f"missing:{record['target_relative_path']}")
                continue
            if path.stat().st_size != int(record["size_bytes"]):
                errors.append(f"size:{record['target_relative_path']}")
            if _sha256(path) != record["copied_sha256"]:
                errors.append(f"sha256:{record['target_relative_path']}")
    return {
        "csv_files": count,
        "csv_rows": rows,
        "csv_size_bytes": size,
        "errors": errors,
        "inventory_sha256": _sha256(inventory_path),
        "status": "PASS" if not errors and count == 69 else "FAIL",
    }


def validate_database() -> dict[str, Any]:
    expected_schema_counts = {MARKET_DB: 8, RESEARCH_DB: 8, FORWARD_DB: 4}
    database_results: dict[str, Any] = {}
    for database in (MARKET_DB, RESEARCH_DB, FORWARD_DB):
        with connect("saxo_migrator", database, application_name="saxo_db_validate") as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW server_version")
                version = cursor.fetchone()[0]
                cursor.execute("SHOW timezone")
                timezone_name = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM pg_namespace WHERE nspname = ANY(%s)",
                    (["catalog", "ops", "raw", "staging", "curated", "derived", "quality", "analytics"],),
                )
                schema_count = int(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT COUNT(*) FROM ops.schema_migration WHERE target_database = %s",
                    (database,),
                )
                migration_count = int(cursor.fetchone()[0])
                database_results[database] = {
                    "migration_count": migration_count,
                    "schema_count": schema_count,
                    "server_version": version,
                    "timezone": timezone_name,
                    "status": "PASS" if timezone_name == "UTC" and schema_count == expected_schema_counts[database] else "FAIL",
                }

    zero_counts: dict[str, int] = {}
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_validate_zero") as conn:
        with conn.cursor() as cursor:
            for relation in MARKET_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM {relation}")
                zero_counts[relation] = int(cursor.fetchone()[0])
    # ops.schema_migration is intentionally not part of the zero-data gate.
    zero_data = all(count == 0 for count in zero_counts.values())
    return {"databases": database_results, "market_table_counts": zero_counts, "zero_data": zero_data}


def validate_migrations() -> dict[str, Any]:
    expected = []
    for path in list_migrations():
        number = migration_number(path)
        expected.append(
            {
                "filename": path.name,
                "migration": number,
                "sha256": migration_sha256(path),
                "targets": list(MIGRATION_TARGETS[number]),
            }
        )
    applied = validate_applied_checksums()
    return {"expected": expected, "applied_checks": len(applied), "status": "PASS"}


def validate_compose() -> dict[str, Any]:
    process = subprocess.run(
        ["docker", "compose", "-p", "saxo-market-data", "ps", "--format", "json"],
        cwd=project_root(),
        text=True,
        capture_output=True,
        check=False,
    )
    healthy = process.returncode == 0 and '"Health":"healthy"' in process.stdout
    return {"healthy": healthy, "status": "PASS" if healthy else "FAIL"}


def validate_db2_data() -> dict[str, Any]:
    expected_market = {
        "catalog.source_dataset": 6,
        "catalog.instrument": 18,
        "ops.ingestion_run": 69,
        "ops.source_file": 69,
        "raw.market_bar_revision": 636_629,
        "raw.reference_observation": 90_894,
        "curated.market_bar": 394_992,
        "curated.etf_total_return_daily": 54_285,
        "quality.event": 5,
        "ops.research_snapshot": 1,
    }
    actual_market: dict[str, int] = {}
    details: dict[str, Any] = {}
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_validate_db2_market") as conn:
        with conn.cursor() as cursor:
            for relation in expected_market:
                cursor.execute(f"SELECT COUNT(*) FROM {relation}")
                actual_market[relation] = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT horizon_minutes, COUNT(*), COUNT(*) FILTER (WHERE is_complete) "
                "FROM raw.market_bar_revision GROUP BY horizon_minutes ORDER BY horizon_minutes"
            )
            details["raw_horizons"] = {
                str(horizon): {"rows": int(rows), "complete_rows": int(complete)}
                for horizon, rows, complete in cursor.fetchall()
            }
            cursor.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE is_complete), "
                "COUNT(*) FILTER (WHERE NOT is_complete), "
                "COUNT(*) - COUNT(DISTINCT (instrument_id,horizon_minutes,time_utc,price_basis)) "
                "FROM curated.market_bar"
            )
            curated_rows, completed, incomplete, duplicates = cursor.fetchone()
            details["curated_1h"] = {
                "rows": int(curated_rows),
                "completed_rows": int(completed),
                "incomplete_rows": int(incomplete),
                "duplicate_rows": int(duplicates),
            }
            cursor.execute(
                "SELECT quality_status, COUNT(*) FROM curated.etf_total_return_daily "
                "GROUP BY quality_status ORDER BY quality_status"
            )
            details["total_return_quality"] = {str(status): int(count) for status, count in cursor.fetchall()}
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(row_count),0) FROM ops.source_file")
            file_count, source_rows = cursor.fetchone()
            details["source_registry"] = {"files": int(file_count), "source_rows": int(source_rows)}
            cursor.execute("SELECT COUNT(*) FROM ops.ingestion_run WHERE status <> 'PASS'")
            details["non_pass_ingestion_runs"] = int(cursor.fetchone()[0])
            cursor.execute(
                """
                WITH joined AS (
                    SELECT sf.row_count, ds.dataset_kind, l.raw_rows, l.curated_rows
                    FROM ops.source_file sf
                    JOIN catalog.source_dataset ds
                      ON ds.source_dataset_id = sf.source_dataset_id
                    JOIN analytics.v_data_lineage l
                      ON l.source_file_id = sf.source_file_id
                )
                SELECT COUNT(*) FROM joined
                WHERE CASE
                    WHEN dataset_kind = 'total_return' THEN curated_rows <> row_count OR raw_rows <> 0
                    ELSE raw_rows <> row_count
                END
                """
            )
            details["source_lineage_mismatches"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT DISTINCT coverage_status FROM analytics.v_data_coverage ORDER BY coverage_status")
            details["coverage_statuses"] = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT COUNT(*) FROM quality.event WHERE status='OPEN' AND rule_id='source_series_quality_gate'"
            )
            details["open_source_quality_events"] = int(cursor.fetchone()[0])

    research: dict[str, Any] = {}
    with connect("saxo_migrator", RESEARCH_DB, application_name="saxo_db_validate_db2_research") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            research["default_transaction_read_only"] = str(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*), MAX(snapshot_sha256) FROM ops.research_snapshot")
            snapshot_count, snapshot_sha = cursor.fetchone()
            research["snapshot_count"] = int(snapshot_count)
            research["snapshot_sha256"] = None if snapshot_sha is None else str(snapshot_sha).strip()
            for name, query in {
                "raw_max_time_utc": "SELECT MAX(time_utc) FROM raw.market_bar_revision",
                "curated_max_time_utc": "SELECT MAX(time_utc) FROM curated.market_bar",
                "total_return_max_date": "SELECT MAX(date) FROM curated.etf_total_return_daily",
                "reference_max_time_utc": "SELECT MAX(observation_time_utc) FROM raw.reference_observation",
            }.items():
                cursor.execute(query)
                value = cursor.fetchone()[0]
                research[name] = None if value is None else value.isoformat()
            cursor.execute(
                "SELECT COUNT(*) FROM raw.market_bar_revision WHERE time_utc > '2024-06-28T23:59:59Z'::timestamptz"
            )
            research["raw_post_cutoff_rows"] = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT COUNT(*) FROM curated.market_bar WHERE time_utc > '2024-06-28T23:59:59Z'::timestamptz"
            )
            research["curated_post_cutoff_rows"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM curated.etf_total_return_daily WHERE date > '2024-06-28'::date")
            research["total_return_post_cutoff_rows"] = int(cursor.fetchone()[0])

    content_path = project_root() / "manifests" / "db2_research_snapshot_content.json"
    dump_manifest_path = project_root() / "manifests" / "db2_research_snapshot_dump.json"
    implementation_manifest_path = project_root() / "manifests" / "db2_implementation_manifest.json"
    artifacts: dict[str, Any] = {
        "content_manifest_exists": content_path.is_file(),
        "dump_manifest_exists": dump_manifest_path.is_file(),
        "implementation_manifest_exists": implementation_manifest_path.is_file(),
    }
    if content_path.is_file():
        artifacts["content_manifest_sha256"] = _sha256(content_path)
        artifacts["content_manifest_matches_registry"] = artifacts["content_manifest_sha256"] == research["snapshot_sha256"]
    if dump_manifest_path.is_file():
        dump_manifest = json.loads(dump_manifest_path.read_text(encoding="utf-8"))
        dump_path = project_root() / dump_manifest["dump_relative_path"]
        artifacts["dump_exists"] = dump_path.is_file()
        artifacts["dump_sha256_match"] = dump_path.is_file() and _sha256(dump_path) == dump_manifest["dump_sha256"]
        artifacts["pg_restore_list_pass"] = dump_manifest["pg_restore_list_pass"] is True
    if implementation_manifest_path.is_file():
        implementation_manifest = json.loads(implementation_manifest_path.read_text(encoding="utf-8"))
        mismatches: list[str] = []
        for relative_path, expected in implementation_manifest.get("artifacts", {}).items():
            path = project_root() / relative_path
            if not path.is_file():
                mismatches.append(f"missing:{relative_path}")
                continue
            if path.stat().st_size != int(expected["size_bytes"]):
                mismatches.append(f"size:{relative_path}")
            if _sha256(path) != expected["sha256"]:
                mismatches.append(f"sha256:{relative_path}")
        artifacts["implementation_manifest_status_pass"] = implementation_manifest.get("status") == "PASS"
        artifacts["implementation_artifact_mismatches"] = mismatches

    expected_details = (
        details.get("raw_horizons") == {
            "60": {"rows": 394_992, "complete_rows": 394_979},
            "240": {"rows": 130_389, "complete_rows": 130_376},
            "1440": {"rows": 111_248, "complete_rows": 111_230},
        }
        and details.get("curated_1h") == {
            "rows": 394_992,
            "completed_rows": 394_979,
            "incomplete_rows": 13,
            "duplicate_rows": 0,
        }
        and details.get("total_return_quality") == {"PASS": 54_283, "WARN": 2}
        and details.get("source_registry") == {"files": 69, "source_rows": 781_808}
        and details.get("non_pass_ingestion_runs") == 0
        and details.get("source_lineage_mismatches") == 0
        and details.get("coverage_statuses") == ["NOT_EVALUATED"]
        and details.get("open_source_quality_events") == 5
    )
    research_pass = (
        research.get("default_transaction_read_only") == "on"
        and research.get("snapshot_count") == 1
        and research.get("raw_post_cutoff_rows") == 0
        and research.get("curated_post_cutoff_rows") == 0
        and research.get("total_return_post_cutoff_rows") == 0
        and artifacts.get("content_manifest_matches_registry") is True
        and artifacts.get("dump_sha256_match") is True
        and artifacts.get("pg_restore_list_pass") is True
        and artifacts.get("implementation_manifest_status_pass") is True
        and artifacts.get("implementation_artifact_mismatches") == []
    )
    status = "PASS" if actual_market == expected_market and expected_details and research_pass else "FAIL"
    return {
        "actual_market_counts": actual_market,
        "artifacts": artifacts,
        "details": details,
        "expected_market_counts": expected_market,
        "research": research,
        "status": status,
    }


def validate_db3_data() -> dict[str, Any]:
    canonical_uics = [36590, 31933, 31874, 31871, 34910, 7522053, 7522010,
                      3441903, 31996, 31923, 32664, 21, 42]
    offline: dict[str, Any] = {}
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_validate_db3_market") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT migration_number FROM ops.schema_migration WHERE migration_number IN ('0010','0011','0012') ORDER BY 1"
            )
            offline["market_migrations"] = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT c.session_calendar_id, c.metadata_json->>'verification_status',
                       COUNT(DISTINCT i.instrument_id)
                FROM catalog.session_calendar c
                JOIN catalog.instrument i USING (session_calendar_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM' AND i.uic=ANY(%s)
                GROUP BY c.session_calendar_id, c.metadata_json ORDER BY c.session_calendar_id
                """,
                (canonical_uics,),
            )
            offline["calendars"] = [
                {"calendar_id": row[0], "verification_status": row[1], "instruments": int(row[2])}
                for row in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT w.data_status, COUNT(*)
                FROM ops.watermark w JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM' AND i.uic=ANY(%s)
                  AND w.horizon_minutes=60
                GROUP BY w.data_status ORDER BY w.data_status
                """,
                (canonical_uics,),
            )
            offline["watermarks"] = {str(status): int(count) for status, count in cursor.fetchall()}
            cursor.execute(
                """
                SELECT '4h', COUNT(*), COUNT(*) FILTER (WHERE quality_status='FAIL'),
                       COUNT(*) FILTER (WHERE is_complete AND quality_status='PASS')
                FROM derived.market_bar_4h
                WHERE derivation_version='db3_accepted_1h_calendar_v1'
                UNION ALL
                SELECT '1d', COUNT(*), COUNT(*) FILTER (WHERE quality_status='FAIL'),
                       COUNT(*) FILTER (WHERE is_complete AND quality_status='PASS')
                FROM derived.market_bar_1d_risk
                WHERE derivation_version='db3_accepted_1h_calendar_v1'
                ORDER BY 1
                """
            )
            offline["derived"] = {
                str(kind): {"rows": int(rows), "fail_rows": int(fails), "analysis_eligible_rows": int(eligible)}
                for kind, rows, fails, eligible in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT coverage_status, COUNT(*), COALESCE(SUM(missing_rows),0),
                       COALESCE(SUM(out_of_session_rows),0)
                FROM analytics.v_data_coverage c
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM' AND i.uic=ANY(%s)
                GROUP BY coverage_status ORDER BY coverage_status
                """,
                (canonical_uics,),
            )
            offline["coverage"] = {
                str(status): {
                    "instruments": int(count),
                    "missing_rows": int(missing),
                    "out_of_session_rows": int(outside),
                }
                for status, count, missing, outside in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT freshness_status, COUNT(*) FROM analytics.v_data_freshness f
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM' AND i.uic=ANY(%s)
                GROUP BY freshness_status ORDER BY freshness_status
                """,
                (canonical_uics,),
            )
            offline["freshness"] = {str(status): int(count) for status, count in cursor.fetchall()}
            cursor.execute("SELECT COUNT(*) FROM staging.market_bar")
            offline["staging_rows"] = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT ingestion_run_id,status,successful_series,inserted_rows,updated_rows,
                       revision_rows,source_manifest_sha256,run_manifest_relative_path
                FROM ops.ingestion_run WHERE trigger='manual_db3'
                ORDER BY ingestion_run_id DESC LIMIT 2
                """
            )
            live_runs = cursor.fetchall()

    with connect("saxo_migrator", RESEARCH_DB, application_name="saxo_db_validate_db3_research") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            offline["research_default_transaction_read_only"] = str(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM ops.schema_migration WHERE migration_number IN ('0010','0011','0012')")
            offline["research_db3_migrations"] = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM raw.market_bar_revision WHERE time_utc > '2024-06-28T23:59:59Z'),
                    (SELECT COUNT(*) FROM curated.market_bar WHERE time_utc > '2024-06-28T23:59:59Z'),
                    (SELECT COUNT(*) FROM curated.etf_total_return_daily WHERE date > '2024-06-28')
                """
            )
            offline["research_post_cutoff_rows"] = [int(value) for value in cursor.fetchone()]

    db3_manifest_path = project_root() / "manifests" / "db3_implementation_manifest.json"
    manifest_check: dict[str, Any] = {"exists": db3_manifest_path.is_file()}
    if db3_manifest_path.is_file():
        payload = json.loads(db3_manifest_path.read_text(encoding="utf-8"))
        recorded_migrations = {
            str(item["number"]): str(item["sha256"])
            for item in payload.get("migrations", [])
        }
        actual_migrations = {
            number: _sha256(project_root() / f"db/migrations/{filename}")
            for number, filename in {
                "0010": "0010_db3_incremental_support.sql",
                "0011": "0011_db3_coverage_refinement.sql",
                "0012": "0012_db3_full_refetch_guard.sql",
            }.items()
        }
        manifest_check.update(
            {
                "offline_status_pass": payload.get("offline_status") == "PASS",
                "live_status": payload.get("live_status"),
                "migration_hashes_match": recorded_migrations == actual_migrations,
                # These counts are immutable implementation-time evidence, not a
                # live invariant. Current derived rows are validated separately
                # above because successful incremental/full-refetch runs change
                # them by design.
                "derived_baseline_valid": db3_manifest_baseline_is_valid(payload),
                "no_persisted_credentials": (
                    payload.get("security", {}).get("access_token_persisted") is False
                    and payload.get("security", {}).get("account_identifier_persisted") is False
                ),
            }
        )
    offline["implementation_manifest"] = manifest_check

    offline_pass = (
        offline["market_migrations"] == ["0010", "0011", "0012"]
        and offline["calendars"] == [
            {"calendar_id": "SBFX_24X5", "verification_status": "PROVISIONAL", "instruments": 2},
            {"calendar_id": "XNYS_US_EQUITY", "verification_status": "VERIFIED", "instruments": 11},
        ]
        and offline["watermarks"] == {"ACTIVE": 13}
        and set(offline["derived"]) == {"1d", "4h"}
        and all(item["rows"] > 0 and item["fail_rows"] == 0 for item in offline["derived"].values())
        and "FAIL" not in offline["coverage"]
        and sum(item["instruments"] for item in offline["coverage"].values()) == 13
        and sum(offline["freshness"].values()) == 13
        and offline["staging_rows"] == 0
        and offline["research_default_transaction_read_only"] == "on"
        and offline["research_db3_migrations"] == 0
        and offline["research_post_cutoff_rows"] == [0, 0, 0]
        and manifest_check.get("offline_status_pass") is True
        and manifest_check.get("migration_hashes_match") is True
        and manifest_check.get("derived_baseline_valid") is True
        and manifest_check.get("no_persisted_credentials") is True
    )
    offline["status"] = "PASS" if offline_pass else "FAIL"

    live: dict[str, Any] = {
        "access_token_present_in_process": bool(os.environ.get("SAXO_ACCESS_TOKEN")),
        "pass_run_count_inspected": len(live_runs),
        "runs": [],
    }
    live_pass = len(live_runs) == 2 and all(row[1] == "PASS" and int(row[2]) == 13 for row in live_runs)
    for row in live_runs:
        run = {
            "ingestion_run_id": int(row[0]),
            "status": str(row[1]),
            "successful_series": int(row[2]),
            "inserted_rows": int(row[3]),
            "updated_rows": int(row[4]),
            "revision_rows": int(row[5]),
            "manifest_status": "MISSING",
        }
        relative_path = row[7]
        if relative_path:
            path = project_root() / str(relative_path)
            if path.is_file() and row[6] is not None and _sha256(path) == str(row[6]).strip():
                payload = json.loads(path.read_text(encoding="utf-8"))
                artifacts = payload.get("artifacts", [])
                safe_manifest = (
                    payload.get("status") == "PASS"
                    and payload.get("orders_or_prechecks_sent") == 0
                    and payload.get("write_request_count") == 0
                    and payload.get("access_token_saved") is False
                    and payload.get("account_identifier_saved") is False
                    and payload.get("smoke_test", {}).get("http_status") == 200
                    and payload.get("smoke_test", {}).get("body_saved") is False
                    and sum(str(item.get("relative_path", "")).endswith("/detail.json") for item in artifacts) == 13
                    and sum(str(item.get("relative_path", "")).endswith("/trading_schedule.json") for item in artifacts) == 13
                    and sum("/chart_" in str(item.get("relative_path", "")) for item in artifacts) >= 13
                )
                run["manifest_status"] = "PASS" if safe_manifest else "FAIL"
                live_pass = live_pass and safe_manifest
            else:
                run["manifest_status"] = "FAIL"
                live_pass = False
        else:
            live_pass = False
        live["runs"].append(run)
    if live_pass:
        # A changing current sample can update at most one row per instrument
        # during the immediate second read; historical churn is not idempotent.
        newest = live["runs"][0]
        live_pass = newest["inserted_rows"] <= 13 and newest["updated_rows"] <= 13
    live["status"] = (
        "PASS" if live_pass
        else "BLOCKED_LIVE_SIM_EXECUTION" if live["access_token_present_in_process"]
        else "BLOCKED_LIVE_SIM_TOKEN"
    )
    status = "FAIL" if not offline_pass else live["status"]
    return {"offline": offline, "live": live, "status": status}


def run_validation(phase: str = "db1") -> dict[str, Any]:
    result: dict[str, Any] = {"phase": phase.upper()}
    try:
        result["source_inventory"] = validate_import_inventory()
        result["database"] = validate_database()
        result["migrations"] = validate_migrations()
        result["compose"] = validate_compose()
        phase_gate = result["database"]["zero_data"] if phase == "db1" else True
        if phase == "db2":
            result["db2"] = validate_db2_data()
            phase_gate = result["db2"]["status"] == "PASS"
        if phase == "db3":
            result["db3"] = validate_db3_data()
            phase_gate = result["db3"]["status"] == "PASS"
        foundation_pass = (
            result["source_inventory"]["status"] == "PASS"
            and phase_gate
            and all(item["status"] == "PASS" for item in result["database"]["databases"].values())
            and result["migrations"]["status"] == "PASS"
            and result["compose"]["status"] == "PASS"
        )
        if phase == "db3" and result.get("db3", {}).get("status", "").startswith("BLOCKED"):
            result["status"] = result["db3"]["status"]
        else:
            result["status"] = "PASS" if foundation_pass else "FAIL"
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = type(exc).__name__
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("db1", "db2", "db3"), default="db1")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = run_validation(args.phase)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2 if result["status"].startswith("BLOCKED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
