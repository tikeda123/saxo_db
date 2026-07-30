"""Immutable fixed-window total-return research contract loading and checks."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from .connection import project_root


CONTRACT_RELATIVE_PATH = "specs/total_return_research_contract_v1.json"
FULL_HISTORY_CONTRACT_RELATIVE_PATH = (
    "specs/total_return_full_history_research_contract_v1.json"
)
ALLOWED_AVAILABILITY = {"AVAILABLE", "AVAILABLE_WITH_WARNINGS"}


class TotalReturnContractError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_total_return_research_contract() -> dict[str, Any]:
    path = project_root() / CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_VERIFIED")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_VERIFIED") from exc
    validate_total_return_research_contract(payload, root=project_root())
    return payload


def load_total_return_research_contracts() -> list[dict[str, Any]]:
    contracts = [load_total_return_research_contract()]
    path = project_root() / FULL_HISTORY_CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_VERIFIED")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_VERIFIED") from exc
    validate_total_return_research_contract(payload, root=project_root())
    contracts.append(payload)
    return contracts


def validate_total_return_research_contract(
    payload: Mapping[str, Any], *, root: Path
) -> None:
    if payload.get("schema_version") != 1 or payload.get("status") != "ACTIVE":
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_ACTIVE")
    if payload.get("usage_mode") not in {
        "fixed_window_research",
        "full_history_research",
    }:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_USAGE_INVALID")
    if payload.get("dataset_kind") != "total_return" or payload.get("price_basis") != "etf_total_return":
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_IDENTITY_INVALID")
    if payload.get("horizon_minutes") != 1440:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_HORIZON_INVALID")

    window = payload.get("window")
    if not isinstance(window, dict):
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WINDOW_INVALID")
    try:
        first = date.fromisoformat(str(window["first_session_date"]))
        last = date.fromisoformat(str(window["last_session_date"]))
        rows = int(window["rows_per_instrument"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WINDOW_INVALID") from exc
    if first > last or rows <= 0 or window.get("freshness_required") is not False:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WINDOW_INVALID")

    definition = payload.get("total_return_definition")
    if (
        not isinstance(definition, dict)
        or not str(definition.get("definition_id") or "").strip()
        or definition.get("unadjusted_price_masquerading_as_total_return") is not False
    ):
        raise TotalReturnContractError("TOTAL_RETURN_DEFINITION_UNKNOWN")

    instruments = payload.get("instruments")
    if not isinstance(instruments, dict) or len(instruments) != 11:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENTS_INVALID")
    for ticker, item in instruments.items():
        if not str(ticker).isupper() or not isinstance(item, dict):
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENTS_INVALID")
        if item.get("availability") not in ALLOWED_AVAILABILITY:
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENTS_INVALID")
        warnings = item.get("approved_warning_codes")
        if not isinstance(warnings, list):
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENTS_INVALID")
        if item.get("availability") == "AVAILABLE" and warnings:
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WARNING_INVALID")
        warning_evidence = item.get("warning_evidence")
        if item.get("availability") == "AVAILABLE_WITH_WARNINGS":
            if (
                not warnings
                or not isinstance(warning_evidence, dict)
                or not isinstance(warning_evidence.get("quality_warn_rows"), int)
                or warning_evidence["quality_warn_rows"] <= 0
                or warning_evidence.get("automatic_corrections") != 0
            ):
                raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WARNING_INVALID")
        elif warning_evidence is not None:
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_WARNING_INVALID")

        if payload.get("usage_mode") == "full_history_research":
            source = item.get("source")
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("relative_path"), str)
                or source["relative_path"].startswith("/")
                or ".." in Path(source["relative_path"]).parts
                or not isinstance(source.get("sha256"), str)
                or len(source["sha256"]) != 64
                or not isinstance(source.get("ordered_content_sha256"), str)
                or len(source["ordered_content_sha256"]) != 64
            ):
                raise TotalReturnContractError(
                    "TOTAL_RETURN_RESEARCH_CONTRACT_LINEAGE_INVALID"
                )
            selected = root / source["relative_path"]
            if (
                not selected.is_file()
                or selected.is_symlink()
                or _sha256(selected) != source["sha256"]
            ):
                raise TotalReturnContractError(
                    "TOTAL_RETURN_RESEARCH_CONTRACT_LINEAGE_INVALID"
                )

    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_LINEAGE_INVALID")
    identity_pairs = [
        ("source_manifest_relative_path", "source_manifest_sha256"),
        ("quality_evidence_relative_path", "quality_evidence_sha256"),
    ]
    identity_pairs.append(
        (
            ("lineage_manifest_relative_path", "lineage_manifest_sha256")
            if payload.get("usage_mode") == "full_history_research"
            else ("normalized_csv_relative_path", "normalized_csv_sha256")
        )
    )
    for path_key, hash_key in identity_pairs:
        relative_path = identity.get(path_key)
        expected_sha = identity.get(hash_key)
        if (
            not isinstance(relative_path, str)
            or relative_path.startswith("/")
            or ".." in Path(relative_path).parts
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
        ):
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_LINEAGE_INVALID")
        selected = root / relative_path
        if not selected.is_file() or selected.is_symlink() or _sha256(selected) != expected_sha:
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_LINEAGE_INVALID")


def contract_for_request(contract_id: str | None, instrument_key: str) -> dict[str, Any]:
    contracts = load_total_return_research_contracts()
    if contract_id in {None, ""}:
        contract = contracts[0]
    else:
        matches = [item for item in contracts if item["contract_id"] == contract_id]
        if len(matches) != 1:
            raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_FOUND")
        contract = matches[0]
    ticker = instrument_key.upper()
    if ticker not in contract["instruments"]:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_CONTRACT_INSTRUMENT_NOT_FOUND")
    return contract


def validate_requested_window(contract: Mapping[str, Any], start: date, end: date) -> None:
    window = contract["window"]
    first = date.fromisoformat(window["first_session_date"])
    last_exclusive = date.fromisoformat(window["last_session_date"]) + timedelta(days=1)
    if start < first or end > last_exclusive or start >= end:
        raise TotalReturnContractError("TOTAL_RETURN_RESEARCH_WINDOW_OUTSIDE_CONTRACT")


def public_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "contract_id": contract["contract_id"],
        "status": contract["status"],
        "usage_mode": contract["usage_mode"],
        "source_dataset_id": contract["source_dataset_id"],
        "provider": contract["series_provider"],
        "price_basis": contract["price_basis"],
        "horizon_minutes": contract["horizon_minutes"],
        "window": contract["window"],
        "identity": contract["identity"],
        "total_return_definition": contract["total_return_definition"],
        "non_blocking_metadata": contract["non_blocking_metadata"],
        "blocking_gates": contract["blocking_gates"],
        "instruments": contract["instruments"],
    }
    if "range_query" in contract:
        payload["range_query"] = contract["range_query"]
    return payload
