from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from market_db.c2_external_decisions import (
    C2DecisionError,
    load_operational_gate_template,
    load_provider_decision_template,
)
from market_db.c2_sim_read_session import C2SIMReadOperationalError
from market_db.connection import project_root
from market_db.operator_ui import (
    InvalidAccessToken,
    JobAlreadyRunning,
    ReconcileJobManager,
    OperatorState,
    _empty_c2_observation_audit,
    adopt_legacy_c2_observation_status,
    allowed_browser_request,
    c2_decision_guidance,
    c2_observation_operator_guidance,
    load_c2_observation_audit,
    make_handler,
    operator_html,
    oauth_configuration_diagnostics,
    oauth_keychain_service_entry_present,
    operator_periodic_scope_profile,
    save_c2_observation_audit,
    sanitize_c2_observation_result,
    sanitized_line,
)
from market_db.periodic_update import (
    ACTIVE_SCOPE_PROFILE,
    CANDIDATE_READY_SCOPE_PROFILE,
)
from market_db.saxo_auth import PendingAuthorization, SaxoAuthError


class FakeProcess:
    def __init__(self, output: str, exit_code: int = 0, release: threading.Event | None = None):
        self.stdout = io.StringIO(output)
        self.exit_code = exit_code
        self.release = release

    def wait(self) -> int:
        if self.release is not None:
            assert self.release.wait(timeout=2)
        return self.exit_code


def wait_for_terminal(manager: ReconcileJobManager) -> dict[str, object]:
    for _ in range(200):
        status = manager.status()
        if status["status"] != "RUNNING":
            return status
        time.sleep(0.005)
    raise AssertionError("operator job did not finish")


def test_operator_periodic_scope_activates_candidates_only_after_gate(monkeypatch):
    monkeypatch.setattr(
        "market_db.operator_ui.candidate_scope_readiness",
        lambda: {"status": "BLOCKED_CANDIDATE_SCOPE_NOT_READY"},
    )
    assert operator_periodic_scope_profile() == ACTIVE_SCOPE_PROFILE

    monkeypatch.setattr(
        "market_db.operator_ui.candidate_scope_readiness",
        lambda: {"status": "PASS"},
    )
    assert operator_periodic_scope_profile() == CANDIDATE_READY_SCOPE_PROFILE


def test_operator_runs_only_fixed_command_and_redacts_token(monkeypatch):
    secret = ".".join(("eyJheader", "payload", "signature"))
    calls = []

    def popen(command, **kwargs):
        calls.append((command, {**kwargs, "env": dict(kwargs["env"])}))
        return FakeProcess(f'{{"status":"PASS"}}\nBearer {secret}\n{secret}\n')

    monkeypatch.setenv("SAXO_ACCESS_TOKEN", "old-parent-value")
    manager = ReconcileJobManager(popen_factory=popen)
    manager.start(secret)
    status = wait_for_terminal(manager)

    command, kwargs = calls[0]
    assert command == [sys.executable, "-m", "market_db.incremental_update", "reconcile"]
    assert secret not in " ".join(command)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is not None
    assert Path(kwargs["cwd"]) == project_root()
    assert kwargs["env"]["SAXO_ACCESS_TOKEN"] == secret
    assert status["status"] == "PASS"
    serialized = json.dumps(status)
    assert secret not in serialized
    assert "<redacted>" in serialized
    assert status["orders_or_prechecks_sent"] == 0


def test_operator_oauth_job_uses_keychain_mode_without_static_access_token(monkeypatch):
    calls = []

    def popen(command, **kwargs):
        calls.append((command, {**kwargs, "env": dict(kwargs["env"])}))
        return FakeProcess('{"status":"PASS"}\n')

    monkeypatch.setenv("SAXO_ACCESS_TOKEN", "must-not-be-inherited")
    manager = ReconcileJobManager(popen_factory=popen)
    manager.start_oauth(callback_port=8765)
    status = wait_for_terminal(manager)

    command, kwargs = calls[0]
    assert command == [
        sys.executable,
        "-m",
        "market_db.incremental_update",
        "reconcile",
        "--auth-mode",
        "keychain",
        "--callback-port",
        "8765",
    ]
    assert "SAXO_ACCESS_TOKEN" not in kwargs["env"]
    assert status["status"] == "PASS"
    assert status["orders_or_prechecks_sent"] == 0


def test_operator_rejects_empty_token_and_concurrent_job():
    release = threading.Event()
    manager = ReconcileJobManager(
        popen_factory=lambda command, **kwargs: FakeProcess("running\n", release=release)
    )
    with pytest.raises(InvalidAccessToken):
        manager.start("   ")
    manager.start("session-token")
    with pytest.raises(JobAlreadyRunning):
        manager.start("another-token")
    release.set()
    assert wait_for_terminal(manager)["status"] == "PASS"


