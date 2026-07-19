"""Read-only data-management UI models over fixed saxo_market relations."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Protocol, Sequence


MAX_INVENTORY_ROWS = 1_000
MAX_UI_PAGE_ROWS = 200
MAX_QUALITY_EVENTS = 500
SERIES_ID_LENGTH = 24
CANONICAL_DERIVATION_VERSION = "db3_accepted_1h_calendar_v1"
ELIGIBILITY_MODES = ("eligible", "stored_complete")
STATUS_PRIORITY = {
    "FAIL": 5,
    "STALE": 4,
    "WARN": 3,
    "NOT_EVALUATED": 2,
    "PASS": 1,
}


class QueryReader(Protocol):
    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


INVENTORY_SQL = (
    "SELECT * FROM analytics.v_data_inventory "
    "ORDER BY layer, symbol, horizon_minutes, price_basis, source_dataset_id LIMIT %s"
)
DATASET_SQL = """
SELECT source_dataset_id, dataset_name, provider, environment, dataset_kind,
       price_basis, canonical_horizon_minutes, authoritative_layer,
       research_eligibility, active, source_manifest_relative_path
FROM catalog.source_dataset
ORDER BY source_dataset_id
"""
COVERAGE_SQL = "SELECT * FROM analytics.v_data_coverage ORDER BY symbol, horizon_minutes LIMIT %s"
FRESHNESS_SQL = "SELECT * FROM analytics.v_data_freshness ORDER BY symbol, horizon_minutes LIMIT %s"
COVERAGE_DETAIL_SQL = (
    "SELECT * FROM analytics.v_data_coverage "
    "WHERE instrument_id=%s ORDER BY horizon_minutes"
)
FRESHNESS_DETAIL_SQL = (
    "SELECT * FROM analytics.v_data_freshness "
    "WHERE instrument_id=%s ORDER BY horizon_minutes, price_basis"
)
RUN_SQL = "SELECT * FROM ops.v_ingestion_status ORDER BY started_at_utc DESC LIMIT %s"
QUALITY_SQL = (
    "SELECT * FROM quality.v_open_event "
    "ORDER BY severity, created_at_utc DESC, quality_event_id DESC LIMIT %s"
)
LINEAGE_SQL = (
    "SELECT * FROM analytics.v_data_lineage "
    "ORDER BY source_dataset_id, relative_path, source_file_id LIMIT %s"
)
LINEAGE_DETAIL_SQL = (
    "SELECT * FROM analytics.v_data_lineage WHERE source_dataset_id=%s "
    "ORDER BY relative_path, source_file_id LIMIT %s"
)
BACKUP_SQL = "SELECT * FROM ops.v_backup_status ORDER BY database_name LIMIT %s"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_offset(value: str | None) -> int:
    try:
        selected = int(value or "0")
    except ValueError as exc:
        raise ValueError("offset must be an integer") from exc
    if not 0 <= selected <= 100_000:
        raise ValueError("offset is outside the allowed range")
    return selected


def normalized_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def series_role(row: Mapping[str, Any]) -> str:
    layer = normalized_text(row.get("layer")).lower()
    basis = normalized_text(row.get("price_basis")).lower()
    horizon = row.get("horizon_minutes")
    if layer == "curated" and basis == "etf_total_return":
        return "TOTAL_RETURN_DAILY"
    if layer == "curated" and horizon == 60:
        return "CANONICAL_1H"
    if layer == "derived" and horizon == 240:
        return "DERIVED_4H"
    if layer == "derived" and horizon == 1440:
        return "DERIVED_1D_RISK"
    if layer == "raw":
        return "RAW_ARCHIVE"
    if layer == "research_metadata":
        return "REFERENCE_METADATA"
    return "UNKNOWN_ROLE"


def layer_label(row: Mapping[str, Any], role: str | None = None) -> str:
    selected = role or series_role(row)
    return {
        "CANONICAL_1H": "1H",
        "DERIVED_4H": "4H",
        "DERIVED_1D_RISK": "1D",
        "TOTAL_RETURN_DAILY": "1D-TR",
        "RAW_ARCHIVE": f"RAW-{row.get('horizon_minutes') or 'REF'}",
        "REFERENCE_METADATA": "META",
    }.get(selected, "UNKNOWN")


def _series_identity(row: Mapping[str, Any]) -> str:
    values = (
        row.get("source_dataset_id"),
        row.get("instrument_id"),
        row.get("symbol"),
        row.get("layer"),
        row.get("horizon_minutes"),
        row.get("price_basis"),
    )
    return "\x1f".join("" if value is None else str(value) for value in values)


def series_id(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_series_identity(row).encode("utf-8")).hexdigest()[:SERIES_ID_LENGTH]


def status_of(row: Mapping[str, Any]) -> str:
    candidates = (
        normalized_text(row.get("quality_status")).upper(),
        normalized_text(row.get("freshness_status")).upper(),
    )
    known = [value for value in candidates if value in STATUS_PRIORITY]
    return max(known, key=STATUS_PRIORITY.__getitem__) if known else "NOT_EVALUATED"


def chart_kind(role: str) -> str | None:
    if role in {"CANONICAL_1H", "DERIVED_4H", "DERIVED_1D_RISK"}:
        return "ohlc"
    if role == "TOTAL_RETURN_DAILY":
        return "line"
    return None


def normalize_series(row: Mapping[str, Any]) -> dict[str, Any]:
    role = series_role(row)
    return {
        **dict(row),
        "series_id": series_id(row),
        "role": role,
        "layer_label": layer_label(row, role),
        "status": status_of(row),
        "chart_kind": chart_kind(role),
        "chart_available": chart_kind(role) is not None,
        "authoritative": role in {"CANONICAL_1H", "DERIVED_4H", "DERIVED_1D_RISK"},
    }


def inventory_series(reader: QueryReader) -> list[dict[str, Any]]:
    rows = reader.query(INVENTORY_SQL, (MAX_INVENTORY_ROWS,))
    normalized = [normalize_series(row) for row in rows]
    ids = [row["series_id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise RuntimeError("series identity collision")
    return normalized


def filter_series(
    rows: Sequence[dict[str, Any]],
    *,
    role: str = "",
    category: str = "",
    symbol: str = "",
    layer: str = "",
    status: str = "",
    canonical_only: bool = False,
) -> list[dict[str, Any]]:
    allowed_roles = {
        "CANONICAL_1H", "DERIVED_4H", "DERIVED_1D_RISK", "TOTAL_RETURN_DAILY",
        "RAW_ARCHIVE", "REFERENCE_METADATA", "UNKNOWN_ROLE",
    }
    if role and role not in allowed_roles:
        raise ValueError("unknown role")
    if status and status not in STATUS_PRIORITY:
        raise ValueError("unknown status")
    if len(symbol) > 64 or len(category) > 64 or len(layer) > 16:
        raise ValueError("filter is too long")
    result = list(rows)
    if canonical_only:
        result = [row for row in result if row["authoritative"]]
    if role:
        result = [row for row in result if row["role"] == role]
    if category:
        result = [row for row in result if normalized_text(row.get("category")) == category]
    if symbol:
        lowered = symbol.casefold()
        result = [row for row in result if lowered in normalized_text(row.get("symbol")).casefold()]
    if layer:
        result = [row for row in result if normalized_text(row.get("layer_label")).lower() == layer.lower()]
    if status:
        result = [row for row in result if row["status"] == status]
    return sorted(
        result,
        key=lambda row: (
            -STATUS_PRIORITY.get(row["status"], 0),
            normalized_text(row.get("category")),
            normalized_text(row.get("symbol")),
            normalized_text(row.get("layer_label")),
        ),
    )


def resolve_series(reader: QueryReader, selected_id: str) -> dict[str, Any]:
    if len(selected_id) != SERIES_ID_LENGTH or any(character not in "0123456789abcdef" for character in selected_id):
        raise ValueError("invalid series_id")
    for row in inventory_series(reader):
        if row["series_id"] == selected_id:
            return row
    raise LookupError("series not found")


@dataclass(frozen=True)
class UIChartQuery:
    statement: str
    date_bounds: bool = False
    series_kind: str = "ohlc"


UI_CHART_QUERIES: Mapping[str, UIChartQuery] = {
    "CANONICAL_1H": UIChartQuery(
        f"""
        SELECT * FROM (
        SELECT b.time_utc, NULL::DATE AS session_date, b.open, b.high, b.low, b.close,
               b.volume, NULL::NUMERIC AS value, b.price_basis, b.quality_status
        FROM curated.market_bar b
        WHERE b.instrument_id=%s
          AND b.price_basis=%s
          AND b.horizon_minutes=60
          AND b.time_utc >= %s AND b.time_utc < %s
          AND b.is_complete
          AND b.quality_status IN ('PASS','WARN','NOT_EVALUATED')
          AND (%s='stored_complete' OR b.quality_status='PASS')
        ORDER BY b.time_utc DESC, b.price_basis DESC
        LIMIT %s
        ) selected
        ORDER BY selected.time_utc, selected.price_basis
        """
    ),
    "DERIVED_4H": UIChartQuery(
        f"""
        SELECT * FROM (
        SELECT b.time_utc, NULL::DATE AS session_date, b.open, b.high, b.low, b.close,
               b.volume, NULL::NUMERIC AS value, b.price_basis, b.quality_status
        FROM derived.market_bar_4h b
        WHERE b.instrument_id=%s
          AND b.price_basis=%s
          AND b.derivation_version='{CANONICAL_DERIVATION_VERSION}'
          AND b.time_utc >= %s AND b.time_utc < %s
          AND b.is_complete
          AND b.quality_status IN ('PASS','WARN','NOT_EVALUATED')
          AND (%s='stored_complete' OR b.quality_status='PASS')
        ORDER BY b.time_utc DESC, b.price_basis DESC
        LIMIT %s
        ) selected
        ORDER BY selected.time_utc, selected.price_basis
        """
    ),
    "DERIVED_1D_RISK": UIChartQuery(
        f"""
        SELECT * FROM (
        SELECT NULL::TIMESTAMPTZ AS time_utc, b.session_date, b.open, b.high, b.low, b.close,
               b.volume, NULL::NUMERIC AS value, b.price_basis, b.quality_status
        FROM derived.market_bar_1d_risk b
        WHERE b.instrument_id=%s
          AND b.price_basis=%s
          AND b.derivation_version='{CANONICAL_DERIVATION_VERSION}'
          AND b.session_date >= %s AND b.session_date < %s
          AND b.is_complete
          AND b.quality_status IN ('PASS','WARN','NOT_EVALUATED')
          AND (%s='stored_complete' OR b.quality_status='PASS')
        ORDER BY b.session_date DESC, b.price_basis DESC
        LIMIT %s
        ) selected
        ORDER BY selected.session_date, selected.price_basis
        """,
        date_bounds=True,
    ),
    "TOTAL_RETURN_DAILY": UIChartQuery(
        """
        SELECT * FROM (
        SELECT NULL::TIMESTAMPTZ AS time_utc, b.date AS session_date,
               NULL::NUMERIC AS open, NULL::NUMERIC AS high, NULL::NUMERIC AS low,
               NULL::NUMERIC AS close, b.volume, b.total_return_index AS value,
               'etf_total_return'::TEXT AS price_basis, b.quality_status
        FROM curated.etf_total_return_daily b
        WHERE b.source_dataset_id=%s AND b.ticker=%s
          AND b.date >= %s AND b.date < %s
          AND b.quality_status IN ('PASS','WARN','NOT_EVALUATED')
          AND (%s='stored_complete' OR b.quality_status='PASS')
        ORDER BY b.date DESC
        LIMIT %s
        ) selected
        ORDER BY selected.session_date
        """,
        date_bounds=True,
        series_kind="line",
    ),
}


def chart_rows(
    reader: QueryReader,
    series: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    limit: int,
    eligibility: str,
) -> tuple[str, list[dict[str, Any]]]:
    if eligibility not in ELIGIBILITY_MODES:
        raise ValueError("invalid eligibility")
    if start >= end:
        raise ValueError("start must be earlier than end")
    role = normalized_text(series.get("role"))
    try:
        query = UI_CHART_QUERIES[role]
    except KeyError as exc:
        raise ValueError("series is not chartable") from exc
    lower: datetime | date = start.date() if query.date_bounds else start
    upper: datetime | date = end.date() if query.date_bounds else end
    if role == "TOTAL_RETURN_DAILY":
        params = (
            series.get("source_dataset_id"), series.get("symbol"), lower, upper,
            eligibility, limit + 1,
        )
    else:
        instrument_id = series.get("instrument_id")
        if instrument_id is None:
            raise ValueError("series has no instrument")
        params = (instrument_id, series.get("price_basis"), lower, upper, eligibility, limit + 1)
    return query.series_kind, reader.query(query.statement, params)


def series_detail(reader: QueryReader, selected_id: str) -> dict[str, Any]:
    series = resolve_series(reader, selected_id)
    instrument_id = series.get("instrument_id")
    coverage = (
        reader.query(COVERAGE_DETAIL_SQL, (instrument_id,))
        if instrument_id is not None else []
    )
    freshness = (
        reader.query(FRESHNESS_DETAIL_SQL, (instrument_id,))
        if instrument_id is not None else []
    )
    source_dataset_id = series.get("source_dataset_id")
    lineage = (
        reader.query(LINEAGE_DETAIL_SQL, (source_dataset_id, 100))
        if source_dataset_id is not None else []
    )
    return {"series": series, "coverage": coverage, "freshness": freshness, "lineage": lineage}


def overview_payload(reader: QueryReader) -> dict[str, Any]:
    series = inventory_series(reader)
    datasets = reader.query(DATASET_SQL)
    runs = reader.query(RUN_SQL, (1,))
    backups = reader.query(BACKUP_SQL, (10,))
    role_totals: dict[str, dict[str, int]] = {}
    for row in series:
        role = row["role"]
        selected = role_totals.setdefault(role, {"series_count": 0, "row_count": 0})
        selected["series_count"] += 1
        selected["row_count"] += int(row.get("row_count") or 0)
    authoritative = [row for row in series if row["authoritative"]]
    canonical_guardrails = sorted(
        (row for row in series if row["role"] == "CANONICAL_1H"),
        key=lambda row: (
            -STATUS_PRIORITY.get(row["status"], 0),
            normalized_text(row.get("category")),
            normalized_text(row.get("symbol")),
        ),
    )
    category_totals = Counter(normalized_text(row.get("category")) or "unknown" for row in authoritative)
    status_totals = Counter(row["status"] for row in authoritative)
    cards = {
        "active_dataset_count": sum(1 for row in datasets if row.get("active") is True),
        "canonical_instrument_count": len({row.get("instrument_id") for row in series if row["role"] == "CANONICAL_1H"}),
        "canonical_1h_rows": role_totals.get("CANONICAL_1H", {}).get("row_count", 0),
        "derived_4h_rows": role_totals.get("DERIVED_4H", {}).get("row_count", 0),
        "derived_1d_rows": role_totals.get("DERIVED_1D_RISK", {}).get("row_count", 0),
        "attention_series_count": sum(status_totals.get(value, 0) for value in ("FAIL", "STALE", "WARN")),
    }
    return {
        "generated_at_utc": utc_now(),
        "cards": cards,
        "role_totals": role_totals,
        "category_totals": dict(sorted(category_totals.items())),
        "status_totals": dict(sorted(status_totals.items())),
        "canonical_guardrails": canonical_guardrails,
        "latest_run": runs[0] if runs else None,
        "backups": backups,
        "inventory_row_count": len(series),
    }


def worst_status(values: Sequence[str]) -> str:
    known = [value for value in values if value in STATUS_PRIORITY]
    return max(known, key=STATUS_PRIORITY.__getitem__) if known else "NOT_EVALUATED"


def quality_summary_payload(reader: QueryReader) -> dict[str, Any]:
    coverage = reader.query(COVERAGE_SQL, (MAX_INVENTORY_ROWS,))
    freshness = reader.query(FRESHNESS_SQL, (MAX_INVENTORY_ROWS,))
    events = reader.query(QUALITY_SQL, (MAX_QUALITY_EVENTS,))
    matrix: dict[Any, dict[str, Any]] = {}
    for row in coverage:
        matrix[row.get("instrument_id")] = {
            **row,
            "coverage_status": normalized_text(row.get("coverage_status")) or "NOT_EVALUATED",
        }
    for row in freshness:
        selected = matrix.setdefault(row.get("instrument_id"), {})
        selected.update({
            "instrument_id": row.get("instrument_id"),
            "symbol": row.get("symbol"),
            "category": row.get("category"),
            "latest_complete_time_utc": row.get("latest_complete_time_utc"),
            "freshness_seconds": row.get("freshness_seconds"),
            "freshness_status": normalized_text(row.get("freshness_status")) or "NOT_EVALUATED",
            "data_status": row.get("data_status"),
        })
    normalized_events: list[dict[str, Any]] = []
    for source in events:
        event = dict(source)
        event["scope_kind"] = normalized_text(event.get("scope_kind")).upper() or "UNKNOWN"
        applicability = normalized_text(event.get("applicability")).upper()
        event["applicability"] = (
            applicability if applicability in {"CURRENT", "HISTORICAL", "UNKNOWN"} else "UNKNOWN"
        )
        event["current_blocker"] = bool(event.get("current_blocker")) or (
            normalized_text(event.get("status")).upper() in {"OPEN", "ACKNOWLEDGED"}
            and normalized_text(event.get("severity")).upper() in {"ERROR", "CRITICAL"}
            and event["applicability"] in {"CURRENT", "UNKNOWN"}
        )
        normalized_events.append(event)

    def blocks_canonical_1h(event: Mapping[str, Any]) -> bool:
        """Apply an event blocker only to its declared canonical series scope."""
        if not event["current_blocker"]:
            return False
        if event["scope_kind"] == "UNKNOWN":
            return True
        affected_layer = normalized_text(event.get("affected_layer")).lower()
        if affected_layer and affected_layer != "curated":
            return False
        horizon = event.get("horizon_minutes")
        if horizon is not None and int(horizon) != 60:
            return False
        price_basis = normalized_text(event.get("price_basis")).lower()
        if price_basis and price_basis != "native_ohlc":
            return False
        return True

    global_blockers: list[dict[str, Any]] = []
    for event in normalized_events:
        if not blocks_canonical_1h(event):
            continue
        instrument_id = event.get("instrument_id")
        if instrument_id is None:
            global_blockers.append(event)
            continue
        selected = matrix.setdefault(
            instrument_id,
            {
                "instrument_id": instrument_id,
                "instrument_key": event.get("instrument_key"),
                "symbol": event.get("symbol"),
                "category": event.get("category"),
            },
        )
        selected.setdefault("current_blocker_event_ids", []).append(event.get("quality_event_id"))

    rows = []
    for item in matrix.values():
        item["current_blocker_event_ids"] = item.get("current_blocker_event_ids", [])
        item["global_blocker_event_ids"] = [
            event.get("quality_event_id") for event in global_blockers
        ]
        item["current_blocker_count"] = (
            len(item["current_blocker_event_ids"]) + len(item["global_blocker_event_ids"])
        )
        item["status"] = "FAIL" if item["current_blocker_count"] else worst_status([
            normalized_text(item.get("coverage_status")),
            normalized_text(item.get("freshness_status")),
        ])
        rows.append(item)
    rows.sort(key=lambda row: (-STATUS_PRIORITY.get(row["status"], 0), normalized_text(row.get("symbol"))))
    historical = [row for row in normalized_events if row["applicability"] == "HISTORICAL"]
    unresolved = [row for row in normalized_events if row["applicability"] in {"CURRENT", "UNKNOWN"}]
    severity_totals = Counter(normalized_text(row.get("severity")) or "UNKNOWN" for row in historical)
    return {
        "generated_at_utc": utc_now(),
        "current": rows,
        "current_status_totals": dict(Counter(row["status"] for row in rows)),
        "blocking_event_count": sum(1 for row in normalized_events if row["current_blocker"]),
        "canonical_blocking_event_count": sum(
            1 for row in normalized_events if blocks_canonical_1h(row)
        ),
        "global_blockers": global_blockers,
        "unresolved_events": unresolved,
        "historical_open_events": historical,
        "applicability_totals": dict(
            sorted(Counter(row["applicability"] for row in normalized_events).items())
        ),
        "historical_severity_totals": dict(sorted(severity_totals.items())),
    }


def chart_marks(reader: QueryReader, series: Mapping[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    instrument_id = series.get("instrument_id")
    if instrument_id is None or start >= end:
        return []
    return reader.query(
        """
        SELECT quality_event_id, time_utc, rule_id, severity, status, action, created_at_utc
        FROM quality.v_open_event
        WHERE instrument_id=%s AND time_utc >= %s AND time_utc < %s
        ORDER BY time_utc, quality_event_id
        LIMIT %s
        """,
        (instrument_id, start, end, MAX_QUALITY_EVENTS),
    )
