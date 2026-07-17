"""Verified PostgreSQL custom-format backups and conservative retention."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .connection import FORWARD_DB, MARKET_DB, RESEARCH_DB, connect, project_root


DATABASES = (MARKET_DB, RESEARCH_DB, FORWARD_DB)
BACKUP_DIRECTORY = Path("backups/postgres")
BACKUP_NAME = re.compile(
    r"^(saxo_market|saxo_research_v13|saxo_forward_v13)_(\d{8}T\d{6}Z)\.dump$"
)
RESTORE_DATABASE = re.compile(r"^saxo_db4_restore_[0-9a-f]{12}$")
DAILY_KEEP = 7
WEEKLY_KEEP = 4

DOCKER_PREFIX = ("docker", "compose", "exec", "-T", "postgres")

SIGNATURE_QUERIES = {
    MARKET_DB: """
        SELECT jsonb_build_object(
            'schema_migration', (SELECT COUNT(*) FROM ops.schema_migration),
            'instrument', (SELECT COUNT(*) FROM catalog.instrument),
            'ingestion_run', (SELECT COUNT(*) FROM ops.ingestion_run),
            'raw_market_bar_revision', (SELECT COUNT(*) FROM raw.market_bar_revision),
            'raw_reference_observation', (SELECT COUNT(*) FROM raw.reference_observation),
            'curated_market_bar', (SELECT COUNT(*) FROM curated.market_bar),
            'curated_total_return', (SELECT COUNT(*) FROM curated.etf_total_return_daily),
            'derived_4h', (SELECT COUNT(*) FROM derived.market_bar_4h),
            'derived_1d', (SELECT COUNT(*) FROM derived.market_bar_1d_risk),
            'research_snapshot', (SELECT COUNT(*) FROM ops.research_snapshot),
            'snapshot_cutoff', (SELECT MAX(cutoff_utc)::TEXT FROM ops.research_snapshot),
            'instrument_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT instrument_id FROM catalog.instrument GROUP BY instrument_id HAVING COUNT(*) > 1
                ) d
            ),
            'curated_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT instrument_id,horizon_minutes,time_utc,price_basis
                    FROM curated.market_bar
                    GROUP BY instrument_id,horizon_minutes,time_utc,price_basis HAVING COUNT(*) > 1
                ) d
            )
        )::TEXT
    """,
    RESEARCH_DB: """
        SELECT jsonb_build_object(
            'schema_migration', (SELECT COUNT(*) FROM ops.schema_migration),
            'instrument', (SELECT COUNT(*) FROM catalog.instrument),
            'raw_market_bar_revision', (SELECT COUNT(*) FROM raw.market_bar_revision),
            'raw_reference_observation', (SELECT COUNT(*) FROM raw.reference_observation),
            'curated_market_bar', (SELECT COUNT(*) FROM curated.market_bar),
            'curated_total_return', (SELECT COUNT(*) FROM curated.etf_total_return_daily),
            'research_snapshot', (SELECT COUNT(*) FROM ops.research_snapshot),
            'snapshot_cutoff', (SELECT MAX(cutoff_utc)::TEXT FROM ops.research_snapshot),
            'instrument_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT instrument_id FROM catalog.instrument GROUP BY instrument_id HAVING COUNT(*) > 1
                ) d
            ),
            'curated_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT instrument_id,horizon_minutes,time_utc,price_basis
                    FROM curated.market_bar
                    GROUP BY instrument_id,horizon_minutes,time_utc,price_basis HAVING COUNT(*) > 1
                ) d
            )
        )::TEXT
    """,
    FORWARD_DB: """
        SELECT jsonb_build_object(
            'schema_migration', (SELECT COUNT(*) FROM ops.schema_migration),
            'source_dataset', (SELECT COUNT(*) FROM catalog.source_dataset),
            'instrument', (SELECT COUNT(*) FROM catalog.instrument),
            'ingestion_run', (SELECT COUNT(*) FROM ops.ingestion_run),
            'source_file', (SELECT COUNT(*) FROM ops.source_file),
            'raw_market_bar_revision', (SELECT COUNT(*) FROM raw.market_bar_revision),
            'instrument_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT instrument_id FROM catalog.instrument GROUP BY instrument_id HAVING COUNT(*) > 1
                ) d
            ),
            'raw_pk_duplicates', (
                SELECT COUNT(*) FROM (
                    SELECT ingestion_run_id,instrument_id,horizon_minutes,time_utc,price_basis
                    FROM raw.market_bar_revision
                    GROUP BY ingestion_run_id,instrument_id,horizon_minutes,time_utc,price_basis
                    HAVING COUNT(*) > 1
                ) d
            )
        )::TEXT
    """,
}


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_label(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_backup_path(path: Path) -> Path:
    root = (project_root() / BACKUP_DIRECTORY).resolve()
    selected = path.resolve()
    if selected.parent != root or not BACKUP_NAME.fullmatch(selected.name):
        raise BackupError("backup path is outside the allow-listed directory or naming contract")
    return selected


class BackupRegistry:
    def start(self, database: str, relative_path: str) -> int:
        with connect("saxo_ops_operator", MARKET_DB, application_name="saxo_db4_backup_start") as conn:
            with conn.cursor() as cursor:
                cursor.execute("CALL ops.start_backup_run(%s, %s, NULL)", (database, relative_path))
                backup_run_id = int(cursor.fetchone()[0])
            conn.commit()
        return backup_run_id

    def finish(
        self,
        backup_run_id: int,
        status: str,
        sha256: str | None,
        size_bytes: int | None,
        pg_restore_list_pass: bool,
        error_code: str | None,
    ) -> None:
        with connect("saxo_ops_operator", MARKET_DB, application_name="saxo_db4_backup_finish") as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "CALL ops.finish_backup_run(%s, %s, %s, %s, %s, %s)",
                    (backup_run_id, status, sha256, size_bytes, pg_restore_list_pass, error_code),
                )
            conn.commit()

    def record_restore(self, backup_run_id: int, status: str, error_code: str | None) -> None:
        with connect("saxo_ops_operator", MARKET_DB, application_name="saxo_db4_restore_record") as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "CALL ops.record_restore_smoke(%s, %s, %s)",
                    (backup_run_id, status, error_code),
                )
            conn.commit()


def run_command(
    command: Sequence[str],
    *,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
) -> subprocess.CompletedProcess[Any]:
    stdin_stream = stdin_path.open("rb") if stdin_path is not None else None
    stdout_stream = stdout_path.open("wb") if stdout_path is not None else subprocess.PIPE
    try:
        process = subprocess.run(
            list(command),
            cwd=project_root(),
            stdin=stdin_stream,
            stdout=stdout_stream,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    finally:
        if stdin_stream is not None:
            stdin_stream.close()
        if stdout_path is not None:
            stdout_stream.close()
    return process


def checked(command: Sequence[str], *, stdin_path: Path | None = None, stage: str) -> bytes:
    process = run_command(command, stdin_path=stdin_path)
    if process.returncode != 0:
        raise BackupError(f"{stage} failed")
    return bytes(process.stdout or b"")


def database_signature(database: str) -> dict[str, Any]:
    if database not in DATABASES:
        raise BackupError("database is not allow-listed")
    output = checked(
        (*DOCKER_PREFIX, "psql", "-U", "postgres", "-d", database, "-XAtqc", SIGNATURE_QUERIES[database]),
        stage="SIGNATURE_QUERY",
    )
    try:
        value = json.loads(output.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("SIGNATURE_PARSE failed") from exc
    if not isinstance(value, dict):
        raise BackupError("SIGNATURE_PARSE failed")
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def create_backup(
    database: str,
    *,
    now: datetime | None = None,
    registry: BackupRegistry | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] = run_command,
) -> dict[str, Any]:
    if database not in DATABASES:
        raise BackupError("database is not allow-listed")
    selected_now = now or utc_now()
    relative_path = BACKUP_DIRECTORY / f"{database}_{timestamp_label(selected_now)}.dump"
    dump_path = ensure_backup_path(project_root() / relative_path)
    manifest_path = dump_path.with_suffix(".manifest.json")
    partial_path = dump_path.with_suffix(".dump.partial")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    if dump_path.exists() or manifest_path.exists() or partial_path.exists():
        raise BackupError("backup artifact already exists")

    selected_registry = registry or BackupRegistry()
    backup_run_id = selected_registry.start(database, relative_path.as_posix())
    stage = "PG_DUMP"
    finished = False
    try:
        process = command_runner(
            (*DOCKER_PREFIX, "pg_dump", "-U", "postgres", "-d", database, "-Fc"),
            stdout_path=partial_path,
        )
        if process.returncode != 0 or not partial_path.is_file() or partial_path.stat().st_size == 0:
            raise BackupError("PG_DUMP failed")
        os.replace(partial_path, dump_path)

        stage = "PG_RESTORE_LIST"
        process = command_runner(
            (*DOCKER_PREFIX, "pg_restore", "--list"),
            stdin_path=dump_path,
        )
        if process.returncode != 0 or not process.stdout:
            raise BackupError("PG_RESTORE_LIST failed")

        digest = sha256_file(dump_path)
        size_bytes = dump_path.stat().st_size
        selected_registry.finish(backup_run_id, "PASS", digest, size_bytes, True, None)
        finished = True
        payload = {
            "backup_run_id": backup_run_id,
            "created_at_utc": selected_now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "database_name": database,
            "dump_relative_path": relative_path.as_posix(),
            "dump_sha256": digest,
            "dump_size_bytes": size_bytes,
            "pg_restore_list_pass": True,
            "status": "PASS",
        }
        write_json_atomic(manifest_path, payload)
        return payload
    except Exception as exc:
        partial_path.unlink(missing_ok=True)
        if not finished:
            try:
                selected_registry.finish(backup_run_id, "FAILED", None, None, False, stage)
            except Exception:
                pass
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"{stage} failed") from exc


def restore_smoke(
    manifest_path: Path,
    *,
    registry: BackupRegistry | None = None,
) -> dict[str, Any]:
    selected_manifest = manifest_path.resolve()
    root = (project_root() / BACKUP_DIRECTORY).resolve()
    if selected_manifest.parent != root or selected_manifest.suffixes[-2:] != [".manifest", ".json"]:
        raise BackupError("restore manifest is outside the allow-listed directory")
    payload = json.loads(selected_manifest.read_text(encoding="utf-8"))
    database = str(payload.get("database_name", ""))
    if database not in DATABASES or payload.get("status") != "PASS":
        raise BackupError("restore requires a verified PASS manifest")
    dump_path = ensure_backup_path(project_root() / str(payload.get("dump_relative_path", "")))
    if not dump_path.is_file() or sha256_file(dump_path) != payload.get("dump_sha256"):
        raise BackupError("restore dump SHA-256 mismatch")
    backup_run_id = int(payload["backup_run_id"])
    temporary_database = f"saxo_db4_restore_{uuid.uuid4().hex[:12]}"
    if not RESTORE_DATABASE.fullmatch(temporary_database):
        raise BackupError("temporary restore database contract failed")
    selected_registry = registry or BackupRegistry()
    created = False
    stage = "CREATE_TEMP_DATABASE"
    try:
        checked((*DOCKER_PREFIX, "createdb", "-U", "postgres", temporary_database), stage=stage)
        created = True
        stage = "PG_RESTORE"
        checked(
            (*DOCKER_PREFIX, "pg_restore", "-U", "postgres", "-d", temporary_database,
             "--exit-on-error", "--no-owner", "--no-privileges"),
            stdin_path=dump_path,
            stage=stage,
        )
        stage = "SIGNATURE_COMPARE"
        source = database_signature(database)
        output = checked(
            (*DOCKER_PREFIX, "psql", "-U", "postgres", "-d", temporary_database,
             "-XAtqc", SIGNATURE_QUERIES[database]),
            stage=stage,
        )
        restored = json.loads(output.decode("utf-8").strip())
        if source != restored:
            raise BackupError("SIGNATURE_COMPARE failed")
        stage = "DROP_TEMP_DATABASE"
        checked(
            (*DOCKER_PREFIX, "dropdb", "-U", "postgres", "--force", temporary_database),
            stage=stage,
        )
        created = False
        selected_registry.record_restore(backup_run_id, "PASS", None)
        return {
            "backup_run_id": backup_run_id,
            "database_name": database,
            "signature": source,
            "status": "PASS",
            "temporary_database_removed": True,
        }
    except Exception as exc:
        try:
            selected_registry.record_restore(backup_run_id, "FAILED", stage)
        except Exception:
            pass
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"{stage} failed") from exc
    finally:
        if created:
            checked(
                (*DOCKER_PREFIX, "dropdb", "-U", "postgres", "--force", temporary_database),
                stage="DROP_TEMP_DATABASE",
            )


@dataclass(frozen=True)
class RetentionDecision:
    keep: tuple[Path, ...]
    delete: tuple[Path, ...]


def retention_decision(paths: Iterable[Path]) -> RetentionDecision:
    parsed: list[tuple[Path, str, datetime]] = []
    for path in paths:
        match = BACKUP_NAME.fullmatch(path.name)
        if match is None:
            continue
        parsed.append(
            (
                path,
                match.group(1),
                datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
            )
        )
    parsed.sort(key=lambda item: item[2], reverse=True)

    daily: dict[tuple[str, int, int, int], Path] = {}
    weekly: dict[tuple[str, int, int], Path] = {}
    for path, database, created in parsed:
        daily.setdefault((database, created.year, created.month, created.day), path)
        iso = created.isocalendar()
        weekly.setdefault((database, iso.year, iso.week), path)
    retained: set[Path] = set()
    for database in DATABASES:
        retained.update(
            list(path for key, path in daily.items() if key[0] == database)[:DAILY_KEEP]
        )
        retained.update(
            list(path for key, path in weekly.items() if key[0] == database)[:WEEKLY_KEEP]
        )
    keep = tuple(path for path, _, _ in parsed if path in retained)
    delete = tuple(path for path, _, _ in parsed if path not in retained)
    return RetentionDecision(keep=keep, delete=delete)


def apply_retention(*, apply: bool = False) -> dict[str, Any]:
    root = (project_root() / BACKUP_DIRECTORY).resolve()
    root.mkdir(parents=True, exist_ok=True)
    decision = retention_decision(path for path in root.iterdir() if path.is_file())
    deleted: list[str] = []
    for dump_path in decision.delete:
        selected = ensure_backup_path(dump_path)
        manifest = selected.with_suffix(".manifest.json")
        if apply:
            selected.unlink()
            manifest.unlink(missing_ok=True)
            deleted.append(selected.relative_to(project_root()).as_posix())
    return {
        "apply": apply,
        "daily_keep": DAILY_KEEP,
        "weekly_keep": WEEKLY_KEEP,
        "kept": [path.relative_to(project_root()).as_posix() for path in decision.keep],
        "delete_candidates": [path.relative_to(project_root()).as_posix() for path in decision.delete],
        "deleted": deleted,
        "status": "PASS",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and verify allow-listed PostgreSQL backups")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("database", choices=DATABASES)
    backup.add_argument("--restore-smoke", action="store_true")
    restore = commands.add_parser("restore-smoke")
    restore.add_argument("manifest")
    retention = commands.add_parser("retention")
    retention.add_argument("--apply", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "create":
            result = create_backup(args.database)
            if args.restore_smoke:
                manifest = project_root() / Path(result["dump_relative_path"]).with_suffix(".manifest.json")
                result["restore_smoke"] = restore_smoke(manifest)
        elif args.command == "restore-smoke":
            result = restore_smoke(Path(args.manifest))
        else:
            result = apply_retention(apply=args.apply)
    except (BackupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAILED", "error_code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_code": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