def test_operator_page_never_uses_browser_storage_and_clears_password_input():
    page = operator_html("csrf", "nonce").decode("utf-8")
    assert 'type="password"' in page
    assert "tokenInput.value = '';" in page
    assert "(job.output || []).join('\\n')" in page
    assert "(job.output || []).join('\n')" not in page
    assert "localStorage." not in page
    assert "sessionStorage." not in page
    assert "document.cookie" not in page
    assert "shell=True" not in page
    assert 'id="oauth-start"' in page
    assert 'id="oauth-reconcile"' in page
    assert "/api/reconcile/oauth" in page
    assert "oauthReconcile.disabled = true;" in page
    assert "DataVersion変更は警告として記録" in page
    assert "自動reconcileは無効です" in page
    assert 'id="periodic-start"' in page
    assert "/api/oauth/status" in page
    assert "/api/periodic/status" in page
    assert "oauthStart.disabled = auth.status === 'AUTH_CONFIG_MISSING';" in page
    assert "periodicStart.disabled = auth.status !== 'AUTH_READY';" in page
    assert "EURUSDとETF 11系列" in page
    assert "USDJPYはprovider品質問題" in page
    assert 'href="http://127.0.0.1:8766/ui/overview"' in page
    assert 'href="http://127.0.0.1:8766/ui/catalog"' in page
    assert 'id="c2-readiness-state"' in page
    assert "1. App Key設定" in page
    assert "2. C2 OAuth接続" in page
    assert "3. 初回SIM観測開始" in page
    assert "4. 後続段階: provider / allocation・paper評価gate" in page
    assert "5. LIVE_ORDER_ELIGIBILITY" in page
    assert page.index("1. App Key設定") < page.index("2. C2 OAuth接続")
    assert page.index("2. C2 OAuth接続") < page.index("3. 初回SIM観測開始")
    assert page.index("3. 初回SIM観測開始") < page.index("4. 後続段階")
    assert page.index("4. 後続段階") < page.index("5. LIVE_ORDER_ELIGIBILITY")
    assert 'id="c2-oauth-start"' in page
    assert 'id="c2-oauth-step-state"' in page
    assert 'id="c2-refresh-keeper-state"' in page
    assert 'id="c2-decision-step-state"' in page
    assert 'id="c2-start-step-state"' in page
    assert "後続条件が未決定でもGET-onlyの技術観測を1回開始できます" in page
    assert "C2手動access token入力: <strong>表示しない・受付APIなし</strong>" in page
    assert 'id="c2-credential-mode"' in page
    assert 'id="c2-gate-state"' in page
    assert 'id="c2-provider-state"' in page
    assert 'id="c2-kill-switch"' in page
    assert 'id="c2-start" type="button" disabled' in page
    assert 'id="c2-observation-ack" type="checkbox"' in page
    assert 'id="c2-observation-state"' in page
    assert 'id="c2-observation-attempt-count"' in page
    assert 'id="c2-observation-history"' in page
    assert 'id="c2-observation-next-action"' in page
    assert 'id="c2-noaccess-investigation"' in page
    assert "ETF11 quoteのNoAccess調査結果" in page
    assert "現在必要な利用者設定" in page
    assert "https://www.developer.saxo/openapi/learn/pricing" in page
    assert "openapi.help.saxo/hc/en-us/articles/4405160773661" in page
    assert "IDLE: 認証またはkill switch状態" in page
    assert "SUCCEEDED: GET=" in page
    assert "FAILED:" in page
    assert "readC2ObservationStatus()" in page
    assert "/api/c2/sim-read/observe" in page
    assert "SIM_APP_TRADING_DISABLED_GET_ONLY" in page
    assert "/api/c2/sim-read/readiness" in page
    assert "/api/c2/sim-read/prepare" not in page
    assert "c2OAuthStart.disabled = result.oauth_connection_allowed !== true;" in page
    assert "await beginOAuth(c2Message);" in page
    assert 'id="c2-config-blocker"' in page
    assert 'id="c2-app-key-state"' in page
    assert 'id="c2-runtime-callback-uri"' in page
    assert "SIM OAuth App Key" in page
    assert "http://localhost/saxo/oauth/callback" in page
    assert "trading</dt><dd><strong>disabled必須" in page
    assert 'id="c2-app-key-setup" hidden' in page
    assert 'id="c2-app-key-input" type="password"' in page
    assert 'id="c2-app-key-save"' in page
    assert 'id="c2-app-key-delete"' in page
    assert "安全に保存してOAuthを有効化" in page
    assert "/api/c2/oauth/app-key" in page
    assert "c2AppKeyInput.value = '';" in page
    assert "App Key設定 → C2 OAuth接続 → 初回SIM観測 → 後続provider/gate決定" in page
    assert "read -r -s" not in page
    assert "既存DB3定期更新を停止" in page
    assert "開始・停止ボタンはC2 SIM Readには作用しません" in page
    assert "c2OAuthStart.disabled = auth.status === 'AUTH_CONFIG_MISSING';" not in page
    assert 'id="c2-portal-action"' in page
    assert "https://www.developer.saxo/openapi/appmanagement" in page
    assert "既存SIM PKCE applicationを開き、App Keyをコピー" in page
    assert "SIGNAL_TOTAL_RETURN_DAILY — シグナル用adjusted total-return日足" in page
    assert "VALUATION_PRICE_DAILY — 評価用official-close日足" in page
    assert "通常監視を1時間ごとの遅延" in page
    assert "リアルタイムfeed不要" in page
    assert "二方向Bid/Askを要求しません" in page
    assert "現在必要な利用者設定:</strong> なし" in page
    assert "GET /api/v1/bars" in page
    assert "official close、total return、execution priceとは主張しません" in page
    assert "4418427366289-How-do-I-enable-market-data" in page
    assert "4417064381457-How-can-I-get-Stocks-ETFs-CFD-and-other-non-FX-on-my-demo-account" in page
    assert "GET allow-list 15件" in page
    assert "/api/c2/decisions/provider" in page
    assert "/api/c2/decisions/gate" in page
    assert "App Key・token・refresh credentialはこの欄へ入力しない" in page
    assert "初回SIM観測を止めません" in page
    assert "今回の初回SIM観測や後続decisionを完了しても、注文" in page


