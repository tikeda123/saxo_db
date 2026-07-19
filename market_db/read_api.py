"""Loopback-only Flask read API over fixed DB4 query contracts."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from flask import Flask, jsonify, render_template, request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .connection import MARKET_DB, read_secret, target
from .data_ui import (
    MAX_UI_PAGE_ROWS,
    chart_marks,
    chart_rows as ui_chart_rows,
    filter_series,
    inventory_series,
    overview_payload,
    parse_offset,
    quality_summary_payload,
    resolve_series,
    series_detail,
)
from .inspect import QUERY_SPECS


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_OPERATION_ROWS = 1_000
MAX_BAR_ROWS = 10_000
API_VERSION = 1
CONTRACT_REVISION = "1.1"
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

    def query_atomic(
        self, queries: Sequence[tuple[str, Sequence[Any]]]
    ) -> list[list[dict[str, Any]]]: ...


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

    def query_atomic(
        self, queries: Sequence[tuple[str, Sequence[Any]]]
    ) -> list[list[dict[str, Any]]]:
        """Run fixed component reads under one repeatable read-only snapshot."""
        results: list[list[dict[str, Any]]] = []
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    for statement, params in queries:
                        cursor.execute(statement, tuple(params))
                        results.append([dict(row) for row in cursor.fetchall()])
        return results

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
        SELECT i.market_key AS instrument_key, i.instrument_id, i.symbol, i.category,
               '1h'::TEXT AS layer,
               b.time_utc, NULL::DATE AS session_date, b.price_basis,
               b.open, b.high, b.low, b.close, b.volume,
               b.is_complete, b.quality_status
        FROM curated.market_bar b
        JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
        WHERE i.market_key=%s
          AND b.time_utc >= %s AND b.time_utc < %s
          AND b.is_complete AND b.quality_status='PASS'
        ORDER BY b.time_utc, b.price_basis
        LIMIT %s
        """
    ),
    "4h": BarQuery(
        """
        SELECT i.market_key AS instrument_key, i.instrument_id, i.symbol, i.category,
               '4h'::TEXT AS layer,
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
        SELECT i.market_key AS instrument_key, i.instrument_id, i.symbol, i.category,
               '1d'::TEXT AS layer,
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


SERIES_STATUS_QUERIES = (
    (
        "SELECT transaction_timestamp() AS read_at_utc, "
        "txid_current_snapshot()::TEXT AS snapshot_marker",
        (),
    ),
    (
        """
        SELECT i.instrument_id, i.market_key AS instrument_key, i.symbol, i.category,
               '1h'::TEXT AS layer, 60::SMALLINT AS horizon_minutes, %s::TEXT AS price_basis
        FROM catalog.instrument i
        WHERE i.market_key=%s AND i.active_to_utc IS NULL
          AND EXISTS (
              SELECT 1 FROM analytics.v_data_freshness f
              WHERE f.instrument_id=i.instrument_id AND f.horizon_minutes=60
                AND f.price_basis=%s
          )
        """,
        ("price_basis", "instrument_key", "price_basis"),
    ),
    (
        """
        SELECT c.*
        FROM analytics.v_data_coverage c
        WHERE c.instrument_key=%s AND c.horizon_minutes=60 AND c.price_basis=%s
        """,
        ("instrument_key", "price_basis"),
    ),
    (
        """
        SELECT f.*
        FROM analytics.v_data_freshness f
        WHERE f.instrument_key=%s AND f.horizon_minutes=60 AND f.price_basis=%s
        """,
        ("instrument_key", "price_basis"),
    ),
    (
        """
        SELECT e.*
        FROM quality.v_open_event e
        WHERE e.severity IN ('ERROR','CRITICAL')
          AND (e.instrument_key=%s OR e.instrument_id IS NULL)
          AND (
              e.scope_kind='UNKNOWN'
              OR (
                  (e.affected_layer IS NULL OR e.affected_layer='curated')
                  AND (e.horizon_minutes IS NULL OR e.horizon_minutes=60)
                  AND (e.price_basis IS NULL OR e.price_basis=%s)
              )
          )
        ORDER BY e.quality_event_id
        """,
        ("instrument_key", "price_basis"),
    ),
    (
        """
        SELECT r.*
        FROM ops.v_ingestion_status r
        WHERE r.ingestion_run_id = (
            SELECT f.last_ingestion_run_id
            FROM analytics.v_data_freshness f
            WHERE f.instrument_key=%s AND f.horizon_minutes=60 AND f.price_basis=%s
        )
        """,
        ("instrument_key", "price_basis"),
    ),
    (
        "SELECT quality_event_high_watermark FROM quality.v_event_high_watermark",
        (),
    ),
)


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


def series_status_payload(
    reader: QueryReader, *, instrument_key: str, layer: str, price_basis: str
) -> dict[str, Any] | None:
    if not instrument_key or len(instrument_key) > 64:
        raise ValueError("instrument_key is required")
    if layer != "1h":
        raise ValueError("layer must be 1h")
    if not price_basis or len(price_basis) > 64:
        raise ValueError("price_basis is required")
    values = {"instrument_key": instrument_key, "price_basis": price_basis}
    queries = [
        (
            statement,
            tuple(values[name] for name in parameter_names),
        )
        for statement, parameter_names in SERIES_STATUS_QUERIES
    ]
    result_sets = reader.query_atomic(queries)
    if len(result_sets) != len(SERIES_STATUS_QUERIES):
        raise RuntimeError("atomic series status result count mismatch")
    context_rows, identity_rows, coverage_rows, freshness_rows, events, run_rows, high_rows = result_sets
    if not identity_rows:
        return None
    if len(identity_rows) != 1:
        raise RuntimeError("series identity is ambiguous")

    context = context_rows[0]
    series = identity_rows[0]
    coverage = coverage_rows[0] if coverage_rows else None
    freshness = freshness_rows[0] if freshness_rows else None
    latest_run = run_rows[0] if run_rows else None
    current_blockers = [
        row for row in events
        if str(row.get("applicability") or "UNKNOWN").upper() in {"CURRENT", "UNKNOWN"}
        and bool(row.get("current_blocker", True))
    ]
    historical = [
        row for row in events
        if str(row.get("applicability") or "UNKNOWN").upper() == "HISTORICAL"
    ]
    unknown_count = sum(
        str(row.get("applicability") or "UNKNOWN").upper() == "UNKNOWN"
        for row in current_blockers
    )
    quality_status = "FAIL" if current_blockers else "PASS"
    reasons: list[str] = []
    warnings: list[str] = []
    if coverage is None:
        reasons.append("COVERAGE_COMPONENT_MISSING")
    else:
        coverage_status = str(coverage.get("coverage_status") or "UNKNOWN")
        if coverage_status == "WARN":
            warnings.append("COVERAGE_WARN")
        elif coverage_status != "PASS":
            reasons.append(f"COVERAGE_{coverage_status}")
    if freshness is None:
        reasons.append("FRESHNESS_COMPONENT_MISSING")
    else:
        if str(freshness.get("data_status")) != "ACTIVE":
            reasons.append(f"DATA_{freshness.get('data_status') or 'UNKNOWN'}")
        if str(freshness.get("freshness_status")) != "PASS":
            reasons.append(f"FRESHNESS_{freshness.get('freshness_status') or 'UNKNOWN'}")
    if current_blockers:
        reasons.append("QUALITY_CURRENT_OR_UNKNOWN_BLOCKER")
    if latest_run is None or str(latest_run.get("status")) != "PASS":
        reasons.append("LATEST_INGESTION_RUN_NOT_PASS")

    high_watermark = (
        int(high_rows[0].get("quality_event_high_watermark") or 0) if high_rows else 0
    )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": context.get("read_at_utc"),
        "series": series,
        "consistency": {
            "read_at_utc": context.get("read_at_utc"),
            "snapshot_marker": context.get("snapshot_marker"),
            "watermark_data_version": freshness.get("data_version") if freshness else None,
            "latest_ingestion_run_id": (
                freshness.get("last_ingestion_run_id") if freshness else None
            ),
            "quality_event_high_watermark": high_watermark,
        },
        "state": {
            "coverage_status": coverage.get("coverage_status") if coverage else "NOT_EVALUATED",
            "freshness_status": freshness.get("freshness_status") if freshness else "NOT_EVALUATED",
            "quality_status": quality_status,
            "eligibility_status": (
                "BLOCKED" if reasons
                else "ELIGIBLE_WITH_WARNINGS" if warnings
                else "ELIGIBLE"
            ),
            "eligibility_reasons": reasons,
            "eligibility_warnings": warnings,
            "current_blockers": current_blockers,
            "unknown_blocker_count": unknown_count,
            "historical_unresolved_event_count": len(historical),
        },
        "components": {
            "coverage": coverage,
            "freshness": freshness,
            "latest_ingestion_run": latest_run,
        },
    }


def create_app(reader: QueryReader | None = None) -> Flask:
    selected_reader = reader or DatabaseReader()
    app = Flask(__name__)
    app.config.update(JSON_SORT_KEYS=True, MAX_CONTENT_LENGTH=16_384)

    @app.after_request
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
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
                "api_version": API_VERSION,
                "contract_revision": CONTRACT_REVISION,
            }
        )

    @app.get("/ui/")
    @app.get("/ui/<path:ui_path>")
    def data_management_ui(ui_path: str = "overview"):
        return render_template("data_ui.html", initial_view=ui_path)

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
        return jsonify(
            json_value(
                {
                    "api_version": API_VERSION,
                    "contract_revision": CONTRACT_REVISION,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "command": command,
                    "row_count": len(rows),
                    "rows": rows,
                }
            )
        )

    @app.get("/api/v1/series-status")
    def series_status():
        try:
            payload = series_status_payload(
                selected_reader,
                instrument_key=request.args.get("instrument_key", "").strip().lower(),
                layer=request.args.get("layer", "").strip().lower(),
                price_basis=request.args.get("price_basis", "").strip().lower(),
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        if payload is None:
            return jsonify({"status": "FAILED", "error_code": "SERIES_NOT_FOUND"}), 404
        return jsonify(json_value(payload))

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
                    "api_version": API_VERSION,
                    "contract_revision": CONTRACT_REVISION,
                    "generated_at_utc": datetime.now(timezone.utc),
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

    @app.get("/api/v1/ui/overview")
    def ui_overview():
        return jsonify(json_value({"api_version": 1, "data": overview_payload(selected_reader)}))

    @app.get("/api/v1/ui/series")
    def ui_series():
        try:
            limit = parse_limit(request.args.get("limit"), MAX_UI_PAGE_ROWS)
            offset = parse_offset(request.args.get("offset"))
            canonical_value = request.args.get("canonical_only", "false").strip().lower()
            if canonical_value not in {"true", "false"}:
                raise ValueError("canonical_only must be true or false")
            rows = filter_series(
                inventory_series(selected_reader),
                role=request.args.get("role", "").strip().upper(),
                category=request.args.get("category", "").strip(),
                symbol=request.args.get("symbol", "").strip(),
                layer=request.args.get("layer", "").strip(),
                status=request.args.get("status", "").strip().upper(),
                canonical_only=canonical_value == "true",
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        selected = rows[offset:offset + limit]
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": selected,
                    "paging": {
                        "offset": offset,
                        "limit": limit,
                        "total": len(rows),
                        "has_more": offset + len(selected) < len(rows),
                    },
                    "warnings": [],
                }
            )
        )

    @app.get("/api/v1/ui/series/<selected_id>")
    def ui_series_detail(selected_id: str):
        try:
            detail = series_detail(selected_reader, selected_id)
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        except LookupError:
            return jsonify({"status": "FAILED", "error_code": "SERIES_NOT_FOUND"}), 404
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": detail,
                    "paging": None,
                    "warnings": [],
                }
            )
        )

    @app.get("/api/v1/ui/chart-bars")
    def ui_chart_bars():
        try:
            selected_id = request.args.get("series_id", "").strip().lower()
            start = parse_utc(request.args.get("start", ""), "start")
            end = parse_utc(request.args.get("end", ""), "end")
            limit = parse_limit(request.args.get("limit"), MAX_BAR_ROWS)
            eligibility = request.args.get("eligibility", "eligible").strip().lower()
            series = resolve_series(selected_reader, selected_id)
            kind, rows = ui_chart_rows(
                selected_reader,
                series,
                start=start,
                end=end,
                limit=limit,
                eligibility=eligibility,
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        except LookupError:
            return jsonify({"status": "FAILED", "error_code": "SERIES_NOT_FOUND"}), 404
        truncated = len(rows) > limit
        warnings = []
        if eligibility == "stored_complete":
            warnings.append("NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED")
        if truncated:
            warnings.append("RESULT_TRUNCATED")
        selected_rows = rows[-limit:] if truncated else rows
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": selected_rows,
                    "paging": {"limit": limit, "truncated": truncated},
                    "warnings": warnings,
                    "series_id": selected_id,
                    "series_kind": kind,
                    "eligibility": eligibility,
                    "start": start,
                    "end": end,
                }
            )
        )

    @app.get("/api/v1/ui/chart-marks")
    def ui_chart_marks():
        try:
            selected_id = request.args.get("series_id", "").strip().lower()
            start = parse_utc(request.args.get("start", ""), "start")
            end = parse_utc(request.args.get("end", ""), "end")
            series = resolve_series(selected_reader, selected_id)
            rows = chart_marks(selected_reader, series, start, end)
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        except LookupError:
            return jsonify({"status": "FAILED", "error_code": "SERIES_NOT_FOUND"}), 404
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": rows,
                    "paging": {"limit": len(rows), "truncated": False},
                    "warnings": [],
                }
            )
        )

    @app.get("/api/v1/ui/quality-summary")
    def ui_quality_summary():
        return jsonify(json_value({"api_version": 1, "data": quality_summary_payload(selected_reader)}))

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
