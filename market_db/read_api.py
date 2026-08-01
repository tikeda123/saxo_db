"""Loopback-only Flask read API over fixed DB4 query contracts."""

from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from flask import Flask, jsonify, render_template, request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .connection import MARKET_DB, RESEARCH_DB, project_root, read_secret, target
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
from .instrument_reference import instrument_catalog_payload, reference_for_key
from .inspect import QUERY_SPECS
from .strategy_external_contract import (
    EXPECTED_ROLES as STRATEGY_EXTERNAL_ROLES,
    StrategyExternalContractError,
    public_strategy_external_contract,
    public_strategy_external_status,
)
from .total_return_contract import (
    TotalReturnContractError,
    contract_for_request,
    load_total_return_research_contracts,
    public_contract,
    validate_requested_window,
)
from .total_return_history import load_full_history_series, select_full_history_rows


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_OPERATION_ROWS = 1_000
MAX_BAR_ROWS = 10_000
MAX_TOTAL_RETURN_ROWS = 10_000
MAX_STRATEGY_RECEIPT_ROWS = 1_000
MAX_STRATEGY_CALENDAR_ROWS = 5_000
API_VERSION = 1
CONTRACT_REVISION = "1.2"
C2_DAILY_CLOSE_KEYS = (
    "spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld",
)
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
SNAPSHOT_MANIFEST_ALLOWLIST = frozenset(
    {"manifests/db2_research_snapshot_content.json"}
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


class SnapshotDatabaseReader(DatabaseReader):
    """Dedicated fixed pool for the frozen research snapshot database."""

    @staticmethod
    def _create_pool() -> ConnectionPool:
        selected = target("v13_research_reader", RESEARCH_DB)
        return ConnectionPool(
            conninfo="",
            kwargs={
                "host": selected.host,
                "port": selected.port,
                "dbname": selected.database,
                "user": selected.role,
                "password": read_secret(selected.secret_path),
                "application_name": "saxo_db_snapshot_read_api",
                "connect_timeout": 10,
                "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
            },
            min_size=0,
            max_size=3,
            open=True,
            name="saxo-db-snapshot-read-api",
        )


class SnapshotReadError(RuntimeError):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class TotalReturnReadError(RuntimeError):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class StrategyExternalReadError(RuntimeError):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class CursorError(RuntimeError):
    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class CursorCodec:
    secret: bytes

    def encode(self, payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        signature = hmac.new(self.secret, body, hashlib.sha256).digest()
        return ".".join(
            (
                base64.urlsafe_b64encode(body).decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            )
        )

    def decode(self, token: str) -> dict[str, Any]:
        if not token or len(token) > 4096 or token.count(".") != 1:
            raise CursorError("CURSOR_INVALID", 400)
        encoded_body, encoded_signature = token.split(".", 1)
        try:
            body = base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4))
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise CursorError("CURSOR_INVALID", 400) from exc
        expected = hmac.new(self.secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise CursorError("CURSOR_INVALID", 400)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise CursorError("CURSOR_INVALID", 400)
        return payload


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
          AND NOT EXISTS (
              SELECT 1 FROM catalog.series_publication_state p
              WHERE p.instrument_id=i.instrument_id AND p.horizon_minutes=60
                AND p.price_basis=b.price_basis
                AND p.publication_status NOT IN ('STAGING','PUBLISHED')
          )
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
          AND NOT EXISTS (
              SELECT 1 FROM catalog.series_publication_state p
              WHERE p.instrument_id=i.instrument_id AND p.horizon_minutes=60
                AND p.price_basis=b.price_basis
                AND p.publication_status NOT IN ('STAGING','PUBLISHED')
          )
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
          AND NOT EXISTS (
              SELECT 1 FROM catalog.series_publication_state p
              WHERE p.instrument_id=i.instrument_id AND p.horizon_minutes=60
                AND p.price_basis=b.price_basis
                AND p.publication_status NOT IN ('STAGING','PUBLISHED')
          )
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
               i.asset_type,
               '1h'::TEXT AS layer, 60::SMALLINT AS horizon_minutes, %s::TEXT AS price_basis
        FROM catalog.instrument i
        WHERE i.market_key=%s AND i.active_to_utc IS NULL
          AND (
              EXISTS (
                  SELECT 1 FROM analytics.v_data_freshness f
                  WHERE f.instrument_id=i.instrument_id AND f.horizon_minutes=60
                    AND f.price_basis=%s
              )
              OR EXISTS (
                  SELECT 1 FROM catalog.series_publication_state p
                  WHERE p.instrument_id=i.instrument_id AND p.horizon_minutes=60
                    AND p.price_basis=%s
              )
          )
        """,
        ("price_basis", "instrument_key", "price_basis", "price_basis"),
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
          AND NOT (
              e.rule_id='db3_atomic_run_gate'
              AND EXISTS (
                  SELECT 1
                  FROM ops.v_ingestion_status source_revision_run
                  WHERE source_revision_run.ingestion_run_id=e.ingestion_run_id
                    AND source_revision_run.error_code IN (
                        'BLOCKED_FULL_REFETCH_REQUIRED',
                        'BLOCKED_BOUNDED_REVISION_REQUIRED',
                        'BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED'
                    )
              )
          )
          AND (
              e.instrument_key=%s
              OR e.scope_kind IN ('GLOBAL','UNKNOWN','DATASET','LAYER')
              OR (
                  e.instrument_id IS NULL AND e.scope_kind='RUN'
                  AND (
                      NOT (e.scope_evidence ? 'selected_instrument_keys')
                      OR (e.scope_evidence->'selected_instrument_keys') ? %s
                  )
              )
          )
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
        ("instrument_key", "instrument_key", "price_basis"),
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
    (
        """
        SELECT p.publication_status,p.quality_status,p.coverage_status,
               p.freshness_status,p.blocker_code,p.last_evaluated_run_id,
               p.evidence_manifest_relative_path,p.evidence_manifest_sha256,
               p.last_accepted_complete_time_utc,
               p.consecutive_normal_passes,p.updated_at_utc,
               p.consumer_availability_status,p.research_policy_id,
               p.provider_advertised_start_utc,p.effective_coverage_start_utc,
               p.coverage_limitation,p.warning_metadata_json,
               p.policy_approved_at_utc,p.policy_approved_by
        FROM catalog.series_publication_state p
        JOIN catalog.instrument i USING (instrument_id)
        WHERE i.market_key=%s AND p.horizon_minutes=60 AND p.price_basis=%s
        """,
        ("instrument_key", "price_basis"),
    ),
    (
        """
        SELECT r.*
        FROM ops.v_data_version_revision_state r
        WHERE r.instrument_key=%s AND r.horizon_minutes=60 AND r.price_basis=%s
        """,
        ("instrument_key", "price_basis"),
    ),
)


SNAPSHOT_CONTEXT_QUERY = """
SELECT transaction_timestamp() AS read_at_utc,
       txid_current_snapshot()::TEXT AS snapshot_marker,
       current_database() AS database_name,
       current_user AS role_name,
       current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('statement_timeout') AS statement_timeout
"""

SNAPSHOT_METADATA_QUERY = """
SELECT snapshot_id, plan_id, research_line_id, cutoff_utc,
       source_database, source_manifest_sha256, row_counts_json,
       snapshot_sha256, frozen_at_utc, status,
       snapshot_manifest_relative_path
FROM ops.research_snapshot
WHERE snapshot_id=%s
"""

SNAPSHOT_SERIES_QUERY = """
SELECT i.instrument_id, i.market_key AS instrument_key, i.symbol, i.category,
       '1h'::TEXT AS layer, 60::SMALLINT AS horizon_minutes,
       %s::TEXT AS price_basis
FROM catalog.instrument i
WHERE i.market_key=%s AND i.active_to_utc IS NULL
  AND EXISTS (
      SELECT 1 FROM curated.market_bar b
      WHERE b.instrument_id=i.instrument_id
        AND b.horizon_minutes=60 AND b.price_basis=%s
  )
"""

SNAPSHOT_INTEGRITY_QUERY = """
SELECT COUNT(*)::BIGINT AS curated_market_bar_rows,
       MIN(time_utc) AS curated_min_time_utc,
       MAX(time_utc) AS curated_max_time_utc,
       COUNT(*) FILTER (
           WHERE time_utc > (
               SELECT cutoff_utc FROM ops.research_snapshot WHERE snapshot_id=%s
           )
       )::BIGINT AS post_cutoff_rows
FROM curated.market_bar
"""

SNAPSHOT_BARS_QUERY = """
SELECT i.market_key AS instrument_key, i.instrument_id, i.symbol, i.category,
       '1h'::TEXT AS layer, b.time_utc, b.price_basis,
       b.open, b.high, b.low, b.close, b.volume,
       b.is_complete, b.quality_status
FROM curated.market_bar b
JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
WHERE i.market_key=%s
  AND b.horizon_minutes=60 AND b.price_basis=%s
  AND b.time_utc >= %s AND b.time_utc < %s
  AND b.time_utc <= (
      SELECT cutoff_utc FROM ops.research_snapshot WHERE snapshot_id=%s
  )
  AND (
      %s::TIMESTAMPTZ IS NULL
      OR (b.time_utc, b.instrument_id, b.price_basis) > (
          %s::TIMESTAMPTZ, %s::BIGINT, %s::TEXT
      )
  )
  AND b.is_complete AND b.quality_status='PASS'
ORDER BY b.time_utc, b.instrument_id, b.price_basis
LIMIT %s
"""

TOTAL_RETURN_CONTEXT_QUERY = """
SELECT transaction_timestamp() AS read_at_utc,
       txid_current_snapshot()::TEXT AS snapshot_marker,
       current_database() AS database_name,
       current_user AS role_name,
       current_setting('transaction_read_only') AS transaction_read_only
"""

TOTAL_RETURN_ROWS_QUERY = """
WITH eligible_mappings AS (
    SELECT m.source_dataset_id, m.external_series_key, m.instrument_id,
           m.mapping_kind, m.mapping_reason, m.approved_at_utc, m.approved_by,
           i.market_key AS instrument_key, i.symbol, i.category,
           ds.dataset_name, ds.provider, ds.price_basis,
           ds.research_eligibility, ds.source_manifest_sha256 AS state_revision,
           COALESCE((ds.metadata_json->>'current')::BOOLEAN,FALSE) AS is_current
    FROM catalog.series_instrument_mapping m
    JOIN catalog.instrument i ON i.instrument_id=m.instrument_id
    JOIN catalog.source_dataset ds ON ds.source_dataset_id=m.source_dataset_id
    WHERE i.market_key=%s
      AND i.active_to_utc IS NULL
      AND m.active
      AND m.approved_at_utc IS NOT NULL
      AND ds.dataset_kind='total_return'
      AND ds.price_basis='etf_total_return'
      AND (%s::TEXT IS NULL OR m.source_dataset_id=%s)
), candidates AS (
    SELECT e.*,
           COUNT(*) OVER ()::BIGINT AS mapping_count
    FROM eligible_mappings e
    WHERE %s::TEXT IS NOT NULL
       OR e.is_current
       OR NOT EXISTS (SELECT 1 FROM eligible_mappings x WHERE x.is_current)
)
SELECT c.source_dataset_id, c.external_series_key, c.instrument_id,
       c.mapping_kind, c.mapping_reason, c.approved_at_utc, c.approved_by,
       c.instrument_key, c.symbol, c.category, c.dataset_name, c.provider,
       c.price_basis, c.research_eligibility, c.state_revision, c.mapping_count,
       d.date AS session_date, d.total_return_index AS value,
       d.volume, d.quality_status,
       'etf_total_return'::TEXT AS row_price_basis
FROM candidates c
LEFT JOIN LATERAL (
    SELECT date, total_return_index, volume, quality_status
    FROM curated.etf_total_return_daily d
    WHERE d.source_dataset_id=c.source_dataset_id
      AND d.ticker=c.external_series_key
      AND d.date >= %s::DATE AND d.date < %s::DATE
      AND (
          (%s='eligible' AND d.quality_status='PASS')
          OR (%s='stored_complete' AND d.quality_status IN ('PASS','WARN','NOT_EVALUATED'))
      )
      AND (%s::DATE IS NULL OR d.date > %s::DATE)
    ORDER BY d.date
    LIMIT %s
) d ON TRUE
ORDER BY c.source_dataset_id, c.external_series_key, d.date NULLS FIRST
"""

TOTAL_RETURN_STATUS_QUERY = """
SELECT *
FROM analytics.v_total_return_research_series
WHERE instrument_key=%s
  AND source_dataset_id=%s
  AND external_series_key=%s
"""


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


SnapshotManifestLoader = Callable[[str], tuple[dict[str, Any], str]]


def load_snapshot_manifest(relative_path: str) -> tuple[dict[str, Any], str]:
    if relative_path not in SNAPSHOT_MANIFEST_ALLOWLIST:
        raise SnapshotReadError("SNAPSHOT_NOT_VERIFIED", 503)
    path = project_root() / relative_path
    if not path.is_file() or path.is_symlink():
        raise SnapshotReadError("SNAPSHOT_NOT_VERIFIED", 503)
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotReadError("SNAPSHOT_NOT_VERIFIED", 503) from exc
    return payload, hashlib.sha256(content).hexdigest()


def ordered_content_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        json_value(list(rows)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_iso_date(value: str, field: str) -> date:
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if selected.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return selected


def strategy_external_migration_applied(reader: QueryReader) -> bool:
    rows = reader.query(
        "SELECT to_regclass('analytics.v_strategy_external_data_contract_status') "
        "IS NOT NULL AS migration_applied"
    )
    return bool(rows and rows[0].get("migration_applied") is True)


def strategy_external_status_rows(reader: QueryReader) -> list[dict[str, Any]]:
    rows = reader.query(
        """
        SELECT edc_id, dataset_role, contract_id, contract_state,
               availability_state, provider_id, dataset_id, price_basis,
               horizon_minutes, target_read_endpoint, latest_receipt_id,
               last_good_receipt_id,
               source_as_of, source_observed_at_utc, available_at_utc,
               accepted_at_utc, expected_by_utc, published_at_utc,
               freshness_state, quality_state, revision_state,
               cost_confidence, warning_ids, blocker_ids,
               decision_required_ids, provider_data_version,
               manifest_sha256, ordered_content_sha256, calendar_id
        FROM analytics.v_strategy_external_data_contract_status
        ORDER BY edc_id
        """
    )
    return [
        {
            "edc_id": row["edc_id"],
            "dataset_role": row["dataset_role"],
            "contract_id": row["contract_id"],
            "contract_state": row["contract_state"],
            "availability_state": row["availability_state"],
            "provider_id": row.get("provider_id"),
            "dataset_id": row.get("dataset_id"),
            "price_basis": row.get("price_basis"),
            "horizon_minutes": row.get("horizon_minutes"),
            "target_read_endpoint": row.get("target_read_endpoint"),
            "latest_receipt_id": row.get("latest_receipt_id"),
            "source_as_of": row.get("source_as_of"),
            "source_observed_at_utc": row.get("source_observed_at_utc"),
            "available_at_utc": row.get("available_at_utc"),
            "accepted_at_utc": row.get("accepted_at_utc"),
            "published_at_utc": row.get("published_at_utc"),
            "freshness": {
                "state": row["freshness_state"],
                "expected_by_utc": row.get("expected_by_utc"),
            },
            "quality": {
                "state": row["quality_state"],
                "warning_ids": row.get("warning_ids") or [],
                "blocker_ids": row.get("blocker_ids") or [],
                "last_good_receipt_id": row.get("last_good_receipt_id"),
            },
            "revision": {
                "state": row["revision_state"],
                "provider_data_version": row.get("provider_data_version"),
                "manifest_sha256": (
                    str(row["manifest_sha256"]).strip()
                    if row.get("manifest_sha256") is not None
                    else None
                ),
                "ordered_content_sha256": (
                    str(row["ordered_content_sha256"]).strip()
                    if row.get("ordered_content_sha256") is not None
                    else None
                ),
            },
            "calendar_id": row.get("calendar_id"),
            "cost_confidence": row["cost_confidence"],
            "blocker_ids": row.get("blocker_ids") or [],
            "decision_required_ids": row.get("decision_required_ids") or [],
            "last_good_receipt_id": row.get("last_good_receipt_id"),
        }
        for row in rows
    ]


def strategy_external_receipts(
    reader: QueryReader,
    *,
    dataset_role: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if dataset_role is not None and dataset_role not in STRATEGY_EXTERNAL_ROLES:
        raise ValueError("unknown dataset role")
    return reader.query(
        """
        SELECT receipt_id, edc_id, dataset_role, contract_id,
               availability_state, dataset_id, provider_id,
               provider_data_version, lineage_id, manifest_sha256,
               ordered_content_sha256, calendar_id, source_as_of,
               source_observed_at_utc, available_at_utc,
               accepted_at_utc, expected_by_utc, published_at_utc,
               freshness_state, quality_state, revision_state,
               cost_confidence, warning_ids, blocker_ids,
               values_modified, interpolation_performed, payload,
               receipt_sha256, supersedes_receipt_id, created_at_utc
        FROM analytics.v_strategy_external_data_receipt
        WHERE (%s::TEXT IS NULL OR dataset_role=%s)
        ORDER BY created_at_utc DESC, receipt_id DESC
        LIMIT %s
        """,
        (dataset_role, dataset_role, limit),
    )


def strategy_calendar_payload(
    reader: QueryReader,
    *,
    calendar_id: str,
    start: date,
    end: date,
    limit: int,
) -> dict[str, Any]:
    if not calendar_id or len(calendar_id) > 128 or start >= end:
        raise ValueError("invalid calendar query")
    receipt_rows = reader.query(
        """
        SELECT receipt_id,availability_state,provider_id,
               provider_data_version,lineage_id,ordered_content_sha256,
               source_observed_at_utc,accepted_at_utc,warning_ids,
               blocker_ids,payload
        FROM analytics.v_strategy_external_data_receipt
        WHERE dataset_role='COMMON_REGULAR_SESSION_CALENDAR'
          AND calendar_id=%s
          AND availability_state IN ('AVAILABLE','AVAILABLE_WITH_WARNINGS')
          AND accepted_at_utc IS NOT NULL
        ORDER BY created_at_utc DESC,receipt_id DESC
        LIMIT 1
        """,
        (calendar_id,),
    )
    if len(receipt_rows) != 1:
        raise StrategyExternalReadError("CALENDAR_NOT_FOUND", 404)
    contract = public_strategy_external_contract()
    calendar_contract = next(
        item
        for item in contract["contracts"]
        if item["dataset_role"] == "COMMON_REGULAR_SESSION_CALENDAR"
    )
    accepted = receipt_rows[0]
    payload = accepted.get("payload") or {}
    raw_sessions = payload.get("sessions") or []
    try:
        selected_sessions = [
            row for row in raw_sessions
            if isinstance(row, dict)
            and isinstance(row.get("session_date"), str)
            and start <= date.fromisoformat(row["session_date"]) < end
        ]
    except ValueError as exc:
        raise StrategyExternalReadError("CALENDAR_RECEIPT_INVALID", 503) from exc
    truncated = len(selected_sessions) > limit
    sessions = selected_sessions[:limit]
    metadata = {
        key: payload.get(key)
        for key in (
            "calendar_id", "calendar_version", "tzdb_version",
            "published_at_utc", "valid_from", "valid_to", "source_urls",
            "normalized_sha256", "normalization", "source_sha256",
        )
    }
    metadata.update(
        {
            "provider": accepted.get("provider_id"),
            "provider_data_version": accepted.get("provider_data_version"),
            "lineage_id": accepted.get("lineage_id"),
            "receipt_id": accepted.get("receipt_id"),
            "accepted_at_utc": accepted.get("accepted_at_utc"),
            "source_observed_at_utc": accepted.get("source_observed_at_utc"),
        }
    )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "dataset_role": "COMMON_REGULAR_SESSION_CALENDAR",
        "contract_id": calendar_contract["contract_id"],
        "contract_state": calendar_contract["contract_state"],
        "availability_state": accepted["availability_state"],
        "blocker_ids": accepted.get("blocker_ids") or [],
        "warning_ids": accepted.get("warning_ids") or [],
        "decision_required_ids": calendar_contract["decision_required_ids"],
        "common_calendar_verified": True,
        "evidence_only": False,
        "calendar": metadata,
        "start": start,
        "end": end,
        "row_count": len(sessions),
        "truncated": truncated,
        "sessions": sessions,
    }


def _same_utc(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    left_value = left if isinstance(left, datetime) else parse_utc(str(left), "timestamp")
    right_value = right if isinstance(right, datetime) else parse_utc(str(right), "timestamp")
    return left_value.astimezone(timezone.utc) == right_value.astimezone(timezone.utc)


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


def c2_daily_close_status_payload(
    reader: QueryReader,
    *,
    scheduler_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize ETF11 accepted daily closes against completed XNYS sessions."""

    rows = reader.query(
        """
        WITH latest AS (
            SELECT b.instrument_id,b.session_date,b.price_basis,TRUE AS is_complete,
                   CASE WHEN b.derivation_status='PASS' THEN 'PASS' ELSE 'WARN' END
                       AS quality_status,
                   b.close,b.source_last_ingestion_run_id,b.imputed_bar_count,
                   b.latest_imputed_session_date,
                   b.derivation_status AS imputation_status,b.warning_ids
            FROM analytics.v_c2_daily_close_status_latest b
            WHERE b.instrument_key=ANY(%s)
              AND b.derivation_status IN ('PASS','PASS_WITH_IMPUTATION_WARNING')
        )
        SELECT lower(i.market_key) AS instrument_key,i.symbol,i.category,
               l.session_date AS latest_session_date,
               l.price_basis,l.is_complete,l.quality_status,
               l.close,l.source_last_ingestion_run_id,
               COALESCE(l.imputed_bar_count,0) AS imputed_bar_count,
               l.latest_imputed_session_date,
               l.imputation_status,COALESCE(l.warning_ids,ARRAY[]::TEXT[]) AS warning_ids,
               f.latest_expected_complete_time_utc::DATE AS expected_session_date,
               f.latest_expected_complete_time_utc,
               f.next_expected_time_utc::DATE AS next_session_date,
               f.next_expected_time_utc,
               r.availability_status AS revision_availability_status,
               r.reconciliation_status AS revision_status,
               r.review_status AS revision_review_status,
               r.reason_code AS revision_reason_code,
               r.new_data_version AS observed_provider_data_version,
               r.last_accepted_data_version
        FROM catalog.instrument i
        LEFT JOIN latest l USING (instrument_id)
        LEFT JOIN analytics.v_data_freshness f
          ON f.instrument_id=i.instrument_id AND f.horizon_minutes=60
         AND f.price_basis='native_ohlc'
        LEFT JOIN ops.v_series_revision_availability r
          ON r.instrument_key=i.market_key AND r.horizon_minutes=60
         AND r.price_basis='native_ohlc'
        WHERE lower(i.market_key)=ANY(%s)
          AND i.active_to_utc IS NULL
        ORDER BY array_position(%s::TEXT[],lower(i.market_key))
        """,
        (list(C2_DAILY_CLOSE_KEYS), list(C2_DAILY_CLOSE_KEYS), list(C2_DAILY_CLOSE_KEYS)),
    )
    series: list[dict[str, Any]] = []
    for row in rows:
        latest = row.get("latest_session_date")
        expected = row.get("expected_session_date")
        if latest is None or expected is None:
            freshness = "NOT_EVALUATED"
        elif latest >= expected:
            freshness = "PASS"
        else:
            freshness = "STALE"
        series.append(
            {
                **row,
                "layer": "1d",
                "freshness_status": freshness,
                "update_status": (
                    "REVIEW_PENDING_DATA_NOT_ADVANCED"
                    if row.get("revision_availability_status")
                    == "AVAILABLE_WITH_REVISION_WARNING"
                    else
                    "CURRENT" if freshness == "PASS"
                    else "UPDATE_REQUIRED" if freshness == "STALE"
                    else "NOT_EVALUATED"
                ),
                "availability_status": (
                    row.get("revision_availability_status")
                    if row.get("revision_availability_status")
                    in {"AVAILABLE_WITH_REVISION_WARNING", "AVAILABLE_WITH_WARNINGS"}
                    else "AVAILABLE" if latest is not None
                    else "BLOCKED_DATA_NOT_AVAILABLE"
                ),
            }
        )
    scheduler = dict(scheduler_status or {})
    lifecycle_status = str(scheduler.get("status") or "NOT_EVALUATED")
    scheduler_runtime_status = str(
        (scheduler.get("scheduler") or {}).get("service_status")
        or ("RUNNING" if lifecycle_status == "PASS" else lifecycle_status)
    )
    stale_count = sum(row["freshness_status"] == "STALE" for row in series)
    missing_count = sum(row["latest_session_date"] is None for row in series)
    current_count = sum(row["freshness_status"] == "PASS" for row in series)
    revision_warning_count = sum(
        row.get("revision_availability_status") == "AVAILABLE_WITH_REVISION_WARNING"
        for row in series
    )
    imputation_warning_count = sum(
        int(row.get("imputed_bar_count") or 0) > 0 for row in series
    )
    next_expected = next(
        (row.get("next_expected_time_utc") for row in series if row.get("next_expected_time_utc")),
        None,
    )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": datetime.now(timezone.utc),
        "contract_id": "c2_etf11_native_ohlc_daily_close_v1",
        "purpose": "C2_LOW_FREQUENCY_PAPER_REFERENCE_PRICE",
        "price_semantics": {
            "price_basis": "native_ohlc",
            "value": "completed regular-session derived daily close",
            "not_total_return": True,
            "not_official_primary_exchange_close": True,
            "not_execution_price": True,
            "realtime_or_bid_ask_required": False,
            "imputed_hourly_rows_may_exist": True,
            "daily_close_must_be_actual_provider_row": True,
        },
        "freshness_policy": {
            "calendar": "XNYS_US_EQUITY",
            "expected_after_session_close_minutes": 45,
            "weekend_or_holiday_wait_is_blocking": False,
        },
        "state": {
            "status": (
                "AVAILABLE_WITH_IMPUTATION_WARNING"
                if len(series) == len(C2_DAILY_CLOSE_KEYS) and not stale_count
                and not missing_count and imputation_warning_count
                else "PASS" if len(series) == len(C2_DAILY_CLOSE_KEYS) and not stale_count and not missing_count
                else "AVAILABLE_WITH_FRESHNESS_WARNING" if series and not missing_count
                else "BLOCKED_DATA_NOT_AVAILABLE"
            ),
            "series_count": len(series),
            "current_count": current_count,
            "stale_count": stale_count,
            "missing_count": missing_count,
            "revision_warning_count": revision_warning_count,
            "imputation_warning_count": imputation_warning_count,
            "scheduler_status": scheduler_runtime_status,
            "scheduler_lifecycle_status": lifecycle_status,
            "interface_blockers": (
                [] if lifecycle_status == "PASS"
                else [lifecycle_status] if lifecycle_status not in {"NOT_EVALUATED", "STOPPED"}
                else []
            ),
            "operational_warnings": (
                [scheduler_runtime_status]
                if scheduler_runtime_status == "RUNNING_DEGRADED" else []
            ),
            "market_wait_status": (
                "NEXT_SESSION_WAIT_NON_BLOCKING" if not stale_count and not missing_count
                else "REVISION_REVIEW_PENDING" if revision_warning_count
                else "UPDATE_REQUIRED"
            ),
            "next_expected_bar_time_utc": next_expected,
            "orders_or_prechecks_sent": 0,
            "write_requests_to_saxo": 0,
        },
        "strategy_endpoint": "/api/v1/c2/daily-close-status",
        "hourly_overlay_endpoint": (
            "/api/v1/c2/hourly-overlay?instrument_key=<etf>&"
            "start=<UTC>&end=<UTC>&limit=<N>"
        ),
        "series": series,
        "read_only": True,
    }