def test_c2_decision_guidance_keeps_unverified_providers_blocked_and_has_zero_io():
    result = c2_decision_guidance()
    assert set(result["provider_roles"]) == {
        "SIGNAL_TOTAL_RETURN_DAILY",
        "VALUATION_PRICE_DAILY",
    }
    assert all(
        role["recommended_action"] == "KEEP_BLOCKED"
        for role in result["provider_roles"].values()
    )
    proposed = result["operational_gate"]["proposed_values"]
    assert proposed["evaluation_mode"] == "LOW_FREQUENCY_DELAYED_OR_DAILY"
    assert proposed["max_quote_age_seconds"] == 90_000
    assert proposed["max_atomic_span_seconds"] == 90_000
    assert proposed["max_delayed_by_minutes"] == 60
    assert proposed["allow_sim_delayed_quotes"] is True
    assert proposed["accepted_price_types"] == ["Indicative", "Tradable"]
    assert proposed["require_two_sided_bid_ask"] is False
    assert result["operational_gate"]["explicit_start"]["planned_gets"] == 15
    assert result["operational_gate"]["explicit_start"]["action_exposed"] is True
    assert result["saxo_api_gets_performed"] == 0
    assert result["receipt_registration_performed"] is False
    assert result["db3_scheduler_changed"] is False
    assert result["orders_or_prechecks_sent"] == 0


def test_oauth_configuration_diagnostics_explains_missing_app_key_without_exposing_it():
    result = oauth_configuration_diagnostics(
        8765,
        "AUTH_CONFIG_MISSING",
        environment={},
        keychain_available=True,
    )
    assert result["status"] == "BLOCKED_CONFIG"
    assert result["blocker_code"] == "SIM_OAUTH_APP_KEY_NOT_SET"
    assert result["app_key_configured"] is False
    assert result["app_key_value_exposed"] is False
    assert result["portal_redirect_uri_required"] == "http://localhost/saxo/oauth/callback"
    assert result["runtime_callback_uri"] == "http://localhost:8765/saxo/oauth/callback"
    assert result["portal_trading_setting_required"] == "disabled"
    assert result["saxo_api_gets_performed"] == 0


def test_missing_app_key_with_existing_keychain_entry_requires_one_portal_copy_only():
    result = oauth_configuration_diagnostics(
        8765,
        "AUTH_CONFIG_MISSING",
        environment={},
        keychain_available=True,
        keychain_entry_present=True,
    )
    assert result["status"] == "BLOCKED_CONFIG"
    assert result["keychain_service_entry_present"] is True
    assert "Portalで必要な操作は1つだけ" in result["next_actions_ja"][0]
    assert "App Keyを1回コピー" in result["next_actions_ja"][0]
    assert "安全に保存してOAuthを有効化" in result["next_actions_ja"][1]
    assert "再起動せず" in result["next_actions_ja"][2]
    assert result["application_management_url"] == "https://www.developer.saxo/openapi/appmanagement"


def test_keychain_entry_probe_requests_metadata_only_and_discards_all_output(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr("market_db.operator_ui.sys.platform", "darwin")
    monkeypatch.setattr("market_db.operator_ui.Path.is_file", lambda _path: True)
    assert oauth_keychain_service_entry_present(runner=runner) is True
    command, kwargs = calls[0]
    assert command == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "com.tikeda.saxodb.oauth.sim",
    ]
    assert "-w" not in command
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_oauth_configuration_diagnostics_reports_ready_but_never_returns_app_key():
    app_key = "local-sim-app-key-never-returned"
    result = oauth_configuration_diagnostics(
        8765,
        "AUTH_LOGIN_REQUIRED",
        environment={"SAXO_OAUTH_APP_KEY": app_key},
        keychain_available=True,
    )
    assert result["status"] == "READY"
    assert result["technical_configuration_ready"] is True
    assert result["app_key_configured"] is True
    assert app_key not in json.dumps(result, ensure_ascii=False)


