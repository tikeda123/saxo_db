"""Fail-closed C2 Strategy Analysis external-data contract registry."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .connection import project_root


CONTRACT_RELATIVE_PATH = "specs/strategy_external_data_contract_v1.json"
RECEIPT_SCHEMA_RELATIVE_PATH = "specs/strategy_external_data_receipt_v1.schema.json"
MANIFEST_RELATIVE_PATH = "manifests/strategy_external_data_contract_manifest_v1.json"

EXPECTED_ROLES = (
    "CURRENT_NATIVE_MARKET_BAR",
    "SIGNAL_TOTAL_RETURN_DAILY",
    "VALUATION_PRICE_DAILY",
    "COMMON_REGULAR_SESSION_CALENDAR",
    "DISTRIBUTION_DECLARATION",
    "DISTRIBUTION_CASH_TRANSACTION",
    "INSTRUMENT_REFERENCE",
    "PROPOSAL_PRICE_SNAPSHOT",
    "FEE_ESTIMATE_AND_ACTUAL",
    "CURRENCY_AND_AMOUNT_UNIT",
    "REVISION_AND_LATENCY_STATE",
)

ALLOWED_CONTRACT_STATES = {
    "ACTIVE",
    "READY_FOR_READ_ONLY_VALIDATION",
    "CLOSED_SPEC",
    "BLOCKED_EXTERNAL_CONTRACT",
}
ALLOWED_AVAILABILITY_STATES = {
    "AVAILABLE",
    "NOT_EVALUATED",
    "BLOCKED_EXTERNAL_CONTRACT",
}
PUBLIC_AVAILABLE_STATES = {"AVAILABLE", "AVAILABLE_WITH_WARNINGS"}
RECEIPT_ALLOWED_KEYS = {
    "schema_version", "receipt_id", "contract_id", "dataset_role",
    "availability_state", "dataset_id", "provider_id", "provider_data_version",
    "lineage_id", "manifest_sha256", "ordered_content_sha256", "calendar_id",
    "source_as_of", "source_observed_at_utc", "available_at_utc",
    "accepted_at_utc", "expected_by_utc", "published_at_utc", "freshness_state",
    "quality_state", "revision_state", "cost_confidence", "warning_ids",
    "blocker_ids", "values_modified", "interpolation_performed",
    "account_fingerprint", "payload", "receipt_sha256", "supersedes_receipt_id",
}
RECEIPT_REQUIRED_KEYS = {
    "schema_version", "receipt_id", "contract_id", "dataset_role",
    "availability_state", "source_observed_at_utc", "available_at_utc",
    "freshness_state", "quality_state", "revision_state", "cost_confidence",
    "warning_ids", "blocker_ids", "values_modified", "interpolation_performed",
    "payload", "receipt_sha256",
}
_SECRET_KEYS = {
    "access_token", "refresh_token", "authorization", "accountkey", "clientkey",
    "account_key", "client_key", "tradableon", "tradable_on", "password", "secret",
}
ALLOWED_QUALITY_STATES = {
    "PASS",
    "PASS_WITH_WARNINGS",
    "NOT_EVALUATED",
    "FAIL_DATA_QUALITY",
}
ALLOWED_FRESHNESS_STATES = {
    "CURRENT",
    "DELAYED",
    "DATA_NOT_READY",
    "STALE",
    "NOT_EVALUATED_SLA",
    "BLOCKED_INTERFACE_OPERATIONAL",
}


class StrategyExternalContractError(ValueError):
    pass


def canonical_json_sha256(value: Any) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _SECRET_KEYS or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _valid_sha256(value: Any, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_time(value: Any, *, nullable: bool = False) -> datetime | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    try:
        selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyExternalContractError(
            "STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID"
        ) from exc
    if selected.tzinfo is None or selected.utcoffset() != timezone.utc.utcoffset(selected):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    return selected


def validate_strategy_external_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate the checked-in receipt schema and fail closed on unsafe evidence."""

    if set(receipt) - RECEIPT_ALLOWED_KEYS or not RECEIPT_REQUIRED_KEYS <= set(receipt):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("schema_version") != 1 or _contains_secret_key(receipt):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    contract, _ = load_strategy_external_contract()
    selected = next(
        (
            item for item in contract["contracts"]
            if item["contract_id"] == receipt.get("contract_id")
            and item["dataset_role"] == receipt.get("dataset_role")
        ),
        None,
    )
    if selected is None:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_CONTRACT_INVALID")
    if any(
        not isinstance(receipt.get(key), str) or not receipt.get(key)
        for key in ("receipt_id", "contract_id", "dataset_role")
    ):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    observed = _receipt_time(receipt.get("source_observed_at_utc"))
    available_at = _receipt_time(receipt.get("available_at_utc"))
    accepted_at = _receipt_time(receipt.get("accepted_at_utc"), nullable=True)
    _receipt_time(receipt.get("expected_by_utc"), nullable=True)
    _receipt_time(receipt.get("published_at_utc"), nullable=True)
    if available_at is None or observed is None or available_at < observed:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if accepted_at is not None and accepted_at < observed:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("availability_state") not in {
        "AVAILABLE", "AVAILABLE_WITH_WARNINGS", "DATA_NOT_READY",
        "BLOCKED_EXTERNAL_CONTRACT", "BLOCKED_INTERFACE_OPERATIONAL",
        "FAIL_DATA_QUALITY",
    }:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("freshness_state") not in ALLOWED_FRESHNESS_STATES:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("quality_state") not in ALLOWED_QUALITY_STATES:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("revision_state") not in {
        "CURRENT_ACCEPTED", "REVISION_DETECTED", "REVISION_REVIEW_PENDING",
        "REVISION_ACCEPTED_NEXT_DECISION", "SUPERSEDED", "NOT_EVALUATED",
    } or receipt.get("cost_confidence") not in {
        "ACTUAL_BOOKED", "PUBLISHED_SCHEDULE_ESTIMATE", "RESEARCH_MODEL_ONLY",
        "UNKNOWN", "NOT_APPLICABLE",
    }:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    for key in ("warning_ids", "blocker_ids"):
        values = receipt.get(key)
        if not isinstance(values, list) or len(values) != len(set(values)) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if receipt.get("values_modified") is not False or receipt.get("interpolation_performed") is not False:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    if not isinstance(receipt.get("payload"), dict):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    for key in ("manifest_sha256", "ordered_content_sha256"):
        if not _valid_sha256(receipt.get(key), nullable=True):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_SCHEMA_INVALID")
    digest = receipt.get("receipt_sha256")
    without_digest = dict(receipt)
    without_digest.pop("receipt_sha256", None)
    if not _valid_sha256(digest) or digest != canonical_json_sha256(without_digest):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_HASH_INVALID")
    available = receipt.get("availability_state") in PUBLIC_AVAILABLE_STATES
    if available:
        delayed_low_frequency_quote = (
            receipt.get("dataset_role") == "PROPOSAL_PRICE_SNAPSHOT"
            and receipt.get("freshness_state") == "DELAYED"
            and "SIM_DELAYED_QUOTE_ACCEPTED_BY_POLICY"
            in receipt.get("warning_ids", [])
        )
        if (
            receipt.get("accepted_at_utc") is None
            or (
                receipt.get("freshness_state") != "CURRENT"
                and not delayed_low_frequency_quote
            )
            or receipt.get("quality_state") not in {"PASS", "PASS_WITH_WARNINGS"}
            or not receipt.get("provider_id")
            or not receipt.get("lineage_id")
            or not _valid_sha256(receipt.get("ordered_content_sha256"))
        ):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_FAIL_CLOSED_INVALID")
        available_fields = set(receipt) | set(receipt["payload"])
        if not set(selected["required_receipt_fields"]) <= available_fields:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_FIELDS_MISSING")
    if receipt.get("availability_state") == "AVAILABLE_WITH_WARNINGS" and (
        receipt.get("quality_state") != "PASS_WITH_WARNINGS" or not receipt["warning_ids"]
    ):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_RECEIPT_FAIL_CLOSED_INVALID")


