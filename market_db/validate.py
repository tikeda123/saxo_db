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

import duckdb

from .connection import FORWARD_DB, MARKET_DB, RESEARCH_DB, connect, project_root
from .migrate import MIGRATION_TARGETS, list_migrations, migration_number, migration_sha256, validate_applied_checksums
from .read_api import DatabaseReader, LOOPBACK_HOST, OPERATION_COMMANDS, operation_rows


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


def manifest_artifact_state(payload: dict[str, Any]) -> tuple[list[str], set[str]]:
    """Return mismatches and the paths currently attested by an implementation manifest."""
    mismatches: list[str] = []
    valid_paths: set[str] = set()
    for relative_path, expected in payload.get("artifacts", {}).items():
        path = project_root() / relative_path
        if not path.is_file():
            mismatches.append(f"missing:{relative_path}")
            continue
        size_match = path.stat().st_size == int(expected["size_bytes"])
        sha_match = _sha256(path) == expected["sha256"]
        if not size_match:
            mismatches.append(f"size:{relative_path}")
        if not sha_match:
            mismatches.append(f"sha256:{relative_path}")
        if size_match and sha_match:
            valid_paths.add(relative_path)
    return mismatches, valid_paths


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


def db4_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Check immutable DB4 drill evidence without freezing changing data counts."""
    read_api = payload.get("read_api", {})
    backups = payload.get("backups", {})
    retention = payload.get("retention", {})
    parquet = payload.get("parquet", {})
    security = payload.get("security", {})
    comparisons = read_api.get("api_cli_comparisons", {})
    return (
        payload.get("phase") == "DB4"
        and payload.get("status") == "PASS"
        and read_api.get("health_status") == "PASS"
        and read_api.get("bind_host") == LOOPBACK_HOST
        and read_api.get("role_name") == "saxo_app_reader"
        and read_api.get("transaction_read_only") == "on"
        and set(comparisons) == set(OPERATION_COMMANDS)
        and all(value is True for value in comparisons.values())
        and set(backups.get("verified_databases", [])) == {MARKET_DB, RESEARCH_DB, FORWARD_DB}
        and backups.get("restore_smoke_database") == MARKET_DB
        and backups.get("restore_smoke_status") == "PASS"
        and retention.get("dry_run_status") == "PASS"
        and retention.get("apply_status") == "PASS"
        and retention.get("deleted") == []
        and parquet.get("status") == "PASS"
        and parquet.get("row_count") == parquet.get("readback_row_count")
        and isinstance(parquet.get("row_count"), int)
        and parquet.get("row_count", 0) > 0
        and security.get("access_token_persisted") is False
        and security.get("account_identifier_persisted") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
    )


def dmi1_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate contract implementation separately from the reconciliation gate."""
    security = payload.get("security", {})
    reconciliation = payload.get("reconciliation", {})
    blocked_gate = (
        payload.get("status") == "BLOCKED_DATA_RECONCILIATION"
        and reconciliation.get("status") == "BLOCKED_DATA_RECONCILIATION"
        and isinstance(reconciliation.get("unknown_event_count"), int)
        and reconciliation.get("unknown_event_count", 0) > 0
    )


    passed_gate = (
        payload.get("status") == "PASS"
        and reconciliation.get("status") == "PASS"
        and reconciliation.get("unknown_event_count") == 0
        and reconciliation.get("current_event_count") == 5
        and reconciliation.get("historical_event_count") == 17
        and reconciliation.get("base_event_unchanged") is True
    )
    return (
        payload.get("phase") == "DMI1"
        and payload.get("contract_status") == "PASS"
        and (blocked_gate or passed_gate)
        and payload.get("migration", {}).get("number") in {"0015", "0017"}
        and payload.get("migration", {}).get("status") == "APPLIED"
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
    )


