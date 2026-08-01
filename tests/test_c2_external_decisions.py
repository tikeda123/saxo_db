from __future__ import annotations

from copy import deepcopy
import json

import pytest

from market_db.c2_external_decisions import (
    C2DecisionError,
    load_operational_gate_template,
    load_operational_gates,
    load_provider_decision_template,
    save_operational_gates,
    save_provider_decisions,
    validate_operational_gates,
    validate_provider_decisions,
)


def accepted_operational_gates():
    value = deepcopy(load_operational_gate_template())
    value.update(
        {
            "status": "ACCEPTED",
            "accepted_by": "C2 data owner",
            "accepted_at_utc": "2026-07-31T02:00:00Z",
        }
    )
    value["account_context"]["accepted_base_currencies"] = ["USD"]
    value["quote"].update(
        {
            "evaluation_mode": "LOW_FREQUENCY_DELAYED_OR_DAILY",
            "max_quote_age_seconds": 90_000,
            "max_atomic_span_seconds": 90_000,
            "max_delayed_by_minutes": 60,
            "allow_sim_delayed_quotes": True,
            "accepted_price_types": ["Indicative", "Tradable"],
            "require_two_sided_bid_ask": False,
        }
    )
    value["fee"]["unknown_policy"] = "BLOCK_CONSUMER"
    value["distribution_revision"].update(
        {
            "issuer_revision_lookback_business_days": 5,
            "cash_correction_lookback_calendar_days": 60,
            "require_negative_event_state": True,
        }
    )
    value["sla"]["role_max_lag_seconds"] = {
        "PROPOSAL_PRICE_SNAPSHOT": 10,
        "INSTRUMENT_REFERENCE": 86400,
    }
    return value


def test_checked_in_decision_templates_are_valid_but_not_accepted():
    provider = load_provider_decision_template()
    gates = load_operational_gate_template()
    with pytest.raises(C2DecisionError, match="PROVIDER_DECISION_REQUIRED"):
        validate_provider_decisions(provider, require_approved=True)
    with pytest.raises(C2DecisionError, match="OPERATIONAL_GATE_NOT_ACCEPTED"):
        validate_operational_gates(gates, require_accepted=True)


def test_accepted_operational_gate_requires_all_numeric_and_policy_fields():
    value = accepted_operational_gates()
    validate_operational_gates(value, require_accepted=True)
    value["quote"]["max_quote_age_seconds"] = None
    with pytest.raises(C2DecisionError, match="QUOTE_THRESHOLD_INVALID"):
        validate_operational_gates(value, require_accepted=True)


def test_accepted_operational_gate_requires_utc_approval_time():
    value = accepted_operational_gates()
    value["accepted_at_utc"] = "2026-07-31T11:00:00+09:00"
    with pytest.raises(C2DecisionError, match="APPROVAL_MISSING"):
        validate_operational_gates(value, require_accepted=True)


def test_provider_cannot_be_approved_without_contract_evidence():
    value = load_provider_decision_template()
    value["decisions"][0]["status"] = "APPROVED"
    with pytest.raises(C2DecisionError, match="EVIDENCE_MISSING"):
        validate_provider_decisions(value, require_approved=False)


def test_runtime_operational_gate_overrides_template_without_editing_repository(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime/c2/operational_gate_decision.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        json.dumps(accepted_operational_gates(), sort_keys=True), encoding="utf-8"
    )
    monkeypatch.setattr("market_db.c2_external_decisions.project_root", lambda: tmp_path)
    value = load_operational_gates()
    assert value["status"] == "ACCEPTED"


def test_runtime_decision_writers_are_atomic_owner_only_and_do_not_edit_templates(
    tmp_path, monkeypatch
):
    provider = load_provider_decision_template()
    gates = accepted_operational_gates()
    provider_template_before = json.dumps(provider, sort_keys=True)
    monkeypatch.setattr("market_db.c2_external_decisions.project_root", lambda: tmp_path)

    saved_provider = save_provider_decisions(provider)
    saved_gates = save_operational_gates(gates)

    provider_path = tmp_path / ".runtime/c2/provider_decision.json"
    gate_path = tmp_path / ".runtime/c2/operational_gate_decision.json"
    assert provider_path.is_file()
    assert gate_path.is_file()
    assert provider_path.stat().st_mode & 0o777 == 0o600
    assert gate_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(provider_path.read_text()) == saved_provider
    assert json.loads(gate_path.read_text()) == saved_gates
    assert json.dumps(provider, sort_keys=True) == provider_template_before
    assert not list((tmp_path / ".runtime/c2").glob("*.tmp"))
