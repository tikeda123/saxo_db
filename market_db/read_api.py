"""Loopback-only Flask read API over fixed DB4 query contracts."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from flask import Flask, jsonify, request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .connection import MARKET_DB, read_secret, target
from .inspect import QUERY_SPECS


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_OPERATION_ROWS = 1_000
MAX_BAR_ROWS = 10_000
OPERATION_COMMANDS = (
    "inventory",
    "coverage",
    "freshness",
    "runs",
    "quality",
    "lineage",
    "storage",
    "backups",
)


class QueryReader(Protocol):
    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


class DatabaseReader:
    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self._owns_pool = pool is None
        self.pool = pool or self._create_pool()

    @staticmethod
    def _create_pool() -> ConnectionPool:
        selected = target("saxo_app_reader", MARKET_DB)
        return ConnectionPool(
            conninfo="",
            kwargs={
                "host": selected.host,
                "port": selected.port,
                "dbname": selected.database,
                "user": selected.role,
                "password": read_secret(selected.secret_path),
                "application_name": "saxo_db_read_api",
                "connect_timeout": 10,
                "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
            },
            min_size=0,
            max_size=5,
            open=True,
            name="saxo-db-read-api",
        )

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(statement, tuple(params))
                    return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        if self._owns_pool:
            self.pool.close()


@dataclass(frozen=True)
class BarQuery:
    statement: str
    date_bounds: bool = False


BAR_QUERIES: Mapping[str, BarQuery] = {
    "1h": BarQuery(
        """
        SELECT i.market_key AS instrument_key, i.symbol, '1h'::TEXT AS layer,
               b.time_utc, NULL::DATE AS session_date, b.price_basis,
               b.open, b.high, b.low, b.close, b.volume,
               b.is_complete, b.quality_status
        FROM curated.market_bar b
        JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
        WHERE i.market_key=%s
          AND b.derivation_version='db3_accepted_1h_calendar_v1'
          AND b.time_utc >= %s AND b.time_utc < %s
          AND b.is_complete AND b.quality_status='PASS'
        ORDER BY b.time_utc, b.price_basis
        LIMIT %s
        """
    ),
    "4h": BarQuery(
        """
        SELECT i.market_key AS instrument_key, i.symbol, '4h'::TEXT AS layer,
               b.time_utc, NULL::DATE AS session_date, b.price_basis,
               b.open, b.high, b.low, b.close, b.volume,
               b.is_complete, b.quality_status
        FROM derived.market_bar_4h b
        JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
        WHERE i.market_key=%s
          AND b.time_utc >= %s AND b.time_utc < %s
          AND b.is_complete AND b.quality_status='PASS'
        ORDER BY b.time_utc, b.price_basis
        LIMIT %s
        """
    ),
    "1d": BarQuery(
        """
        SELECT i.market_key AS instrument_key, i.symbol, '1d'::TEXT AS layer,
               NULL::TIMESTAMPTZ AS time_utc, b.session_date, b.price_basis,
               b.open, b.high, b.low, b.close, b.volume,
               b.is_complete, b.quality_status
        FROM derived.market_bar_1d_risk b
        JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
        WHERE i.market_key=%s
          AND b.derivation_version='db3_accepted_1h_calendar_v1'
          AND b.session_date >= %s AND b.session_date < %s
          AND b.is_complete AND b.quality_status='PASS'
        ORDER BY b.session_date, b.price_basis
        LIMIT %s
        """,
        date_bounds=True,
    ),
}


def parse_utc(value: str, field: str) -> datetime:
    try:
        selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return selected.astimezone(timezone.utc)


def parse_limit(value: str | None, maximum: int) -> int:
    try:
        selected = int(value or min(200, maximum))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= selected <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return selected


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        selected = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return selected.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def operation_rows(reader: QueryReader, command: str, limit: int) -> list[dict[str, Any]]:
    if command not in OPERATION_COMMANDS:
        raise ValueError("operation is not allow-listed")
    spec = QUERY_SPECS[(MARKET_DB, command)]
    statement = f"SELECT * FROM {spec.relation} ORDER BY {spec.order_by} LIMIT %s"
    return reader.query(statement, (limit,))


def bar_rows(
    reader: QueryReader,
    *,
    instrument_key: str,
    layer: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    if not instrument_key or len(instrument_key) > 64:
        raise ValueError("instrument_key is required")
    try:
        query = BAR_QUERIES[layer]
    except KeyError as exc:
        raise ValueError("layer must be one of: 1h, 4h, 1d") from exc
    if start >= end:
        raise ValueError("start must be earlier than end")
    lower: datetime | date = start.date() if query.date_bounds else start
    upper: datetime | date = end.date() if query.date_bounds else end
    return reader.query(query.statement, (instrument_key, lower, upper, limit + 1))


def create_app(reader: QueryReader | None = None) -> Flask:
    selected_reader = reader or DatabaseReader()
    app = Flask(__name__)
    app.config.update(JSON_SORT_KEYS=True, MAX_CONTENT_LENGTH=16_384)

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"status": "FAILED", "error_code": "NOT_FOUND"}), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return jsonify({"status": "FAILED", "error_code": "READ_ONLY_API"}), 405

    @app.errorhandler(Exception)
    def unexpected(error):
        return jsonify({"status": "FAILED", "error_code": "DATABASE_UNAVAILABLE"}), 503

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "saxo_db_read_api",
                "status": "PASS",
                "read_only": True,
                "api_version": 1,
            }
        )

    @app.get("/health")
    def health():
        rows = selected_reader.query(
            """
            SELECT current_database() AS database_name,
                   current_user AS role_name,
                   current_setting('transaction_read_only') AS transaction_read_only,
                   current_setting('statement_timeout') AS statement_timeout
            """
        )
        row = rows[0]
        healthy = (
            row.get("database_name") == MARKET_DB
            and row.get("role_name") == "saxo_app_reader"
            and row.get("transaction_read_only") == "on"
        )
        return jsonify(json_value({"status": "PASS" if healthy else "FAIL", "database": row})), (
            200 if healthy else 503
        )

    @app.get("/api/v1/operations/<command>")
    def operations(command: str):
        try:
            limit = parse_limit(request.args.get("limit"), MAX_OPERATION_ROWS)
            rows = operation_rows(selected_reader, command, limit)
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value({"command": command, "row_count": len(rows), "rows": rows}))

    @app.get("/api/v1/bars")
    def bars():
        try:
            instrument_key = request.args.get("instrument_key", "").strip().lower()
            layer = request.args.get("layer", "").strip().lower()
            start = parse_utc(request.args.get("start", ""), "start")
            end = parse_utc(request.args.get("end", ""), "end")
            limit = parse_limit(request.args.get("limit"), MAX_BAR_ROWS)
            rows = bar_rows(
                selected_reader,
                instrument_key=instrument_key,
                layer=layer,
                start=start,
                end=end,
                limit=limit,
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        truncated = len(rows) > limit
        selected = rows[:limit]
        return jsonify(
            json_value(
                {
                    "instrument_key": instrument_key,
                    "layer": layer,
                    "start": start,
                    "end": end,
                    "row_count": len(selected),
                    "truncated": truncated,
                    "rows": selected,
                }
            )
        )

    @app.get("/api/v1/manifests")
    def manifests():
        datasets = selected_reader.query(
            """
            SELECT source_dataset_id, dataset_name, provider, environment,
                   dataset_kind, price_basis, research_eligibility
            FROM catalog.source_dataset
            ORDER BY source_dataset_id
            """
        )
        snapshots = selected_reader.query(
            """
            SELECT snapshot_id, plan_id, research_line_id, cutoff_utc,
                   source_database, snapshot_sha256, status,
                   snapshot_manifest_relative_path, dump_relative_path,
                   dump_sha256, dump_size_bytes, dump_pg_restore_list_pass
            FROM ops.research_snapshot
            ORDER BY frozen_at_utc DESC, snapshot_id DESC
            """
        )
        return jsonify(json_value({"datasets": datasets, "snapshots": snapshots}))

    @app.get("/api/v1/layer-counts")
    def layer_counts():
        rows = selected_reader.query(
            """
            SELECT '1h'::TEXT AS layer, COUNT(*)::BIGINT AS row_count FROM curated.market_bar
            UNION ALL
            SELECT '4h'::TEXT, COUNT(*)::BIGINT FROM derived.market_bar_4h
            WHERE derivation_version='db3_accepted_1h_calendar_v1'
            UNION ALL
            SELECT '1d'::TEXT, COUNT(*)::BIGINT FROM derived.market_bar_1d_risk
            WHERE derivation_version='db3_accepted_1h_calendar_v1'
            ORDER BY layer
            """
        )
        return jsonify(json_value({"row_count": len(rows), "rows": rows}))

    if isinstance(selected_reader, DatabaseReader):
        atexit.register(selected_reader.close)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the loopback-only DB4 read API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("port must be between 1 and 65535")
    app = create_app()
    app.run(host=LOOPBACK_HOST, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
