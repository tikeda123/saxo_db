from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from market_db.c2_external_decisions import load_provider_decision_template
from market_db.c2_sim_read_readiness import (
    INPUT_ACK,
    C2SIMReadCredentialSlot,
    C2SIMReadinessError,
    c2_sim_read_readiness,
    load_operator_input_contract,
    safe_existing_auth_status,
)
from tests.test_c2_external_decisions import accepted_operational_gates
from tests.test_c2_sim_oauth import approved_provider_decisions


NOW = datetime(2026, 7, 31, 3, 0, tzinfo=timezone.utc)


def test_checked_in_operator_input_contract_is_loopback_nonpersistent_and_no_start():
    contract = load_operator_input_contract()
    assert contract["transport"]["bind"] == "127.0.0.1"
    assert contract["transport"]["csrf_required"] is True
    assert contract["transport"]["browser_storage_allowed"] is False
    assert contract["fields"][0]["type"] == "password"
    assert "does not expose" in contract["start_contract"]


def test_ephemeral_credential_slot_is_single_use_redacted_and_expires():
    current = [NOW]
    slot = C2SIMReadCredentialSlot(clock=lambda: current[0])
    assert "super-secret" not in repr(slot)
    prepared = slot.prepare(
        {
            "access_token": "super-secret",
            "lease_minutes": 2,
            "contract_ack": INPUT_ACK,
        }
    )
    assert prepared["status"] == "EPHEMERAL_CREDENTIAL_READY"
    assert prepared["credential_persisted"] is False
    assert "super-secret" not in json.dumps(prepared)
    token, expiry, fingerprint_key = slot.take_once()
    assert token == "super-secret"
    assert expiry == NOW + timedelta(minutes=2)
    assert len(fingerprint_key) == 32
    assert slot.status()["status"] == "EMPTY"
    with pytest.raises(C2SIMReadinessError, match="CREDENTIAL_NOT_READY"):
        slot.take_once()

    slot.prepare(
        {"access_token": "expires", "lease_minutes": 1, "contract_ack": INPUT_ACK}
    )
    current[0] = NOW + timedelta(minutes=2)
    assert slot.status()["status"] == "EMPTY"


def test_readiness_stops_on_missing_initial_oauth_without_api_work():
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_CONFIG_MISSING", "token_values_exposed": False},
        credential_slot_status=C2SIMReadCredentialSlot(clock=lambda: NOW).status(),
    )
    assert result["status"] == "STOP_INITIAL_OAUTH_REQUIRED"
    assert result["auth_ready"] is False
    assert result["manual_credential_input_allowed"] is False
    assert result["automatic_start_allowed"] is False
    assert result["saxo_api_gets_performed"] == 0
    assert result["receipt_registration_performed"] is False
    assert sum(item["planned_calls"] for item in result["allow_list_get_plan"]) == 15
    assert result["workflow_steps"][0]["status"] == "BLOCKED_CONFIG"
    assert result["workflow_steps"][1]["id"] == "SIM_OBSERVATION_START"
    assert result["workflow_steps"][1]["status"] == "BLOCKED"
    assert result["workflow_steps"][3]["status"] == "PROHIBITED"


def test_accepted_gate_still_requires_initial_oauth_and_never_enables_token_paste():
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_CONFIG_MISSING"},
        credential_slot_status=C2SIMReadCredentialSlot(clock=lambda: NOW).status(),
        operational_gates=accepted_operational_gates(),
        provider_decisions=load_provider_decision_template(),
    )
    assert result["status"] == "STOP_INITIAL_OAUTH_REQUIRED"
    assert result["manual_credential_input_allowed"] is False
    assert result["manual_credential_input_deprecated"] is True
    assert result["explicit_start_required"] is True
    assert result["automatic_start_allowed"] is False


def test_oauth_connection_is_available_before_provider_and_gate_decisions():
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_LOGIN_REQUIRED"},
        credential_slot_status=C2SIMReadCredentialSlot(clock=lambda: NOW).status(),
    )
    assert result["status"] == "STOP_INITIAL_OAUTH_REQUIRED"
    assert result["oauth_connection_allowed"] is True
    assert result["workflow_steps"][0]["status"] == "AVAILABLE"
    assert result["workflow_steps"][1]["status"] == "BLOCKED"
    assert result["workflow_steps"][2]["status"] == "DECISION_REQUIRED"
    assert result["c2_data_execution_allowed"] is False
    assert result["periodic_execution_allowed"] is False
    assert result["saxo_api_gets_performed"] == 0


