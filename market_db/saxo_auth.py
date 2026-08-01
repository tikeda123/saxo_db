"""Saxo SIM OAuth PKCE and rotating refresh-token management.

Only the refresh credential is persisted, and only in the macOS Keychain.
Access tokens remain in the owning process memory.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import fcntl
import hashlib
import json
import os
import platform
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Mapping, Protocol

from .connection import project_root


SIM_AUTHORIZATION_URL = "https://sim.logonvalidation.net/authorize"
SIM_TOKEN_URL = "https://sim.logonvalidation.net/token"
DEFAULT_CALLBACK_PORT = 8764
CALLBACK_PATH = "/saxo/oauth/callback"
APP_KEY_ENV = "SAXO_OAUTH_APP_KEY"
APP_KEY_KEYCHAIN_SERVICE = "com.tikeda.saxodb.oauth.sim.app-key"
APP_KEY_KEYCHAIN_ACCOUNT = "operator-ui"
KEYCHAIN_SERVICE = "com.tikeda.saxodb.oauth.sim"
MIN_REFRESH_MARGIN_SECONDS = 30
ACCESS_REFRESH_MARGIN_SECONDS = 300
REFRESH_LOCK_RELATIVE_PATH = ".runtime/saxo_oauth_refresh.lock"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SaxoAuthError(RuntimeError):
    """Sanitized authentication failure safe for status and logs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OAuthConfig:
    app_key: str
    callback_port: int = DEFAULT_CALLBACK_PORT
    authorization_url: str = SIM_AUTHORIZATION_URL
    token_url: str = SIM_TOKEN_URL

    def __post_init__(self) -> None:
        if not self.app_key.strip() or len(self.app_key) > 256:
            raise SaxoAuthError("AUTH_CONFIG_MISSING")
        if not 1024 <= self.callback_port <= 65535:
            raise SaxoAuthError("AUTH_CALLBACK_PORT_INVALID")
        if self.authorization_url != SIM_AUTHORIZATION_URL or self.token_url != SIM_TOKEN_URL:
            raise SaxoAuthError("AUTH_NON_SIM_ENDPOINT_BLOCKED")

    @classmethod
    def from_environment(cls, *, callback_port: int = DEFAULT_CALLBACK_PORT) -> "OAuthConfig":
        return cls(os.environ.get(APP_KEY_ENV, "").strip(), callback_port=callback_port)

    @classmethod
    def from_local_configuration(
        cls,
        *,
        callback_port: int = DEFAULT_CALLBACK_PORT,
        app_key_store: CredentialStore | None = None,
    ) -> "OAuthConfig":
        """Load the public client identifier from env or the Operator Keychain.

        The value is needed in process memory for OAuth refresh, but is never
        emitted to logs, argv, runtime state, or database receipts.
        """

        selected = os.environ.get(APP_KEY_ENV, "").strip()
        if not selected:
            store = app_key_store or MacOSKeychainStore(
                service=APP_KEY_KEYCHAIN_SERVICE
            )
            raw = store.get(APP_KEY_KEYCHAIN_ACCOUNT)
            if raw is None:
                raise SaxoAuthError("AUTH_CONFIG_MISSING")
            try:
                selected = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                raise SaxoAuthError("AUTH_APP_KEY_KEYCHAIN_VALUE_INVALID") from None
        return cls(selected, callback_port=callback_port)

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.callback_port}{CALLBACK_PATH}"

    @property
    def keychain_account(self) -> str:
        digest = hashlib.sha256(self.app_key.encode("utf-8")).hexdigest()[:24]
        return f"pkce-{digest}"

    @property
    def app_key_fingerprint(self) -> str:
        return hashlib.sha256(self.app_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingAuthorization:
    authorization_url: str
    state: str
    code_verifier: str
    redirect_uri: str


@dataclass(frozen=True)
class RefreshCredential:
    refresh_token: str
    code_verifier: str
    expires_at_epoch: float
    app_key_fingerprint: str

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "refresh_token": self.refresh_token,
                "code_verifier": self.code_verifier,
                "expires_at_epoch": self.expires_at_epoch,
                "app_key_fingerprint": self.app_key_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "RefreshCredential":
        try:
            payload = json.loads(value.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError
            credential = cls(
                refresh_token=str(payload["refresh_token"]),
                code_verifier=str(payload["code_verifier"]),
                expires_at_epoch=float(payload["expires_at_epoch"]),
                app_key_fingerprint=str(payload["app_key_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise SaxoAuthError("AUTH_KEYCHAIN_VALUE_INVALID") from None
        if not credential.refresh_token or not 43 <= len(credential.code_verifier) <= 128:
            raise SaxoAuthError("AUTH_KEYCHAIN_VALUE_INVALID")
        return credential


@dataclass(frozen=True)
class AccessLease:
    access_token: str
    expires_at_epoch: float


class CredentialStore(Protocol):
    def get(self, account: str) -> bytes | None: ...

    def put(self, account: str, value: bytes) -> None: ...

    def delete(self, account: str) -> None: ...


class MacOSKeychainStore:
    """Store one opaque credential without placing it in process arguments."""

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.service = service
        self._runner = runner

    @staticmethod
    def _available() -> bool:
        return platform.system() == "Darwin" and os.path.isfile("/usr/bin/security")

    def get(self, account: str) -> bytes | None:
        if not self._available():
            raise SaxoAuthError("AUTH_KEYCHAIN_UNAVAILABLE")
        status, value = self._get_native(account)
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            raise SaxoAuthError("AUTH_KEYCHAIN_READ_FAILED")
        return value

    def _get_native(self, account: str) -> tuple[int, bytes | None]:
        """Read opaque bytes through Security.framework without a CLI prompt."""
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        service = self.service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        find = security.SecKeychainFindGenericPassword
        find.restype = ctypes.c_int32
        find.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ]
        status = find(
            None, len(service), service, len(account_bytes), account_bytes,
            ctypes.byref(password_length), ctypes.byref(password_data), ctypes.byref(item),
        )
        try:
            value = ctypes.string_at(password_data, password_length.value) if status == 0 else None
        finally:
            if password_data.value:
                security.SecKeychainItemFreeContent(None, password_data)
            if item.value:
                core_foundation.CFRelease(item)
        return int(status), value

    def put(self, account: str, value: bytes) -> None:
        if not self._available():
            raise SaxoAuthError("AUTH_KEYCHAIN_UNAVAILABLE")
        if self._put_native(account, value) != 0:
            raise SaxoAuthError("AUTH_KEYCHAIN_WRITE_FAILED")

    def _put_native(self, account: str, value: bytes) -> int:
        """Store opaque bytes through Security.framework, never through argv."""
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        service = self.service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        value_buffer = ctypes.create_string_buffer(value, len(value))
        item = ctypes.c_void_p()
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()

        add = security.SecKeychainAddGenericPassword
        add.restype = ctypes.c_int32
        add.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        status = add(
            None, len(service), service, len(account_bytes), account_bytes,
            len(value), value_buffer, ctypes.byref(item),
        )
        if status == 0:
            if item.value:
                core_foundation.CFRelease(item)
            return 0
        if status != -25299:  # errSecDuplicateItem
            return int(status)

        find = security.SecKeychainFindGenericPassword
        find.restype = ctypes.c_int32
        find.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ]
        status = find(
            None, len(service), service, len(account_bytes), account_bytes,
            ctypes.byref(password_length), ctypes.byref(password_data), ctypes.byref(item),
        )
        if password_data.value:
            security.SecKeychainItemFreeContent(None, password_data)
        if status != 0:
            return int(status)
        modify = security.SecKeychainItemModifyAttributesAndData
        modify.restype = ctypes.c_int32
        modify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        status = modify(item, None, len(value), value_buffer)
        if item.value:
            core_foundation.CFRelease(item)
        return int(status)

    def delete(self, account: str) -> None:
        if not self._available():
            raise SaxoAuthError("AUTH_KEYCHAIN_UNAVAILABLE")
        completed = self._runner(
            [
                "/usr/bin/security", "delete-generic-password",
                "-a", account, "-s", self.service,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode not in {0, 44}:
            raise SaxoAuthError("AUTH_KEYCHAIN_DELETE_FAILED")


class OAuthTransport(Protocol):
    def post_form(self, url: str, fields: Mapping[str, str]) -> dict[str, Any]: ...


class UrllibOAuthTransport:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def post_form(self, url: str, fields: Mapping[str, str]) -> dict[str, Any]:
        if url != SIM_TOKEN_URL:
            raise SaxoAuthError("AUTH_NON_SIM_ENDPOINT_BLOCKED")
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(dict(fields)).encode("ascii"),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = int(response.status)
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = b""
        except (OSError, TimeoutError):
            raise SaxoAuthError("AUTH_TOKEN_ENDPOINT_UNAVAILABLE") from None
        if not 200 <= status < 300:
            code = "AUTH_LOGIN_REQUIRED" if status in {400, 401, 403} else f"AUTH_TOKEN_HTTP_{status}"
            raise SaxoAuthError(code)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID") from None
        if not isinstance(payload, dict):
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID")
        return payload


class SaxoOAuthManager:
    def __init__(
        self,
        config: OAuthConfig,
        *,
        store: CredentialStore | None = None,
        transport: OAuthTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store or MacOSKeychainStore()
        self.transport = transport or UrllibOAuthTransport()
        self.clock = clock
        self._lease: AccessLease | None = None
        self._refresh_expires_at_epoch: float | None = None

    def begin_authorization(self) -> PendingAuthorization:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.config.app_key,
                "state": state,
                "redirect_uri": self.config.redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return PendingAuthorization(
            authorization_url=f"{self.config.authorization_url}?{query}",
            state=state,
            code_verifier=verifier,
            redirect_uri=self.config.redirect_uri,
        )

    def complete_authorization(self, pending: PendingAuthorization, code: str) -> dict[str, Any]:
        selected_code = code.strip()
        if not selected_code or len(selected_code) > 4096:
            raise SaxoAuthError("AUTHORIZATION_CODE_INVALID")
        payload = self.transport.post_form(
            self.config.token_url,
            {
                "grant_type": "authorization_code",
                "client_id": self.config.app_key,
                "code": selected_code,
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.code_verifier,
            },
        )
        return self._accept_token_response(payload, pending.code_verifier)

    def _accept_token_response(self, payload: Mapping[str, Any], verifier: str) -> dict[str, Any]:
        try:
            access_token = str(payload["access_token"])
            refresh_token = str(payload["refresh_token"])
            access_seconds = int(payload["expires_in"])
            refresh_seconds = int(payload["refresh_token_expires_in"])
            token_type = str(payload["token_type"])
        except (KeyError, TypeError, ValueError):
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID") from None
        if (
            not access_token
            or not refresh_token
            or access_seconds <= 0
            or refresh_seconds <= 0
            or token_type.casefold() != "bearer"
        ):
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID")
        now = self.clock()
        credential = RefreshCredential(
            refresh_token=refresh_token,
            code_verifier=verifier,
            expires_at_epoch=now + refresh_seconds,
            app_key_fingerprint=self.config.app_key_fingerprint,
        )
        self.store.put(self.config.keychain_account, credential.to_bytes())
        self._lease = AccessLease(access_token, now + access_seconds)
        self._refresh_expires_at_epoch = credential.expires_at_epoch
        return self.status()

    def _load_refresh_credential(self) -> RefreshCredential:
        encoded = self.store.get(self.config.keychain_account)
        if encoded is None:
            raise SaxoAuthError("AUTH_LOGIN_REQUIRED")
        credential = RefreshCredential.from_bytes(encoded)
        if credential.app_key_fingerprint != self.config.app_key_fingerprint:
            raise SaxoAuthError("AUTH_APP_KEY_MISMATCH")
        if credential.expires_at_epoch <= self.clock() + MIN_REFRESH_MARGIN_SECONDS:
            raise SaxoAuthError("AUTH_LOGIN_REQUIRED")
        return credential

    def access_token(self, *, force_refresh: bool = False) -> str:
        now = self.clock()
        if (
            not force_refresh
            and self._lease is not None
            and self._lease.expires_at_epoch > now + ACCESS_REFRESH_MARGIN_SECONDS
            and self._refresh_expires_at_epoch is not None
            and self._refresh_expires_at_epoch > now + ACCESS_REFRESH_MARGIN_SECONDS
        ):
            return self._lease.access_token
        lock_path = project_root() / REFRESH_LOCK_RELATIVE_PATH
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_stream:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                # A different process may have rotated the refresh credential
                # while this process waited. Re-read it only after taking the
                # repository-owned lock; access tokens remain process-local.
                credential = self._load_refresh_credential()
                payload = self.transport.post_form(
                    self.config.token_url,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": credential.refresh_token,
                        "code_verifier": credential.code_verifier,
                    },
                )
                self._accept_token_response(payload, credential.code_verifier)
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            # fdopen owns the descriptor after successful construction.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if self._lease is None:
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID")
        return self._lease.access_token

    def access_lease(self, *, force_refresh: bool = False) -> AccessLease:
        """Return a process-local lease without exposing it through status/logs."""

        self.access_token(force_refresh=force_refresh)
        if self._lease is None or self._lease.expires_at_epoch <= self.clock():
            raise SaxoAuthError("AUTH_TOKEN_RESPONSE_INVALID")
        return self._lease

    def status(self) -> dict[str, Any]:
        now = self.clock()
        refresh_expiry: float | None = None
        status = "AUTH_LOGIN_REQUIRED"
        try:
            encoded = self.store.get(self.config.keychain_account)
            if encoded is not None:
                credential = RefreshCredential.from_bytes(encoded)
                if credential.app_key_fingerprint != self.config.app_key_fingerprint:
                    raise SaxoAuthError("AUTH_APP_KEY_MISMATCH")
                refresh_expiry = credential.expires_at_epoch
                status = "AUTH_READY" if refresh_expiry > now + MIN_REFRESH_MARGIN_SECONDS else "AUTH_LOGIN_REQUIRED"
        except SaxoAuthError as exc:
            status = exc.code
        access_expiry = None if self._lease is None else self._lease.expires_at_epoch
        return {
            "status": status,
            "environment": "SIM",
            "app_key_fingerprint": self.config.app_key_fingerprint[:16],
            "access_token_in_memory": self._lease is not None and access_expiry is not None and access_expiry > now,
            "access_expires_at_utc": (
                None if access_expiry is None else _utc_text(datetime.fromtimestamp(access_expiry, timezone.utc))
            ),
            "refresh_credential_present": refresh_expiry is not None,
            "refresh_expires_at_utc": (
                None if refresh_expiry is None else _utc_text(datetime.fromtimestamp(refresh_expiry, timezone.utc))
            ),
            "token_values_exposed": False,
            "orders_or_prechecks_sent": 0,
        }

    def disconnect(self) -> dict[str, Any]:
        self._lease = None
        self._refresh_expires_at_epoch = None
        self.store.delete(self.config.keychain_account)
        return self.status()


class _CallbackState:
    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.code: str | None = None
        self.error_code: str | None = None


def _callback_handler(state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        server_version = "saxo-db-oauth-callback"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            observed_state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            error = (query.get("error") or [""])[0]
            if parsed.path != CALLBACK_PATH or not secrets.compare_digest(observed_state, state.expected_state):
                state.error_code = "AUTH_CALLBACK_STATE_MISMATCH"
                status = 400
                message = "Saxo認証応答を検証できませんでした。"
            elif error or not code:
                state.error_code = "AUTHORIZATION_DENIED"
                status = 400
                message = "Saxo認証が完了しませんでした。"
            else:
                state.code = code
                status = 200
                message = "Saxo認証を受け付けました。この画面を閉じてください。"
            body = (
                "<!doctype html><html lang='ja'><meta charset='utf-8'>"
                f"<title>saxo_db OAuth</title><p>{message}</p></html>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def interactive_login(
    manager: SaxoOAuthManager,
    *,
    timeout_seconds: float = 180.0,
    browser_open: Callable[[str], Any] = webbrowser.open,
) -> dict[str, Any]:
    pending = manager.begin_authorization()
    callback_state = _CallbackState(pending.state)
    server = ThreadingHTTPServer(("127.0.0.1", manager.config.callback_port), _callback_handler(callback_state))
    server.timeout = 0.5
    try:
        if not browser_open(pending.authorization_url):
            raise SaxoAuthError("AUTH_BROWSER_OPEN_FAILED")
        deadline = time.monotonic() + timeout_seconds
        while callback_state.code is None and callback_state.error_code is None:
            if time.monotonic() >= deadline:
                raise SaxoAuthError("AUTH_CALLBACK_TIMEOUT")
            server.handle_request()
        if callback_state.error_code is not None:
            raise SaxoAuthError(callback_state.error_code)
        return manager.complete_authorization(pending, callback_state.code or "")
    finally:
        server.server_close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saxo SIM OAuth PKCE credential manager")
    parser.add_argument("command", choices=("status", "login", "refresh", "logout"))
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        manager = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=args.callback_port))
        if args.command == "status":
            result = manager.status()
        elif args.command == "login":
            result = interactive_login(manager, timeout_seconds=args.timeout_seconds)
        elif args.command == "refresh":
            manager.access_token(force_refresh=True)
            result = manager.status()
        else:
            result = manager.disconnect()
    except SaxoAuthError as exc:
        result = {
            "status": exc.code,
            "environment": "SIM",
            "token_values_exposed": False,
            "orders_or_prechecks_sent": 0,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"AUTH_READY", "AUTH_LOGIN_REQUIRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
