from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from market_db.c2_sim_oauth import (
    C2AccountBinding,
    C2OAuthRefreshKeeper,
    C2SIMOAuthCredentialAdapter,
    READ_ONLY_ACK,
    load_oauth_contract,
)
from market_db.c2_sim_read_session import (
    C2SIMReadContractBlocked,
    C2SIMReadOperationalError,
    ObservationPreflightEvidence,
    PreflightEvidence,
)
from market_db.c2_external_decisions import load_provider_decision_template
from market_db.saxo_auth import AccessLease, OAuthConfig
from tests.test_c2_external_decisions import accepted_operational_gates


NOW = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
ETF11 = ["SPY", "IWM", "EFA", "EEM", "VNQ", "SHY", "IEF", "TLT", "TIP", "LQD", "GLD"]


def approved_provider_decisions():
    value = deepcopy(load_provider_decision_template())
    for decision in value["decisions"]:
        decision.update(
            {
                "status": "APPROVED",
                "provider_id": "approved-provider",
                "provider_legal_name": "Approved Provider",
                "source_contract_reference": "contract-evidence",
                "license_and_redistribution_status": "approved-for-local-research",
                "definition_id": f"definition-{decision['dataset_role'].lower()}",
                "instrument_set": ETF11,
                "coverage_start": "2005-01-01",
                "publication_sla": "T+1",
                "revision_policy": "append-only-receipt",
                "lineage_method": "provider-id-and-hash",
                "content_identity_method": "sha256",
                "approved_by": "C2 data owner",
                "approved_at_utc": "2026-07-31T04:00:00Z",
            }
        )
    return value


class MemoryStore:
    def __init__(self):
        self.values: dict[str, bytes] = {}

    def get(self, account: str) -> bytes | None:
        return self.values.get(account)

    def put(self, account: str, value: bytes) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


class FakeManager:
    def __init__(self, config: OAuthConfig, *, status: str = "AUTH_READY"):
        self.config = config
        self.auth_status = status
        self.access_calls: list[bool] = []
        self.disconnected = False

    def status(self):
        return {
            "status": self.auth_status,
            "access_token": "must-not-escape",
            "refresh_token": "must-not-escape",
        }

    def access_lease(self, *, force_refresh: bool = False):
        self.access_calls.append(force_refresh)
        if self.auth_status != "AUTH_READY":
            raise AssertionError("access lease must not be requested when auth is not ready")
        return AccessLease("access-secret", NOW.timestamp() + 600)

    def disconnect(self):
        self.disconnected = True
        self.auth_status = "AUTH_LOGIN_REQUIRED"
        return {"status": self.auth_status}


class FakeSession:
    def __init__(self, access_token, *, fingerprint_key, account_fingerprint, **_kwargs):
        self.received_token = access_token
        self.fingerprint_key = fingerprint_key
        self.account_fingerprint = account_fingerprint
        self.request_count = 3
        self.write_request_count = 0
        self.events = []
        self.closed = False

    def preflight(self, _gates):
        return PreflightEvidence(
            observed_at_utc="2026-07-31T05:00:00Z",
            account_fingerprint=self.account_fingerprint,
            data_level="Full",
            account_count=1,
            account_currency="USD",
            currency_decimals=2,
        )

    def instrument_reference(self):
        return []

    def preflight_observation(self):
        return ObservationPreflightEvidence(
            observed_at_utc="2026-07-31T05:00:00Z",
            account_fingerprint=self.account_fingerprint,
            data_level="Full",
            account_count=1,
            account_currencies=("USD",),
            balance_currency="USD",
            currency_decimals=2,
        )

    def instrument_reference_observation(self):
        return []

    def atomic_quotes(self, _gates):
        return [], {}

    def atomic_quotes_observation(self):
        return [], {}

    def build_receipts(self, *_args, **_kwargs):
        return []

    def close(self):
        self.closed = True


def session_factory(account_fingerprint: str, captured: list[FakeSession]):
    def build(access_token, **kwargs):
        session = FakeSession(
            access_token,
            account_fingerprint=account_fingerprint,
            **kwargs,
        )
        captured.append(session)
        return session

    return build


def test_oauth_contract_is_sim_keychain_rotating_and_get_only():
    contract = load_oauth_contract()
    assert contract["environment"] == "SIM"
    assert contract["credential_store"] == "macOS Keychain"
    assert contract["access_token_persistence"] == "process_memory_only"
    assert contract["refresh_rotation_required"] is True
    assert contract["write_methods_allowed"] == []


def test_account_binding_round_trip_rejects_wrong_environment_or_key():
    binding = C2AccountBinding(
        environment="SIM",
        app_key_fingerprint="a" * 64,
        fingerprint_key=b"k" * 32,
        account_fingerprint="b" * 64,
        created_at_utc="2026-07-31T05:00:00Z",
        bound_at_utc="2026-07-31T05:00:00Z",
    )
    assert C2AccountBinding.from_bytes(binding.to_bytes()) == binding
    assert b"AccountKey" not in binding.to_bytes()


def test_first_successful_preflight_binds_account_without_persisting_access_token(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    store = MemoryStore()
    captured: list[FakeSession] = []
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=tmp_path / "disabled",
        session_factory=session_factory("a" * 64, captured),
        clock=lambda: NOW,
    )

    with adapter.open_session(
        accepted_operational_gates(),
        approved_provider_decisions(),
        read_only_ack=READ_ONLY_ACK,
    ) as session:
        session.preflight(accepted_operational_gates())

    stored = store.values[adapter.binding_account]
    assert C2AccountBinding.from_bytes(stored).account_fingerprint == "a" * 64
    assert b"access-secret" not in stored
    assert captured[0].received_token == "access-secret"
    assert manager.access_calls == [False]