def test_prepared_legacy_credential_does_not_bypass_initial_oauth():
    slot = C2SIMReadCredentialSlot(clock=lambda: NOW)
    slot.prepare(
        {"access_token": "ephemeral", "lease_minutes": 5, "contract_ack": INPUT_ACK}
    )
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_CONFIG_MISSING"},
        credential_slot_status=slot.status(),
        operational_gates=accepted_operational_gates(),
        provider_decisions=load_provider_decision_template(),
    )
    assert result["status"] == "STOP_INITIAL_OAUTH_REQUIRED"
    assert result["auth_ready"] is False
    assert result["automatic_start_allowed"] is False
    assert result["orders_allowed"] is False
    assert result["prechecks_allowed"] is False


def test_auth_ready_enables_initial_observation_while_provider_decisions_remain_downstream():
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_READY"},
        credential_slot_status=C2SIMReadCredentialSlot(clock=lambda: NOW).status(),
        operational_gates=accepted_operational_gates(),
        provider_decisions=load_provider_decision_template(),
    )
    assert result["status"] == "READY_FOR_SIM_OBSERVATION"
    assert result["credential_mode"] == "OAUTH_PKCE_KEYCHAIN_ROTATING_REFRESH"
    assert result["automatic_refresh_allowed"] is True
    assert result["automatic_start_allowed"] is False
    assert result["sim_observation_start_allowed"] is True
    assert result["c2_data_execution_allowed"] is False
    assert result["sim_allocation_paper_evaluation_allowed"] is False
    assert result["live_order_eligibility_allowed"] is False
    assert result["workflow_steps"][0]["status"] == "COMPLETE"
    assert result["workflow_steps"][1]["status"] == "READY"
    assert result["workflow_steps"][2]["status"] == "DECISION_REQUIRED"
    assert result["workflow_steps"][3]["status"] == "PROHIBITED"
    assert "SIGNAL_TOTAL_RETURN_DAILY provider selection" in result[
        "non_blocking_for_initial_observation"
    ]
    assert result["refresh_credential_storage"] == "macOS Keychain only"
    assert result["access_token_persistence_allowed"] is False


def test_approved_downstream_decisions_do_not_enable_allocation_or_orders_implicitly():
    result = c2_sim_read_readiness(
        auth_status={"status": "AUTH_READY"},
        credential_slot_status=C2SIMReadCredentialSlot(clock=lambda: NOW).status(),
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
    )
    assert result["status"] == "READY_FOR_SIM_OBSERVATION"
    assert result["provider_and_gate_decisions_ready"] is True
    assert result["c2_data_execution_allowed"] is False
    assert result["sim_observation_start_allowed"] is True
    assert result["workflow_steps"][1]["status"] == "READY"
    assert result["workflow_steps"][2]["status"] == "COMPLETE"
    assert result["sim_allocation_paper_evaluation_allowed"] is False
    assert result["workflow_steps"][3]["status"] == "PROHIBITED"
    assert result["automatic_start_allowed"] is False


def test_existing_auth_readiness_drops_all_manager_details_and_never_starts_oauth():
    class Manager:
        def __init__(self, _config):
            pass

        def status(self):
            return {
                "status": "AUTH_READY",
                "environment": "SIM",
                "access_token": "must-not-escape",
                "refresh_token": "must-not-escape",
                "account_identifier": "must-not-escape",
            }

    result = safe_existing_auth_status(
        config_factory=lambda **_: object(), manager_factory=Manager
    )
    assert result == {
        "status": "AUTH_READY",
        "environment": "SIM",
        "token_values_exposed": False,
        "credential_values_saved_by_readiness": False,
        "oauth_started": False,
        "saxo_api_gets_performed": 0,
        "orders_or_prechecks_sent": 0,
    }
    assert "must-not-escape" not in json.dumps(result)
