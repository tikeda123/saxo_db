"""Evidence-oriented DB1 validator with no state-changing operations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
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


def run_validation() -> dict[str, Any]:
    result: dict[str, Any] = {"phase": "DB1"}
    try:
        result["source_inventory"] = validate_import_inventory()
        result["database"] = validate_database()
        result["migrations"] = validate_migrations()
        result["compose"] = validate_compose()
        result["status"] = "PASS" if (
            result["source_inventory"]["status"] == "PASS"
            and result["database"]["zero_data"]
            and all(item["status"] == "PASS" for item in result["database"]["databases"].values())
            and result["migrations"]["status"] == "PASS"
            and result["compose"]["status"] == "PASS"
        ) else "FAIL"
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = type(exc).__name__
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("db1",), default="db1")
    args = parser.parse_args(list(argv) if argv is not None else None)
    del args
    result = run_validation()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
