"""Allow-listed PostgreSQL connections backed by local secret files."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg


DEFAULT_MARKET_DB = "saxo_market"
LIVE_MARKET_DB = "saxo_market_live"
MARKET_DB = os.environ.get("SAXO_MARKET_DB", DEFAULT_MARKET_DB).strip()
if MARKET_DB not in {DEFAULT_MARKET_DB, LIVE_MARKET_DB}:
    raise RuntimeError("SAXO_MARKET_DB must select an allow-listed local database")
RESEARCH_DB = "saxo_research_v13"
FORWARD_DB = "saxo_forward_v13"
ADMIN_DB = "postgres"

ROLE_SECRET = {
    "postgres": "postgres_password",
    "saxo_migrator": "saxo_migrator_password",
    "saxo_ingest": "saxo_ingest_password",
    "saxo_app_reader": "saxo_app_reader_password",
    "saxo_analyst_reader": "saxo_analyst_reader_password",
    "saxo_ops_operator": "saxo_ops_operator_password",
    "v13_research_reader": "v13_research_reader_password",
    "v13_forward_writer": "v13_forward_writer_password",
}

ALLOWED_TARGETS = {
    "postgres": {ADMIN_DB, MARKET_DB, RESEARCH_DB, FORWARD_DB},
    "saxo_migrator": {MARKET_DB, RESEARCH_DB, FORWARD_DB},
    "saxo_ingest": {MARKET_DB},
    "saxo_app_reader": {MARKET_DB},
    "saxo_analyst_reader": {MARKET_DB},
    "saxo_ops_operator": {MARKET_DB},
    "v13_research_reader": {RESEARCH_DB},
    "v13_forward_writer": {FORWARD_DB},
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ConnectionTarget:
    role: str
    database: str
    host: str
    port: int
    secret_path: Path

    def safe_summary(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "secret_file": str(self.secret_path.relative_to(project_root())),
        }


def target(role: str, database: str) -> ConnectionTarget:
    allowed = ALLOWED_TARGETS.get(role)
    if allowed is None or database not in allowed:
        raise ValueError(f"connection target is not allowed: role={role} database={database}")
    host = os.environ.get("SAXO_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("SAXO_DB_PORT", "54329"))
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("only the local PostgreSQL host is allowed")
    path = project_root() / ".secrets" / ROLE_SECRET[role]
    return ConnectionTarget(role, database, host, port, path)


def read_secret(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"secret file is unavailable: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError(f"secret file mode must be 0600: {path.name}")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 48:
        raise RuntimeError(f"secret is unexpectedly short: {path.name}")
    return value


def connect(
    role: str,
    database: str,
    *,
    autocommit: bool = False,
    application_name: str = "saxo_db",
    connect_timeout: int = 10,
) -> psycopg.Connection[Any]:
    selected = target(role, database)
    return psycopg.connect(
        host=selected.host,
        port=selected.port,
        dbname=selected.database,
        user=selected.role,
        password=read_secret(selected.secret_path),
        application_name=application_name,
        connect_timeout=connect_timeout,
        autocommit=autocommit,
    )


def raw_connection_kwargs(role: str, database: str) -> dict[str, Any]:
    """Return kwargs for tests that intentionally probe a denied database."""

    if role not in ROLE_SECRET:
        raise ValueError(f"unknown role: {role}")
    if database not in {ADMIN_DB, MARKET_DB, RESEARCH_DB, FORWARD_DB}:
        raise ValueError(f"unknown database: {database}")
    host = os.environ.get("SAXO_DB_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("only the local PostgreSQL host is allowed")
    path = project_root() / ".secrets" / ROLE_SECRET[role]
    return {
        "host": host,
        "port": int(os.environ.get("SAXO_DB_PORT", "54329")),
        "dbname": database,
        "user": role,
        "password": read_secret(path),
        "application_name": "saxo_db_denial_test",
        "connect_timeout": 5,
    }