def dmi2a_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate the atomic current-series preflight contract."""
    endpoint = payload.get("endpoint", {})
    transaction = payload.get("transaction", {})
    runtime = payload.get("runtime_evidence", {})
    security = payload.get("security", {})
    return (
        payload.get("phase") == "DMI2A"
        and payload.get("status") == "PASS"
        and payload.get("contract_revision") == "1.1"
        and endpoint.get("method") == "GET"
        and endpoint.get("path") == "/api/v1/series-status"
        and endpoint.get("supported_layers") == ["1h"]
        and transaction.get("read_only") is True
        and transaction.get("isolation") == "REPEATABLE READ"
        and transaction.get("single_snapshot") is True
        and runtime.get("unknown_blocker_count") == 0
        and runtime.get("quality_event_high_watermark", 0) > 0
        and payload.get("migration", {}).get("number") == "0018"
        and payload.get("migration", {}).get("status") == "APPLIED"
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
    )


def dmi2b_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate the frozen snapshot-bound 1H read contract."""
    endpoint = payload.get("endpoint", {})
    source = payload.get("source", {})
    transaction = payload.get("transaction", {})
    runtime = payload.get("runtime_evidence", {})
    fail_closed = payload.get("fail_closed_evidence", {})
    security = payload.get("security", {})

    def sha256_value(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    return (
        payload.get("phase") == "DMI2B"
        and payload.get("status") == "PASS"
        and payload.get("contract_revision") == "1.2"
        and endpoint.get("method") == "GET"
        and endpoint.get("path") == "/api/v1/snapshots/{snapshot_id}/bars"
        and endpoint.get("supported_layers") == ["1h"]
        and source.get("database") == RESEARCH_DB
        and source.get("role") == "v13_research_reader"
        and source.get("separate_connection_pool") is True
        and transaction.get("read_only") is True
        and transaction.get("isolation") == "REPEATABLE READ"
        and transaction.get("single_snapshot") is True
        and runtime.get("snapshot_id") == 1
        and runtime.get("integrity_status") == "PASS"
        and isinstance(runtime.get("row_count"), int)
        and runtime.get("row_count", 0) > 0
        and sha256_value(runtime.get("snapshot_sha256"))
        and sha256_value(runtime.get("ordered_content_sha256"))
        and runtime.get("current_database_update_invariant") is True
        and fail_closed.get("unknown_snapshot") is True
        and fail_closed.get("unavailable_layer") is True
        and fail_closed.get("write_method") is True
        and payload.get("migration", {}).get("status") == "NOT_REQUIRED"
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("fdw_or_dblink_added") is False
        and security.get("saxo_write_requests") == 0
    )


def dmi3_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate the explicit total-return mapping and stable endpoint contract."""
    endpoint = payload.get("endpoint", {})
    mapping = payload.get("mapping", {})
    transaction = payload.get("transaction", {})
    runtime = payload.get("runtime_evidence", {})
    security = payload.get("security", {})

    def sha256_value(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    return (
        payload.get("phase") == "DMI3"
        and payload.get("status") == "PASS"
        and payload.get("contract_revision") == "1.2"
        and endpoint.get("method") == "GET"
        and endpoint.get("path") == "/api/v1/total-return"
        and endpoint.get("supported_price_basis") == ["etf_total_return"]
        and mapping.get("table") == "catalog.series_instrument_mapping"
        and mapping.get("approved_mapping_count", 0) > 0
        and mapping.get("unapproved_mapping_count") == 0
        and mapping.get("ambiguous_mapping_count") == 0
        and transaction.get("read_only") is True
        and transaction.get("isolation") == "REPEATABLE READ"
        and transaction.get("single_snapshot") is True
        and isinstance(runtime.get("row_count"), int)
        and runtime.get("row_count", 0) > 0
        and runtime.get("parity_status") == "PASS"
        and sha256_value(runtime.get("ordered_content_sha256"))
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
    )


def dmi4_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate cursor binding, pagination parity, and the v1 contract artifact."""
    cursor = payload.get("cursor", {})
    contract = payload.get("contract", {})
    runtime = payload.get("runtime_evidence", {})
    security = payload.get("security", {})
    fail_closed = payload.get("fail_closed", {})
    return (
        payload.get("phase") == "DMI4"
        and payload.get("status") == "PASS"
        and payload.get("contract_revision") == "1.2"
        and cursor.get("codec") == "HMAC-SHA256"
        and cursor.get("query_bound") is True
        and cursor.get("snapshot_bound") is True
        and cursor.get("state_revision_bound") is True
        and cursor.get("composite_key") == [
            "time_utc", "instrument_id", "price_basis"
        ]
        and cursor.get("total_return_key") == ["session_date"]
        and cursor.get("restart_expiry") is True
        and contract.get("openapi_relative_path") == "specs/read_api_v1_openapi.yaml"
        and contract.get("compatibility_status") == "PASS"
        and runtime.get("snapshot_direct_parity") == "PASS"
        and runtime.get("total_return_direct_parity") == "PASS"
        and runtime.get("snapshot_missing_count") == 0
        and runtime.get("snapshot_duplicate_count") == 0
        and runtime.get("snapshot_order_reversal_count") == 0
        and runtime.get("total_return_missing_count") == 0
        and runtime.get("total_return_duplicate_count") == 0
        and runtime.get("total_return_order_reversal_count") == 0
        and fail_closed.get("tampered_cursor") == "CURSOR_INVALID"
        and fail_closed.get("query_mismatch") == "CURSOR_QUERY_MISMATCH"
        and fail_closed.get("state_revision_change") == "CURSOR_EXPIRED"
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("arbitrary_sql_enabled") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
    )


def dmi5_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    """Validate non-data readiness, owned lifecycle, and zero-mutation evidence."""
    lifecycle = payload.get("lifecycle", {})
    preflight = payload.get("preflight", {})
    incident = payload.get("incident_reproduction", {})
    contract = payload.get("contract", {})
    mutation = payload.get("mutation_invariant", {})
    security = payload.get("security", {})
    tests = payload.get("test_evidence", {})
    return (
        payload.get("phase") == "DMI5"
        and payload.get("phase_name") == "DMI5_READ_API_OPERATIONAL_READINESS"
        and payload.get("status") == "PASS"
        and payload.get("migration", {}).get("status") == "NOT_REQUIRED"
        and lifecycle.get("start") == "PASS"
        and lifecycle.get("status") == "PASS"
        and lifecycle.get("stop") == "PASS"
        and lifecycle.get("second_start_idempotent") is True
        and lifecycle.get("postgres_healthy_after_stop") is True
        and incident.get("status") == "BLOCKED_READ_API_NOT_RUNNING"
        and incident.get("exit_code") == 2
        and preflight.get("status") == "PASS"
        and preflight.get("exit_code") == 0
        and preflight.get("request_paths") == [
            "/", "/health", "/api/v1/bars", "/api/v1/total-return"
        ]
        and preflight.get("market_rows_received") == 0
        and preflight.get("metadata_rows_received") == 0
        and contract.get("host") == LOOPBACK_HOST
        and contract.get("port") == 8766
        and contract.get("api_version") == 1
        and contract.get("contract_revision") == "1.2"
        and contract.get("role_name") == "saxo_app_reader"
        and contract.get("transaction_read_only") == "on"
        and contract.get("statement_timeout") == "30s"
        and mutation.get("data_mutation_commands") == 0
        and mutation.get("market_table_dml_counter_delta") == 0
        and mutation.get("migration_history_unchanged") is True
        and security.get("bind_host") == LOOPBACK_HOST
        and security.get("access_token_saved") is False
        and security.get("account_identifier_saved") is False
        and security.get("database_write_routes") == 0
        and security.get("saxo_write_requests") == 0
        and tests.get("integration_smoke") == "PASS"
        and tests.get("full_regression") == "PASS"
        and isinstance(tests.get("passed"), int)
        and tests.get("passed") == tests.get("total")
    )


def periodic_update_manifest_baseline_is_valid(payload: dict[str, Any]) -> bool:
    authentication = payload.get("authentication", {})
    schedule = payload.get("schedule", {})
    runtime = payload.get("runtime_acceptance", {})
    security = payload.get("security", {})
    tests = payload.get("test_evidence", {})
    total_return = payload.get("total_return", {})
    return (
        payload.get("phase") == "DPU2R"
        and payload.get("phase_name") == "S6V5A_PERIODIC_MARKET_DATA_FOUNDATION"
        and payload.get("status") == "SIM_RESEARCH_READY"
        and payload.get("migration", {}).get("number") == "0023"
        and payload.get("migration", {}).get("status") == "APPLIED"
        and authentication.get("flow") == "authorization_code_pkce"
        and authentication.get("access_token_storage") == "process_memory_only"
        and authentication.get("refresh_credential_storage") == "macos_keychain_only"
        and schedule.get("instrument_keys")
        == ["spy", "iwm", "efa", "eem", "vnq", "eurusd"]
        and schedule.get("first_regular_bar_start_et") == "10:30:15"
        and schedule.get("first_regular_bar_deadline_et") == "10:33:00"
        and schedule.get("fx_hourly_start_minute_utc") == 3
        and schedule.get("complete_slot_contract") == "SESSION_FULLY_CONTAINED_1H_V1"
        and runtime.get("oauth") == "AUTH_READY"
        and runtime.get("scheduler") == "RUNNING"
        and runtime.get("service_status") == "PASS"
        and runtime.get("service_managed") is True
        and runtime.get("three_xnys_session_sla") == "NOT_REQUIRED_FOR_SIM_RESEARCH_START"
        and isinstance(runtime.get("saxo_requests"), int)
        and runtime.get("saxo_requests", 0) > 0
        and security.get("access_token_saved") is False
        and security.get("refresh_token_in_repository") is False
        and security.get("account_identifier_saved") is False
        and security.get("saxo_write_requests") == 0
        and security.get("orders_or_prechecks_sent") == 0
        and total_return.get("status") == "PASS_SIM_RESEARCH_CURRENT"
        and total_return.get("provider") == "Yahoo Finance chart endpoint"
        and total_return.get("research_eligibility") == "SIM_RESEARCH_ONLY"
        and total_return.get("quality_status") == "PASS"
        and total_return.get("development_dataset_promoted") is False
        and total_return.get("operator_decision_required") is False
        and isinstance(total_return.get("current_dataset_id"), str)
        and total_return.get("current_dataset_id", "").startswith("SIMTR_")
        and total_return.get("read_api_total_return") == "PASS"
        and total_return.get("read_api_manifests") == "PASS"
        and tests.get("unit") == "PASS"
        and tests.get("database_integration") == "PASS"
        and isinstance(tests.get("database_integration_passed"), int)
        and tests.get("database_integration_passed") > 0
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
            {"calendar_id": "SBFX_24X5", "verification_status": "VERIFIED", "instruments": 2},
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


def validate_db4_data() -> dict[str, Any]:
    result: dict[str, Any] = {}
    migration_path = project_root() / "db/migrations/0013_db4_read_api_and_restore_smoke.sql"
    with connect("saxo_migrator", MARKET_DB, application_name="saxo_db_validate_db4") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT filename, sha256 FROM ops.schema_migration WHERE migration_number='0013'"
            )
            migration_row = cursor.fetchone()
            result["migration"] = {
                "applied": migration_row is not None,
                "filename": None if migration_row is None else str(migration_row[0]),
                "sha256_match": (
                    migration_row is not None
                    and str(migration_row[1]).strip() == _sha256(migration_path)
                ),
            }
            cursor.execute(
                """
                SELECT
                    has_table_privilege('saxo_app_reader','curated.market_bar','SELECT'),
                    has_table_privilege('saxo_app_reader','curated.market_bar','INSERT'),
                    has_table_privilege('saxo_app_reader','ops.backup_run','SELECT'),
                    has_table_privilege('saxo_app_reader','ops.backup_run','UPDATE'),
                    has_function_privilege(
                        'saxo_ops_operator',
                        'ops.record_restore_smoke(bigint,text,text)',
                        'EXECUTE'
                    )
                """
            )
            privileges = cursor.fetchone()
            result["privileges"] = {
                "app_reader_bar_select": bool(privileges[0]),
                "app_reader_bar_insert": bool(privileges[1]),
                "app_reader_backup_table_select": bool(privileges[2]),
                "app_reader_backup_table_update": bool(privileges[3]),
                "ops_restore_procedure_execute": bool(privileges[4]),
            }
            cursor.execute(
                """
                SELECT DISTINCT ON (database_name)
                       database_name, backup_run_id, relative_path, sha256, size_bytes,
                       pg_restore_list_pass, restore_smoke_test_status
                FROM ops.backup_run
                WHERE status='PASS'
                ORDER BY database_name, finished_at_utc DESC, backup_run_id DESC
                """
            )
            backup_rows = cursor.fetchall()

    backup_results: dict[str, Any] = {}
    for database, backup_run_id, relative_path, sha256, size_bytes, list_pass, restore_status in backup_rows:
        path = project_root() / str(relative_path)
        manifest_path = path.with_suffix(".manifest.json")
        entry: dict[str, Any] = {
            "backup_run_id": int(backup_run_id),
            "dump_exists": path.is_file(),
            "manifest_exists": manifest_path.is_file(),
            "pg_restore_list_pass": list_pass is True,
            "restore_smoke_test_status": None if restore_status is None else str(restore_status),
            "sha256_match": path.is_file() and _sha256(path) == str(sha256).strip(),
            "size_match": path.is_file() and path.stat().st_size == int(size_bytes),
        }
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry["manifest_match"] = (
                manifest.get("status") == "PASS"
                and manifest.get("backup_run_id") == int(backup_run_id)
                and manifest.get("database_name") == str(database)
                and manifest.get("dump_relative_path") == str(relative_path)
                and manifest.get("dump_sha256") == str(sha256).strip()
                and manifest.get("dump_size_bytes") == int(size_bytes)
                and manifest.get("pg_restore_list_pass") is True
            )
        else:
            entry["manifest_match"] = False
        backup_results[str(database)] = entry
    result["backups"] = backup_results

    with connect("postgres", "postgres", application_name="saxo_db_validate_db4_temp") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM pg_database WHERE datname LIKE 'saxo_db4_restore_%'")
            result["temporary_restore_databases"] = int(cursor.fetchone()[0])

    reader = DatabaseReader()
    try:
        health = reader.query(
            """
            SELECT current_database() AS database_name, current_user AS role_name,
                   current_setting('transaction_read_only') AS transaction_read_only
            """
        )[0]
        operation_counts = {
            command: len(operation_rows(reader, command, 1)) for command in OPERATION_COMMANDS
        }
    finally:
        reader.close()
    result["read_api"] = {"health": health, "operation_sample_counts": operation_counts}

    manifest_path = project_root() / "manifests/db4_implementation_manifest.json"
    implementation: dict[str, Any] = {"exists": manifest_path.is_file()}
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatches, _ = manifest_artifact_state(payload)
        parquet_manifest_path = project_root() / str(
            payload.get("parquet", {}).get("manifest_relative_path", "")
        )
        parquet_check = False
        if parquet_manifest_path.is_file():
            parquet_manifest = json.loads(parquet_manifest_path.read_text(encoding="utf-8"))
            parquet_path = project_root() / str(parquet_manifest.get("parquet_relative_path", ""))
            if parquet_path.is_file():
                with duckdb.connect(":memory:") as duck:
                    readback = int(
                        duck.execute(
                            "SELECT COUNT(*) FROM read_parquet(?)", [str(parquet_path)]
                        ).fetchone()[0]
                    )
                parquet_check = (
                    parquet_manifest.get("status") == "PASS"
                    and _sha256(parquet_path) == parquet_manifest.get("parquet_sha256")
                    and parquet_path.stat().st_size == parquet_manifest.get("parquet_size_bytes")
                    and readback == parquet_manifest.get("row_count")
                    and readback == parquet_manifest.get("readback_row_count")
                )
        implementation.update(
            {
                "artifact_mismatches": mismatches,
                "baseline_valid": db4_manifest_baseline_is_valid(payload),
                "parquet_verified": parquet_check,
            }
        )

    extension_path = project_root() / "manifests/data_management_web_ui_implementation_manifest.json"
    extension: dict[str, Any] = {"exists": extension_path.is_file(), "status": "NOT_PRESENT"}
    if extension_path.is_file():
        extension_payload = json.loads(extension_path.read_text(encoding="utf-8"))
        extension_mismatches, extension_valid_paths = manifest_artifact_state(extension_payload)
        parent = extension_payload.get("parent_evidence", {})
        extension_baseline_valid = (
            extension_payload.get("phase") == "DMUI4"
            and extension_payload.get("status") == "PASS"
            and extension_payload.get("orders_or_prechecks_sent") == 0
            and extension_payload.get("database_write_routes") == 0
            and extension_payload.get("access_token_saved") is False
            and extension_payload.get("account_identifier_saved") is False
            and parent.get("relative_path") == "manifests/db4_implementation_manifest.json"
            and parent.get("sha256") == _sha256(manifest_path)
            and extension_payload.get("migration", {}).get("number") == "0014"
            and extension_payload.get("chart_framework", {}).get("version") == "5.2.0"
        )
        baseline_mismatches = implementation.get("artifact_mismatches", [])
        superseded_paths = sorted({
            item.split(":", 1)[1]
            for item in baseline_mismatches
            if ":" in item and item.split(":", 1)[1] in extension_valid_paths
        })
        implementation["artifact_mismatches"] = [
            item for item in baseline_mismatches
            if ":" not in item or item.split(":", 1)[1] not in extension_valid_paths
        ]
        implementation["superseded_artifacts"] = superseded_paths
        extension.update({
            "artifact_mismatches": extension_mismatches,
            "baseline_valid": extension_baseline_valid,
            "status": "PASS" if extension_baseline_valid and not extension_mismatches else "FAIL",
        })
    implementation["extension_manifest"] = extension

    dmi1_path = project_root() / "manifests/dmi1_implementation_manifest.json"
    dmi1_extension: dict[str, Any] = {"exists": dmi1_path.is_file(), "status": "NOT_PRESENT"}
    if dmi1_path.is_file():
        dmi1_payload = json.loads(dmi1_path.read_text(encoding="utf-8"))
        dmi1_mismatches, dmi1_valid_paths = manifest_artifact_state(dmi1_payload)
        dmi1_parent = dmi1_payload.get("parent_evidence", {})
        dmi1_baseline_valid = (
            dmi1_manifest_baseline_is_valid(dmi1_payload)
            and dmi1_parent.get("relative_path")
            == "manifests/data_management_web_ui_implementation_manifest.json"
            and dmi1_parent.get("sha256") == _sha256(extension_path)
            and dmi1_payload.get("migration", {}).get("filename")
            in {
                "0015_read_api_contract_hardening.sql",
                "0017_quality_event_price_basis_derivation.sql",
            }
            and dmi1_payload.get("migration", {}).get("sha256")
            == _sha256(
                project_root() / "db/migrations"
                / dmi1_payload.get("migration", {}).get("filename", "invalid")
            )
        )

        def without_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi1_valid_paths
            })
            remaining = [
                item for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi1_valid_paths
            ]
            return remaining, superseded

        implementation["artifact_mismatches"], dmi1_base_superseded = without_superseded(
            implementation.get("artifact_mismatches", [])
        )
        extension["artifact_mismatches"], dmi1_extension_superseded = without_superseded(
            extension.get("artifact_mismatches", [])
        )
        if extension.get("baseline_valid") is True and not extension["artifact_mismatches"]:
            extension["status"] = "PASS"
        dmi1_extension.update({
            "artifact_mismatches": dmi1_mismatches,
            "baseline_valid": dmi1_baseline_valid,
            "gate_status": dmi1_payload.get("status"),
            "reconciliation_status": dmi1_payload.get("reconciliation", {}).get("status"),
            "superseded_db4_artifacts": dmi1_base_superseded,
            "superseded_dmui4_artifacts": dmi1_extension_superseded,
            "status": "PASS" if dmi1_baseline_valid and not dmi1_mismatches else "FAIL",
        })
    implementation["dmi1_extension_manifest"] = dmi1_extension

    dmi2a_path = project_root() / "manifests/dmi2a_implementation_manifest.json"
    dmi2a_extension: dict[str, Any] = {"exists": dmi2a_path.is_file(), "status": "NOT_PRESENT"}
    if dmi2a_path.is_file():
        dmi2a_payload = json.loads(dmi2a_path.read_text(encoding="utf-8"))
        dmi2a_mismatches, dmi2a_valid_paths = manifest_artifact_state(dmi2a_payload)
        dmi2a_parent = dmi2a_payload.get("parent_evidence", {})
        dmi2a_baseline_valid = (
            dmi2a_manifest_baseline_is_valid(dmi2a_payload)
            and dmi2a_parent.get("relative_path") == "manifests/dmi1_implementation_manifest.json"
            and dmi2a_parent.get("sha256") == _sha256(dmi1_path)
            and dmi2a_payload.get("migration", {}).get("filename")
            == "0018_series_status_high_watermark.sql"
            and dmi2a_payload.get("migration", {}).get("sha256")
            == _sha256(project_root() / "db/migrations/0018_series_status_high_watermark.sql")
        )

        def remove_dmi2a_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi2a_valid_paths
            })
            remaining = [
                item for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi2a_valid_paths
            ]
            return remaining, superseded

        implementation["artifact_mismatches"], dmi2a_db4_superseded = remove_dmi2a_superseded(
            implementation.get("artifact_mismatches", [])
        )
        extension["artifact_mismatches"], dmi2a_dmui_superseded = remove_dmi2a_superseded(
            extension.get("artifact_mismatches", [])
        )
        dmi1_extension["artifact_mismatches"], dmi2a_dmi1_superseded = remove_dmi2a_superseded(
            dmi1_extension.get("artifact_mismatches", [])
        )
        if extension.get("baseline_valid") is True and not extension["artifact_mismatches"]:
            extension["status"] = "PASS"
        if dmi1_extension.get("baseline_valid") is True and not dmi1_extension["artifact_mismatches"]:
            dmi1_extension["status"] = "PASS"
        dmi2a_extension.update({
            "artifact_mismatches": dmi2a_mismatches,
            "baseline_valid": dmi2a_baseline_valid,
            "gate_status": dmi2a_payload.get("status"),
            "superseded_db4_artifacts": dmi2a_db4_superseded,
            "superseded_dmui4_artifacts": dmi2a_dmui_superseded,
            "superseded_dmi1_artifacts": dmi2a_dmi1_superseded,
            "status": "PASS" if dmi2a_baseline_valid and not dmi2a_mismatches else "FAIL",
        })
    implementation["dmi2a_extension_manifest"] = dmi2a_extension

    dmi2b_path = project_root() / "manifests/dmi2b_implementation_manifest.json"
    dmi2b_extension: dict[str, Any] = {"exists": dmi2b_path.is_file(), "status": "NOT_PRESENT"}
    if dmi2b_path.is_file():
        dmi2b_payload = json.loads(dmi2b_path.read_text(encoding="utf-8"))
        dmi2b_mismatches, dmi2b_valid_paths = manifest_artifact_state(dmi2b_payload)
        dmi2b_parent = dmi2b_payload.get("parent_evidence", {})
        dmi2b_baseline_valid = (
            dmi2b_manifest_baseline_is_valid(dmi2b_payload)
            and dmi2b_parent.get("relative_path")
            == "manifests/dmi2a_implementation_manifest.json"
            and dmi2b_parent.get("sha256") == _sha256(dmi2a_path)
        )

        def remove_dmi2b_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi2b_valid_paths
            })
            remaining = [
                item for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi2b_valid_paths
            ]
            return remaining, superseded

        implementation["artifact_mismatches"], dmi2b_db4_superseded = (
            remove_dmi2b_superseded(implementation.get("artifact_mismatches", []))
        )
        extension["artifact_mismatches"], dmi2b_dmui_superseded = (
            remove_dmi2b_superseded(extension.get("artifact_mismatches", []))
        )
        dmi1_extension["artifact_mismatches"], dmi2b_dmi1_superseded = (
            remove_dmi2b_superseded(dmi1_extension.get("artifact_mismatches", []))
        )
        dmi2a_extension["artifact_mismatches"], dmi2b_dmi2a_superseded = (
            remove_dmi2b_superseded(dmi2a_extension.get("artifact_mismatches", []))
        )
        for extension_manifest in (extension, dmi1_extension, dmi2a_extension):
            if (
                extension_manifest.get("baseline_valid") is True
                and not extension_manifest["artifact_mismatches"]
            ):
                extension_manifest["status"] = "PASS"
        dmi2b_extension.update({
            "artifact_mismatches": dmi2b_mismatches,
            "baseline_valid": dmi2b_baseline_valid,
            "gate_status": dmi2b_payload.get("status"),
            "superseded_db4_artifacts": dmi2b_db4_superseded,
            "superseded_dmui4_artifacts": dmi2b_dmui_superseded,
            "superseded_dmi1_artifacts": dmi2b_dmi1_superseded,
            "superseded_dmi2a_artifacts": dmi2b_dmi2a_superseded,
            "status": "PASS" if dmi2b_baseline_valid and not dmi2b_mismatches else "FAIL",
        })
    implementation["dmi2b_extension_manifest"] = dmi2b_extension

    dmi3_path = project_root() / "manifests/dmi3_implementation_manifest.json"
    dmi3_extension: dict[str, Any] = {"exists": dmi3_path.is_file(), "status": "NOT_PRESENT"}
    if dmi3_path.is_file():
        dmi3_payload = json.loads(dmi3_path.read_text(encoding="utf-8"))
        dmi3_mismatches, dmi3_valid_paths = manifest_artifact_state(dmi3_payload)
        dmi3_parent = dmi3_payload.get("parent_evidence", {})
        dmi3_baseline_valid = (
            dmi3_manifest_baseline_is_valid(dmi3_payload)
            and dmi3_parent.get("relative_path")
            == "manifests/dmi2b_implementation_manifest.json"
            and dmi3_parent.get("sha256") == _sha256(dmi2b_path)
            and dmi3_payload.get("migration", {}).get("number") == "0019"
            and dmi3_payload.get("migration", {}).get("status") == "APPLIED"
            and dmi3_payload.get("migration", {}).get("sha256")
            == _sha256(project_root() / "db/migrations/0019_total_return_mapping.sql")
        )

        def remove_dmi3_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi3_valid_paths
            })
            remaining = [
                item
                for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi3_valid_paths
            ]
            return remaining, superseded

        implementation["artifact_mismatches"], dmi3_db4_superseded = (
            remove_dmi3_superseded(implementation.get("artifact_mismatches", []))
        )
        extension["artifact_mismatches"], dmi3_dmui_superseded = (
            remove_dmi3_superseded(extension.get("artifact_mismatches", []))
        )
        dmi1_extension["artifact_mismatches"], dmi3_dmi1_superseded = (
            remove_dmi3_superseded(dmi1_extension.get("artifact_mismatches", []))
        )
        dmi2a_extension["artifact_mismatches"], dmi3_dmi2a_superseded = (
            remove_dmi3_superseded(dmi2a_extension.get("artifact_mismatches", []))
        )
        dmi2b_extension["artifact_mismatches"], dmi3_dmi2b_superseded = (
            remove_dmi3_superseded(dmi2b_extension.get("artifact_mismatches", []))
        )
        for extension_manifest in (
            extension, dmi1_extension, dmi2a_extension, dmi2b_extension
        ):
            if (
                extension_manifest.get("baseline_valid") is True
                and not extension_manifest["artifact_mismatches"]
            ):
                extension_manifest["status"] = "PASS"
        dmi3_extension.update({
            "artifact_mismatches": dmi3_mismatches,
            "baseline_valid": dmi3_baseline_valid,
            "gate_status": dmi3_payload.get("status"),
            "superseded_db4_artifacts": dmi3_db4_superseded,
            "superseded_dmui4_artifacts": dmi3_dmui_superseded,
            "superseded_dmi1_artifacts": dmi3_dmi1_superseded,
            "superseded_dmi2a_artifacts": dmi3_dmi2a_superseded,
            "superseded_dmi2b_artifacts": dmi3_dmi2b_superseded,
            "status": "PASS" if dmi3_baseline_valid and not dmi3_mismatches else "FAIL",
        })
    implementation["dmi3_extension_manifest"] = dmi3_extension

    dmi4_path = project_root() / "manifests/dmi4_implementation_manifest.json"
    dmi4_extension: dict[str, Any] = {"exists": dmi4_path.is_file(), "status": "NOT_PRESENT"}
    if dmi4_path.is_file():
        dmi4_payload = json.loads(dmi4_path.read_text(encoding="utf-8"))
        dmi4_mismatches, dmi4_valid_paths = manifest_artifact_state(dmi4_payload)
        dmi4_parent = dmi4_payload.get("parent_evidence", {})
        dmi4_baseline_valid = (
            dmi4_manifest_baseline_is_valid(dmi4_payload)
            and dmi4_parent.get("relative_path") == "manifests/dmi3_implementation_manifest.json"
            and dmi4_parent.get("sha256") == _sha256(dmi3_path)
            and dmi4_payload.get("migration", {}).get("status") == "NOT_REQUIRED"
            and dmi4_payload.get("contract", {}).get("openapi_sha256")
            == _sha256(project_root() / "specs/read_api_v1_openapi.yaml")
        )

        def remove_dmi4_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi4_valid_paths
            })
            remaining = [
                item
                for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi4_valid_paths
            ]
            return remaining, superseded

        for extension_manifest in (
            implementation, extension, dmi1_extension, dmi2a_extension,
            dmi2b_extension, dmi3_extension,
        ):
            extension_manifest["artifact_mismatches"], _ = remove_dmi4_superseded(
                extension_manifest.get("artifact_mismatches", [])
            )
            if (
                extension_manifest.get("baseline_valid") is True
                and not extension_manifest["artifact_mismatches"]
            ):
                extension_manifest["status"] = "PASS"
        dmi4_extension.update({
            "artifact_mismatches": dmi4_mismatches,
            "baseline_valid": dmi4_baseline_valid,
            "gate_status": dmi4_payload.get("status"),
            "status": "PASS" if dmi4_baseline_valid and not dmi4_mismatches else "FAIL",
        })
    implementation["dmi4_extension_manifest"] = dmi4_extension

    dmi5_path = project_root() / "manifests/read_api_operational_readiness_implementation_manifest.json"
    dmi5_extension: dict[str, Any] = {"exists": dmi5_path.is_file(), "status": "NOT_PRESENT"}
    if dmi5_path.is_file():
        dmi5_payload = json.loads(dmi5_path.read_text(encoding="utf-8"))
        dmi5_mismatches, dmi5_valid_paths = manifest_artifact_state(dmi5_payload)
        dmi5_parent = dmi5_payload.get("parent_evidence", {})
        dmi5_baseline_valid = (
            dmi5_manifest_baseline_is_valid(dmi5_payload)
            and dmi5_parent.get("relative_path") == "manifests/dmi4_implementation_manifest.json"
            and dmi5_parent.get("sha256") == _sha256(dmi4_path)
            and dmi5_payload.get("contract", {}).get("openapi_sha256")
            == _sha256(project_root() / "specs/read_api_v1_openapi.yaml")
            and dmi5_payload.get("contract", {}).get("readiness_contract_sha256")
            == _sha256(project_root() / "specs/read_api_operational_readiness_v1.json")
            and dmi5_payload.get("contract", {}).get("readiness_schema_sha256")
            == _sha256(project_root() / "specs/read_api_operational_readiness_v1.schema.json")
        )

        def remove_dmi5_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in dmi5_valid_paths
            })
            remaining = [
                item
                for item in items
                if ":" not in item or item.split(":", 1)[1] not in dmi5_valid_paths
            ]
            return remaining, superseded

        for extension_manifest in (
            implementation, extension, dmi1_extension, dmi2a_extension,
            dmi2b_extension, dmi3_extension, dmi4_extension,
        ):
            extension_manifest["artifact_mismatches"], _ = remove_dmi5_superseded(
                extension_manifest.get("artifact_mismatches", [])
            )
            if (
                extension_manifest.get("baseline_valid") is True
                and not extension_manifest["artifact_mismatches"]
            ):
                extension_manifest["status"] = "PASS"
        dmi5_extension.update({
            "artifact_mismatches": dmi5_mismatches,
            "baseline_valid": dmi5_baseline_valid,
            "gate_status": dmi5_payload.get("status"),
            "status": "PASS" if dmi5_baseline_valid and not dmi5_mismatches else "FAIL",
        })
    implementation["dmi5_extension_manifest"] = dmi5_extension

    periodic_path = project_root() / "manifests/periodic_market_data_update_implementation_manifest.json"
    periodic_extension: dict[str, Any] = {
        "exists": periodic_path.is_file(), "status": "NOT_PRESENT"
    }
    if periodic_path.is_file():
        periodic_payload = json.loads(periodic_path.read_text(encoding="utf-8"))
        periodic_mismatches, periodic_valid_paths = manifest_artifact_state(periodic_payload)
        periodic_parent = periodic_payload.get("parent_evidence", {})
        periodic_baseline_valid = (
            periodic_update_manifest_baseline_is_valid(periodic_payload)
            and periodic_parent.get("relative_path")
            == "manifests/read_api_operational_readiness_implementation_manifest.json"
            and periodic_parent.get("sha256") == _sha256(dmi5_path)
            and periodic_payload.get("profile", {}).get("relative_path")
            == "specs/source_collection/s6v5a_periodic_update_v1.json"
            and periodic_payload.get("profile", {}).get("sha256")
            == _sha256(project_root() / "specs/source_collection/s6v5a_periodic_update_v1.json")
        )

        def remove_periodic_superseded(items: list[str]) -> tuple[list[str], list[str]]:
            superseded = sorted({
                item.split(":", 1)[1]
                for item in items
                if ":" in item and item.split(":", 1)[1] in periodic_valid_paths
            })
            remaining = [
                item for item in items
                if ":" not in item or item.split(":", 1)[1] not in periodic_valid_paths
            ]
            return remaining, superseded

        superseded_by_manifest: dict[str, list[str]] = {}
        for name, extension_manifest in (
            ("db4", implementation),
            ("dmui4", extension),
            ("dmi1", dmi1_extension),
            ("dmi2a", dmi2a_extension),
            ("dmi2b", dmi2b_extension),
            ("dmi3", dmi3_extension),
            ("dmi4", dmi4_extension),
            ("dmi5", dmi5_extension),
        ):
            extension_manifest["artifact_mismatches"], superseded = remove_periodic_superseded(
                extension_manifest.get("artifact_mismatches", [])
            )
            superseded_by_manifest[name] = superseded
            if (
                extension_manifest.get("baseline_valid") is True
                and not extension_manifest["artifact_mismatches"]
            ):
                extension_manifest["status"] = "PASS"
        periodic_extension.update({
            "artifact_mismatches": periodic_mismatches,
            "baseline_valid": periodic_baseline_valid,
            "gate_status": periodic_payload.get("status"),
            "superseded_artifacts": superseded_by_manifest,
            "status": "PASS" if periodic_baseline_valid and not periodic_mismatches else "FAIL",
        })
    implementation["periodic_update_extension_manifest"] = periodic_extension
    result["implementation_manifest"] = implementation

    backup_pass = (
        set(backup_results) == {MARKET_DB, RESEARCH_DB, FORWARD_DB}
        and all(
            item["dump_exists"]
            and item["manifest_exists"]
            and item["pg_restore_list_pass"]
            and item["sha256_match"]
            and item["size_match"]
            and item["manifest_match"]
            for item in backup_results.values()
        )
        and backup_results.get(MARKET_DB, {}).get("restore_smoke_test_status") == "PASS"
    )
    privilege_pass = result["privileges"] == {
        "app_reader_bar_select": True,
        "app_reader_bar_insert": False,
        "app_reader_backup_table_select": False,
        "app_reader_backup_table_update": False,
        "ops_restore_procedure_execute": True,
    }
    api_pass = (
        health.get("database_name") == MARKET_DB
        and health.get("role_name") == "saxo_app_reader"
        and health.get("transaction_read_only") == "on"
        and set(operation_counts) == set(OPERATION_COMMANDS)
    )
    implementation_pass = (
        implementation.get("baseline_valid") is True
        and implementation.get("artifact_mismatches") == []
        and implementation.get("parquet_verified") is True
        and implementation.get("extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi1_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi2a_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi2b_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi3_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi4_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("dmi5_extension_manifest", {}).get("status") in {"PASS", "NOT_PRESENT"}
        and implementation.get("periodic_update_extension_manifest", {}).get("status")
        in {"PASS", "NOT_PRESENT"}
    )
    result["status"] = "PASS" if (
        result["migration"]["applied"]
        and result["migration"]["sha256_match"]
        and privilege_pass
        and backup_pass
        and result["temporary_restore_databases"] == 0
        and api_pass
        and implementation_pass
    ) else "FAIL"
    return result


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
        if phase == "db4":
            result["db3"] = validate_db3_data()
            result["db4"] = validate_db4_data()
            phase_gate = result["db3"]["status"] == "PASS" and result["db4"]["status"] == "PASS"
        foundation_pass = (
            result["source_inventory"]["status"] == "PASS"
            and phase_gate
            and all(item["status"] == "PASS" for item in result["database"]["databases"].values())
            and result["migrations"]["status"] == "PASS"
            and result["compose"]["status"] == "PASS"
        )
        if phase in {"db3", "db4"} and result.get("db3", {}).get("status", "").startswith("BLOCKED"):
            result["status"] = result["db3"]["status"]
        else:
            result["status"] = "PASS" if foundation_pass else "FAIL"
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = type(exc).__name__
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("db1", "db2", "db3", "db4"), default="db1")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = run_validation(args.phase)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2 if result["status"].startswith("BLOCKED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
