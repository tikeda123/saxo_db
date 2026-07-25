from __future__ import annotations

import json
import subprocess
import urllib.parse

import pytest

from market_db.saxo_auth import (
    APP_KEY_ENV,
    CALLBACK_PATH,
    MacOSKeychainStore,
    OAuthConfig,
    RefreshCredential,
    SaxoAuthError,
    SaxoOAuthManager,
)


class MemoryStore:
    def __init__(self):
        self.values: dict[str, bytes] = {}

    def get(self, account: str) -> bytes | None:
        return self.values.get(account)

    def put(self, account: str, value: bytes) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


class FakeOAuthTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_form(self, url, fields):
        self.calls.append((url, dict(fields)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def token_response(access: str, refresh: str, *, access_seconds: int = 1200, refresh_seconds: int = 2400):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": access_seconds,
        "refresh_token_expires_in": refresh_seconds,
        "token_type": "Bearer",
    }


def test_pkce_authorization_url_is_sim_localhost_and_has_no_secret(monkeypatch):
    monkeypatch.setenv(APP_KEY_ENV, "sim-app-key")
    manager = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=8764), store=MemoryStore())
    pending = manager.begin_authorization()
    parsed = urllib.parse.urlsplit(pending.authorization_url)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "sim.logonvalidation.net"
    assert query["client_id"] == ["sim-app-key"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"http://localhost:8764{CALLBACK_PATH}"]
    assert pending.code_verifier not in pending.authorization_url
    assert 43 <= len(pending.code_verifier) <= 128


def test_authorization_persists_only_refresh_credential_and_redacts_status():
    now = [1_000_000.0]
    store = MemoryStore()
    transport = FakeOAuthTransport([token_response("access-secret", "refresh-secret")])
    manager = SaxoOAuthManager(
        OAuthConfig("sim-app-key"), store=store, transport=transport, clock=lambda: now[0]
    )
    pending = manager.begin_authorization()
    status = manager.complete_authorization(pending, "one-time-code")

    stored = RefreshCredential.from_bytes(store.values[manager.config.keychain_account])
    assert stored.refresh_token == "refresh-secret"
    assert b"access-secret" not in store.values[manager.config.keychain_account]
    assert manager.access_token() == "access-secret"
    serialized = json.dumps(status)
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert status["status"] == "AUTH_READY"
    assert status["token_values_exposed"] is False


def test_authorization_accepts_http_201_token_response(monkeypatch):
    payload = token_response("access-from-201", "refresh-from-201")

    class CreatedResponse:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "market_db.saxo_auth.urllib.request.urlopen",
        lambda _request, timeout: CreatedResponse(),
    )
    store = MemoryStore()
    manager = SaxoOAuthManager(OAuthConfig("sim-app-key"), store=store, clock=lambda: 1_000_000.0)
    pending = manager.begin_authorization()

    status = manager.complete_authorization(pending, "one-time-code")

    stored = RefreshCredential.from_bytes(store.values[manager.config.keychain_account])
    assert status["status"] == "AUTH_READY"
    assert status["token_values_exposed"] is False
    assert stored.refresh_token == "refresh-from-201"
    assert b"access-from-201" not in store.values[manager.config.keychain_account]


def test_refresh_rotates_credential_and_old_refresh_is_not_reused():
    now = [2_000_000.0]
    store = MemoryStore()
    transport = FakeOAuthTransport(
        [
            token_response("access-one", "refresh-one", access_seconds=60),
            token_response("access-two", "refresh-two"),
        ]
    )
    manager = SaxoOAuthManager(
        OAuthConfig("sim-app-key"), store=store, transport=transport, clock=lambda: now[0]
    )
    pending = manager.begin_authorization()
    manager.complete_authorization(pending, "one-time-code")
    assert manager.access_token() == "access-two"

    refresh_call = transport.calls[1][1]
    assert refresh_call["grant_type"] == "refresh_token"
    assert refresh_call["refresh_token"] == "refresh-one"
    assert "client_id" not in refresh_call
    stored = RefreshCredential.from_bytes(store.values[manager.config.keychain_account])
    assert stored.refresh_token == "refresh-two"


def test_refresh_chain_is_rotated_before_refresh_expiry_even_with_valid_access_token():
    now = [4_000_000.0]
    store = MemoryStore()
    transport = FakeOAuthTransport(
        [
            token_response("access-one", "refresh-one", access_seconds=1200, refresh_seconds=600),
            token_response("access-two", "refresh-two", access_seconds=1200, refresh_seconds=2400),
        ]
    )
    manager = SaxoOAuthManager(
        OAuthConfig("sim-app-key"), store=store, transport=transport, clock=lambda: now[0]
    )
    pending = manager.begin_authorization()
    manager.complete_authorization(pending, "one-time-code")
    now[0] += 350

    assert manager.access_token() == "access-two"
    assert transport.calls[1][1]["refresh_token"] == "refresh-one"


def test_expired_or_missing_refresh_credential_requires_human_login():
    now = 3_000_000.0
    store = MemoryStore()
    manager = SaxoOAuthManager(OAuthConfig("sim-app-key"), store=store, clock=lambda: now)
    with pytest.raises(SaxoAuthError, match="AUTH_LOGIN_REQUIRED"):
        manager.access_token()

    store.put(
        manager.config.keychain_account,
        RefreshCredential(
            "refresh", "v" * 64, now + 10, manager.config.app_key_fingerprint
        ).to_bytes(),
    )
    with pytest.raises(SaxoAuthError, match="AUTH_LOGIN_REQUIRED"):
        manager.access_token()


def test_keychain_write_passes_credential_only_to_native_keychain_api(monkeypatch):
    calls = []
    monkeypatch.setattr(MacOSKeychainStore, "_available", staticmethod(lambda: True))
    monkeypatch.setattr(
        MacOSKeychainStore,
        "_put_native",
        lambda _self, account, value: calls.append((account, value)) or 0,
    )
    store = MacOSKeychainStore()
    secret = b'{"refresh_token":"do-not-expose"}'
    store.put("account", secret)

    assert calls == [("account", secret)]


def test_keychain_read_uses_native_keychain_api_without_security_cli(monkeypatch):
    monkeypatch.setattr(MacOSKeychainStore, "_available", staticmethod(lambda: True))
    monkeypatch.setattr(MacOSKeychainStore, "_get_native", lambda _self, _account: (0, b"opaque"))
    assert MacOSKeychainStore().get("account") == b"opaque"


def test_non_sim_oauth_endpoints_and_empty_app_key_are_blocked():
    with pytest.raises(SaxoAuthError, match="AUTH_CONFIG_MISSING"):
        OAuthConfig("")
    with pytest.raises(SaxoAuthError, match="AUTH_NON_SIM_ENDPOINT_BLOCKED"):
        OAuthConfig("app", token_url="https://example.invalid/token")
