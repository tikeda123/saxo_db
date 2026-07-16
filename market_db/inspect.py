"""Read-only operational inspection over an allow-list of database views."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from psycopg.rows import dict_row

from .connection import MARKET_DB, RESEARCH_DB, connect


@dataclass(frozen=True)
class QuerySpec:
    role: str
    relation: str
    order_by: str
    alert_column: str | None = None
    alert_values: frozenset[str] = frozenset()


QUERY_SPECS: dict[tuple[str, str], QuerySpec] = {
    (MARKET_DB, "inventory"): QuerySpec(
        "saxo_app_reader", "analytics.v_data_inventory", "layer, symbol, horizon_minutes"
    ),
    (MARKET_DB, "coverage"): QuerySpec(
        "saxo_analyst_reader",
        "analytics.v_data_coverage",
        "symbol, horizon_minutes",
        "coverage_status",
        frozenset({"WARN", "FAIL"}),
    ),
    (MARKET_DB, "freshness"): QuerySpec(
        "saxo_app_reader",
        "analytics.v_data_freshness",
        "symbol, horizon_minutes",
        "freshness_status",
        frozenset({"STALE", "FAIL"}),
    ),
    (MARKET_DB, "runs"): QuerySpec(
        "saxo_app_reader",
        "ops.v_ingestion_status",
        "started_at_utc DESC, ingestion_run_id DESC",
        "status",
        frozenset({"FAILED", "BLOCKED"}),
    ),
    (MARKET_DB, "quality"): QuerySpec(
        "saxo_app_reader",
        "quality.v_open_event",
        "created_at_utc DESC, quality_event_id DESC",
        "severity",
        frozenset({"ERROR", "CRITICAL"}),
    ),
    (MARKET_DB, "lineage"): QuerySpec(
        "saxo_analyst_reader", "analytics.v_data_lineage", "source_dataset_id, source_file_id"
    ),
    (MARKET_DB, "storage"): QuerySpec(
        "saxo_analyst_reader",
        "ops.v_storage_usage",
        "size_bytes DESC, schema_name, relation_name",
        "partition_review_threshold_status",
        frozenset({"REVIEW"}),
    ),
    (MARKET_DB, "backups"): QuerySpec(
        "saxo_app_reader", "ops.v_backup_status", "database_name"
    ),
    (RESEARCH_DB, "inventory"): QuerySpec(
        "v13_research_reader", "analytics.v_data_inventory", "layer, symbol, horizon_minutes"
    ),
    (RESEARCH_DB, "coverage"): QuerySpec(
        "v13_research_reader",
        "analytics.v_data_coverage",
        "symbol, horizon_minutes",
        "coverage_status",
        frozenset({"WARN", "FAIL"}),
    ),
    (RESEARCH_DB, "lineage"): QuerySpec(
        "v13_research_reader", "analytics.v_data_lineage", "source_dataset_id, source_file_id"
    ),
    (RESEARCH_DB, "storage"): QuerySpec(
        "v13_research_reader",
        "ops.v_storage_usage",
        "size_bytes DESC, schema_name, relation_name",
        "partition_review_threshold_status",
        frozenset({"REVIEW"}),
    ),
}


def query_spec(database: str, command: str) -> QuerySpec:
    try:
        return QUERY_SPECS[(database, command)]
    except KeyError as exc:
        raise ValueError(f"inspection is not allowed: database={database} command={command}") from exc


def fetch_rows(database: str, command: str, limit: int = 200) -> list[dict[str, Any]]:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    spec = query_spec(database, command)
    # relation and ordering are constants from QUERY_SPECS; callers cannot supply SQL.
    statement = f"SELECT * FROM {spec.relation} ORDER BY {spec.order_by} LIMIT %s"
    with connect(spec.role, database, application_name=f"saxo_db_inspect_{command}") as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(statement, (limit,))
            return [dict(row) for row in cursor.fetchall()]


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        selected = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return selected.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def render_json(database: str, command: str, rows: Sequence[dict[str, Any]]) -> str:
    payload = {
        "command": command,
        "database": database,
        "row_count": len(rows),
        "rows": [{key: _json_value(value) for key, value in row.items()} for row in rows],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def render_table(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "(0 rows)"
    columns = list(rows[0])
    values = [["" if row.get(column) is None else str(_json_value(row.get(column))) for column in columns] for row in rows]
    widths = [
        min(64, max(len(column), *(len(row[index]) for row in values)))
        for index, column in enumerate(columns)
    ]

    def line(parts: Sequence[str]) -> str:
        return " | ".join(part[: widths[index]].ljust(widths[index]) for index, part in enumerate(parts))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([line(columns), separator, *(line(row) for row in values), f"({len(rows)} rows)"])


def has_alert(spec: QuerySpec, rows: Sequence[dict[str, Any]]) -> bool:
    if spec.alert_column is None:
        return False
    return any(str(row.get(spec.alert_column)) in spec.alert_values for row in rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect allow-listed operational views")
    parser.add_argument(
        "command",
        choices=("inventory", "coverage", "freshness", "runs", "quality", "lineage", "storage", "backups"),
    )
    parser.add_argument("--database", choices=(MARKET_DB, RESEARCH_DB), default=MARKET_DB)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--fail-on-alert", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        spec = query_spec(args.database, args.command)
        rows = fetch_rows(args.database, args.command, args.limit)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"inspection failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # psycopg errors are sanitized by omitting connection details.
        print(f"inspection failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(render_json(args.database, args.command, rows) if args.format == "json" else render_table(rows))
    return 2 if args.fail_on_alert and has_alert(spec, rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