def test_oauth_ready_diagnostics_points_to_initial_observation_not_provider_decision():
    result = oauth_configuration_diagnostics(
        8765,
        "AUTH_READY",
        environment={"SAXO_OAUTH_APP_KEY": "local-app-key-never-returned"},
        keychain_available=True,
    )
    assert result["status"] == "READY"
    assert "初回SIM観測を開始" in result["next_actions_ja"][0]
    assert "provider/gate未決定でも15 GET" in result["next_actions_ja"][0]
    assert result["saxo_api_gets_performed"] == 0


def test_app_key_is_written_only_by_explicit_save_and_never_returned(monkeypatch):
    class Store:
        def __init__(self):
            self.value = None
            self.put_calls = 0
            self.delete_calls = 0

        def get(self, _account):
            return self.value

        def put(self, _account, value):
            self.put_calls += 1
            self.value = value

        def delete(self, _account):
            self.delete_calls += 1
            self.value = None

    class Manager:
        def __init__(self, config):
            self.config = config

        def status(self):
            return {
                "status": "AUTH_LOGIN_REQUIRED",
                "token_values_exposed": False,
                "orders_or_prechecks_sent": 0,
            }

    monkeypatch.delenv("SAXO_OAUTH_APP_KEY", raising=False)
    monkeypatch.setattr(
        "market_db.operator_ui.oauth_keychain_service_entry_present", lambda: False
    )
    store = Store()
    state = OperatorState(
        ReconcileJobManager(),
        8765,
        app_key_store=store,
        oauth_manager_factory=Manager,
    )
    assert store.put_calls == 0
    assert state.oauth_status()["status"] == "AUTH_CONFIG_MISSING"

    app_key = "public-client-id-never-returned"
    result = state.save_oauth_app_key(app_key)
    assert store.put_calls == 1
    assert store.value == app_key.encode("utf-8")
    assert result["status"] == "PASS"
    assert result["auth_status"] == "AUTH_LOGIN_REQUIRED"
    assert result["app_key_source"] == "MACOS_KEYCHAIN"
    assert result["app_key_value_exposed"] is False
    assert result["oauth_started"] is False
    assert result["saxo_api_gets_performed"] == 0
    assert app_key not in json.dumps(result)
    status = state.oauth_status()
    assert status["configuration"]["app_key_configured"] is True
    assert status["configuration"]["app_key_source"] == "MACOS_KEYCHAIN"
    assert app_key not in json.dumps(status)

    with pytest.raises(SaxoAuthError, match="DELETE_CONFIRMATION_REQUIRED"):
        state.delete_oauth_app_key("wrong")
    deleted = state.delete_oauth_app_key("DELETE_C2_OAUTH_APP_KEY_CONFIGURATION")
    assert store.delete_calls == 1
    assert deleted["app_key_configured"] is False
    assert deleted["oauth_started"] is False
    assert deleted["db_writes_performed"] == 0


