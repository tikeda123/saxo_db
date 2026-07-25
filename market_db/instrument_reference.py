"""Versioned, read-only product definitions for the data-management UI and MCP."""

from __future__ import annotations

import copy
import json
from collections import Counter
from functools import lru_cache
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .connection import project_root


REFERENCE_RELATIVE_PATH = "specs/instrument_reference_v1.json"
ALLOWED_OFFICIAL_SOURCE_HOSTS = frozenset(
    {
        "www.cmegroup.com",
        "www.home.saxo",
        "www.ishares.com",
        "www.lbma.org.uk",
        "www.spglobal.com",
        "www.ssga.com",
        "investor.vanguard.com",
    }
)
EXPECTED_INSTRUMENT_KEYS = frozenset(
    {
        "eem", "efa", "eurusd", "gld", "gold", "icom", "ief", "iwm", "lqd",
        "shy", "spy", "tip", "tlt", "us500", "usdjpy", "us_treasury", "vnq", "wti",
    }
)

INSTRUMENT_LIST_SQL = """
SELECT instrument_id, provider, environment, market_key AS instrument_key, symbol,
       asset_type, category, currency, exchange_id, session_calendar_id
FROM catalog.instrument
WHERE active_to_utc IS NULL
ORDER BY market_key
"""
INSTRUMENT_DETAIL_SQL = """
SELECT instrument_id, provider, environment, market_key AS instrument_key, symbol,
       asset_type, category, currency, exchange_id, session_calendar_id
FROM catalog.instrument
WHERE instrument_id=%s AND active_to_utc IS NULL
"""


class QueryReader(Protocol):
    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


