"""Read-only, hash-bound access to the ETF11 full-history research source."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .connection import project_root
from .total_return_contract import TotalReturnContractError


def _ordered_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_full_history_series(
    contract: Mapping[str, Any], instrument_key: str
) -> dict[str, Any]:
    """Validate one immutable source file and return its common-window API rows."""
    if contract.get("usage_mode") != "full_history_research":
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_USAGE_INVALID")
    ticker = instrument_key.upper()
    try:
        instrument = contract["instruments"][ticker]
        source = instrument["source"]
        first = date.fromisoformat(contract["window"]["first_session_date"])
        last = date.fromisoformat(contract["window"]["last_session_date"])
        expected_rows = int(contract["window"]["rows_per_instrument"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TotalReturnContractError(
            "TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENT_NOT_FOUND"
        ) from exc

    warning_dates = set((instrument.get("warning_evidence") or {}).get("session_dates") or [])
    path = project_root() / source["relative_path"]
    rows: list[dict[str, Any]] = []
    observed_dates: list[date] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for source_row in csv.DictReader(handle):
                session_date = date.fromisoformat(source_row["date"])
                if session_date < first or session_date > last:
                    continue
                if source_row.get("ticker") != ticker:
                    raise TotalReturnContractError(
                        "TOTAL_RETURN_RESEARCH_SOURCE_IDENTITY_MISMATCH"
                    )
                adjusted_close = Decimal(source_row["adjusted_close"])
                total_return = Decimal(source_row["total_return_index"])
                if not adjusted_close.is_finite() or adjusted_close <= 0:
                    raise TotalReturnContractError(
                        "TOTAL_RETURN_RESEARCH_SOURCE_QUALITY_FAILED"
                    )
                if not total_return.is_finite() or total_return <= 0:
                    raise TotalReturnContractError(
                        "TOTAL_RETURN_RESEARCH_SOURCE_QUALITY_FAILED"
                    )
                observed_dates.append(session_date)
                rows.append(
                    {
                        "source_dataset_id": contract["source_dataset_id"],
                        "external_series_key": ticker,
                        "session_date": source_row["date"],
                        "value": source_row["total_return_index"],
                        "volume": source_row["volume"],
                        "quality_status": (
                            "WARN" if source_row["date"] in warning_dates else "PASS"
                        ),
                        "price_basis": contract["price_basis"],
                    }
                )
    except (OSError, UnicodeError, csv.Error, KeyError, ValueError, InvalidOperation) as exc:
        raise TotalReturnContractError(
            "TOTAL_RETURN_RESEARCH_SOURCE_QUALITY_FAILED"
        ) from exc

    if (
        len(rows) != expected_rows
        or not observed_dates
        or observed_dates[0] != first
        or observed_dates[-1] != last
        or observed_dates != sorted(observed_dates)
        or len(set(observed_dates)) != len(observed_dates)
    ):
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_SOURCE_COVERAGE_FAILED")
    warn_count = sum(row["quality_status"] == "WARN" for row in rows)
    expected_warn_count = int(
        (instrument.get("warning_evidence") or {}).get("quality_warn_rows") or 0
    )
    if warn_count != expected_warn_count:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_SOURCE_QUALITY_FAILED")
    ordered_content_sha256 = _ordered_hash(rows)
    if ordered_content_sha256 != source["ordered_content_sha256"]:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_SOURCE_CONTENT_MISMATCH")
    return {
        "ticker": ticker,
        "rows": rows,
        "row_count": len(rows),
        "first_session_date": observed_dates[0].isoformat(),
        "last_session_date": observed_dates[-1].isoformat(),
        "duplicate_count": len(observed_dates) - len(set(observed_dates)),
        "null_or_nonpositive_count": 0,
        "ordered_time_status": "PASS",
        "quality_warn_count": warn_count,
        "source_file_sha256": source["sha256"],
        "ordered_content_sha256": ordered_content_sha256,
    }


def select_full_history_rows(
    series: Mapping[str, Any],
    *,
    start: date,
    end: date,
    after_date: date | None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in series["rows"]
        if start <= date.fromisoformat(row["session_date"]) < end
        and (after_date is None or date.fromisoformat(row["session_date"]) > after_date)
    ]