def finalize_strategy_external_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    selected = dict(receipt)
    selected.pop("receipt_sha256", None)
    selected["receipt_sha256"] = canonical_json_sha256(selected)
    validate_strategy_external_receipt(selected)
    return selected


def _read_json(relative_path: str) -> tuple[dict[str, Any], str]:
    path = project_root() / relative_path
    if not path.is_file() or path.is_symlink():
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_NOT_VERIFIED")
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrategyExternalContractError(
            "STRATEGY_EXTERNAL_CONTRACT_NOT_VERIFIED"
        ) from exc
    if not isinstance(payload, dict):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_NOT_VERIFIED")
    return payload, hashlib.sha256(content).hexdigest()


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_PATH_INVALID")
    path = Path(value)
    if ".." in path.parts:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_PATH_INVALID")
    return value


def _validate_contract(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_SCHEMA_INVALID")
    if payload.get("bundle_status") != "BLOCKED_EXTERNAL_CONTRACT":
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
    instruments = payload.get("instruments")
    if not isinstance(instruments, list) or len(instruments) != 11:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_INSTRUMENTS_INVALID")
    if len(set(instruments)) != 11 or any(
        not isinstance(item, str) or item != item.upper() for item in instruments
    ):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_INSTRUMENTS_INVALID")

    contracts = payload.get("contracts")
    if not isinstance(contracts, list):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_ROLES_INVALID")
    roles = tuple(item.get("dataset_role") for item in contracts if isinstance(item, dict))
    if roles != EXPECTED_ROLES:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_ROLES_INVALID")

    for item in contracts:
        if item.get("contract_state") not in ALLOWED_CONTRACT_STATES:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
        if item.get("availability_state") not in ALLOWED_AVAILABILITY_STATES:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
        quality = item.get("quality")
        freshness = item.get("freshness")
        if not isinstance(quality, dict) or quality.get("state") not in ALLOWED_QUALITY_STATES:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
        if (
            not isinstance(freshness, dict)
            or freshness.get("state") not in ALLOWED_FRESHNESS_STATES
        ):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
        blocker_ids = item.get("blocker_ids")
        decision_ids = item.get("decision_required_ids")
        if not isinstance(blocker_ids, list) or not isinstance(decision_ids, list):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_STATE_INVALID")
        if item.get("availability_state") == "BLOCKED_EXTERNAL_CONTRACT":
            if not blocker_ids or quality.get("state") == "PASS" or freshness.get("state") == "CURRENT":
                raise StrategyExternalContractError(
                    "STRATEGY_EXTERNAL_CONTRACT_FAIL_CLOSED_INVALID"
                )
            if item.get("dataset_id") is not None or item.get("last_good_receipt_id") is not None:
                raise StrategyExternalContractError(
                    "STRATEGY_EXTERNAL_CONTRACT_FAIL_CLOSED_INVALID"
                )
        required_fields = item.get("required_receipt_fields")
        if not isinstance(required_fields, list) or not required_fields:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_RECEIPT_INVALID")

    security = payload.get("security")
    if not isinstance(security, dict) or any(
        security.get(key) is not expected
        for key, expected in {
            "read_only_api": True,
            "account_identifiers_in_public_receipts": False,
            "tokens_in_public_receipts": False,
            "orders_allowed": False,
            "prechecks_allowed": False,
        }.items()
    ):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_SECURITY_INVALID")


def load_strategy_external_contract() -> tuple[dict[str, Any], str]:
    payload, contract_sha256 = _read_json(CONTRACT_RELATIVE_PATH)
    _validate_contract(payload)

    manifest_path = _safe_relative_path(payload.get("manifest_relative_path"))
    if manifest_path != MANIFEST_RELATIVE_PATH:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
    manifest, manifest_sha256 = _read_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("bundle_id") != payload.get("bundle_id"):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
    expected = {
        CONTRACT_RELATIVE_PATH: contract_sha256,
        RECEIPT_SCHEMA_RELATIVE_PATH: _read_json(RECEIPT_SCHEMA_RELATIVE_PATH)[1],
    }
    observed: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
        relative_path = _safe_relative_path(item.get("relative_path"))
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
        observed[relative_path] = sha256
    if observed != expected:
        raise StrategyExternalContractError("STRATEGY_EXTERNAL_CONTRACT_MANIFEST_INVALID")
    return payload, manifest_sha256


def public_strategy_external_contract() -> dict[str, Any]:
    payload, manifest_sha256 = load_strategy_external_contract()
    return {
        "bundle_id": payload["bundle_id"],
        "bundle_status": payload["bundle_status"],
        "contract_revision": payload["contract_revision"],
        "instruments": payload["instruments"],
        "contracts": payload["contracts"],
        "decision_registry": payload["decision_registry"],
        "manifest_relative_path": payload["manifest_relative_path"],
        "manifest_sha256": manifest_sha256,
        "receipt_schema_relative_path": RECEIPT_SCHEMA_RELATIVE_PATH,
        "security": payload["security"],
    }


def public_strategy_external_status(
    receipt_rows: list[dict[str, Any]] | None = None,
    *,
    migration_applied: bool,
) -> dict[str, Any]:
    contract = public_strategy_external_contract()
    rows_by_role = {
        str(row.get("dataset_role")): row for row in (receipt_rows or [])
    }
    sources: list[dict[str, Any]] = []
    for item in contract["contracts"]:
        observed = rows_by_role.get(item["dataset_role"])
        sources.append(observed if observed is not None else item)
    blocked = [
        item["dataset_role"]
        for item in sources
        if item.get("availability_state") not in PUBLIC_AVAILABLE_STATES
    ]
    warnings = [
        item["dataset_role"]
        for item in sources
        if item.get("availability_state") == "AVAILABLE_WITH_WARNINGS"
    ]
    return {
        "bundle_id": contract["bundle_id"],
        "overall": (
            "BLOCKED_EXTERNAL_CONTRACT" if blocked
            else "AVAILABLE_WITH_WARNINGS" if warnings
            else "AVAILABLE"
        ),
        "migration_status": "APPLIED" if migration_applied else "NOT_APPLIED",
        "manifest_sha256": contract["manifest_sha256"],
        "blocked_roles": blocked,
        "warning_roles": warnings,
        "sources": sources,
    }
