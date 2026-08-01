"""Validate user-owned C2 provider and operational-gate decision documents."""

from __future__ import annotations

import json
import os
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .connection import project_root


PROVIDER_DECISION_TEMPLATE = "specs/c2_external_provider_decision_template_v1.json"
OPERATIONAL_GATE_TEMPLATE = "specs/c2_external_operational_gate_decision_template_v1.json"
PROVIDER_DECISION_RUNTIME = ".runtime/c2/provider_decision.json"
OPERATIONAL_GATE_RUNTIME = ".runtime/c2/operational_gate_decision.json"
ETF11 = [
    "SPY", "IWM", "EFA", "EEM", "VNQ", "SHY", "IEF", "TLT", "TIP", "LQD", "GLD"
]


class C2DecisionError(ValueError):
    pass


def _load(relative_path: str) -> dict[str, Any]:
    path = project_root() / relative_path
    if not path.is_file() or path.is_symlink():
        raise C2DecisionError("C2_DECISION_DOCUMENT_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise C2DecisionError("C2_DECISION_DOCUMENT_NOT_VERIFIED") from exc
    if not isinstance(value, dict):
        raise C2DecisionError("C2_DECISION_DOCUMENT_NOT_VERIFIED")
    return value


def _load_runtime_or_template(runtime_path: str, template_path: str) -> dict[str, Any]:
    selected = project_root() / runtime_path
    return _load(runtime_path) if selected.exists() else _load(template_path)


def load_provider_decision_template() -> dict[str, Any]:
    value = _load(PROVIDER_DECISION_TEMPLATE)
    validate_provider_decisions(value, require_approved=False)
    return value


def load_operational_gate_template() -> dict[str, Any]:
    value = _load(OPERATIONAL_GATE_TEMPLATE)
    validate_operational_gates(value, require_accepted=False)
    return value


def load_provider_decisions() -> dict[str, Any]:
    value = _load_runtime_or_template(PROVIDER_DECISION_RUNTIME, PROVIDER_DECISION_TEMPLATE)
    validate_provider_decisions(value, require_approved=False)
    return value


def load_operational_gates() -> dict[str, Any]:
    value = _load_runtime_or_template(OPERATIONAL_GATE_RUNTIME, OPERATIONAL_GATE_TEMPLATE)
    validate_operational_gates(value, require_accepted=False)
    return value


def _write_runtime(relative_path: str, value: Mapping[str, Any]) -> None:
    """Atomically write one user-owned runtime decision with owner-only mode."""

    root = project_root().resolve()
    selected = root / relative_path
    selected_parent = selected.parent
    selected_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if selected.is_symlink() or selected_parent.resolve() != selected_parent:
        raise C2DecisionError("C2_DECISION_RUNTIME_PATH_NOT_VERIFIED")
    try:
        selected_parent.resolve().relative_to(root)
    except ValueError as exc:
        raise C2DecisionError("C2_DECISION_RUNTIME_PATH_NOT_VERIFIED") from exc
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = selected_parent / f".{selected.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, selected)
        os.chmod(selected, 0o600)
    except OSError as exc:
        raise C2DecisionError("C2_DECISION_RUNTIME_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save_provider_decisions(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = deepcopy(dict(value))
    validate_provider_decisions(selected, require_approved=False)
    _write_runtime(PROVIDER_DECISION_RUNTIME, selected)
    return selected


def save_operational_gates(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = deepcopy(dict(value))
    validate_operational_gates(selected, require_accepted=False)
    _write_runtime(OPERATIONAL_GATE_RUNTIME, selected)
    return selected


def _positive_number(value: Any, *, allow_zero: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value >= 0 if allow_zero else value > 0


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return selected.tzinfo is not None and selected.utcoffset() == timezone.utc.utcoffset(selected)


def validate_provider_decisions(
    value: Mapping[str, Any], *, require_approved: bool
) -> None:
    if value.get("schema_version") != 1 or value.get("decision_type") != "C2_EXTERNAL_PROVIDER_SELECTION":
        raise C2DecisionError("C2_PROVIDER_DECISION_SCHEMA_INVALID")
    decisions = value.get("decisions")
    if not isinstance(decisions, list) or {
        item.get("dataset_role") for item in decisions if isinstance(item, Mapping)
    } != {"SIGNAL_TOTAL_RETURN_DAILY", "VALUATION_PRICE_DAILY"}:
        raise C2DecisionError("C2_PROVIDER_DECISION_SCHEMA_INVALID")
    required_when_approved = {
        "provider_id", "provider_legal_name", "source_contract_reference",
        "license_and_redistribution_status", "definition_id", "instrument_set",
        "coverage_start", "publication_sla", "revision_policy",
        "lineage_method", "content_identity_method", "approved_by",
        "approved_at_utc",
    }
    for decision in decisions:
        if not isinstance(decision, Mapping) or decision.get("status") not in {
            "DECISION_REQUIRED", "APPROVED", "REJECTED"
        }:
            raise C2DecisionError("C2_PROVIDER_DECISION_SCHEMA_INVALID")
        if decision.get("instrument_set") not in (None, ETF11):
            raise C2DecisionError("C2_PROVIDER_DECISION_INSTRUMENT_SET_INVALID")
        if decision["status"] == "APPROVED" and any(
            decision.get(field) in (None, "", []) for field in required_when_approved
        ):
            raise C2DecisionError("C2_PROVIDER_DECISION_EVIDENCE_MISSING")
        if decision["status"] == "APPROVED" and not _is_utc_timestamp(
            decision.get("approved_at_utc")
        ):
            raise C2DecisionError("C2_PROVIDER_DECISION_APPROVAL_TIME_INVALID")
    if require_approved and any(item["status"] != "APPROVED" for item in decisions):
        raise C2DecisionError("BLOCKED_EXTERNAL_CONTRACT_PROVIDER_DECISION_REQUIRED")


def validate_operational_gates(
    value: Mapping[str, Any], *, require_accepted: bool
) -> None:
    if value.get("schema_version") != 1 or value.get("decision_type") != "C2_OPERATIONAL_GATES":
        raise C2DecisionError("C2_OPERATIONAL_GATE_SCHEMA_INVALID")
    status = value.get("status")
    if status not in {"DECISION_REQUIRED", "ACCEPTED", "REJECTED"}:
        raise C2DecisionError("C2_OPERATIONAL_GATE_SCHEMA_INVALID")
    quote = value.get("quote")
    account = value.get("account_context")
    fee = value.get("fee")
    distribution = value.get("distribution_revision")
    sla = value.get("sla")
    if not all(isinstance(item, Mapping) for item in (quote, account, fee, distribution, sla)):
        raise C2DecisionError("C2_OPERATIONAL_GATE_SCHEMA_INVALID")
    if fee.get("unknown_policy") not in {
        None, "BLOCK_CONSUMER", "AVAILABLE_WITH_WARNING_SIM_RESEARCH_ONLY"
    }:
        raise C2DecisionError("C2_OPERATIONAL_GATE_SCHEMA_INVALID")
    if status == "ACCEPTED":
        if not (
            quote.get("evaluation_mode") == "LOW_FREQUENCY_DELAYED_OR_DAILY"
            and _positive_number(quote.get("max_quote_age_seconds"))
            and _positive_number(quote.get("max_atomic_span_seconds"))
            and _positive_number(quote.get("max_delayed_by_minutes"), allow_zero=True)
            and quote.get("allow_sim_delayed_quotes") is True
            and isinstance(quote.get("accepted_price_types"), list)
            and quote["accepted_price_types"]
            and all(isinstance(item, str) and item for item in quote["accepted_price_types"])
            and len(quote["accepted_price_types"]) == len(set(quote["accepted_price_types"]))
            and "Indicative" in quote["accepted_price_types"]
            and quote.get("require_two_sided_bid_ask") is False
        ):
            raise C2DecisionError("C2_OPERATIONAL_GATE_QUOTE_THRESHOLD_INVALID")
        if not (
            account.get("environment") == "SIM"
            and account.get("require_all_11_etfs") is True
            and isinstance(account.get("accepted_base_currencies"), list)
            and account["accepted_base_currencies"]
            and all(
                isinstance(item, str) and item and item == item.upper()
                for item in account["accepted_base_currencies"]
            )
            and len(account["accepted_base_currencies"])
            == len(set(account["accepted_base_currencies"]))
        ):
            raise C2DecisionError("C2_OPERATIONAL_GATE_ACCOUNT_CONTEXT_INVALID")
        if fee.get("unknown_policy") is None:
            raise C2DecisionError("C2_OPERATIONAL_GATE_FEE_POLICY_INVALID")
        if not (
            _positive_number(distribution.get("issuer_revision_lookback_business_days"))
            and _positive_number(distribution.get("cash_correction_lookback_calendar_days"))
            and isinstance(distribution.get("require_negative_event_state"), bool)
        ):
            raise C2DecisionError("C2_OPERATIONAL_GATE_DISTRIBUTION_POLICY_INVALID")
        thresholds = sla.get("role_max_lag_seconds")
        if not isinstance(thresholds, Mapping) or not thresholds or any(
            not isinstance(role, str) or not role or not _positive_number(seconds)
            for role, seconds in thresholds.items()
        ):
            raise C2DecisionError("C2_OPERATIONAL_GATE_SLA_INVALID")
        if not (
            value.get("accepted_by")
            and _is_utc_timestamp(value.get("accepted_at_utc"))
            and sla.get("late_state") == "DATA_NOT_READY"
            and sla.get("interface_failure_state") == "BLOCKED_INTERFACE_OPERATIONAL"
            and sla.get("quality_failure_state") == "FAIL_DATA_QUALITY"
        ):
            raise C2DecisionError("C2_OPERATIONAL_GATE_APPROVAL_MISSING")
    if require_accepted and status != "ACCEPTED":
        raise C2DecisionError("BLOCKED_EXTERNAL_CONTRACT_OPERATIONAL_GATE_NOT_ACCEPTED")
