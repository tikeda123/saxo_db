"""Readiness and in-memory credential slot for C2 SIM read observations.

This module performs no OAuth flow and no Saxo request.  It only validates the
local decision/config state and a future loopback UI credential envelope.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .c2_external_decisions import (
    C2DecisionError,
    OPERATIONAL_GATE_TEMPLATE,
    PROVIDER_DECISION_TEMPLATE,
    load_operational_gates,
    load_provider_decisions,
    validate_operational_gates,
)
from .c2_sim_read_session import (
    ETF11_KEYS,
    OBSERVATION_CONTRACT_ID,
    READ_ENDPOINT_IDS,
    SESSION_CONTRACT_ID,
    load_ephemeral_session_contract,
    load_observation_start_contract,
)
from .c2_sim_oauth import C2_KILL_SWITCH_RELATIVE_PATH, C2_OAUTH_CONTRACT_ID
from .connection import project_root
from .saxo_auth import OAuthConfig, SaxoAuthError, SaxoOAuthManager


MAX_MANUAL_LEASE_MINUTES = 15
INPUT_ACK = "SIM_READ_ONLY_NO_ORDER_NO_PERSISTENCE"
INPUT_CONTRACT_RELATIVE_PATH = "specs/c2_sim_read_operator_input_contract_v1.json"


class C2SIMReadinessError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_operator_input_contract() -> dict[str, Any]:
    path = project_root() / INPUT_CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_CONTRACT_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_CONTRACT_NOT_VERIFIED") from exc
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("input_contract_id") == "c2_sim_read_operator_input_v1"
        and value.get("transport", {}).get("bind") == "127.0.0.1"
        and value.get("transport", {}).get("same_origin_required") is True
        and value.get("transport", {}).get("csrf_required") is True
        and value.get("transport", {}).get("browser_storage_allowed") is False
        and value.get("start_contract", "").startswith("a separate")
    ):
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_CONTRACT_NOT_VERIFIED")
    field_names = [item.get("name") for item in value.get("fields", []) if isinstance(item, dict)]
    if field_names != ["access_token", "lease_minutes", "contract_ack"]:
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_CONTRACT_NOT_VERIFIED")
    return value


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_ui_credential_input(payload: Mapping[str, Any]) -> tuple[str, int]:
    if set(payload) != {"access_token", "lease_minutes", "contract_ack"}:
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_SCHEMA_INVALID")
    token = payload.get("access_token")
    lease_minutes = payload.get("lease_minutes")
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token) > 8_192
        or any(ord(character) < 32 for character in token)
        or isinstance(lease_minutes, bool)
        or not isinstance(lease_minutes, int)
        or not 1 <= lease_minutes <= MAX_MANUAL_LEASE_MINUTES
        or payload.get("contract_ack") != INPUT_ACK
    ):
        raise C2SIMReadinessError("C2_SIM_READ_INPUT_SCHEMA_INVALID")
    return token.strip(), lease_minutes


class C2SIMReadCredentialSlot:
    """Keep one future access token in process memory and consume it once."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._fingerprint_key: bytes | None = None
        self._expires_at_utc: datetime | None = None

    def __repr__(self) -> str:
        return "C2SIMReadCredentialSlot(access_token=<redacted>, persistence=False)"

    def _clear_unlocked(self) -> None:
        self._token = None
        self._fingerprint_key = None
        self._expires_at_utc = None

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._clear_unlocked()
        return self.status()

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        token, lease_minutes = validate_ui_credential_input(payload)
        now = self._clock().astimezone(timezone.utc)
        with self._lock:
            self._clear_unlocked()
            self._token = token
            self._fingerprint_key = secrets.token_bytes(32)
            self._expires_at_utc = now + timedelta(minutes=lease_minutes)
        token = ""
        return self.status()

    def status(self) -> dict[str, Any]:
        now = self._clock().astimezone(timezone.utc)
        with self._lock:
            if self._expires_at_utc is not None and self._expires_at_utc <= now:
                self._clear_unlocked()
            present = self._token is not None
            expiry = self._expires_at_utc
        return {
            "status": "EPHEMERAL_CREDENTIAL_READY" if present else "EMPTY",
            "credential_present": present,
            "expires_at_utc": None if expiry is None else _utc_text(expiry),
            "credential_persisted": False,
            "credential_values_exposed": False,
            "single_use": True,
        }

    def take_once(self) -> tuple[str, datetime, bytes]:
        now = self._clock().astimezone(timezone.utc)
        with self._lock:
            if (
                self._token is None
                or self._fingerprint_key is None
                or self._expires_at_utc is None
                or self._expires_at_utc <= now
            ):
                self._clear_unlocked()
                raise C2SIMReadinessError("C2_SIM_READ_EPHEMERAL_CREDENTIAL_NOT_READY")
            selected = (self._token, self._expires_at_utc, self._fingerprint_key)
            self._clear_unlocked()
        return selected