def test_oauth_readiness_can_refresh_before_provider_and_gate_decisions(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=MemoryStore(),
        kill_switch_path=tmp_path / "disabled",
        clock=lambda: NOW,
    )

    result = adapter.prepare_auth_lease(read_only_ack=READ_ONLY_ACK)

    assert result["status"] == "C2_OAUTH_READINESS_READY"
    assert result["saxo_api_gets_performed"] == 0
    assert result["receipt_registration_performed"] is False
    assert result["periodic_execution_performed"] is False
    assert manager.access_calls == [False]
    assert "access-secret" not in str(result)


def test_data_session_is_blocked_by_provider_decision_before_token_use(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=MemoryStore(),
        kill_switch_path=tmp_path / "disabled",
        clock=lambda: NOW,
    )

    with pytest.raises(C2SIMReadContractBlocked, match="PROVIDER_DECISION_REQUIRED"):
        adapter.open_session(
            accepted_operational_gates(),
            load_provider_decision_template(),
            read_only_ack=READ_ONLY_ACK,
        )
    assert manager.access_calls == []


def test_initial_observation_session_does_not_require_provider_or_operational_gate(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    store = MemoryStore()
    captured: list[FakeSession] = []
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=tmp_path / "disabled",
        session_factory=session_factory("c" * 64, captured),
        clock=lambda: NOW,
    )
    with adapter.open_observation_session(read_only_ack=READ_ONLY_ACK) as session:
        evidence = session.preflight_observation()
    assert evidence.account_currencies == ("USD",)
    assert manager.access_calls == [False]
    assert C2AccountBinding.from_bytes(
        store.values[adapter.binding_account]
    ).account_fingerprint == "c" * 64
    assert b"access-secret" not in store.values[adapter.binding_account]


def test_bound_account_mismatch_fails_closed_and_does_not_replace_binding(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    store = MemoryStore()
    first: list[FakeSession] = []
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=tmp_path / "disabled",
        session_factory=session_factory("a" * 64, first),
        clock=lambda: NOW,
    )
    with adapter.open_session(
        accepted_operational_gates(),
        approved_provider_decisions(),
        read_only_ack=READ_ONLY_ACK,
    ) as session:
        session.preflight(accepted_operational_gates())
    original = store.values[adapter.binding_account]

    second: list[FakeSession] = []
    mismatch = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=tmp_path / "disabled",
        session_factory=session_factory("b" * 64, second),
        clock=lambda: NOW,
    )
    with mismatch.open_session(
        accepted_operational_gates(),
        approved_provider_decisions(),
        read_only_ack=READ_ONLY_ACK,
    ) as session:
        with pytest.raises(C2SIMReadOperationalError, match="ACCOUNT_BINDING_MISMATCH"):
            session.preflight(accepted_operational_gates())
    assert store.values[adapter.binding_account] == original


def test_kill_switch_and_read_only_ack_block_before_credential_or_session_use(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    store = MemoryStore()
    kill_switch = tmp_path / "disabled"
    kill_switch.write_text("operator stop\n", encoding="utf-8")
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=kill_switch,
        clock=lambda: NOW,
    )
    with pytest.raises(C2SIMReadOperationalError, match="KILL_SWITCH"):
        adapter.open_session(
            accepted_operational_gates(),
            approved_provider_decisions(),
            read_only_ack=READ_ONLY_ACK,
        )
    assert manager.access_calls == []
    assert adapter.status()["automatic_refresh_allowed"] is False

    kill_switch.unlink()
    with pytest.raises(C2SIMReadOperationalError, match="READ_ONLY_ACK_REQUIRED"):
        adapter.open_observation_session(read_only_ack="wrong")
    assert manager.access_calls == []


def test_kill_switch_blocks_an_already_open_managed_session(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    kill_switch = tmp_path / "disabled"
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=MemoryStore(),
        kill_switch_path=kill_switch,
        session_factory=session_factory("a" * 64, []),
        clock=lambda: NOW,
    )
    session = adapter.open_session(
        accepted_operational_gates(),
        approved_provider_decisions(),
        read_only_ack=READ_ONLY_ACK,
    )
    kill_switch.write_text("operator stop\n", encoding="utf-8")
    with pytest.raises(C2SIMReadOperationalError, match="KILL_SWITCH"):
        session.preflight(accepted_operational_gates())
    session.close()


def test_status_and_local_revoke_are_redacted(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    store = MemoryStore()
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=store,
        kill_switch_path=tmp_path / "disabled",
        clock=lambda: NOW,
    )
    status = adapter.status()
    assert status["status"] == "C2_OAUTH_READY_ACCOUNT_UNBOUND"
    assert "must-not-escape" not in str(status)

    result = adapter.revoke_local_credentials()
    assert manager.disconnected is True
    assert result["remote_revocation_required_in_saxo_ui"] is True
    assert "must-not-escape" not in str(result)


def test_refresh_keeper_runs_oauth_only_without_data_or_receipt_work(tmp_path):
    config = OAuthConfig("sim-app-key")
    manager = FakeManager(config)
    adapter = C2SIMOAuthCredentialAdapter(
        config,
        manager=manager,
        binding_store=MemoryStore(),
        kill_switch_path=tmp_path / "disabled",
        clock=lambda: NOW,
    )
    keeper = C2OAuthRefreshKeeper(adapter, interval_seconds=60)

    result = keeper.run_once()

    assert result["status"] == "RUNNING"
    assert result["purpose"] == "OAUTH_REFRESH_ONLY"
    assert result["saxo_api_gets_performed"] == 0
    assert result["receipt_registration_performed"] is False
    assert result["periodic_data_execution_performed"] is False
    assert result["orders_or_prechecks_sent"] == 0
    assert manager.access_calls == [False]
    assert "access-secret" not in str(result)
