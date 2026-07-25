"""Provider-neutral DPU3 gates; acquisition stays blocked until contract freeze."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .connection import project_root


PROFILE_PATH = Path("specs/source_collection/s6v5a_periodic_update_v1.json")
REQUIRED_TICKERS = ("SPY", "IWM", "EFA", "EEM", "VNQ")
VALUE_FIELDS = (
    "close_unadjusted",
    "adjusted_close",
    "dividend_cash",
    "split_factor",
)


def provider_gate(path: Path | None = None) -> dict[str, Any]:
    selected = path or project_root() / PROFILE_PATH
    payload = json.loads(selected.read_text(encoding="utf-8"))
    contract = payload.get("total_return", {})
    if (
        contract.get("status") != "READY"
        or contract.get("scheduled") is not True
        or not isinstance(contract.get("provider"), str)
        or not contract.get("provider", "").strip()
    ):
        return {
            "status": "BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED",
            "scheduled": False,
            "operator_decision_required": True,
            "required_provider_contract": list(contract.get("required_provider_contract", [])),
            "development_dataset_promoted": False,
        }
    return {
        "status": "READY",
        "scheduled": True,
        "operator_decision_required": False,
        "provider": contract["provider"],
        "development_dataset_promoted": False,
    }


def classify_provider_error(http_status: int | None) -> dict[str, Any]:
    """Keep provider transport/auth failures out of data-quality findings."""

    if http_status in {401, 403}:
        domain = "interface_auth"
    else:
        domain = "interface_operational"
    return {
        "status": "BLOCKED",
        "error_domain": domain,
        "quality_status": "NOT_EVALUATED",
        "publish_current_dataset": False,
    }


def _decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _date(row: Mapping[str, Any]) -> date | None:
    value = row.get("date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _canonical_row(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "ticker": str(row["ticker"]).upper(),
        "date": str(row["date"]),
        **{field: str(row[field]) for field in VALUE_FIELDS},
        "provider_revision": str(row["provider_revision"]),
    }


def evaluate_total_return_batch(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fail closed on structural, price, order, and corporate-action defects."""

    selected = list(rows)
    errors: list[str] = []
    seen: set[tuple[str, date]] = set()
    previous: dict[str, date] = {}
    canonical: list[dict[str, str]] = []
    for index, row in enumerate(selected):
        ticker = str(row.get("ticker", "")).upper()
        session_date = _date(row)
        if ticker not in REQUIRED_TICKERS:
            errors.append(f"UNSUPPORTED_TICKER:{index}")
        if session_date is None:
            errors.append(f"INVALID_DATE:{index}")
        elif (ticker, session_date) in seen:
            errors.append(f"DUPLICATE_DATE:{ticker}:{session_date.isoformat()}")
        else:
            seen.add((ticker, session_date))
            if ticker in previous and session_date < previous[ticker]:
                errors.append(f"DATE_REVERSAL:{ticker}:{session_date.isoformat()}")
            previous[ticker] = session_date

        values = {field: _decimal(row, field) for field in VALUE_FIELDS}
        if values["close_unadjusted"] is None or values["close_unadjusted"] <= 0:
            errors.append(f"NONPOSITIVE_CLOSE:{index}")
        if values["adjusted_close"] is None or values["adjusted_close"] <= 0:
            errors.append(f"NONPOSITIVE_ADJUSTED_CLOSE:{index}")
        if values["dividend_cash"] is None or values["dividend_cash"] < 0:
            errors.append(f"INVALID_DIVIDEND:{index}")
        if values["split_factor"] is None or values["split_factor"] <= 0:
            errors.append(f"INVALID_SPLIT_FACTOR:{index}")
        if not str(row.get("provider_revision", "")).strip():
            errors.append(f"PROVIDER_REVISION_MISSING:{index}")
        try:
            canonical.append(_canonical_row(row))
        except KeyError:
            errors.append(f"REQUIRED_FIELD_MISSING:{index}")

    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "status": "PASS" if not errors else "FAIL",
        "row_count": len(selected),
        "errors": sorted(set(errors)),
        "duplicate_count": sum(error.startswith("DUPLICATE_DATE:") for error in errors),
        "ordered_content_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def revision_keys(
    previous_rows: Iterable[Mapping[str, Any]],
    current_rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    def indexed(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
        return {
            (str(row["ticker"]).upper(), str(row["date"])): _canonical_row(row)
            for row in rows
        }

    previous = indexed(previous_rows)
    current = indexed(current_rows)
    return tuple(
        f"{ticker}:{session_date}"
        for ticker, session_date in sorted(previous.keys() & current.keys())
        if previous[(ticker, session_date)] != current[(ticker, session_date)]
    )