def test_app_key_http_save_is_loopback_csrf_protected_and_redacted(monkeypatch):
    class Store:
        def __init__(self):
            self.value = None
            self.put_calls = 0

        def get(self, _account):
            return self.value

        def put(self, _account, value):
            self.put_calls += 1
            self.value = value

        def delete(self, _account):
            self.value = None

    class Manager:
        def __init__(self, config):
            self.config = config

        def status(self):
            return {
                "status": "AUTH_LOGIN_REQUIRED",
                "token_values_exposed": False,
                "orders_or_prechecks_sent": 0,
            }

    monkeypatch.delenv("SAXO_OAUTH_APP_KEY", raising=False)
    monkeypatch.setattr(
        "market_db.operator_ui.oauth_keychain_service_entry_present", lambda: False
    )
    store = Store()
    state = OperatorState(
        ReconcileJobManager(),
        0,
        app_key_store=store,
        oauth_manager_factory=Manager,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.port = int(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    app_key = "browser-public-client-id-never-returned"
    try:
        url = f"http://127.0.0.1:{state.port}/api/c2/oauth/app-key"
        blocked = urllib.request.Request(
            url,
            data=json.dumps({"app_key": app_key}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(blocked, timeout=2)
        assert captured.value.code == 403
        assert store.put_calls == 0

        allowed = urllib.request.Request(
            url,
            data=json.dumps({"app_key": app_key}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                "X-CSRF-Token": state.csrf_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            body = response.read()
            assert response.headers["Cache-Control"].startswith("no-store")
        assert store.put_calls == 1
        assert app_key.encode() not in body
        assert json.loads(body)["app_key_value_exposed"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_c2_provider_decision_is_saved_only_by_explicit_method_with_server_utc(
    monkeypatch,
):
    captured = []
    state = OperatorState.__new__(OperatorState)
    state.c2_decision_lock = threading.Lock()
    state.c2_provider_decisions = load_provider_decision_template()
    state.c2_operational_gates = load_operational_gate_template()
    state.c2_readiness = lambda: {"provider_and_gate_decisions_ready": False}
    monkeypatch.setattr(
        "market_db.operator_ui.save_provider_decisions",
        lambda value: captured.append(json.loads(json.dumps(value))) or value,
    )
    evidence = {
        "provider_id": "licensed-provider-a",
        "provider_legal_name": "Licensed Provider A",
        "source_contract_reference": "contract-ref-1",
        "license_and_redistribution_status": "internal redistribution approved",
        "definition_id": "adjusted-total-return-v1",
        "coverage_start": "2002-01-01",
        "publication_sla": "next daily cycle minus 30 minutes",
        "revision_policy": "append-only superseding receipt",
        "lineage_method": "immutable source receipt to normalized series",
        "content_identity_method": "ordered content sha256",
    }
    result = state.save_c2_provider_decision(
        {
            "dataset_role": "SIGNAL_TOTAL_RETURN_DAILY",
            "action": "APPROVE",
            "approved_by": "data owner",
            "rationale": "contract and RFI evidence reviewed",
            "evidence": evidence,
        }
    )
    assert len(captured) == 1
    selected = captured[0]["decisions"][0]
    assert selected["status"] == "APPROVED"
    assert selected["instrument_set"] == [
        "SPY", "IWM", "EFA", "EEM", "VNQ", "SHY", "IEF", "TLT", "TIP", "LQD", "GLD"
    ]
    assert selected["approved_by"] == "data owner"
    assert selected["approved_at_utc"].endswith("Z")
    assert selected["decision_basis"] == "contract and RFI evidence reviewed"
    assert result["saxo_api_gets_performed"] == 0
    assert result["receipt_registration_performed"] is False
    assert result["db_writes_performed"] == 0
    assert result["db3_scheduler_changed"] is False
    assert result["orders_or_prechecks_sent"] == 0


def test_c2_provider_approval_rejects_missing_evidence_without_saving(monkeypatch):
    saved = []
    state = OperatorState.__new__(OperatorState)
    state.c2_decision_lock = threading.Lock()
    state.c2_provider_decisions = load_provider_decision_template()
    state.c2_operational_gates = load_operational_gate_template()
    monkeypatch.setattr(
        "market_db.operator_ui.save_provider_decisions",
        lambda value: saved.append(value) or value,
    )
    with pytest.raises(C2DecisionError, match="EVIDENCE_MISSING"):
        state.save_c2_provider_decision(
            {
                "dataset_role": "SIGNAL_TOTAL_RETURN_DAILY",
                "action": "APPROVE",
                "approved_by": "data owner",
                "rationale": "insufficient evidence",
                "evidence": {},
            }
        )
    assert saved == []


def test_c2_operational_gate_acceptance_uses_structured_values_and_zero_io(
    monkeypatch,
):
    captured = []
    state = OperatorState.__new__(OperatorState)
    state.c2_decision_lock = threading.Lock()
    state.c2_provider_decisions = load_provider_decision_template()
    state.c2_operational_gates = load_operational_gate_template()
    state.c2_readiness = lambda: {"provider_and_gate_decisions_ready": False}
    monkeypatch.setattr(
        "market_db.operator_ui.save_operational_gates",
        lambda value: captured.append(json.loads(json.dumps(value))) or value,
    )
    result = state.save_c2_operational_gate(
        {
            "action": "ACCEPT",
            "accepted_by": "data owner",
            "rationale": "receipt-backed SIM research thresholds",
            "gate": {
                "accepted_base_currencies": ["EUR"],
                "evaluation_mode": "LOW_FREQUENCY_DELAYED_OR_DAILY",
                "max_quote_age_seconds": 90_000,
                "max_atomic_span_seconds": 90_000,
                "max_delayed_by_minutes": 60,
                "allow_sim_delayed_quotes": True,
                "accepted_price_types": ["Indicative", "Tradable"],
                "require_two_sided_bid_ask": False,
                "fee_unknown_policy": "AVAILABLE_WITH_WARNING_SIM_RESEARCH_ONLY",
                "issuer_revision_lookback_business_days": 5,
                "cash_correction_lookback_calendar_days": 60,
                "require_negative_event_state": True,
                "role_max_lag_seconds": {
                    "PROPOSAL_PRICE_SNAPSHOT": 10,
                    "INSTRUMENT_REFERENCE": 86400,
                },
            },
        }
    )
    assert len(captured) == 1
    selected = captured[0]
    assert selected["status"] == "ACCEPTED"
    assert selected["account_context"]["environment"] == "SIM"
    assert selected["account_context"]["require_all_11_etfs"] is True
    assert selected["quote"]["evaluation_mode"] == "LOW_FREQUENCY_DELAYED_OR_DAILY"
    assert selected["quote"]["max_quote_age_seconds"] == 90_000
    assert selected["quote"]["allow_sim_delayed_quotes"] is True
    assert selected["quote"]["require_two_sided_bid_ask"] is False
    assert selected["fee"]["unknown_policy"] == "AVAILABLE_WITH_WARNING_SIM_RESEARCH_ONLY"
    assert selected["accepted_at_utc"].endswith("Z")
    assert result["saxo_api_gets_performed"] == 0
    assert result["db_writes_performed"] == 0
    assert result["orders_or_prechecks_sent"] == 0


def test_c2_decision_http_post_is_same_origin_csrf_protected_and_user_triggered():
    calls = []
    state = OperatorState.__new__(OperatorState)
    state.manager = ReconcileJobManager()
    state.port = 0
    state.csrf_token = "decision-csrf"
    state.script_nonce = "decision-nonce"
    state.save_c2_provider_decision = lambda payload: calls.append(
        json.loads(json.dumps(payload))
    ) or {
        "status": "PASS",
        "decision_status": "DECISION_REQUIRED",
        "saxo_api_gets_performed": 0,
        "db_writes_performed": 0,
        "orders_or_prechecks_sent": 0,
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.port = int(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    payload = {
        "dataset_role": "SIGNAL_TOTAL_RETURN_DAILY",
        "action": "KEEP_BLOCKED",
        "approved_by": "data owner",
        "rationale": "provider evidence is not complete",
        "evidence": {},
    }
    try:
        url = f"http://127.0.0.1:{state.port}/api/c2/decisions/provider"
        blocked = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(blocked, timeout=2)
        assert captured.value.code == 403
        assert calls == []

        allowed = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                "X-CSRF-Token": state.csrf_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            result = json.loads(response.read())
            assert response.headers["Cache-Control"].startswith("no-store")
        assert calls == [payload]
        assert result["saxo_api_gets_performed"] == 0
        assert result["db_writes_performed"] == 0
        assert result["orders_or_prechecks_sent"] == 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_c2_initial_observation_requires_explicit_ack_but_not_provider_decisions(
    monkeypatch,
):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Adapter:
        def __init__(self):
            self.acks = []

        def open_observation_session(self, *, read_only_ack):
            self.acks.append(read_only_ack)
            return Session()

    state = OperatorState.__new__(OperatorState)
    state.c2_observation_lock = threading.Lock()
    state.c2_observation_state_lock = threading.Lock()
    state.c2_observation_audit = _empty_c2_observation_audit()
    state.c2_oauth_adapter = Adapter()
    state.c2_readiness = lambda: {
        "sim_observation_start_allowed": True,
        "provider_and_gate_decisions_ready": False,
    }
    observed = {
        "status": "PASS",
        "request_count": 15,
        "write_request_count": 0,
        "receipt_registration_performed": False,
        "db_writes_performed": 0,
        "orders_or_prechecks_sent": 0,
    }
    monkeypatch.setattr(
        "market_db.operator_ui.run_initial_sim_observation_session",
        lambda _session: observed,
    )
    persisted = []
    monkeypatch.setattr(
        "market_db.operator_ui.save_c2_observation_audit",
        lambda value: persisted.append(json.loads(json.dumps(value)))
        or json.loads(json.dumps(value)),
    )
    with pytest.raises(C2SIMReadOperationalError, match="READ_ONLY_ACK_REQUIRED"):
        state.start_c2_sim_observation("wrong")
    assert state.c2_oauth_adapter.acks == []

    result = state.start_c2_sim_observation(
        "SIM_APP_TRADING_DISABLED_GET_ONLY"
    )
    assert result == observed
    assert state.c2_oauth_adapter.acks == ["SIM_APP_TRADING_DISABLED_GET_ONLY"]
    status = state.c2_observation_status()
    assert status["status"] == "SUCCEEDED"
    assert status["attempt_count"] == 1
    assert status["last_observation"] == observed
    assert status["result_persisted"] is True
    assert status["persistence_scope"] == "SANITIZED_RUNTIME_LAST_RESULT_ONLY"
    assert status["db_writes_performed"] == 0
    assert [item["state"] for item in persisted] == ["RUNNING", "SUCCEEDED"]


def test_c2_observation_audit_is_owner_only_last_result_and_rejects_extra_fields(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("market_db.operator_ui.project_root", lambda: tmp_path)
    result = {
        "status": "FAIL_DATA_QUALITY",
        "error_code": "QUOTE_BID_INVALID",
        "failed_endpoint_id": None,
        "request_count": 15,
        "write_request_count": 0,
        "raw_response_saved": False,
        "receipt_registration_performed": False,
        "db_writes_performed": 0,
        "periodic_execution_started": False,
        "orders_or_prechecks_sent": 0,
        "credential_values_exposed": False,
    }
    audit = _empty_c2_observation_audit()
    audit.update(
        {
            "state": "FAILED",
            "attempt_count": 1,
            "captured_at_utc": "2026-07-31T05:00:00Z",
            "legacy_timestamp_unavailable": True,
            "last_observation": result,
        }
    )
    saved = save_c2_observation_audit(audit)
    assert load_c2_observation_audit() == saved
    path = tmp_path / ".runtime/c2/sim_observation_status.json"
    assert path.stat().st_mode & 0o777 == 0o600
    serialized = path.read_text(encoding="utf-8")
    assert "QUOTE_BID_INVALID" in serialized
    assert "token" not in serialized.casefold()
    unsafe = json.loads(json.dumps(audit))
    unsafe["last_observation"]["account_identifier"] = "must-not-persist"
    with pytest.raises(ValueError, match="AUDIT_INVALID"):
        save_c2_observation_audit(unsafe)


def test_c2_legacy_in_memory_result_can_be_adopted_without_raw_data(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("market_db.operator_ui.project_root", lambda: tmp_path)
    status = adopt_legacy_c2_observation_status(
        {
            "status": "COMPLETED_IN_MEMORY",
            "last_observation": {
                "status": "FAIL_DATA_QUALITY",
                "error_code": "QUOTE_BID_INVALID",
                "failed_endpoint_id": None,
                "request_count": 15,
                "write_request_count": 0,
                "raw_response_saved": False,
                "receipt_registration_performed": False,
                "db_writes_performed": 0,
                "periodic_execution_started": False,
                "orders_or_prechecks_sent": 0,
                "credential_values_exposed": False,
            },
        }
    )
    assert status["state"] == "FAILED"
    assert status["attempt_count"] == 1
    assert status["legacy_timestamp_unavailable"] is True
    assert status["last_observation"]["request_count"] == 15
    assert status["db_writes_performed"] == 0


def test_quote_bid_failure_guidance_explains_old_validator_and_keeps_manual_retry():
    audit = _empty_c2_observation_audit()
    audit.update(
        {
            "state": "FAILED",
            "attempt_count": 1,
            "last_observation": {
                "status": "FAIL_DATA_QUALITY",
                "error_code": "QUOTE_BID_INVALID",
            },
        }
    )
    result = c2_observation_operator_guidance(audit, retry_allowed=True)
    assert result["cause_classification"] == (
        "QUOTE_AVAILABILITY_MISCLASSIFIED_BY_INITIAL_VALIDATOR"
    )
    assert "PriceType" in result["failure_reason_ja"]
    assert "誤分類" in result["failure_reason_ja"]
    assert result["retry_allowed"] is True
    assert "自動再実行はしません" in result["next_action_ja"]
    assert "9:30–16:00 ET" in result["next_action_ja"]
    assert result["historical_result_rewritten"] is False


def test_noaccess_guidance_separates_feed_entitlement_from_initial_observation():
    audit = _empty_c2_observation_audit()
    audit.update(
        {
            "state": "SUCCEEDED",
            "attempt_count": 2,
            "last_observation": {
                "status": "PASS_WITH_WARNINGS",
                "request_count": 15,
                "write_request_count": 0,
                "quote_observation": {
                    "quote_count": 11,
                    "observed_price_types": ["NoAccess"],
                    "unavailable_quote_count": 11,
                },
            },
        }
    )

    result = c2_observation_operator_guidance(audit, retry_allowed=True)

    assert result["cause_classification"] == (
        "SAXO_ETF_PRICE_FEED_ENTITLEMENT_UNAVAILABLE"
    )
    assert "通常取引時間中" in result["failure_reason_ja"]
    assert "OAuth Read権限やApp Keyの障害" in result["failure_reason_ja"]
    assert "リアルタイムBid/Askを要求せず" in result["next_action_ja"]
    assert "利用者設定は不要" in result["next_action_ja"]
    assert "SIMをLIVE accountへlink" in result["next_action_ja"]
    assert result["historical_result_rewritten"] is False


def test_c2_initial_observation_http_post_is_explicit_same_origin_and_csrf_protected():
    calls = []
    state = OperatorState.__new__(OperatorState)
    state.port = 0
    state.csrf_token = "observation-csrf"
    state.script_nonce = "observation-nonce"
    state.start_c2_sim_observation = lambda confirmation: calls.append(confirmation) or {
        "status": "PASS",
        "request_count": 15,
        "write_request_count": 0,
        "receipt_registration_performed": False,
        "db_writes_performed": 0,
        "orders_or_prechecks_sent": 0,
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.port = int(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    payload = {"confirmation": "SIM_APP_TRADING_DISABLED_GET_ONLY"}
    try:
        url = f"http://127.0.0.1:{state.port}/api/c2/sim-read/observe"
        blocked = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(blocked, timeout=2)
        assert captured.value.code == 403
        assert calls == []

        allowed = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                "X-CSRF-Token": state.csrf_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            result = json.loads(response.read())
            assert response.headers["Cache-Control"].startswith("no-store")
        assert calls == ["SIM_APP_TRADING_DISABLED_GET_ONLY"]
        assert result["request_count"] == 15
        assert result["db_writes_performed"] == 0
        assert result["orders_or_prechecks_sent"] == 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_operator_oauth_reconcile_requires_separate_review_and_explicit_apply():
    state = OperatorState.__new__(OperatorState)
    with pytest.raises(
        SaxoAuthError, match="REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY"
    ):
        state.start_reconcile_with_oauth()


def test_operator_oauth_connection_does_not_depend_on_c2_provider_or_gate_decisions():
    class Manager:
        def begin_authorization(self):
            return PendingAuthorization(
                authorization_url="https://sim.logonvalidation.net/authorize?safe=1",
                state="state",
                code_verifier="v" * 64,
                redirect_uri="http://localhost:8765/saxo/oauth/callback",
            )

    state = OperatorState.__new__(OperatorState)
    state.oauth_manager = Manager()
    state.oauth_config_error = None
    state.oauth_lock = threading.Lock()
    state.pending_oauth = None
    result = state.begin_oauth()
    assert result["status"] == "AUTHORIZATION_REQUIRED"
    assert result["orders_or_prechecks_sent"] == 0
    assert result["token_values_exposed"] is False


def test_successful_oauth_starts_refresh_only_keeper_without_data_work():
    started = []

    class Manager:
        def complete_authorization(self, _pending, _code):
            return {"status": "AUTH_READY", "token_values_exposed": False}

    class Keeper:
        def start(self):
            started.append(True)
            return {
                "status": "RUNNING",
                "purpose": "OAUTH_REFRESH_ONLY",
                "saxo_api_gets_performed": 0,
            }

    pending = PendingAuthorization(
        authorization_url="https://sim.logonvalidation.net/authorize?safe=1",
        state="state",
        code_verifier="v" * 64,
        redirect_uri="http://localhost:8765/saxo/oauth/callback",
    )
    state = OperatorState.__new__(OperatorState)
    state.oauth_manager = Manager()
    state.oauth_lock = threading.Lock()
    state.pending_oauth = pending
    state.c2_oauth_refresh_keeper = Keeper()
    result = state.complete_oauth("state", "one-time-code", "")
    assert result["status"] == "AUTH_READY"
    assert started == [True]


def test_operator_requires_exact_loopback_origin_and_port():
    assert allowed_browser_request("127.0.0.1:8765", "http://127.0.0.1:8765", 8765)
    assert allowed_browser_request("localhost:8765", "http://localhost:8765", 8765)
    assert not allowed_browser_request("127.0.0.1:8765", None, 8765)
    assert not allowed_browser_request("127.0.0.1:8765", "http://evil.invalid", 8765)
    assert not allowed_browser_request("127.0.0.1:8765", "http://127.0.0.1:9999", 8765)


def test_generic_bearer_and_jwt_output_is_redacted():
    assert sanitized_line("Authorization: Bearer abc.def.ghi", "") == "Authorization: Bearer <redacted>"
    jwt = ".".join(("eyJabcdefgh", "abcdefghijk", "abcdefghijkl"))
    assert jwt not in sanitized_line(f"unexpected {jwt}", "")


def test_operator_http_is_no_store_and_rejects_cross_site_post(monkeypatch):
    class EmptyStore:
        def get(self, _account):
            return None

        def put(self, _account, _value):
            raise AssertionError("test must not write Keychain")

        def delete(self, _account):
            raise AssertionError("test must not delete Keychain")

    monkeypatch.delenv("SAXO_OAUTH_APP_KEY", raising=False)
    monkeypatch.setattr(
        "market_db.operator_ui.oauth_keychain_service_entry_present", lambda: False
    )
    state = OperatorState(ReconcileJobManager(), 0, app_key_store=EmptyStore())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.port = int(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{state.port}/"
        with urllib.request.urlopen(url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.headers["Cache-Control"].startswith("no-store")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
            assert "DB3 Acquisition Operator" in page

        with urllib.request.urlopen(f"http://127.0.0.1:{state.port}/api/oauth/status", timeout=2) as response:
            auth = json.loads(response.read())
            assert auth["status"] == "AUTH_CONFIG_MISSING"
            assert auth["token_values_exposed"] is False
            assert auth["configuration"]["status"] == "BLOCKED_CONFIG"
            assert auth["configuration"]["app_key_configured"] is False

        with urllib.request.urlopen(
            f"http://127.0.0.1:{state.port}/api/c2/sim-read/readiness", timeout=2
        ) as response:
            readiness = json.loads(response.read())
            assert readiness["status"] == "STOP_INITIAL_OAUTH_REQUIRED"
            assert readiness["saxo_api_gets_performed"] == 0
            assert readiness["automatic_start_allowed"] is False
            assert readiness["oauth_configuration"]["blocker_code"] == "SIM_OAUTH_APP_KEY_NOT_SET"

        c2_prepare = urllib.request.Request(
            f"http://127.0.0.1:{state.port}/api/c2/sim-read/prepare",
            data=b'{"access_token":"must-not-echo"}',
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                "X-CSRF-Token": state.csrf_token,
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(c2_prepare, timeout=2)
        assert captured.value.code == 404
        response_body = captured.value.read()
        assert b"not found" in response_body
        assert b"must-not-echo" not in response_body

        request = urllib.request.Request(
            f"http://127.0.0.1:{state.port}/api/reconcile",
            data=b'{"token":"not-used"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        assert captured.value.code == 403
        assert b"loopback origin required" in captured.value.read()

        reviewed_request = urllib.request.Request(
            f"http://127.0.0.1:{state.port}/api/reconcile/oauth",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                "X-CSRF-Token": state.csrf_token,
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(reviewed_request, timeout=2)
        assert captured.value.code == 409
        assert b"REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY" in captured.value.read()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