def c2_hourly_overlay_payload(
    reader: QueryReader,
    *,
    instrument_key: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> dict[str, Any]:
    """Return the explicit actual-plus-imputed C2 overlay, never canonical bars."""

    key = instrument_key.strip().lower()
    if key not in C2_DAILY_CLOSE_KEYS or start >= end:
        raise ValueError("invalid C2 overlay request")
    rows = reader.query(
        """
        SELECT instrument_key,time_utc,price_basis,open,high,low,close,volume,
               is_complete,source_kind,quality_status,warning_ids,
               imputation_reason,source_time_utc,consecutive_gap_index,
               consecutive_gap_count,candidate_data_version,source_data_version,
               source_ingestion_run_id,source_payload_sha256,
               source_artifact_relative_path,imputation_policy_id,
               official_close_claim,total_return_claim,execution_price_claim
        FROM analytics.v_c2_market_bar_1h_overlay
        WHERE instrument_key=%s AND time_utc >= %s AND time_utc < %s
        ORDER BY time_utc
        LIMIT %s
        """,
        (key, start, end, limit + 1),
    )
    truncated = len(rows) > limit
    selected = rows[:limit]
    imputed_count = sum(row.get("source_kind") == "IMPUTED_PREVIOUS_VALID" for row in selected)
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": datetime.now(timezone.utc),
        "contract_id": "c2_etf11_bounded_hourly_overlay_v1",
        "instrument_key": key,
        "layer": "1h",
        "price_basis": "native_ohlc",
        "availability_status": (
            "AVAILABLE_WITH_IMPUTATION_WARNING" if imputed_count else "AVAILABLE"
        ),
        "warning_ids": (
            ["C2_BOUNDED_IMPUTED_PREVIOUS_VALID"] if imputed_count else []
        ),
        "imputed_row_count": imputed_count,
        "row_count": len(selected),
        "truncated": truncated,
        "claims": {
            "official_close": False,
            "total_return": False,
            "execution_price": False,
        },
        "rows": selected,
        "read_only": True,
    }


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
    (
        context_rows,
        identity_rows,
        coverage_rows,
        freshness_rows,
        events,
        run_rows,
        high_rows,
        publication_rows,
        revision_rows,
    ) = result_sets
    if not identity_rows:
        return None
    if len(identity_rows) != 1:
        raise RuntimeError("series identity is ambiguous")

    context = context_rows[0]
    series = identity_rows[0]
    coverage = coverage_rows[0] if coverage_rows else None
    freshness = freshness_rows[0] if freshness_rows else None
    latest_run = run_rows[0] if run_rows else None
    publication = publication_rows[0] if publication_rows else None
    revision = revision_rows[0] if revision_rows else None
    if (
        revision is None
        and freshness is not None
        and str(freshness.get("data_status")) == "STALE_DATA_VERSION"
    ):
        revision = {
            "reconciliation_status": "DETECTED_LEGACY",
            "availability_status": "BLOCKED",
            "old_data_version": freshness.get("data_version"),
            "new_data_version": None,
            "reason_code": "REVISION_EVENT_PREDATES_BOUNDED_AUDIT_SCHEMA",
        }
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
    quality_status = "PASS"
    if current_blockers or (
        publication is not None and publication.get("quality_status") == "FAIL"
    ):
        quality_status = "FAIL"
    elif publication is not None and publication.get("quality_status") == "WARN":
        quality_status = "WARN"
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
    if publication is not None and publication.get("publication_status") in {
        "CANDIDATE", "BLOCKED"
    }:
        reasons.append(f"PUBLICATION_{publication.get('publication_status') or 'UNKNOWN'}")
        blocker_code = publication.get("blocker_code")
        if blocker_code:
            reasons.append(str(blocker_code))
    elif publication is not None and publication.get("publication_status") == "STAGING":
        warnings.append("PUBLICATION_STAGING")
    if (
        publication is not None
        and publication.get("consumer_availability_status")
        == "AVAILABLE_WITH_WARNINGS"
    ):
        warnings.append("USER_APPROVED_RESEARCH_WARNING_POLICY")
        warning_metadata = publication.get("warning_metadata_json") or {}
        if warning_metadata.get("known_provider_anomaly"):
            warnings.append("KNOWN_PROVIDER_ANOMALY_VALUES_UNMODIFIED")
        elif warning_metadata.get("observed_quarantined_extrema"):
            warnings.append("PROVIDER_EXTREMA_ANOMALIES_EXCLUDED_UNMODIFIED")
        if publication.get("effective_coverage_start_utc") != publication.get(
            "provider_advertised_start_utc"
        ):
            warnings.append("EFFECTIVE_COVERAGE_START_LIMITATION")
    if revision is not None:
        revision_status = str(revision.get("reconciliation_status") or "UNKNOWN")
        availability = str(revision.get("availability_status") or "BLOCKED")
        if availability == "AVAILABLE_WITH_REVISION_WARNING":
            warnings.append("REVISION_REVIEW_PENDING")
            revision = {
                **revision,
                "last_accepted_data_version": (
                    freshness.get("data_version") if freshness is not None else None
                ),
                "last_accepted_complete_time_utc": (
                    freshness.get("latest_complete_time_utc")
                    if freshness is not None else None
                ),
                "last_accepted_ingestion_run_id": (
                    freshness.get("last_ingestion_run_id")
                    if freshness is not None else None
                ),
                "provider_evidence_curated": False,
                "review_pending": str(revision.get("review_status")) == "PENDING_REVIEW",
            }
        elif availability != "AVAILABLE":
            reasons.append(f"REVISION_{revision_status}")

    high_watermark = (
        int(high_rows[0].get("quality_event_high_watermark") or 0) if high_rows else 0
    )
    coverage_assessment = None
    if (
        str(series.get("asset_type")) == "FxSpot"
        and coverage is not None
        and str(coverage.get("coverage_status")) == "WARN"
    ):
        if publication is not None and publication.get("evidence_manifest_relative_path"):
            coverage_assessment = {
                "manifest": publication.get("evidence_manifest_relative_path"),
                "manifest_sha256": publication.get("evidence_manifest_sha256"),
                "warning_acceptance_reason": (
                    "candidate full-history gaps are classified by retained raw/run evidence"
                ),
                "interpolation_performed": False,
                "blocks_freshness_or_current_quality": False,
                "provider_advertised_start_utc": publication.get(
                    "provider_advertised_start_utc"
                ),
                "effective_coverage_start_utc": publication.get(
                    "effective_coverage_start_utc"
                ),
                "limitation": publication.get("coverage_limitation"),
                "research_policy_id": publication.get("research_policy_id"),
            }
        elif instrument_key.lower() in {"eurusd", "usdjpy"}:
            coverage_assessment = {
                "manifest": "manifests/fx_gap_classification/fx_gap_classification_manifest.json",
                "classification_report": "manifests/fx_gap_classification/fx_gap_classification.json",
                "summary_report": "manifests/fx_gap_classification/fx_gap_classification_summary.md",
                "warning_acceptance_reason": (
                    "historical missing slots are individually classified; source gaps and resolved "
                    "quarantine remain visible and are not converted into prices"
                ),
                "interpolation_performed": False,
                "blocks_freshness_or_current_quality": False,
            }
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
            "availability_status": (
                revision.get("availability_status")
                if revision is not None
                else publication.get("consumer_availability_status")
                if publication is not None
                else "AVAILABLE"
            ),
            "revision_review_status": (
                revision.get("review_status") if revision is not None else None
            ),
            "freshness_basis": "LAST_ACCEPTED_CURATED",
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
            "coverage_assessment": coverage_assessment,
            "freshness": freshness,
            "latest_ingestion_run": latest_run,
            "publication": publication,
            "revision": revision,
        },
    }


