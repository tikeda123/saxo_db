"""Keychain-backed, rotating OAuth adapter for C2 SIM GET-only sessions.

The adapter does not broaden Saxo permissions.  It binds the existing PKCE
credential manager to the C2 allow-listed GET session, keeps access tokens in
memory, and stores only the rotating refresh credential plus an opaque account
binding record in macOS Keychain.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .c2_external_decisions import (
    C2DecisionError,
    validate_operational_gates,
    validate_provider_decisions,
)
from .c2_sim_read_session import (
    C2SIMReadContractBlocked,
    C2SIMReadOperationalError,
    EphemeralSIMReadSession,
    ObservationPreflightEvidence,
    PreflightEvidence,
    load_observation_start_contract,
)
from .connection import project_root
from .saxo_auth import (
    AccessLease,
    CredentialStore,
    MacOSKeychainStore,
    OAuthConfig,
    SaxoAuthError,
    SaxoOAuthManager,
)


C2_OAUTH_CONTRACT_ID = "c2_saxo_sim_oauth_keychain_v1"
C2_OAUTH_CONTRACT_RELATIVE_PATH = "specs/c2_saxo_sim_oauth_keychain_v1.json"
C2_BINDING_ACCOUNT_PREFIX = "c2-read-binding-"
C2_KILL_SWITCH_RELATIVE_PATH = ".runtime/c2/sim_read_disabled"
READ_ONLY_ACK = "SIM_APP_TRADING_DISABLED_GET_ONLY"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_oauth_contract() -> dict[str, Any]:
    path = project_root() / C2_OAUTH_CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("C2_SIM_OAUTH_CONTRACT_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("C2_SIM_OAUTH_CONTRACT_NOT_VERIFIED") from exc
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("contract_id") == C2_OAUTH_CONTRACT_ID
        and value.get("environment") == "SIM"
        and value.get("credential_store") == "macOS Keychain"
        and value.get("access_token_persistence") == "process_memory_only"
        and value.get("refresh_rotation_required") is True
        and value.get("read_only_ack") == READ_ONLY_ACK
        and value.get("write_methods_allowed") == []
    ):
        raise RuntimeError("C2_SIM_OAUTH_CONTRACT_NOT_VERIFIED")
    return value


@dataclass(frozen=True)
class C2AccountBinding:
    environment: str
    app_key_fingerprint: str
    fingerprint_key: bytes
    account_fingerprint: str | None
    created_at_utc: str
    bound_at_utc: str | None = None

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "environment": self.environment,
                "app_key_fingerprint": self.app_key_fingerprint,
                "fingerprint_key_b64": base64.b64encode(self.fingerprint_key).decode("ascii"),
                "account_fingerprint": self.account_fingerprint,
                "created_at_utc": self.created_at_utc,
                "bound_at_utc": self.bound_at_utc,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "C2AccountBinding":
        try:
            payload = json.loads(value.decode("utf-8"))
            key = base64.b64decode(payload["fingerprint_key_b64"], validate=True)
            binding = cls(
                environment=str(payload["environment"]),
                app_key_fingerprint=str(payload["app_key_fingerprint"]),
                fingerprint_key=key,
                account_fingerprint=(
                    None
                    if payload.get("account_fingerprint") is None
                    else str(payload["account_fingerprint"])
                ),
                created_at_utc=str(payload["created_at_utc"]),
                bound_at_utc=(
                    None if payload.get("bound_at_utc") is None else str(payload["bound_at_utc"])
                ),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise SaxoAuthError("AUTH_ACCOUNT_BINDING_INVALID") from None
        if (
            payload.get("schema_version") != 1
            or binding.environment != "SIM"
            or len(binding.app_key_fingerprint) != 64
            or len(binding.fingerprint_key) != 32
            or (binding.account_fingerprint is not None and len(binding.account_fingerprint) != 64)
        ):
            raise SaxoAuthError("AUTH_ACCOUNT_BINDING_INVALID")
        return binding


class OAuthLeaseProvider(Protocol):
    config: OAuthConfig

    def status(self) -> dict[str, Any]: ...
    def access_lease(self, *, force_refresh: bool = False) -> AccessLease: ...
    def disconnect(self) -> dict[str, Any]: ...


class ManagedC2SIMReadSession:
    """Add account binding and kill-switch checks around the existing session."""

    def __init__(
        self,
        session: EphemeralSIMReadSession,
        adapter: "C2SIMOAuthCredentialAdapter",
        binding: C2AccountBinding,
    ) -> None:
        self._session = session
        self._adapter = adapter
        self._binding = binding

    def __enter__(self) -> "ManagedC2SIMReadSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return "ManagedC2SIMReadSession(environment='SIM', credential=<redacted>)"

    def close(self) -> None:
        self._session.close()

    @property
    def request_count(self) -> int:
        return self._session.request_count

    @property
    def write_request_count(self) -> int:
        return self._session.write_request_count

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._session.events

    def _guard(self) -> None:
        self._adapter.assert_enabled()

    def preflight(self, gates: Mapping[str, Any]) -> PreflightEvidence:
        self._guard()
        evidence = self._session.preflight(gates)
        self._binding = self._adapter.bind_or_verify_account(self._binding, evidence)
        return evidence

    def preflight_observation(self) -> ObservationPreflightEvidence:
        self._guard()
        evidence = self._session.preflight_observation()
        self._binding = self._adapter.bind_or_verify_account(self._binding, evidence)
        return evidence

    def instrument_reference(self) -> list[dict[str, Any]]:
        self._guard()
        return self._session.instrument_reference()

    def instrument_reference_observation(self) -> list[dict[str, Any]]:
        self._guard()
        return self._session.instrument_reference_observation()

    def atomic_quotes(self, gates: Mapping[str, Any]):
        self._guard()
        return self._session.atomic_quotes(gates)

    def atomic_quotes_observation(self):
        self._guard()
        return self._session.atomic_quotes_observation()

    def build_receipts(self, *args: Any, **kwargs: Any):
        self._guard()
        return self._session.build_receipts(*args, **kwargs)


class C2SIMOAuthCredentialAdapter:
    """Use one initial PKCE login and rotate Keychain refresh credentials."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        manager: OAuthLeaseProvider | None = None,
        binding_store: CredentialStore | None = None,
        kill_switch_path: Path | None = None,
        session_factory: Callable[..., EphemeralSIMReadSession] = EphemeralSIMReadSession,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        load_oauth_contract()
        self.config = config
        self.manager = manager or SaxoOAuthManager(config)
        self.binding_store = binding_store or MacOSKeychainStore()
        self.kill_switch_path = kill_switch_path or (project_root() / C2_KILL_SWITCH_RELATIVE_PATH)
        self.session_factory = session_factory
        self.clock = clock

    @property
    def binding_account(self) -> str:
        return f"{C2_BINDING_ACCOUNT_PREFIX}{self.config.app_key_fingerprint[:24]}"

    def assert_enabled(self) -> None:
        if self.kill_switch_path.exists():
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_KILL_SWITCH")

    def _load_binding(self) -> C2AccountBinding:
        encoded = self.binding_store.get(self.binding_account)
        if encoded is None:
            return C2AccountBinding(
                environment="SIM",
                app_key_fingerprint=self.config.app_key_fingerprint,
                fingerprint_key=secrets.token_bytes(32),
                account_fingerprint=None,
                created_at_utc=_utc_text(self.clock()),
            )
        binding = C2AccountBinding.from_bytes(encoded)
        if (
            binding.environment != "SIM"
            or binding.app_key_fingerprint != self.config.app_key_fingerprint
        ):
            raise SaxoAuthError("AUTH_ACCOUNT_BINDING_MISMATCH")
        return binding

    def bind_or_verify_account(
        self,
        binding: C2AccountBinding,
        evidence: PreflightEvidence | ObservationPreflightEvidence,
    ) -> C2AccountBinding:
        self.assert_enabled()
        if binding.account_fingerprint is None:
            bound = replace(
                binding,
                account_fingerprint=evidence.account_fingerprint,
                bound_at_utc=_utc_text(self.clock()),
            )
            self.binding_store.put(self.binding_account, bound.to_bytes())
            return bound
        if not secrets.compare_digest(binding.account_fingerprint, evidence.account_fingerprint):
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_ACCOUNT_BINDING_MISMATCH"
            )
        return binding

    def status(self) -> dict[str, Any]:
        if self.kill_switch_path.exists():
            return {
                "status": "C2_SIM_READ_KILL_SWITCH_ENGAGED",
                "environment": "SIM",
                "oauth_status": "NOT_CHECKED",
                "account_binding": "NOT_CHECKED",
                "credential_values_exposed": False,
                "automatic_refresh_allowed": False,
            }
        observed = self.manager.status()
        oauth_status = str(observed.get("status") or "AUTH_LOGIN_REQUIRED")
        binding_state = "UNBOUND"
        if oauth_status == "AUTH_READY":
            try:
                binding_state = (
                    "BOUND" if self._load_binding().account_fingerprint is not None else "UNBOUND"
                )
            except SaxoAuthError as exc:
                return {
                    "status": exc.code,
                    "environment": "SIM",
                    "oauth_status": oauth_status,
                    "account_binding": "INVALID",
                    "credential_values_exposed": False,
                    "automatic_refresh_allowed": False,
                }
        status = (
            f"C2_OAUTH_READY_ACCOUNT_{binding_state}"
            if oauth_status == "AUTH_READY"
            else oauth_status
        )
        return {
            "status": status,
            "environment": "SIM",
            "oauth_status": oauth_status,
            "account_binding": binding_state,
            "credential_values_exposed": False,
            "automatic_refresh_allowed": oauth_status == "AUTH_READY",
        }

    def open_session(
        self,
        gates: Mapping[str, Any],
        provider_decisions: Mapping[str, Any],
        *,
        read_only_ack: str,
        force_refresh: bool = False,
    ) -> ManagedC2SIMReadSession:
        self.assert_enabled()
        try:
            validate_operational_gates(gates, require_accepted=True)
            validate_provider_decisions(provider_decisions, require_approved=True)
        except C2DecisionError as exc:
            raise C2SIMReadContractBlocked(str(exc)) from None
        lease = self._access_lease(
            read_only_ack=read_only_ack, force_refresh=force_refresh
        )
        try:
            binding = self._load_binding()
        except SaxoAuthError as exc:
            raise C2SIMReadOperationalError(exc.code) from None
        session = self.session_factory(
            lease.access_token,
            access_expires_at_utc=datetime.fromtimestamp(
                lease.expires_at_epoch, timezone.utc
            ),
            fingerprint_key=binding.fingerprint_key,
            clock=self.clock,
            operational_guard=self.assert_enabled,
        )
        return ManagedC2SIMReadSession(session, self, binding)

    def open_observation_session(
        self,
        *,
        read_only_ack: str,
        force_refresh: bool = False,
    ) -> ManagedC2SIMReadSession:
        """Open the minimal initial observation without downstream provider gates."""

        load_observation_start_contract()
        self.assert_enabled()
        lease = self._access_lease(
            read_only_ack=read_only_ack, force_refresh=force_refresh
        )
        try:
            binding = self._load_binding()
        except SaxoAuthError as exc:
            raise C2SIMReadOperationalError(exc.code) from None
        session = self.session_factory(
            lease.access_token,
            access_expires_at_utc=datetime.fromtimestamp(
                lease.expires_at_epoch, timezone.utc
            ),
            fingerprint_key=binding.fingerprint_key,
            clock=self.clock,
            operational_guard=self.assert_enabled,
        )
        return ManagedC2SIMReadSession(session, self, binding)

    def _access_lease(
        self, *, read_only_ack: str, force_refresh: bool
    ) -> AccessLease:
        self.assert_enabled()
        if read_only_ack != READ_ONLY_ACK:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_READ_ONLY_ACK_REQUIRED"
            )
        try:
            return self.manager.access_lease(force_refresh=force_refresh)
        except SaxoAuthError as exc:
            raise C2SIMReadOperationalError(exc.code) from None

    def prepare_auth_lease(
        self, *, read_only_ack: str, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Maintain OAuth readiness without constructing a Saxo API client."""

        lease = self._access_lease(
            read_only_ack=read_only_ack, force_refresh=force_refresh
        )
        return {
            "status": "C2_OAUTH_READINESS_READY",
            "environment": "SIM",
            "access_expires_at_utc": _utc_text(
                datetime.fromtimestamp(lease.expires_at_epoch, timezone.utc)
            ),
            "access_token_in_process_memory": True,
            "credential_values_exposed": False,
            "saxo_api_gets_performed": 0,
            "receipt_registration_performed": False,
            "periodic_execution_performed": False,
            "orders_or_prechecks_sent": 0,
        }

    def revoke_local_credentials(self) -> dict[str, Any]:
        """Kill local continuation; remote app access is revoked in Saxo UI."""

        self.manager.disconnect()
        self.binding_store.delete(self.binding_account)
        return {
            "status": "AUTH_LOGIN_REQUIRED",
            "environment": "SIM",
            "local_refresh_credential_deleted": True,
            "account_binding_deleted": True,
            "remote_revocation_required_in_saxo_ui": True,
            "credential_values_exposed": False,
        }


class C2OAuthRefreshKeeper:
    """Maintain only the OAuth refresh chain; never construct an API client."""

    def __init__(
        self,
        adapter: C2SIMOAuthCredentialAdapter,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("refresh keeper interval must be at least one second")
        self.adapter = adapter
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {
            "status": "STOPPED",
            "last_refresh_at_utc": None,
            "last_error_code": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            selected = dict(self._status)
        return {
            **selected,
            "purpose": "OAUTH_REFRESH_ONLY",
            "saxo_api_gets_performed": 0,
            "receipt_registration_performed": False,
            "periodic_data_execution_performed": False,
            "orders_or_prechecks_sent": 0,
            "credential_values_exposed": False,
        }

    def run_once(self) -> dict[str, Any]:
        try:
            self.adapter.prepare_auth_lease(read_only_ack=READ_ONLY_ACK)
        except C2SIMReadOperationalError as exc:
            with self._lock:
                self._status = {
                    "status": "BLOCKED_INTERFACE_OPERATIONAL",
                    "last_refresh_at_utc": self._status.get("last_refresh_at_utc"),
                    "last_error_code": exc.code,
                }
            return self.status()
        except Exception:
            with self._lock:
                self._status = {
                    "status": "BLOCKED_INTERFACE_OPERATIONAL",
                    "last_refresh_at_utc": self._status.get("last_refresh_at_utc"),
                    "last_error_code": "BLOCKED_INTERFACE_OPERATIONAL_UNEXPECTED",
                }
            return self.status()
        with self._lock:
            self._status = {
                "status": "RUNNING",
                "last_refresh_at_utc": _utc_text(self.adapter.clock()),
                "last_error_code": None,
            }
        return self.status()

    def _run(self) -> None:
        while not self._stop.is_set():
            result = self.run_once()
            if result["status"] != "RUNNING":
                break
            if self._stop.wait(self.interval_seconds):
                break

    def start(self) -> dict[str, Any]:
        already_running = False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                already_running = True
            else:
                self._stop.clear()
                self._status = {
                    "status": "STARTING",
                    "last_refresh_at_utc": self._status.get("last_refresh_at_utc"),
                    "last_error_code": None,
                }
                self._thread = threading.Thread(
                    target=self._run,
                    name="saxo-db-c2-oauth-refresh-only",
                    daemon=True,
                )
                self._thread.start()
        if already_running:
            return self.status()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(self.interval_seconds + 1.0, 5.0))
        with self._lock:
            self._status = {
                "status": "STOPPED",
                "last_refresh_at_utc": self._status.get("last_refresh_at_utc"),
                "last_error_code": self._status.get("last_error_code"),
            }
        return self.status()