def _validate_text(value: Any, field: str, maximum: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RuntimeError(f"invalid instrument reference field: {field}")
    return value.strip()


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    path = project_root() / REFERENCE_RELATIVE_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("language") != "ja":
        raise RuntimeError("unsupported instrument reference catalog")
    instruments = payload.get("instruments")
    if not isinstance(instruments, list):
        raise RuntimeError("instrument reference list is missing")
    indexed: dict[str, dict[str, Any]] = {}
    required_text = (
        "display_name_ja", "short_name", "instrument_type_ja", "category", "summary_ja",
        "exposure_ja", "benchmark_or_reference", "quote_interpretation_ja",
    )
    for item in instruments:
        if not isinstance(item, dict):
            raise RuntimeError("invalid instrument reference entry")
        key = _validate_text(item.get("instrument_key"), "instrument_key", 64).lower()
        if key in indexed or not key.replace("_", "").isalnum():
            raise RuntimeError("duplicate or invalid instrument reference key")
        for field in required_text:
            _validate_text(item.get(field), field)
        cautions = item.get("data_cautions_ja")
        if not isinstance(cautions, list) or not cautions:
            raise RuntimeError(f"data cautions are missing: {key}")
        for caution in cautions:
            _validate_text(caution, "data_cautions_ja")
        sources = item.get("official_sources")
        if not isinstance(sources, list) or not sources:
            raise RuntimeError(f"official sources are missing: {key}")
        for source in sources:
            label = _validate_text(source.get("label"), "official_sources.label", 200)
            url = _validate_text(source.get("url"), "official_sources.url", 1_000)
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_OFFICIAL_SOURCE_HOSTS:
                raise RuntimeError(f"official source is not allow-listed: {key}/{label}")
        indexed[key] = copy.deepcopy(item)
    if set(indexed) != EXPECTED_INSTRUMENT_KEYS:
        raise RuntimeError("instrument reference catalog does not match the managed key contract")
    return {
        "catalog_id": _validate_text(payload.get("catalog_id"), "catalog_id", 128),
        "as_of_date": _validate_text(payload.get("as_of_date"), "as_of_date", 32),
        "language": "ja",
        "scope_note_ja": _validate_text(payload.get("scope_note_ja"), "scope_note_ja"),
        "instruments": indexed,
    }


def reference_catalog_metadata() -> dict[str, Any]:
    selected = _catalog()
    return {
        "catalog_id": selected["catalog_id"],
        "as_of_date": selected["as_of_date"],
        "language": selected["language"],
        "scope_note_ja": selected["scope_note_ja"],
        "reference_relative_path": REFERENCE_RELATIVE_PATH,
    }


def reference_for_key(instrument_key: str) -> dict[str, Any]:
    key = instrument_key.strip().lower()
    if len(key) > 64 or not key.replace("_", "").isalnum():
        raise ValueError("invalid instrument_key")
    try:
        return copy.deepcopy(_catalog()["instruments"][key])
    except KeyError as exc:
        raise LookupError("instrument reference not found") from exc


def _series_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    roles = sorted({str(row.get("role")) for row in rows if row.get("role")})
    layers = sorted({str(row.get("layer_label")) for row in rows if row.get("layer_label")})
    statuses = Counter(str(row.get("status") or "NOT_EVALUATED") for row in rows)
    earliest_values = [row.get("min_time_utc") for row in rows if row.get("min_time_utc") is not None]
    latest_values = [
        row.get("latest_complete_time_utc") or row.get("max_time_utc")
        for row in rows
        if row.get("latest_complete_time_utc") is not None or row.get("max_time_utc") is not None
    ]
    role_order = {
        "CANONICAL_1H": 0, "TOTAL_RETURN_DAILY": 1, "DERIVED_4H": 2,
        "DERIVED_1D_RISK": 3, "RAW_ARCHIVE": 4, "REFERENCE_METADATA": 5,
    }
    default = min(rows, key=lambda row: role_order.get(str(row.get("role")), 99), default=None)
    return {
        "series_count": len(rows),
        "roles": roles,
        "layers": layers,
        "status_totals": dict(sorted(statuses.items())),
        "earliest_time_utc": min(earliest_values) if earliest_values else None,
        "latest_complete_time_utc": max(latest_values) if latest_values else None,
        "default_series_id": default.get("series_id") if default else None,
    }


def _symbol_root(value: Any) -> str:
    return str(value or "").split(":", 1)[0].strip().upper()


def instrument_catalog_payload(
    reader: QueryReader, series_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    instruments = reader.query(INSTRUMENT_LIST_SQL)
    rows_by_instrument: dict[Any, list[Mapping[str, Any]]] = {}
    unassigned_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in series_rows:
        instrument_id = row.get("instrument_id")
        if instrument_id is None:
            unassigned_by_symbol.setdefault(_symbol_root(row.get("symbol")), []).append(row)
        else:
            rows_by_instrument.setdefault(instrument_id, []).append(row)
    output: list[dict[str, Any]] = []
    for instrument in instruments:
        key = str(instrument.get("instrument_key") or "").lower()
        reference = reference_for_key(key)
        if reference["category"] != instrument.get("category"):
            raise RuntimeError(f"instrument reference category mismatch: {key}")
        managed_rows = list(rows_by_instrument.get(instrument.get("instrument_id"), []))
        if str(instrument.get("asset_type")) == "Etf":
            managed_rows.extend(unassigned_by_symbol.get(_symbol_root(instrument.get("symbol")), []))
        output.append(
            {
                **reference,
                "managed_instrument": dict(instrument),
                "managed_series": _series_summary(managed_rows),
            }
        )
    managed_keys = {str(row.get("instrument_key") or "").lower() for row in instruments}
    if managed_keys != EXPECTED_INSTRUMENT_KEYS:
        raise RuntimeError("active database instruments do not match the reference catalog")
    return {**reference_catalog_metadata(), "instrument_count": len(output), "instruments": output}


def instrument_detail_payload(
    reader: QueryReader,
    *,
    instrument_id: Any,
    series_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if instrument_id is None:
        if not series_rows or any(row.get("role") != "TOTAL_RETURN_DAILY" for row in series_rows):
            return None
        symbol = _symbol_root(series_rows[0].get("symbol"))
        matches = [
            row for row in reader.query(INSTRUMENT_LIST_SQL)
            if row.get("asset_type") == "Etf" and _symbol_root(row.get("symbol")) == symbol
        ]
    else:
        matches = reader.query(INSTRUMENT_DETAIL_SQL, (instrument_id,))
    if not matches:
        return None
    instrument = matches[0]
    key = str(instrument.get("instrument_key") or "").lower()
    reference = reference_for_key(key)
    return {
        **reference,
        "managed_instrument": dict(instrument),
        "managed_series": _series_summary(series_rows),
        "catalog": reference_catalog_metadata(),
    }