def revision_service_status_payload(reader: QueryReader) -> dict[str, Any]:
    rows = reader.query(
        """
        SELECT instrument_key,data_status,availability_status,
               reconciliation_status,reason_code,policy_id,review_status,
               last_accepted_data_version,last_accepted_complete_time_utc,
               last_accepted_ingestion_run_id,latest_evidence_at_utc,
               latest_provider_observed_time_utc,evidence_sample_count
        FROM ops.v_series_revision_availability
        WHERE horizon_minutes=60
        ORDER BY instrument_key
        """
    )
    warning_availability = {
        "AVAILABLE_WITH_REVISION_WARNING",
        "AVAILABLE_WITH_WARNINGS",
    }
    warning_rows = [
        row for row in rows if row.get("availability_status") in warning_availability
    ]
    degraded = [
        row
        for row in rows
        if row.get("availability_status") not in {"AVAILABLE", *warning_availability}
        or (
            row.get("data_status") != "ACTIVE"
            and row.get("availability_status") not in warning_availability
        )
    ]
    service_status = (
        "PASS"
        if not degraded
        else "BLOCKED"
        if rows and len(degraded) == len(rows)
        else "PARTIALLY_DEGRADED"
    )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": datetime.now(timezone.utc),
        "service_status": service_status,
        "managed_series_count": len(rows),
        "available_series_count": len(rows) - len(degraded),
        "warning_series_count": len(warning_rows),
        "warning_series": warning_rows,
        "degraded_series_count": len(degraded),
        "degraded_series": degraded,
        "all_series_stopped": bool(rows) and len(degraded) == len(rows),
        "read_only": True,
    }


