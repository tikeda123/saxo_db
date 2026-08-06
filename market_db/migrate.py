"""Cluster bootstrap and checksum-enforced DB1 migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from psycopg import sql

from .connection import (
    FORWARD_DB,
    MARKET_DB,
    RESEARCH_DB,
    ROLE_SECRET,
    connect,
    project_root,
    read_secret,
)


MIGRATION_TARGETS = {
    "0001": (MARKET_DB, RESEARCH_DB, FORWARD_DB),
    "0002": (MARKET_DB, RESEARCH_DB),
    "0003": (RESEARCH_DB,),
    "0004": (FORWARD_DB,),
    "0005": (MARKET_DB, RESEARCH_DB, FORWARD_DB),
    "0006": (MARKET_DB, RESEARCH_DB),
    "0007": (MARKET_DB,),
    "0008": (MARKET_DB,),
    "0009": (MARKET_DB, RESEARCH_DB),
    "0010": (MARKET_DB,),
    "0011": (MARKET_DB,),
    "0012": (MARKET_DB,),
    "0013": (MARKET_DB,),
    "0014": (MARKET_DB,),
    "0015": (MARKET_DB,),
    "0016": (MARKET_DB,),
    "0017": (MARKET_DB,),
    "0018": (MARKET_DB,),
    "0019": (MARKET_DB,),
    "0020": (MARKET_DB,),
    "0021": (MARKET_DB,),
    "0022": (MARKET_DB,),
    "0023": (MARKET_DB,),
    "0024": (MARKET_DB,),
    "0025": (MARKET_DB,),
    "0026": (MARKET_DB,),
    "0027": (MARKET_DB,),
    "0028": (MARKET_DB,),
    "0029": (MARKET_DB,),
    "0030": (MARKET_DB,),
    "0031": (MARKET_DB,),
    "0032": (MARKET_DB,),
    "0033": (MARKET_DB,),
    "0034": (MARKET_DB,),
    "0035": (MARKET_DB,),
    "0036": (MARKET_DB,),
    "0037": (MARKET_DB,),
    "0038": (MARKET_DB,),
}

LOGIN_ROLES = (
    "saxo_migrator",
    "saxo_ingest",
    "saxo_app_reader",
    "saxo_analyst_reader",
    "saxo_ops_operator",
    "v13_research_reader",
    "v13_forward_writer",
)

DATABASE_CONNECT_ROLES = {
    MARKET_DB: (
        "saxo_migrator",
        "saxo_ingest",
        "saxo_app_reader",
        "saxo_analyst_reader",
        "saxo_ops_operator",
    ),
    RESEARCH_DB: ("saxo_migrator", "v13_research_reader"),
    FORWARD_DB: ("saxo_migrator", "v13_forward_writer"),
}


class MigrationError(RuntimeError):
    pass


def migrations_directory() -> Path:
    return project_root() / "db" / "migrations"


def migration_number(path: Path) -> str:
    match = re.match(r"^(\d{4})_[a-z0-9_]+\.sql$", path.name)
    if not match:
        raise MigrationError(f"invalid migration filename: {path.name}")
    return match.group(1)


def migration_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_migrations(directory: Path | None = None) -> list[Path]:
    base = directory or migrations_directory()
    paths = sorted(base.glob("*.sql"))
    numbers = [migration_number(path) for path in paths]
    if len(numbers) != len(set(numbers)):
        raise MigrationError("duplicate migration number")
    if numbers != sorted(numbers):
        raise MigrationError("migrations are not in ascending order")
    unknown = set(numbers) - set(MIGRATION_TARGETS)
    if unknown:
        raise MigrationError(f"migration target is not declared: {sorted(unknown)}")
    return paths


def select_migrations(
    directory: Path | None = None,
    *,
    through: str | None = None,
) -> list[Path]:
    """Return the ordered migration prefix ending at ``through``.

    A clean database must stop at 0018 before either the licensed legacy
    bundle or the repository synthetic smoke seed is imported.  Migration
    0019 intentionally validates the eleven ETF mappings and therefore cannot
    be applied to an empty schema.  The boundary is explicit and fail-closed;
    arbitrary gaps are not supported.
    """

    paths = list_migrations(directory)
    if through is None:
        return paths
    numbers = [migration_number(path) for path in paths]
    if through not in numbers:
        raise MigrationError(f"unknown migration boundary: {through}")
    return [path for path in paths if migration_number(path) <= through]


def _role_exists(cursor: Any, role: str) -> bool:
    cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
    return bool(cursor.fetchone()[0])


def _database_exists(cursor: Any, database: str) -> bool:
    cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (database,))
    return bool(cursor.fetchone()[0])


def bootstrap_cluster() -> dict[str, Any]:
    created_roles: list[str] = []
    created_databases: list[str] = []
    with connect("postgres", "postgres", autocommit=True, application_name="saxo_db_bootstrap") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('saxo_db_cluster_bootstrap'))")
            try:
                if not _role_exists(cursor, "saxo_db_owner"):
                    cursor.execute(
                        "CREATE ROLE saxo_db_owner NOLOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    )
                    created_roles.append("saxo_db_owner")

                for role in LOGIN_ROLES:
                    if _role_exists(cursor, role):
                        continue
                    password_path = project_root() / ".secrets" / ROLE_SECRET[role]
                    password = read_secret(password_path)
                    command = sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(password))
                    cursor.execute(command)
                    created_roles.append(role)

                cursor.execute("GRANT saxo_db_owner TO saxo_migrator")

                for database in (MARKET_DB, RESEARCH_DB, FORWARD_DB):
                    if not _database_exists(cursor, database):
                        cursor.execute(
                            sql.SQL("CREATE DATABASE {} OWNER saxo_db_owner TEMPLATE template0 ENCODING 'UTF8'").format(
                                sql.Identifier(database)
                            )
                        )
                        created_databases.append(database)
                    cursor.execute(
                        sql.SQL("ALTER DATABASE {} OWNER TO saxo_db_owner").format(sql.Identifier(database))
                    )
                    cursor.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
                    )
                    cursor.execute(
                        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                            sql.Identifier(database),
                            sql.SQL(", ").join(sql.Identifier(role) for role in DATABASE_CONNECT_ROLES[database]),
                        )
                    )
                    cursor.execute(
                        sql.SQL("ALTER DATABASE {} SET timezone TO 'UTC'").format(sql.Identifier(database))
                    )

                reader_settings = {
                    "saxo_app_reader": MARKET_DB,
                    "saxo_analyst_reader": MARKET_DB,
                    "v13_research_reader": RESEARCH_DB,
                }
                for role, database in reader_settings.items():
                    for setting, value in (
                        ("default_transaction_read_only", "on"),
                        ("statement_timeout", "30s"),
                        ("temp_file_limit", "256MB"),
                    ):
                        cursor.execute(
                            sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} TO {}").format(
                                sql.Identifier(role),
                                sql.Identifier(database),
                                sql.Identifier(setting),
                                sql.Literal(value),
                            )
                        )
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtext('saxo_db_cluster_bootstrap'))")
    return {"created_roles": created_roles, "created_databases": created_databases}


def _migration_table_exists(cursor: Any) -> bool:
    cursor.execute("SELECT to_regclass('ops.schema_migration') IS NOT NULL")
    return bool(cursor.fetchone()[0])


def _applied_checksum(cursor: Any, database: str, number: str) -> str | None:
    if not _migration_table_exists(cursor):
        return None
    cursor.execute(
        "SELECT sha256 FROM ops.schema_migration WHERE target_database = %s AND migration_number = %s",
        (database, number),
    )
    row = cursor.fetchone()
    return None if row is None else str(row[0]).strip()


def _record_migration(cursor: Any, database: str, number: str, path: Path, checksum: str) -> None:
    cursor.execute(
        """
        INSERT INTO ops.schema_migration (
            target_database, migration_number, filename, sha256, applied_at_utc
        ) VALUES (%s, %s, %s, %s, clock_timestamp())
        ON CONFLICT (target_database, migration_number) DO NOTHING
        """,
        (database, number, path.name, checksum),
    )


def apply_database_migrations(
    database: str,
    *,
    directory: Path | None = None,
    through: str | None = None,
) -> list[dict[str, str]]:
    all_paths = list_migrations(directory)
    paths = select_migrations(directory, through=through)
    by_number = {migration_number(path): path for path in all_paths}
    bootstrap_path = by_number["0001"]
    bootstrap_checksum = migration_sha256(bootstrap_path)
    results: list[dict[str, str]] = []

    with connect("saxo_migrator", database, application_name="saxo_db_migrate") as conn:
        for path in paths:
            number = migration_number(path)
            if number == "0001" or database not in MIGRATION_TARGETS[number]:
                continue
            checksum = migration_sha256(path)
            with conn.transaction():
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext('saxo_db_database_migration'))")
                    applied = _applied_checksum(cursor, database, number)
                    if applied is not None:
                        if applied != checksum:
                            raise MigrationError(
                                f"checksum mismatch: database={database} migration={number}"
                            )
                        results.append({"database": database, "migration": number, "status": "skipped"})
                        continue

                    cursor.execute(path.read_text(encoding="utf-8"))
                    if not _migration_table_exists(cursor):
                        raise MigrationError(f"migration table was not created in {database}")

                    applied_bootstrap = _applied_checksum(cursor, database, "0001")
                    if applied_bootstrap is None:
                        _record_migration(cursor, database, "0001", bootstrap_path, bootstrap_checksum)
                    elif applied_bootstrap != bootstrap_checksum:
                        raise MigrationError(f"bootstrap checksum mismatch in {database}")

                    _record_migration(cursor, database, number, path, checksum)
                    results.append({"database": database, "migration": number, "status": "applied"})
    return results


def validate_applied_checksums(directory: Path | None = None) -> list[dict[str, str]]:
    paths = list_migrations(directory)
    expected = {migration_number(path): (path.name, migration_sha256(path)) for path in paths}
    results: list[dict[str, str]] = []
    for database in (MARKET_DB, RESEARCH_DB, FORWARD_DB):
        with connect("saxo_migrator", database, application_name="saxo_db_checksum") as conn:
            with conn.cursor() as cursor:
                if not _migration_table_exists(cursor):
                    raise MigrationError(f"missing ops.schema_migration in {database}")
                cursor.execute(
                    "SELECT migration_number, filename, sha256 FROM ops.schema_migration "
                    "WHERE target_database = %s ORDER BY migration_number",
                    (database,),
                )
                for number, filename, checksum in cursor.fetchall():
                    number = str(number)
                    if number not in expected:
                        raise MigrationError(f"unknown applied migration: {database}/{number}")
                    expected_filename, expected_checksum = expected[number]
                    if str(filename) != expected_filename or str(checksum).strip() != expected_checksum:
                        raise MigrationError(f"checksum mismatch: database={database} migration={number}")
                    results.append({"database": database, "migration": number, "status": "valid"})
    return results


def run_all(
    directory: Path | None = None,
    *,
    through: str | None = None,
) -> dict[str, Any]:
    bootstrap = bootstrap_cluster()
    migrations: list[dict[str, str]] = []
    for database in (MARKET_DB, RESEARCH_DB, FORWARD_DB):
        migrations.extend(
            apply_database_migrations(database, directory=directory, through=through)
        )
    checksums = validate_applied_checksums(directory)
    return {"bootstrap": bootstrap, "migrations": migrations, "checksums": checksums}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("all", "bootstrap", "apply", "validate"), default="all")
    parser.add_argument(
        "--through",
        metavar="NNNN",
        help="apply only the contiguous migration prefix through NNNN",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.through and args.command not in {"all", "apply"}:
        parser.error("--through is valid only with all or apply")

    if args.command == "bootstrap":
        result: Any = bootstrap_cluster()
    elif args.command == "apply":
        result = []
        for database in (MARKET_DB, RESEARCH_DB, FORWARD_DB):
            result.extend(apply_database_migrations(database, through=args.through))
    elif args.command == "validate":
        result = validate_applied_checksums()
    else:
        result = run_all(through=args.through)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
