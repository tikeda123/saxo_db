from __future__ import annotations

import json
from pathlib import Path

from market_db.connection import FORWARD_DB, MARKET_DB, RESEARCH_DB, project_root, target


def read(relative_path: str) -> str:
    return (project_root() / relative_path).read_text(encoding="utf-8")


def test_compose_is_local_single_service_and_persistent():
    compose = read("compose.yaml")
    assert "postgres:18.4-bookworm" in compose
    assert '"127.0.0.1:54329:5432"' in compose
    assert "saxo_pg18_data:/var/lib/postgresql" in compose
    assert "POSTGRES_PASSWORD_FILE" in compose
    assert "pg_isready" in compose
    assert "platform:" not in compose
    assert "pgadmin" not in compose.lower()


def test_secrets_and_import_data_are_ignored():
    ignore = read(".gitignore")
    for required in (".secrets/", ".env", "backups/", "*.dump", "data/import/**/*.csv"):
        assert required in ignore
    env_example = read(".env.example").lower()
    assert "password" not in env_example
    assert "token" not in env_example


def test_connection_allow_list_and_localhost(monkeypatch):
    assert target("saxo_app_reader", MARKET_DB).database == MARKET_DB
    assert target("v13_research_reader", RESEARCH_DB).database == RESEARCH_DB
    assert target("v13_forward_writer", FORWARD_DB).database == FORWARD_DB
    try:
        target("saxo_app_reader", FORWARD_DB)
    except ValueError:
        pass
    else:
        raise AssertionError("cross-database target unexpectedly allowed")
    monkeypatch.setenv("SAXO_DB_HOST", "db.example.invalid")
    try:
        target("saxo_app_reader", MARKET_DB)
    except ValueError:
        pass
    else:
        raise AssertionError("remote database host unexpectedly allowed")


def test_sql_security_boundaries_are_explicit():
    sql_files = sorted((project_root() / "db" / "migrations").glob("*.sql"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in sql_files)
    lower = text.lower()
    assert "revoke connect" not in lower  # cluster-level revoke is safely identifier-composed in Python.
    assert "revoke all on schema public from public" in lower
    assert "security definer" in lower
    assert "set search_path = pg_catalog" in lower
    assert "create extension" not in lower
    assert "dblink" not in lower
    assert "foreign data wrapper" not in lower
    assert "password" not in lower


def test_machine_specs_are_valid_json_and_db3_pass_unlocks_db4():
    spec = json.loads(read("specs/v13_phase_db0_database_spec.json"))
    import_spec = json.loads(read("specs/saxo_db_import_spec.json"))
    assert spec["phase"] == "DB0"
    assert import_spec["database_implementation_started"] is True
    assert import_spec["source_files_are_immutable"] is True
    assert import_spec["next_phase"] == "DB4"
    phase_status = {item["phase"]: item["status"] for item in spec["phases"]}
    assert phase_status["DB2"] == "PASS"
    assert phase_status["DB3"] == "PASS"
    assert phase_status["DB4"] == "NEXT"
    assert "DB1" in read("docs/db1_implementation_plan.md")
    assert "DB2" in read("docs/db2_implementation_plan.md")
    assert "docker compose down -v" in read("docs/db1_implementation_plan.md")


def test_tracked_configuration_has_no_host_specific_workspace_path():
    checked = [
        "compose.yaml",
        ".env.example",
        "market_db/connection.py",
        "market_db/migrate.py",
        "market_db/inspect.py",
        "market_db/operate.py",
        "market_db/operator_ui.py",
        "market_db/validate.py",
    ]
    forbidden = "/Users/" + "tikeda/"
    assert all(forbidden not in read(path) for path in checked)