def candidate_publication_state(
    reader: QueryReader, instrument_key: str
) -> dict[str, Any] | None:
    if instrument_key not in {"audusd", "usdcad", "usdchf"}:
        return None
    rows = reader.query(
        """
        SELECT p.publication_status,p.blocker_code,p.quality_status,
               p.coverage_status,p.freshness_status,p.last_accepted_complete_time_utc,
               p.consecutive_normal_passes,p.consumer_availability_status,
               p.research_policy_id,p.provider_advertised_start_utc,
               p.effective_coverage_start_utc,p.coverage_limitation,
               p.warning_metadata_json,p.policy_approved_at_utc,p.policy_approved_by
        FROM catalog.series_publication_state p
        JOIN catalog.instrument i USING (instrument_id)
        WHERE i.market_key=%s AND p.horizon_minutes=60
          AND p.price_basis='bid_ask_mid'
        """,
        (instrument_key,),
    )
    if len(rows) != 1:
        return {
            "publication_status": "BLOCKED",
            "blocker_code": "BLOCKED_PUBLICATION_STATE_MISSING",
        }
    return rows[0]


def snapshot_bars_payload(
    reader: QueryReader,
    *,
    snapshot_id: int,
    instrument_key: str,
    layer: str,
    price_basis: str,
    start: datetime,
    end: datetime,
    limit: int,
    manifest_loader: SnapshotManifestLoader = load_snapshot_manifest,
    cursor_payload: Mapping[str, Any] | None = None,
    cursor_codec: CursorCodec | None = None,
) -> dict[str, Any]:
    if snapshot_id < 1:
        raise ValueError("snapshot_id must be positive")
    if not instrument_key or len(instrument_key) > 64:
        raise ValueError("instrument_key is required")
    if layer in {"4h", "1d"}:
        raise SnapshotReadError("SNAPSHOT_LAYER_NOT_AVAILABLE", 409)
    if layer != "1h":
        raise ValueError("layer must be 1h")
    if not price_basis or len(price_basis) > 64:
        raise ValueError("price_basis is required")
    if start >= end:
        raise ValueError("start must be earlier than end")

    after_time: datetime | None = None
    after_instrument_id: int | None = None
    after_price_basis: str | None = None
    if cursor_payload is not None:
        expected_query = {
            "snapshot_id": snapshot_id,
            "instrument_key": instrument_key,
            "layer": layer,
            "price_basis": price_basis,
            "start": json_value(start),
            "end": json_value(end),
            "limit": limit,
        }
        if (
            cursor_payload.get("kind") != "snapshot-bars"
            or cursor_payload.get("query") != expected_query
        ):
            raise CursorError("CURSOR_QUERY_MISMATCH", 409)
        last = cursor_payload.get("last")
        if not isinstance(last, dict):
            raise CursorError("CURSOR_INVALID", 400)
        try:
            after_time = parse_utc(str(last["time_utc"]), "cursor.time_utc")
            after_instrument_id = int(last["instrument_id"])
            after_price_basis = str(last["price_basis"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorError("CURSOR_INVALID", 400) from exc

    result_sets = reader.query_atomic(
        (
            (SNAPSHOT_CONTEXT_QUERY, ()),
            (SNAPSHOT_METADATA_QUERY, (snapshot_id,)),
            (
                SNAPSHOT_SERIES_QUERY,
                (price_basis, instrument_key, price_basis),
            ),
            (SNAPSHOT_INTEGRITY_QUERY, (snapshot_id,)),
            (
                SNAPSHOT_BARS_QUERY,
                (
                    instrument_key, price_basis, start, end, snapshot_id,
                    after_time, after_time, after_instrument_id,
                    after_price_basis, limit + 1,
                ),
            ),
        )
    )
    if len(result_sets) != 5:
        raise SnapshotReadError("SNAPSHOT_INTEGRITY_FAILED", 503)
    context_rows, metadata_rows, identity_rows, integrity_rows, rows = result_sets
    if not metadata_rows:
        raise SnapshotReadError("SNAPSHOT_NOT_FOUND", 404)
    if len(metadata_rows) != 1 or len(context_rows) != 1 or len(integrity_rows) != 1:
        raise SnapshotReadError("SNAPSHOT_INTEGRITY_FAILED", 503)
    if not identity_rows:
        raise SnapshotReadError("SNAPSHOT_SERIES_NOT_FOUND", 404)
    if len(identity_rows) != 1:
        raise SnapshotReadError("SNAPSHOT_INTEGRITY_FAILED", 503)

    context = context_rows[0]
    metadata = metadata_rows[0]
    series = identity_rows[0]
    integrity = integrity_rows[0]
    relative_path = str(metadata.get("snapshot_manifest_relative_path") or "")
    manifest, manifest_sha256 = manifest_loader(relative_path)
    snapshot_sha256 = str(metadata.get("snapshot_sha256") or "").strip()
    source_manifest_sha256 = str(metadata.get("source_manifest_sha256") or "").strip()
    expected_counts = manifest.get("table_counts_before_snapshot_registry_row")
    raw_manifest_boundaries = manifest.get("boundaries")
    manifest_boundaries = (
        raw_manifest_boundaries if isinstance(raw_manifest_boundaries, dict) else {}
    )
    expected_bar_rows = (
        expected_counts.get("curated.market_bar")
        if isinstance(expected_counts, dict)
        else None
    )
    cutoff_utc = metadata.get("cutoff_utc")
    actual_max_time = integrity.get("curated_max_time_utc")
    integrity_pass = (
        context.get("database_name") == RESEARCH_DB
        and context.get("role_name") == "v13_research_reader"
        and context.get("transaction_read_only") == "on"
        and metadata.get("snapshot_id") == snapshot_id
        and metadata.get("status") == "FROZEN"
        and metadata.get("source_database") == MARKET_DB
        and snapshot_sha256 == manifest_sha256
        and len(snapshot_sha256) == 64
        and manifest.get("phase") == "DB2"
        and manifest.get("snapshot_database") == RESEARCH_DB
        and manifest.get("source_database") == metadata.get("source_database")
        and manifest.get("plan_id") == metadata.get("plan_id")
        and manifest.get("research_line_id") == metadata.get("research_line_id")
        and manifest.get("source_inventory_sha256") == source_manifest_sha256
        and manifest.get("FDW_or_dblink_used") is False
        and _same_utc(manifest.get("cutoff_utc"), cutoff_utc)
        and metadata.get("row_counts_json") == expected_counts
        and isinstance(expected_bar_rows, int)
        and integrity.get("curated_market_bar_rows") == expected_bar_rows
        and _same_utc(actual_max_time, manifest_boundaries.get("curated_max_time_utc"))
        and actual_max_time is not None
        and cutoff_utc is not None
        and actual_max_time <= cutoff_utc
        and integrity.get("post_cutoff_rows") == 0
    )
    if not integrity_pass:
        raise SnapshotReadError("SNAPSHOT_INTEGRITY_FAILED", 503)
    if cursor_payload is not None and cursor_payload.get("snapshot_sha256") != snapshot_sha256:
        raise CursorError("CURSOR_EXPIRED", 409)

    truncated = len(rows) > limit
    selected = rows[:limit]
    next_cursor = None
    if truncated and selected and cursor_codec is not None:
        last_row = selected[-1]
        next_cursor = cursor_codec.encode(
            {
                "version": 1,
                "kind": "snapshot-bars",
                "query": {
                    "snapshot_id": snapshot_id,
                    "instrument_key": instrument_key,
                    "layer": layer,
                    "price_basis": price_basis,
                    "start": json_value(start),
                    "end": json_value(end),
                    "limit": limit,
                },
                "snapshot_sha256": snapshot_sha256,
                "last": {
                    "time_utc": json_value(last_row["time_utc"]),
                    "instrument_id": last_row["instrument_id"],
                    "price_basis": last_row["price_basis"],
                },
            }
        )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": context.get("read_at_utc"),
        "snapshot": {
            "requested_snapshot_id": snapshot_id,
            "resolved_snapshot_id": metadata.get("snapshot_id"),
            "snapshot_sha256": snapshot_sha256,
            "snapshot_manifest_relative_path": relative_path,
            "cutoff_utc": cutoff_utc,
            "source_database": metadata.get("source_database"),
            "snapshot_database": context.get("database_name"),
            "plan_id": metadata.get("plan_id"),
            "research_line_id": metadata.get("research_line_id"),
            "read_at_utc": context.get("read_at_utc"),
            "snapshot_marker": context.get("snapshot_marker"),
        },
        "query": {
            "instrument_key": instrument_key,
            "layer": layer,
            "price_basis": price_basis,
            "start": start,
            "end": end,
            "limit": limit,
        },
        "series": series,
        "integrity": {
            "status": "PASS",
            "manifest_sha256": manifest_sha256,
            "curated_market_bar_rows": integrity.get("curated_market_bar_rows"),
            "curated_max_time_utc": actual_max_time,
            "post_cutoff_rows": integrity.get("post_cutoff_rows"),
        },
        "row_count": len(selected),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "ordered_content_sha256": ordered_content_sha256(selected),
        "rows": selected,
    }


def total_return_status_payload(
    reader: QueryReader,
    *,
    instrument_key: str,
    research_contract_id: str | None,
) -> dict[str, Any]:
    if not instrument_key or len(instrument_key) > 64:
        raise ValueError("instrument_key is required")
    contract = contract_for_request(research_contract_id, instrument_key)
    if contract["usage_mode"] == "full_history_research":
        return _full_history_total_return_status_payload(
            reader, instrument_key=instrument_key, contract=contract
        )
    ticker = instrument_key.upper()
    result_sets = reader.query_atomic(
        (
            (TOTAL_RETURN_CONTEXT_QUERY, ()),
            (
                TOTAL_RETURN_STATUS_QUERY,
                (
                    instrument_key,
                    contract["source_dataset_id"],
                    ticker,
                ),
            ),
        )
    )
    if len(result_sets) != 2 or len(result_sets[0]) != 1:
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    context = result_sets[0][0]
    if (
        context.get("database_name") != MARKET_DB
        or context.get("role_name") != "saxo_app_reader"
        or context.get("transaction_read_only") != "on"
    ):
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    if len(result_sets[1]) != 1:
        raise TotalReturnReadError("TOTAL_RETURN_MAPPING_NOT_FOUND", 404)
    row = result_sets[1][0]

    expected_window = contract["window"]
    expected_identity = contract["identity"]
    blockers: list[str] = []
    if int(row.get("mapping_count") or 0) != 1:
        blockers.append("INSTRUMENT_MAPPING_MISMATCH")
    if row.get("external_series_key") != ticker or row.get("instrument_key") != instrument_key:
        blockers.append("INSTRUMENT_IDENTITY_MISMATCH")
    if row.get("dataset_kind") != "total_return" or row.get("price_basis") != "etf_total_return":
        blockers.append("DATASET_OR_PRICE_BASIS_MISMATCH")
    if int(row.get("canonical_horizon_minutes") or 0) != 1440:
        blockers.append("HORIZON_MISMATCH")
    if row.get("provider") != contract["catalog_provider"]:
        blockers.append("PROVIDER_IDENTITY_MISMATCH")
    if row.get("source_manifest_relative_path") != expected_identity["source_manifest_relative_path"]:
        blockers.append("SOURCE_MANIFEST_PATH_MISMATCH")
    if str(row.get("source_manifest_sha256") or "").strip() != expected_identity["source_manifest_sha256"]:
        blockers.append("SOURCE_MANIFEST_SHA256_MISMATCH")
    if int(row.get("row_count") or 0) != int(expected_window["rows_per_instrument"]):
        blockers.append("LOCKED_WINDOW_ROW_COUNT_MISMATCH")
    if json_value(row.get("min_session_date")) != expected_window["first_session_date"]:
        blockers.append("LOCKED_WINDOW_START_MISMATCH")
    if json_value(row.get("max_session_date")) != expected_window["last_session_date"]:
        blockers.append("LOCKED_WINDOW_END_MISMATCH")
    if int(row.get("duplicate_count") or 0) != 0:
        blockers.append("DUPLICATE_SESSION_DATE")
    if int(row.get("null_or_nonpositive_count") or 0) != 0:
        blockers.append("NULL_OR_NONPOSITIVE_TOTAL_RETURN")
    if int(row.get("quality_fail_count") or 0) != 0:
        blockers.append("QUALITY_FAIL_ROWS_PRESENT")
    if int(row.get("quality_not_evaluated_count") or 0) != 0:
        blockers.append("QUALITY_NOT_EVALUATED_ROWS_PRESENT")
    if int(row.get("missing_source_file_count") or 0) != 0:
        blockers.append("RAW_LINEAGE_MISSING")
    if int(row.get("source_dataset_lineage_mismatch_count") or 0) != 0:
        blockers.append("RAW_LINEAGE_DATASET_MISMATCH")
    source_file_hashes = sorted(str(value).strip() for value in (row.get("source_file_sha256_values") or []))
    if source_file_hashes != [expected_identity["normalized_csv_sha256"]]:
        blockers.append("NORMALIZED_CONTENT_SHA256_MISMATCH")

    instrument_contract = contract["instruments"][ticker]
    warn_rows = int(row.get("quality_warn_count") or 0)
    approved_warnings = list(instrument_contract["approved_warning_codes"])
    expected_warn_rows = int(
        (instrument_contract.get("warning_evidence") or {}).get("quality_warn_rows") or 0
    )
    if warn_rows != expected_warn_rows:
        blockers.append("PROVIDER_WARNING_COUNT_MISMATCH")
    if warn_rows and not approved_warnings:
        blockers.append("UNREVIEWED_PROVIDER_CONTENT_ANOMALY")
    warnings = approved_warnings if not blockers else []
    warnings.append("CATALOG_RESEARCH_ELIGIBILITY_LABEL_NOT_A_FIXED_WINDOW_GATE")
    warnings.append("FRESHNESS_BEYOND_LOCKED_WINDOW_NOT_REQUIRED")

    availability = "BLOCKED" if blockers else instrument_contract["availability"]
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": context["read_at_utc"],
        "consistency": {
            "snapshot_marker": context["snapshot_marker"],
            "transaction_read_only": context["transaction_read_only"],
        },
        "research_contract": public_contract(contract),
        "series": {
            "instrument_id": row.get("instrument_id"),
            "instrument_key": instrument_key,
            "external_series_key": ticker,
            "source_dataset_id": contract["source_dataset_id"],
            "provider": contract["series_provider"],
            "price_basis": "etf_total_return",
            "horizon_minutes": 1440,
        },
        "state": {
            "availability_status": availability,
            "quality_status": "BLOCKED" if blockers else (
                "PASS_WITH_WARNINGS" if instrument_contract["availability"] == "AVAILABLE_WITH_WARNINGS" else "PASS"
            ),
            "coverage_status": "BLOCKED" if blockers else "PASS_LOCKED_WINDOW",
            "freshness_status": "NOT_APPLICABLE_FIXED_WINDOW",
            "current_blockers": blockers,
            "warnings": warnings,
        },
        "evidence": {
            "row_count": row.get("row_count"),
            "first_session_date": row.get("min_session_date"),
            "last_session_date": row.get("max_session_date"),
            "duplicate_count": row.get("duplicate_count"),
            "null_or_nonpositive_count": row.get("null_or_nonpositive_count"),
            "quality_fail_count": row.get("quality_fail_count"),
            "quality_not_evaluated_count": row.get("quality_not_evaluated_count"),
            "quality_warn_count": warn_rows,
            "source_file_count": row.get("source_file_count"),
            "source_file_sha256_values": source_file_hashes,
            "source_manifest_sha256": expected_identity["source_manifest_sha256"],
            "normalized_content_sha256": expected_identity["normalized_csv_sha256"],
            "ordered_time_status": "PASS_PRIMARY_KEY_AND_ASCENDING_API_ORDER",
            "provider_data_version": "NOT_APPLICABLE_NON_SAXO_TOTAL_RETURN_SOURCE",
        },
        "non_blocking_metadata": {
            "legacy_or_current_namespace": row.get("is_current"),
            "catalog_research_eligibility": row.get("research_eligibility"),
            "publication_timestamp": "NOT_A_FIXED_WINDOW_GATE",
        },
    }