def c2_sim_read_readiness(
    *,
    auth_status: Mapping[str, Any],
    credential_slot_status: Mapping[str, Any],
    operational_gates: Mapping[str, Any] | None = None,
    provider_decisions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a sanitized no-I/O readiness result for the local Operator UI."""

    load_ephemeral_session_contract()
    observation_contract = load_observation_start_contract()
    load_operator_input_contract()
    gates = dict(operational_gates or load_operational_gates())
    providers = dict(provider_decisions or load_provider_decisions())
    try:
        validate_operational_gates(gates, require_accepted=True)
        gate_status = "ACCEPTED"
    except C2DecisionError as exc:
        gate_status = str(exc)
    provider_statuses = {
        str(item["dataset_role"]): str(item["status"])
        for item in providers["decisions"]
    }
    provider_ready = all(value == "APPROVED" for value in provider_statuses.values())
    oauth_status = str(auth_status.get("status") or "AUTH_CONFIG_MISSING")
    oauth_ready = oauth_status == "AUTH_READY"
    kill_switch_engaged = (project_root() / C2_KILL_SWITCH_RELATIVE_PATH).exists()
    auth_ready = oauth_ready and not kill_switch_engaged
    gate_ready = gate_status == "ACCEPTED"
    decisions_ready = gate_ready and provider_ready
    oauth_connection_allowed = (
        not kill_switch_engaged
        and oauth_status in {"AUTH_LOGIN_REQUIRED", "AUTH_READY"}
    )
    sim_observation_start_allowed = auth_ready
    if kill_switch_engaged:
        status = "STOP_KILL_SWITCH_ENGAGED"
    elif not auth_ready:
        status = "STOP_INITIAL_OAUTH_REQUIRED"
    else:
        status = "READY_FOR_SIM_OBSERVATION"
    user_actions: list[str] = []
    if not auth_ready:
        user_actions.append(
            "Operator UIのSaxo OAuth接続から初回だけSIM認証する。以後はmacOS Keychainのrefresh credentialを自動rotationする"
        )
    if not gate_ready:
        user_actions.append(
            f"{OPERATIONAL_GATE_TEMPLATE}はSIM allocation/paper evaluation前に決定する。初回SIM観測は止めない"
        )
    if any(value != "APPROVED" for value in provider_statuses.values()):
        user_actions.append(
            f"{PROVIDER_DECISION_TEMPLATE}の2 roleはSIM allocation/paper evaluation前に証拠付きAPPROVEDにする。初回SIM観測は止めない"
        )
    workflow_steps = [
        {
            "step": 1,
            "id": "OAUTH_CONNECTION",
            "status": (
                "COMPLETE"
                if auth_ready
                else "AVAILABLE" if oauth_connection_allowed else "BLOCKED_CONFIG"
            ),
            "allows": ["SIM OAuth authorization", "Keychain refresh rotation", "no-I/O readiness"],
            "forbids": ["Saxo API GET", "receipt registration", "periodic execution", "order"],
        },
        {
            "step": 2,
            "id": "SIM_OBSERVATION_START",
            "status": "READY" if sim_observation_start_allowed else "BLOCKED",
            "allows": ["one explicit 15-call GET-only technical observation"],
            "forbids": ["automatic start", "raw persistence", "receipt registration", "periodic execution", "order"],
        },
        {
            "step": 3,
            "id": "PROVIDER_AND_OPERATIONAL_GATE_DECISION",
            "status": "COMPLETE" if decisions_ready else "DECISION_REQUIRED",
            "stage": "SIM_ALLOCATION_PAPER_EVALUATION",
            "allows": ["local downstream decision document validation"],
            "forbids": ["allocation, PnL, paper evaluation until downstream contracts and receipts are accepted"],
        },
        {
            "step": 4,
            "id": "LIVE_ORDER_ELIGIBILITY",
            "status": "PROHIBITED",
            "allows": [],
            "forbids": ["live order", "SIM order", "precheck", "account mutation"],
        },
    ]
    return {
        "status": status,
        "auth_status": oauth_status,
        "auth_ready": auth_ready,
        "oauth_configuration": dict(auth_status.get("configuration") or {}),
        "credential_slot": dict(credential_slot_status),
        "operational_gate_status": gate_status,
        "provider_decision_statuses": provider_statuses,
        "provider_decisions_ready": provider_ready,
        "provider_and_gate_decisions_ready": decisions_ready,
        "environment": "SIM",
        "session_contract_id": SESSION_CONTRACT_ID,
        "observation_contract_id": OBSERVATION_CONTRACT_ID,
        "oauth_contract_id": C2_OAUTH_CONTRACT_ID,
        "credential_mode": "OAUTH_PKCE_KEYCHAIN_ROTATING_REFRESH",
        "instrument_keys": list(ETF11_KEYS),
        "allow_list_get_plan": [
            {"endpoint_id": "session_capabilities", "planned_calls": 1},
            {"endpoint_id": "accounts_me", "planned_calls": 1},
            {"endpoint_id": "balances_me", "planned_calls": 1},
            {"endpoint_id": "instrument_detail", "planned_calls": 11},
            {"endpoint_id": "info_prices", "planned_calls": 1, "atomic_instrument_count": 11},
            {"endpoint_id": "historical_transactions", "planned_calls": 0, "reason": "separate date-bounded receipt run after distribution gate acceptance"},
        ],
        "allow_list_endpoint_ids": list(READ_ENDPOINT_IDS),
        "manual_credential_input_allowed": False,
        "manual_credential_input_deprecated": True,
        "oauth_connection_allowed": oauth_connection_allowed,
        "read_only_auth_readiness_allowed": auth_ready,
        "sim_observation_start_allowed": sim_observation_start_allowed,
        # Compatibility field for the former combined execution gate.  Keep it
        # fail-closed: only the narrowly scoped observation action above is ready.
        "c2_data_execution_allowed": False,
        "sim_allocation_paper_evaluation_allowed": False,
        "live_order_eligibility_allowed": False,
        "downstream_contract_decisions_ready": decisions_ready,
        "non_blocking_for_initial_observation": list(
            observation_contract["non_blocking_for_initial_observation"]
        ),
        "explicit_start_required": True,
        "automatic_start_allowed": False,
        "explicit_start_action_available": True,
        "automatic_refresh_allowed": oauth_ready and not kill_switch_engaged,
        "periodic_execution_allowed": False,
        "orders_allowed": False,
        "prechecks_allowed": False,
        "account_mutations_allowed": False,
        "access_token_persistence_allowed": False,
        "refresh_credential_storage": "macOS Keychain only",
        "kill_switch_engaged": kill_switch_engaged,
        "saxo_api_gets_performed": 0,
        "receipt_registration_performed": False,
        "workflow_steps": workflow_steps,
        "user_actions": user_actions,
    }


def safe_existing_auth_status(
    *,
    callback_port: int = 8765,
    config_factory: Callable[..., OAuthConfig] = OAuthConfig.from_environment,
    manager_factory: Callable[[OAuthConfig], SaxoOAuthManager] = SaxoOAuthManager,
) -> dict[str, Any]:
    """Inspect local OAuth readiness without starting OAuth or exposing values."""

    try:
        manager = manager_factory(config_factory(callback_port=callback_port))
        observed = manager.status()
        status = str(observed.get("status") or "AUTH_CONFIG_MISSING")
        environment = str(observed.get("environment") or "SIM")
    except SaxoAuthError as exc:
        status = exc.code
        environment = "SIM"
    return {
        "status": status,
        "environment": environment,
        "token_values_exposed": False,
        "credential_values_saved_by_readiness": False,
        "oauth_started": False,
        "saxo_api_gets_performed": 0,
        "orders_or_prechecks_sent": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C2 SIM read no-I/O readiness")
    parser.add_argument("command", choices=("status",))
    parser.add_argument("--callback-port", type=int, default=8765)
    args = parser.parse_args(argv)
    result = c2_sim_read_readiness(
        auth_status=safe_existing_auth_status(callback_port=args.callback_port),
        credential_slot_status=C2SIMReadCredentialSlot().status(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