def _full_history_context(
    reader: QueryReader,
    *,
    instrument_key: str,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    ticker = instrument_key.upper()
    result_sets = reader.query_atomic(
        (
            (TOTAL_RETURN_CONTEXT_QUERY, ()),
            (
                TOTAL_RETURN_STATUS_QUERY,
                (instrument_key, contract["source_dataset_id"], ticker),
            ),
        )
    )
    if len(result_sets) != 2 or len(result_sets[0]) != 1:
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    context = result_sets[0][0]
    if (
        context.get("database_name") != MARKET_DB
        or context.get("role_name") != "saxo_app_reader"
        or context.get("transaction_read_only") != "on"
    ):
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    if len(result_sets[1]) != 1:
        raise TotalReturnReadError("TOTAL_RETURN_MAPPING_NOT_FOUND", 404)
    row = result_sets[1][0]
    blockers: list[str] = []
    if int(row.get("mapping_count") or 0) != 1:
        blockers.append("INSTRUMENT_MAPPING_MISMATCH")
    if row.get("external_series_key") != ticker or row.get("instrument_key") != instrument_key:
        blockers.append("INSTRUMENT_IDENTITY_MISMATCH")
    if row.get("dataset_kind") != "total_return" or row.get("price_basis") != "etf_total_return":
        blockers.append("DATASET_OR_PRICE_BASIS_MISMATCH")
    if int(row.get("canonical_horizon_minutes") or 0) != 1440:
        blockers.append("HORIZON_MISMATCH")
    if row.get("provider") != contract["catalog_provider"]:
        blockers.append("PROVIDER_IDENTITY_MISMATCH")
    identity = contract["identity"]
    if row.get("source_manifest_relative_path") != identity["source_manifest_relative_path"]:
        blockers.append("SOURCE_MANIFEST_PATH_MISMATCH")
    if str(row.get("source_manifest_sha256") or "").strip() != identity["source_manifest_sha256"]:
        blockers.append("SOURCE_MANIFEST_SHA256_MISMATCH")
    try:
        history = load_full_history_series(contract, ticker)
    except TotalReturnContractError as exc:
        raise TotalReturnReadError(str(exc), 503) from exc
    return context, row, history, blockers


def _full_history_total_return_status_payload(
    reader: QueryReader,
    *,
    instrument_key: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    context, row, history, blockers = _full_history_context(
        reader, instrument_key=instrument_key, contract=contract
    )
    ticker = instrument_key.upper()
    instrument_contract = contract["instruments"][ticker]
    warnings = list(instrument_contract["approved_warning_codes"]) if not blockers else []
    warnings.extend(
        [
            "FROZEN_RESEARCH_SOURCE_FRESHNESS_NOT_REQUIRED",
            "STRATEGY_MANIFEST_OWNS_DATE_BOUNDARIES",
        ]
    )
    availability = "BLOCKED" if blockers else instrument_contract["availability"]
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": context["read_at_utc"],
        "consistency": {
            "snapshot_marker": context["snapshot_marker"],
            "transaction_read_only": context["transaction_read_only"],
            "state_revision": history["ordered_content_sha256"],
        },
        "research_contract": public_contract(contract),
        "series": {
            "instrument_id": row.get("instrument_id"),
            "instrument_key": instrument_key,
            "symbol": row.get("symbol"),
            "category": row.get("category"),
            "external_series_key": ticker,
            "source_dataset_id": contract["source_dataset_id"],
            "provider": contract["series_provider"],
            "dataset_name": row.get("dataset_name"),
            "price_basis": contract["price_basis"],
            "horizon_minutes": contract["horizon_minutes"],
        },
        "mapping": {
            "mapping_kind": row.get("mapping_kind"),
            "mapping_reason": row.get("mapping_reason"),
            "approved_at_utc": row.get("approved_at_utc"),
            "approved_by": row.get("approved_by"),
        },
        "state": {
            "availability_status": availability,
            "quality_status": "BLOCKED" if blockers else (
                "PASS_WITH_WARNINGS"
                if instrument_contract["availability"] == "AVAILABLE_WITH_WARNINGS"
                else "PASS"
            ),
            "coverage_status": "BLOCKED" if blockers else "PASS_FULL_AVAILABLE_HISTORY",
            "freshness_status": "NOT_APPLICABLE_FROZEN_RESEARCH_SOURCE",
            "current_blockers": blockers,
            "warnings": warnings,
        },
        "evidence": {
            "row_count": history["row_count"],
            "first_session_date": history["first_session_date"],
            "last_session_date": history["last_session_date"],
            "duplicate_count": history["duplicate_count"],
            "null_or_nonpositive_count": history["null_or_nonpositive_count"],
            "quality_fail_count": 0,
            "quality_not_evaluated_count": 0,
            "quality_warn_count": history["quality_warn_count"],
            "source_file_count": 1,
            "source_file_sha256_values": [history["source_file_sha256"]],
            "source_manifest_sha256": contract["identity"]["source_manifest_sha256"],
            "ordered_content_sha256": history["ordered_content_sha256"],
            "ordered_time_status": history["ordered_time_status"],
            "provider_data_version": "NOT_APPLICABLE_NON_SAXO_TOTAL_RETURN_SOURCE",
            "automatic_value_corrections": 0,
        },
        "non_blocking_metadata": {
            "legacy_or_current_namespace": row.get("is_current"),
            "catalog_research_eligibility": row.get("research_eligibility"),
            "publication_timestamp": "NOT_A_FULL_HISTORY_RESEARCH_GATE",
            "experiment_window": "SELECTED_BY_STRATEGY_MANIFEST",
        },
    }


def _full_history_total_return_payload(
    reader: QueryReader,
    *,
    instrument_key: str,
    start: datetime,
    end: datetime,
    source_dataset_id: str | None,
    limit: int,
    eligibility: str,
    research_contract_id: str | None,
    contract: Mapping[str, Any],
    cursor_payload: Mapping[str, Any] | None,
    cursor_codec: CursorCodec | None,
) -> dict[str, Any]:
    validate_requested_window(contract, start.date(), end.date())
    if source_dataset_id not in {None, contract["source_dataset_id"]}:
        raise ValueError("source_dataset_id does not match research contract")
    source_dataset_id = contract["source_dataset_id"]
    expected_query = {
        "instrument_key": instrument_key,
        "start": json_value(start),
        "end": json_value(end),
        "source_dataset_id": source_dataset_id,
        "limit": limit,
        "eligibility": eligibility,
        "usage_mode": "full_history_research",
        "research_contract_id": research_contract_id,
    }
    if cursor_payload is not None:
        if (
            cursor_payload.get("kind") != "total-return"
            or cursor_payload.get("query") != expected_query
            or cursor_payload.get("source_dataset_id") != source_dataset_id
        ):
            raise CursorError("CURSOR_QUERY_MISMATCH", 409)
        try:
            after_date = date.fromisoformat(str(cursor_payload["last_session_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorError("CURSOR_INVALID", 400) from exc
    else:
        after_date = None

    status = _full_history_total_return_status_payload(
        reader, instrument_key=instrument_key, contract=contract
    )
    if status["state"]["current_blockers"]:
        raise TotalReturnReadError("TOTAL_RETURN_CONTRACT_QUALITY_FAILED", 503)
    state_revision = status["consistency"]["state_revision"]
    if cursor_payload is not None and cursor_payload.get("state_revision") != state_revision:
        raise CursorError("CURSOR_EXPIRED", 409)
    try:
        history = load_full_history_series(contract, instrument_key)
    except TotalReturnContractError as exc:
        raise TotalReturnReadError(str(exc), 503) from exc
    matching = select_full_history_rows(
        history, start=start.date(), end=end.date(), after_date=after_date
    )
    if not matching:
        raise TotalReturnReadError("TOTAL_RETURN_MAPPING_NOT_FOUND", 404)
    truncated = len(matching) > limit
    response_rows = matching[:limit]
    next_cursor = None
    if truncated and cursor_codec is not None:
        next_cursor = cursor_codec.encode(
            {
                "version": 1,
                "kind": "total-return",
                "query": expected_query,
                "source_dataset_id": source_dataset_id,
                "state_revision": state_revision,
                "last_session_date": response_rows[-1]["session_date"],
            }
        )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": status["generated_at_utc"],
        "consistency": {
            "read_at_utc": status["generated_at_utc"],
            "snapshot_marker": status["consistency"]["snapshot_marker"],
            "transaction_read_only": status["consistency"]["transaction_read_only"],
            "mapping_count": 1,
            "state_revision": state_revision,
        },
        "series": status["series"],
        "mapping": status["mapping"],
        "query": {
            "instrument_key": instrument_key,
            "start": start,
            "end": end,
            "source_dataset_id": source_dataset_id,
            "limit": limit,
            "eligibility": eligibility,
            "usage_mode": "full_history_research",
            "research_contract_id": contract["contract_id"],
        },
        "source": {
            "research_eligibility": "FULL_HISTORY_RESEARCH",
            "parity_status": "PASS",
            "usage_mode": "full_history_research",
            "research_contract_id": contract["contract_id"],
            "freshness_required": False,
            "total_return_definition": contract["total_return_definition"],
            "source_file_sha256": history["source_file_sha256"],
            "full_history_ordered_content_sha256": history["ordered_content_sha256"],
        },
        "warnings": status["state"]["warnings"],
        "row_count": len(response_rows),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "ordered_content_sha256": ordered_content_sha256(response_rows),
        "rows": response_rows,
    }


def total_return_payload(
    reader: QueryReader,
    *,
    instrument_key: str,
    start: datetime,
    end: datetime,
    source_dataset_id: str | None,
    limit: int,
    eligibility: str,
    usage_mode: str = "current_operations",
    research_contract_id: str | None = None,
    cursor_payload: Mapping[str, Any] | None = None,
    cursor_codec: CursorCodec | None = None,
) -> dict[str, Any]:
    if not instrument_key or len(instrument_key) > 64:
        raise ValueError("instrument_key is required")
    if start >= end:
        raise ValueError("start must be earlier than end")
    if source_dataset_id is not None and len(source_dataset_id) > 128:
        raise ValueError("source_dataset_id is too long")
    if usage_mode not in {
        "current_operations",
        "fixed_window_research",
        "full_history_research",
    }:
        raise ValueError(
            "usage_mode must be current_operations, fixed_window_research, or full_history_research"
        )
    if eligibility not in {"eligible", "stored_complete"}:
        raise ValueError("eligibility must be eligible or stored_complete")

    contract: dict[str, Any] | None = None
    query_eligibility = eligibility
    if usage_mode in {"fixed_window_research", "full_history_research"}:
        contract = contract_for_request(research_contract_id, instrument_key)
        if contract["usage_mode"] != usage_mode:
            raise ValueError("usage_mode does not match research contract")
    if usage_mode == "fixed_window_research":
        validate_requested_window(contract, start.date(), end.date())
        if source_dataset_id not in {None, contract["source_dataset_id"]}:
            raise ValueError("source_dataset_id does not match research contract")
        source_dataset_id = contract["source_dataset_id"]
        query_eligibility = "stored_complete"
    elif usage_mode == "full_history_research":
        return _full_history_total_return_payload(
            reader,
            instrument_key=instrument_key,
            start=start,
            end=end,
            source_dataset_id=source_dataset_id,
            limit=limit,
            eligibility=eligibility,
            research_contract_id=research_contract_id,
            contract=contract,
            cursor_payload=cursor_payload,
            cursor_codec=cursor_codec,
        )

    if cursor_payload is not None:
        cursor_source_dataset_id = cursor_payload.get("source_dataset_id")
        if source_dataset_id is not None and source_dataset_id != cursor_source_dataset_id:
            raise CursorError("CURSOR_QUERY_MISMATCH", 409)
        source_dataset_id = cursor_source_dataset_id
        expected_query = {
            "instrument_key": instrument_key,
            "start": json_value(start),
            "end": json_value(end),
            "source_dataset_id": source_dataset_id,
            "limit": limit,
            "eligibility": eligibility,
            "usage_mode": usage_mode,
            "research_contract_id": research_contract_id,
        }
        if (
            cursor_payload.get("kind") != "total-return"
            or cursor_payload.get("query") != expected_query
        ):
            raise CursorError("CURSOR_QUERY_MISMATCH", 409)
        try:
            after_date = date.fromisoformat(str(cursor_payload["last_session_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorError("CURSOR_INVALID", 400) from exc
    else:
        after_date = None

    result_sets = reader.query_atomic(
        (
            (TOTAL_RETURN_CONTEXT_QUERY, ()),
            (
                TOTAL_RETURN_ROWS_QUERY,
                (
                    instrument_key,
                    source_dataset_id,
                    source_dataset_id,
                    source_dataset_id,
                    start.date(),
                    end.date(),
                    query_eligibility,
                    query_eligibility,
                    after_date,
                    after_date,
                    limit + 1,
                ),
            ),
        )
    )
    if len(result_sets) != 2 or len(result_sets[0]) != 1:
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    context = result_sets[0][0]
    rows = result_sets[1]
    if context.get("database_name") != MARKET_DB or context.get("role_name") != "saxo_app_reader":
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    if context.get("transaction_read_only") != "on":
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    if not rows:
        raise TotalReturnReadError("TOTAL_RETURN_MAPPING_NOT_FOUND", 404)

    mapping_count = max(int(row.get("mapping_count") or 0) for row in rows)
    if mapping_count > 1:
        raise TotalReturnReadError("SOURCE_DATASET_REQUIRED", 409)
    if mapping_count != 1:
        raise TotalReturnReadError("TOTAL_RETURN_MAPPING_NOT_FOUND", 404)
    mapping = rows[0]
    state_revision = str(mapping.get("state_revision") or "").strip()
    if state_revision and len(state_revision) != 64:
        raise TotalReturnReadError("TOTAL_RETURN_INTEGRITY_FAILED", 503)
    if cursor_payload is not None and (
        len(state_revision) != 64
        or cursor_payload.get("state_revision") != state_revision
    ):
        raise CursorError("CURSOR_EXPIRED", 409)
    row_values = [row for row in rows if row.get("session_date") is not None]
    truncated = len(row_values) > limit
    selected_rows = row_values[:limit]
    response_rows = [
        {
            "source_dataset_id": row["source_dataset_id"],
            "external_series_key": row["external_series_key"],
            "session_date": row["session_date"],
            "value": row["value"],
            "volume": row["volume"],
            "quality_status": row["quality_status"],
            "price_basis": row["row_price_basis"],
        }
        for row in selected_rows
    ]
    warning_codes = (
        ["NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED"]
        if eligibility == "stored_complete"
        else []
    )
    if contract is not None:
        instrument_contract = contract["instruments"][instrument_key.upper()]
        allowed_warning_codes = list(instrument_contract["approved_warning_codes"])
        unexpected_statuses = {
            str(row.get("quality_status"))
            for row in response_rows
            if row.get("quality_status") not in {"PASS", "WARN"}
        }
        if unexpected_statuses:
            raise TotalReturnReadError("TOTAL_RETURN_CONTRACT_QUALITY_FAILED", 503)
        if any(row.get("quality_status") == "WARN" for row in response_rows) and not allowed_warning_codes:
            raise TotalReturnReadError("TOTAL_RETURN_CONTRACT_QUALITY_FAILED", 503)
        warning_codes = allowed_warning_codes + [
            "FIXED_WINDOW_FRESHNESS_NOT_REQUIRED",
            "CATALOG_ELIGIBILITY_LABEL_RETAINED_AS_METADATA",
        ]
    next_cursor = None
    if truncated and selected_rows and cursor_codec is not None and len(state_revision) == 64:
        next_cursor = cursor_codec.encode(
            {
                "version": 1,
                "kind": "total-return",
                "query": {
                    "instrument_key": instrument_key,
                    "start": json_value(start),
                    "end": json_value(end),
                    "source_dataset_id": mapping["source_dataset_id"],
                    "limit": limit,
                    "eligibility": eligibility,
                    "usage_mode": usage_mode,
                    "research_contract_id": research_contract_id,
                },
                "source_dataset_id": mapping["source_dataset_id"],
                "state_revision": state_revision,
                "last_session_date": json_value(selected_rows[-1]["session_date"]),
            }
        )
    return {
        "api_version": API_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "generated_at_utc": context["read_at_utc"],
        "consistency": {
            "read_at_utc": context["read_at_utc"],
            "snapshot_marker": context["snapshot_marker"],
            "mapping_count": mapping_count,
            "state_revision": state_revision,
        },
        "series": {
            "instrument_id": mapping["instrument_id"],
            "instrument_key": mapping["instrument_key"],
            "symbol": mapping["symbol"],
            "category": mapping["category"],
            "source_dataset_id": mapping["source_dataset_id"],
            "external_series_key": mapping["external_series_key"],
            "provider": mapping["provider"],
            "dataset_name": mapping["dataset_name"],
            "price_basis": "etf_total_return",
        },
        "mapping": {
            "mapping_kind": mapping["mapping_kind"],
            "mapping_reason": mapping["mapping_reason"],
            "approved_at_utc": mapping["approved_at_utc"],
            "approved_by": mapping["approved_by"],
        },
        "query": {
            "instrument_key": instrument_key,
            "start": start,
            "end": end,
            "source_dataset_id": source_dataset_id,
            "limit": limit,
            "eligibility": eligibility,
            "usage_mode": usage_mode,
            "research_contract_id": research_contract_id,
        },
        "source": {
            "research_eligibility": mapping["research_eligibility"],
            "parity_status": "PASS",
            "usage_mode": usage_mode,
            "research_contract_id": None if contract is None else contract["contract_id"],
            "freshness_required": True if contract is None else False,
            "total_return_definition": None if contract is None else contract["total_return_definition"],
        },
        "warnings": warning_codes,
        "row_count": len(response_rows),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "ordered_content_sha256": ordered_content_sha256(response_rows),
        "rows": response_rows,
    }


def create_app(
    reader: QueryReader | None = None,
    snapshot_reader: QueryReader | None = None,
    snapshot_manifest_loader: SnapshotManifestLoader = load_snapshot_manifest,
    cursor_secret: bytes | None = None,
    periodic_status_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> Flask:
    use_default_readers = reader is None
    selected_reader = reader or DatabaseReader()
    selected_snapshot_reader = snapshot_reader
    if selected_snapshot_reader is None and use_default_readers:
        selected_snapshot_reader = SnapshotDatabaseReader()
    cursor_codec = CursorCodec(cursor_secret or secrets.token_bytes(32))
    if periodic_status_loader is None:
        from .periodic_update_service import peek_service_status

        periodic_status_loader = peek_service_status
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
        except (TotalReturnContractError, ValueError):
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

    @app.get("/api/v1/service-status")
    def service_status():
        return jsonify(json_value(revision_service_status_payload(selected_reader)))

    @app.get("/api/v1/c2/daily-close-status")
    def c2_daily_close_status():
        try:
            scheduler = periodic_status_loader() if periodic_status_loader else {}
            payload = c2_daily_close_status_payload(
                selected_reader, scheduler_status=scheduler
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/c2/hourly-overlay")
    def c2_hourly_overlay():
        try:
            payload = c2_hourly_overlay_payload(
                selected_reader,
                instrument_key=request.args.get("instrument_key", ""),
                start=parse_utc(request.args.get("start", ""), "start"),
                end=parse_utc(request.args.get("end", ""), "end"),
                limit=parse_limit(request.args.get("limit"), MAX_BAR_ROWS),
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/bars")
    def bars():
        try:
            instrument_key = request.args.get("instrument_key", "").strip().lower()
            layer = request.args.get("layer", "").strip().lower()
            publication = candidate_publication_state(selected_reader, instrument_key)
            if publication is not None and publication.get("publication_status") not in {
                "STAGING", "PUBLISHED"
            }:
                return jsonify(
                    json_value(
                        {
                            "status": "BLOCKED",
                            "error_code": "SERIES_NOT_PUBLISHED",
                            "instrument_key": instrument_key,
                            "publication": publication,
                        }
                    )
                ), 409
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

    @app.get("/api/v1/snapshots/<int:snapshot_id>/bars")
    def snapshot_bars(snapshot_id: int):
        if selected_snapshot_reader is None:
            return jsonify(
                {"status": "FAILED", "error_code": "SNAPSHOT_DATABASE_UNAVAILABLE"}
            ), 503
        try:
            cursor_token = request.args.get("cursor", "").strip()
            cursor_payload = cursor_codec.decode(cursor_token) if cursor_token else None
            payload = snapshot_bars_payload(
                selected_snapshot_reader,
                snapshot_id=snapshot_id,
                instrument_key=request.args.get("instrument_key", "").strip().lower(),
                layer=request.args.get("layer", "").strip().lower(),
                price_basis=request.args.get("price_basis", "").strip().lower(),
                start=parse_utc(request.args.get("start", ""), "start"),
                end=parse_utc(request.args.get("end", ""), "end"),
                limit=parse_limit(request.args.get("limit"), MAX_BAR_ROWS),
                manifest_loader=snapshot_manifest_loader,
                cursor_payload=cursor_payload,
                cursor_codec=cursor_codec,
            )
        except CursorError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except SnapshotReadError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/total-return")
    def total_return():
        try:
            cursor_token = request.args.get("cursor", "").strip()
            cursor_payload = cursor_codec.decode(cursor_token) if cursor_token else None
            payload = total_return_payload(
                selected_reader,
                instrument_key=request.args.get("instrument_key", "").strip().lower(),
                start=parse_utc(request.args.get("start", ""), "start"),
                end=parse_utc(request.args.get("end", ""), "end"),
                source_dataset_id=(
                    request.args.get("source_dataset_id", "").strip() or None
                ),
                limit=parse_limit(request.args.get("limit"), MAX_TOTAL_RETURN_ROWS),
                eligibility=request.args.get("eligibility", "eligible").strip().lower(),
                usage_mode=request.args.get("usage_mode", "current_operations").strip().lower(),
                research_contract_id=(
                    request.args.get("research_contract_id", "").strip() or None
                ),
                cursor_payload=cursor_payload,
                cursor_codec=cursor_codec,
            )
        except CursorError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except TotalReturnReadError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except (TotalReturnContractError, ValueError):
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/total-return-status")
    def total_return_status():
        try:
            payload = total_return_status_payload(
                selected_reader,
                instrument_key=request.args.get("instrument_key", "").strip().lower(),
                research_contract_id=(
                    request.args.get("research_contract_id", "").strip() or None
                ),
            )
        except TotalReturnReadError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except (TotalReturnContractError, ValueError):
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/strategy-data/contracts")
    def strategy_data_contracts():
        try:
            payload = public_strategy_external_contract()
        except StrategyExternalContractError as exc:
            return jsonify({"status": "FAILED", "error_code": str(exc)}), 503
        return jsonify(
            json_value(
                {
                    "api_version": API_VERSION,
                    "contract_revision": CONTRACT_REVISION,
                    "read_only": True,
                    **payload,
                }
            )
        )

    @app.get("/api/v1/strategy-data/status")
    def strategy_data_status():
        try:
            migration_applied = strategy_external_migration_applied(selected_reader)
            receipt_rows = (
                strategy_external_status_rows(selected_reader)
                if migration_applied
                else []
            )
            payload = public_strategy_external_status(
                receipt_rows,
                migration_applied=migration_applied,
            )
        except StrategyExternalContractError as exc:
            return jsonify({"status": "FAILED", "error_code": str(exc)}), 503
        return jsonify(
            json_value(
                {
                    "api_version": API_VERSION,
                    "contract_revision": CONTRACT_REVISION,
                    "read_only": True,
                    "generated_at_utc": datetime.now(timezone.utc),
                    **payload,
                }
            )
        )

    @app.get("/api/v1/strategy-data/receipts")
    def strategy_data_receipts():
        try:
            dataset_role = request.args.get("dataset_role", "").strip().upper() or None
            if dataset_role is not None and dataset_role not in STRATEGY_EXTERNAL_ROLES:
                raise ValueError("unknown dataset role")
            limit = parse_limit(
                request.args.get("limit"), MAX_STRATEGY_RECEIPT_ROWS
            )
            migration_applied = strategy_external_migration_applied(selected_reader)
            rows = (
                strategy_external_receipts(
                    selected_reader,
                    dataset_role=dataset_role,
                    limit=limit,
                )
                if migration_applied
                else []
            )
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(
            json_value(
                {
                    "api_version": API_VERSION,
                    "contract_revision": CONTRACT_REVISION,
                    "read_only": True,
                    "migration_status": "APPLIED" if migration_applied else "NOT_APPLIED",
                    "dataset_role": dataset_role,
                    "row_count": len(rows),
                    "rows": rows,
                }
            )
        )

    @app.get("/api/v1/strategy-data/calendars/<calendar_id>")
    def strategy_data_calendar(calendar_id: str):
        try:
            payload = strategy_calendar_payload(
                selected_reader,
                calendar_id=calendar_id.strip(),
                start=parse_iso_date(request.args.get("start", ""), "start"),
                end=parse_iso_date(request.args.get("end", ""), "end"),
                limit=parse_limit(
                    request.args.get("limit"), MAX_STRATEGY_CALENDAR_ROWS
                ),
            )
        except StrategyExternalReadError as exc:
            return jsonify({"status": "FAILED", "error_code": exc.code}), exc.http_status
        except StrategyExternalContractError as exc:
            return jsonify({"status": "FAILED", "error_code": str(exc)}), 503
        except ValueError:
            return jsonify({"status": "FAILED", "error_code": "INVALID_REQUEST"}), 400
        return jsonify(json_value(payload))

    @app.get("/api/v1/manifests")
    def manifests():
        datasets = selected_reader.query(
            """
            SELECT source_dataset_id, dataset_name, provider, environment,
                   dataset_kind, price_basis, research_eligibility, active,
                   source_manifest_relative_path, source_manifest_sha256,
                   metadata_json
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
        try:
            research_contracts = [
                public_contract(contract)
                for contract in load_total_return_research_contracts()
            ]
        except TotalReturnContractError:
            research_contracts = []
        try:
            strategy_external_contract = public_strategy_external_contract()
        except StrategyExternalContractError:
            strategy_external_contract = None
        return jsonify(
            json_value(
                {
                    "datasets": datasets,
                    "snapshots": snapshots,
                    "total_return_research_contracts": research_contracts,
                    "strategy_external_data_contract": strategy_external_contract,
                }
            )
        )

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

    @app.get("/api/v1/ui/instruments")
    def ui_instruments():
        series_rows = inventory_series(selected_reader)
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": instrument_catalog_payload(selected_reader, series_rows),
                }
            )
        )

    @app.get("/api/v1/ui/instruments/<instrument_key>")
    def ui_instrument_reference(instrument_key: str):
        try:
            reference = reference_for_key(instrument_key)
        except (ValueError, LookupError):
            return jsonify({"status": "FAILED", "error_code": "INSTRUMENT_NOT_FOUND"}), 404
        return jsonify(
            json_value(
                {
                    "api_version": 1,
                    "generated_at_utc": datetime.now(timezone.utc),
                    "data": reference,
                }
            )
        )

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
    if (
        isinstance(selected_snapshot_reader, DatabaseReader)
        and selected_snapshot_reader is not selected_reader
    ):
        atexit.register(selected_snapshot_reader.close)
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
