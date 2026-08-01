"""Local-only, single-job Web operator for the DB3 Saxo reconciliation gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from .c2_external_decisions import (
    C2DecisionError,
    ETF11,
    load_operational_gates,
    load_provider_decisions,
    save_operational_gates,
    save_provider_decisions,
)
from .c2_sim_read_readiness import (
    c2_sim_read_readiness,
)
from .c2_sim_oauth import (
    C2OAuthRefreshKeeper,
    C2SIMOAuthCredentialAdapter,
    READ_ONLY_ACK,
)
from .c2_sim_read_session import (
    C2SIMReadOperationalError,
    run_initial_sim_observation_session,
)
from .connection import project_root
from .periodic_update import (
    ACTIVE_SCOPE_PROFILE,
    CANDIDATE_READY_SCOPE_PROFILE,
    candidate_scope_readiness,
)
from .periodic_update_service import start_service as start_periodic_service
from .periodic_update_service import status_service as periodic_service_status
from .periodic_update_service import stop_service as stop_periodic_service
from .saxo_auth import (
    APP_KEY_KEYCHAIN_ACCOUNT,
    APP_KEY_KEYCHAIN_SERVICE,
    CALLBACK_PATH,
    KEYCHAIN_SERVICE,
    MacOSKeychainStore,
    OAuthConfig,
    PendingAuthorization,
    SaxoAuthError,
    SaxoOAuthManager,
)


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_LINES = 500
RECONCILE_COMMAND = (sys.executable, "-m", "market_db.incremental_update", "reconcile")
TOKEN_ENVIRONMENT_KEY = "SAXO_ACCESS_TOKEN"
OAUTH_APP_KEY_ENVIRONMENT_KEY = "SAXO_OAUTH_APP_KEY"
SAXO_PORTAL_REDIRECT_URI = "http://localhost/saxo/oauth/callback"
SAXO_APPLICATION_MANAGEMENT_URL = "https://www.developer.saxo/openapi/appmanagement"
APP_KEY_DELETE_CONFIRMATION = "DELETE_C2_OAUTH_APP_KEY_CONFIGURATION"
PROVIDER_ROLES = {"SIGNAL_TOTAL_RETURN_DAILY", "VALUATION_PRICE_DAILY"}
PROVIDER_APPROVAL_FIELDS = {
    "provider_id", "provider_legal_name", "source_contract_reference",
    "license_and_redistribution_status", "definition_id", "coverage_start",
    "publication_sla", "revision_policy", "lineage_method",
    "content_identity_method",
}
DECISION_ACTIONS = {"KEEP_BLOCKED", "APPROVE", "REJECT"}
C2_OBSERVATION_AUDIT_RELATIVE_PATH = ".runtime/c2/sim_observation_status.json"
C2_OBSERVATION_STATES = {"IDLE", "READY", "RUNNING", "SUCCEEDED", "FAILED"}


def _safe_observation_value(value: Any) -> Any:
    """Copy a small public JSON value; reject values unsuitable for UI/audit."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("C2_OBSERVATION_AUDIT_VALUE_INVALID")
        return value
    if isinstance(value, str):
        if len(value) > 500 or any(ord(character) < 32 for character in value):
            raise ValueError("C2_OBSERVATION_AUDIT_VALUE_INVALID")
        return value
    if isinstance(value, list) and len(value) <= 32:
        return [_safe_observation_value(item) for item in value]
    raise ValueError("C2_OBSERVATION_AUDIT_VALUE_INVALID")


def sanitize_c2_observation_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the last observation result; never persist prices or identifiers."""

    scalar_fields = {
        "status", "observation_contract_id", "observed_at_utc",
        "minimum_format_identity_quote_checks", "error_code",
        "failed_endpoint_id", "warning_ids", "downstream_stage_status",
        "request_count", "write_request_count", "raw_response_saved",
        "receipt_registration_performed", "db_writes_performed",
        "periodic_execution_started", "orders_or_prechecks_sent",
        "credential_values_exposed",
    }
    section_fields = {
        "account_context": {
            "account_count", "account_currencies", "balance_currency",
            "currency_decimals", "data_level", "raw_identifiers_exposed",
        },
        "instrument_observation": {
            "instrument_count", "instrument_keys", "identity_check",
            "trading_eligibility_gate_applied",
        },
        "quote_observation": {
            "quote_count", "identity_and_reference_price_check",
            "identity_and_bid_ask_check",
            "max_quote_age_seconds", "max_delayed_by_minutes",
            "last_updated_span_seconds", "atomic_wall_span_seconds",
            "observed_price_types", "observed_price_sources",
            "valid_two_sided_quote_count", "unavailable_quote_count",
            "valid_reference_price_count", "single_sided_reference_count",
            "unavailable_instrument_keys", "missing_bid_count",
            "missing_ask_count", "observed_error_codes",
            "observed_market_states",
            "price_values_exposed",
        },
    }
    selected: dict[str, Any] = {}
    for field in scalar_fields:
        if field in value:
            selected[field] = _safe_observation_value(value[field])
    for section, allowed_fields in section_fields.items():
        observed = value.get(section)
        if observed is None:
            continue
        if not isinstance(observed, Mapping):
            raise ValueError("C2_OBSERVATION_AUDIT_VALUE_INVALID")
        selected[section] = {
            field: _safe_observation_value(observed[field])
            for field in allowed_fields
            if field in observed
        }
    if not isinstance(selected.get("status"), str):
        raise ValueError("C2_OBSERVATION_AUDIT_VALUE_INVALID")
    return selected


def _empty_c2_observation_audit() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "audit_type": "C2_SIM_OBSERVATION_LAST_RESULT",
        "state": "IDLE",
        "attempt_count": 0,
        "started_at_utc": None,
        "finished_at_utc": None,
        "captured_at_utc": None,
        "legacy_timestamp_unavailable": False,
        "last_observation": None,
        "sanitized_runtime_only": True,
        "raw_response_saved": False,
        "receipt_registration_performed": False,
        "db_writes_performed": 0,
        "orders_or_prechecks_sent": 0,
    }


def validate_c2_observation_audit(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = set(_empty_c2_observation_audit())
    if set(value) != expected_fields or not (
        value.get("schema_version") == 1
        and value.get("audit_type") == "C2_SIM_OBSERVATION_LAST_RESULT"
        and value.get("state") in C2_OBSERVATION_STATES
        and isinstance(value.get("attempt_count"), int)
        and not isinstance(value.get("attempt_count"), bool)
        and value["attempt_count"] >= 0
        and value.get("sanitized_runtime_only") is True
        and value.get("raw_response_saved") is False
        and value.get("receipt_registration_performed") is False
        and value.get("db_writes_performed") == 0
        and value.get("orders_or_prechecks_sent") == 0
        and isinstance(value.get("legacy_timestamp_unavailable"), bool)
    ):
        raise ValueError("C2_OBSERVATION_AUDIT_INVALID")
    for field in ("started_at_utc", "finished_at_utc", "captured_at_utc"):
        if value.get(field) is not None:
            _safe_observation_value(value[field])
    last = value.get("last_observation")
    if last is not None:
        if not isinstance(last, Mapping):
            raise ValueError("C2_OBSERVATION_AUDIT_INVALID")
        sanitized = sanitize_c2_observation_result(last)
        if dict(last) != sanitized:
            raise ValueError("C2_OBSERVATION_AUDIT_INVALID")
    if value["state"] in {"SUCCEEDED", "FAILED"} and last is None:
        raise ValueError("C2_OBSERVATION_AUDIT_INVALID")
    return json.loads(json.dumps(dict(value), allow_nan=False))


def save_c2_observation_audit(value: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically persist only the redacted last-result audit outside Git/DB."""

    selected_value = validate_c2_observation_audit(value)
    root = project_root().resolve()
    selected = root / C2_OBSERVATION_AUDIT_RELATIVE_PATH
    parent = selected.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if selected.is_symlink() or parent.resolve() != parent:
        raise C2SIMReadOperationalError(
            "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_AUDIT_PATH_INVALID"
        )
    try:
        parent.resolve().relative_to(root)
    except ValueError as exc:
        raise C2SIMReadOperationalError(
            "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_AUDIT_PATH_INVALID"
        ) from exc
    encoded = (
        json.dumps(selected_value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = parent / f".{selected.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, selected)
        os.chmod(selected, 0o600)
    except OSError as exc:
        raise C2SIMReadOperationalError(
            "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_AUDIT_WRITE_FAILED"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return selected_value


def load_c2_observation_audit() -> dict[str, Any]:
    selected = project_root() / C2_OBSERVATION_AUDIT_RELATIVE_PATH
    if not selected.exists():
        return _empty_c2_observation_audit()
    if not selected.is_file() or selected.is_symlink() or selected.stat().st_size > 65_536:
        raise C2SIMReadOperationalError(
            "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_AUDIT_INVALID"
        )
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
        return validate_c2_observation_audit(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise C2SIMReadOperationalError(
            "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_AUDIT_INVALID"
        ) from exc


def adopt_legacy_c2_observation_status(value: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve one pre-audit in-memory result before safely restarting the UI."""

    last = value.get("last_observation")
    if value.get("status") != "COMPLETED_IN_MEMORY" or not isinstance(last, Mapping):
        raise ValueError("C2_OBSERVATION_LEGACY_STATUS_NOT_COMPLETED")
    result = sanitize_c2_observation_result(last)
    state = "SUCCEEDED" if result["status"] in {"PASS", "PASS_WITH_WARNINGS"} else "FAILED"
    audit = _empty_c2_observation_audit()
    audit.update(
        {
            "state": state,
            "attempt_count": 1,
            "captured_at_utc": utc_now(),
            "legacy_timestamp_unavailable": True,
            "last_observation": result,
        }
    )
    return save_c2_observation_audit(audit)


def c2_observation_operator_guidance(
    audit: Mapping[str, Any], *, retry_allowed: bool
) -> dict[str, Any]:
    last = audit.get("last_observation")
    error_code = last.get("error_code") if isinstance(last, Mapping) else None
    quote_observation = (
        last.get("quote_observation") if isinstance(last, Mapping) else None
    )
    observed_price_types = (
        quote_observation.get("observed_price_types", [])
        if isinstance(quote_observation, Mapping)
        else []
    )
    unavailable_quote_count = (
        quote_observation.get("unavailable_quote_count", 0)
        if isinstance(quote_observation, Mapping)
        else 0
    )
    if (
        isinstance(last, Mapping)
        and last.get("status") == "PASS_WITH_WARNINGS"
        and "NoAccess" in observed_price_types
        and isinstance(unavailable_quote_count, int)
        and unavailable_quote_count > 0
    ):
        return {
            "cause_classification": "SAXO_ETF_PRICE_FEED_ENTITLEMENT_UNAVAILABLE",
            "failure_reason_ja": (
                "米国通常取引時間中の再観測でもSaxoがPriceType=NoAccessを返しました。"
                "Saxo公式定義では、これは現在の利用者・applicationの組合せに"
                "対象instrumentの価格feed権限がない状態です。15件のGET、account参照、"
                "ETF11 identityは成功しているため、OAuth Read権限やApp Keyの障害、"
                "単なる市場閉場、価格値の破損とは分類しません。"
            ),
            "retry_allowed": retry_allowed,
            "next_action_ja": (
                "初回SIM技術観測は完了しています。C2は四半期リバランスのため、"
                "リアルタイムBid/Askを要求せず、通常監視は1時間遅延価格または"
                "saxo_dbの日次終値を使います。現在は日次終値fallbackで進めるため"
                "利用者設定は不要です。純SIMで非FX market dataは提供されないため、"
                "遅延InfoPriceも併用したい場合だけSIMをLIVE accountへlinkし、"
                "LIVE側OpenAPI Accessでmarket-data免責に同意する設定フローを使います。"
            ),
            "historical_result_rewritten": False,
        }
    if error_code in {"QUOTE_BID_INVALID", "QUOTE_ASK_INVALID"}:
        return {
            "cause_classification": "QUOTE_AVAILABILITY_MISCLASSIFIED_BY_INITIAL_VALIDATOR",
            "failure_reason_ja": (
                "旧validatorがQuote.ErrorCode・PriceType・market stateより先に"
                "Bid/Askを正値必須として評価したため、閉場・NoMarket・Pending・"
                "NoAccess等の価格未提供もdata-quality FAILへ誤分類し得ました。"
                "raw価格を保存していないため、この旧実行の個別PriceTypeまでは断定しません。"
            ),
            "retry_allowed": retry_allowed,
            "next_action_ja": (
                "自動再実行はしません。必要なら修正版UIで明示再実行してください。"
                "米国ETFの価格availability確認はNYSE Arcaの取引時間内、できれば"
                "core session（9:30–16:00 ET）で行うと判定しやすくなります。"
                "宣言済みの価格未提供はPASS_WITH_WARNINGS、矛盾した価格はFAILEDのままです。"
            ),
            "historical_result_rewritten": False,
        }
    if audit.get("state") == "FAILED":
        return {
            "cause_classification": "OBSERVATION_FAILED_REVIEW_ERROR_CODE",
            "failure_reason_ja": "最終sanitized error codeを確認してください。",
            "retry_allowed": retry_allowed,
            "next_action_ja": "自動再実行はしません。原因確認後に明示再実行できます。",
            "historical_result_rewritten": False,
        }
    return {
        "cause_classification": None,
        "failure_reason_ja": None,
        "retry_allowed": retry_allowed,
        "next_action_ja": (
            "確認checkboxと開始ボタンを操作した場合だけ、GET-only観測を実行します。"
        ),
        "historical_result_rewritten": False,
    }


def _decision_text(value: Any, field: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        raise C2DecisionError(f"C2_DECISION_{field.upper()}_INVALID")
    selected = value.strip()
    if not selected or len(selected) > maximum or any(ord(character) < 32 for character in selected):
        raise C2DecisionError(f"C2_DECISION_{field.upper()}_INVALID")
    return selected


def c2_decision_guidance() -> dict[str, Any]:
    """Return the checked-in, non-secret decision guidance shown by the UI."""

    return {
        "status": "DOWNSTREAM_DECISIONS_REQUIRED_NON_BLOCKING_FOR_SIM_OBSERVATION",
        "provider_roles": {
            "SIGNAL_TOTAL_RETURN_DAILY": {
                "label_ja": "シグナル用adjusted total-return日足",
                "required_definition_ja": "11 ETFの分配金込みadjusted total-return。訂正履歴、lineage、content identityを再現できること。",
                "candidates": [
                    {
                        "id": "LICENSED_MARKET_DATA_PROVIDER",
                        "label_ja": "licensed market-data provider",
                        "recommendation": "RECOMMENDED_AFTER_EVIDENCE",
                        "reason_ja": "11 ETF、利用許諾、訂正履歴、point-in-time identity、SLAを一契約で証明できる候補。特定vendorはまだ未選定です。",
                    },
                    {
                        "id": "SAXO_CLOSE_PLUS_ISSUER_DISTRIBUTION",
                        "label_ja": "Saxo終値＋発行体分配から自作",
                        "recommendation": "FALLBACK_ONLY",
                        "reason_ja": "再投資時点、税・端数、split、訂正、算式をsaxo_db側で新たに所有するため第一候補ではありません。",
                    },
                    {
                        "id": "EXISTING_YAHOO_RESEARCH_DATASET",
                        "label_ja": "既存Yahoo研究snapshot",
                        "recommendation": "DO_NOT_PROMOTE_TO_CURRENT",
                        "reason_ja": "SIM_RESEARCH_ONLYであり、current運用向けの利用許諾・revision・SLA receiptを証明していません。",
                    },
                ],
                "recommended_action": "KEEP_BLOCKED",
                "recommended_action_ja": "特定providerの契約証拠が揃うまでは保留（推奨）",
                "unresolved_risks_ja": [
                    "providerと利用許諾が未確定",
                    "11 ETF完全coverageとpublication SLAが未証明",
                    "訂正履歴・lineage・ordered content identity receiptが未登録",
                ],
                "evidence_refs": [
                    "docs/c2_external_data_source_decision_ledger_proposal_20260731.md#41-edc-01-signal-total-return--edr-01",
                    "docs/c2_external_contract_receipt_resolution_report_20260731.md",
                ],
            },
            "VALUATION_PRICE_DAILY": {
                "label_ja": "評価用official-close日足",
                "required_definition_ja": "primary listing exchangeの未調整official close。venue、session date、revision identityを持つこと。",
                "candidates": [
                    {
                        "id": "LICENSED_PRIMARY_EXCHANGE_OFFICIAL_CLOSE",
                        "label_ja": "licensed official-close feed",
                        "recommendation": "RECOMMENDED_AFTER_EVIDENCE",
                        "reason_ja": "primary exchangeのOfficial Closing Priceを明示でき、total-returnと同一vendorならversion・SLA管理も揃えやすい候補です。",
                    },
                    {
                        "id": "ISSUER_OFFICIAL_PAGES",
                        "label_ja": "ETF発行体公式ページ",
                        "recommendation": "PARITY_ONLY",
                        "reason_ja": "独立照合には有用ですが、11 ETFの日次自動取得・訂正・SLAの正本は未証明です。",
                    },
                    {
                        "id": "SAXO_DAILY_CHART",
                        "label_ja": "Saxo Chart 1D",
                        "recommendation": "PARITY_ONLY",
                        "reason_ja": "broker OHLC候補であり、primary-exchange official closeであることを自動的には証明しません。",
                    },
                ],
                "recommended_action": "KEEP_BLOCKED",
                "recommended_action_ja": "official-close定義と契約証拠が揃うまでは保留（推奨）",
                "unresolved_risks_ja": [
                    "official-close provider未選定",
                    "全11 ticker/venueのsource identity未証明",
                    "Saxo 1Dとのparity閾値・不一致時のinstrument/session隔離方針が未承認",
                ],
                "evidence_refs": [
                    "docs/c2_external_data_source_decision_ledger_proposal_20260731.md#42-edc-02-valuation-official-close--edr-02",
                    "docs/c2_current_external_data_source_and_decision_proposal_20260731.md",
                ],
            },
        },
        "operational_gate": {
            "label_ja": "SIM allocation / paper evaluation向け運用gate",
            "recommended_action": "KEEP_BLOCKED",
            "recommended_action_ja": "四半期リバランス用に遅延Indicativeまたは日次価格を許容し、リアルタイムBid/Askを要求しない",
            "proposed_values": {
                "environment": "SIM",
                "require_all_11_etfs": True,
                "evaluation_mode": "LOW_FREQUENCY_DELAYED_OR_DAILY",
                "max_quote_age_seconds": 90_000,
                "max_atomic_span_seconds": 90_000,
                "max_delayed_by_minutes": 60,
                "allow_sim_delayed_quotes": True,
                "accepted_price_types": ["Indicative", "Tradable"],
                "require_two_sided_bid_ask": False,
                "normal_monitoring_cadence": "HOURLY_DELAYED_OR_DAILY_CLOSE",
                "fee_unknown_policy": "AVAILABLE_WITH_WARNING_SIM_RESEARCH_ONLY",
                "state_mapping": {
                    "late": "DATA_NOT_READY",
                    "interface": "BLOCKED_INTERFACE_OPERATIONAL",
                    "quality": "FAIL_DATA_QUALITY",
                },
            },
            "requires_user_evidence": [
                "accepted base currencyは認証済みaccount metadata receiptで確認する",
                "Indicative/遅延価格または日次終値のsource identityを記録する",
                "distribution訂正lookback日数を決める",
                "role別numeric SLAは採用providerの契約能力に合わせて決める",
            ],
            "unresolved_risks_ja": [
                "純SIMの非FX market dataは公式上利用不可で、現観測もNoAccess",
                "issuer distribution revision monitorとaccount-specific fee scheduleは未確定",
                "upstream accepted receipt不足のためrevision/SLAは未評価",
            ],
            "explicit_start": {
                "enabled_when_ja": "AUTH_READY、SIM/trading disabledの利用者確認、kill switch OFFのすべてを満たすこと。provider/gate未決定は初回観測を止めません。",
                "planned_gets": 15,
                "plan_ja": "session capabilities 1件、accounts 1件、balances 1件、instrument detail 11件、11 ETF atomic InfoPrice 1件。",
                "forbidden_ja": "write method、注文、precheck、取消、資金・口座変更、raw保存、DB receipt登録、periodic開始は行いません。",
                "action_exposed": True,
                "action_note_ja": "利用者が確認欄を選択し、SIM観測開始ボタンを押した時だけ1回実行します。",
            },
            "evidence_refs": [
                "docs/c2_external_data_source_decision_ledger_proposal_20260731.md#5-決定台帳",
                "docs/c2_sim_read_session_and_decision_flow_20260731.md",
                "manifests/c2_external_data_receipts_20260731.json",
            ],
        },
        "secrets_exposed": False,
        "saxo_api_gets_performed": 0,
        "receipt_registration_performed": False,
        "db3_scheduler_changed": False,
        "orders_or_prechecks_sent": 0,
    }


def oauth_keychain_service_entry_present(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    """Check only whether an OAuth service entry exists; never request its value."""

    if sys.platform != "darwin" or not Path("/usr/bin/security").is_file():
        return False
    try:
        result = runner(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def oauth_configuration_diagnostics(
    port: int,
    auth_status: str,
    *,
    environment: Mapping[str, str] | None = None,
    keychain_available: bool | None = None,
    keychain_entry_present: bool = False,
    app_key_configured_override: bool | None = None,
    app_key_source: str | None = None,
    app_key_keychain_entry_present: bool = False,
) -> dict[str, Any]:
    """Describe local OAuth prerequisites without exposing credential values."""

    selected_environment = os.environ if environment is None else environment
    app_key = selected_environment.get(OAUTH_APP_KEY_ENVIRONMENT_KEY, "").strip()
    environment_app_key_configured = bool(app_key)
    app_key_configured = (
        environment_app_key_configured
        if app_key_configured_override is None
        else app_key_configured_override
    )
    app_key_format_valid = app_key_configured and (
        not environment_app_key_configured or len(app_key) <= 256
    )
    callback_port_valid = 1_024 <= port <= 65_535
    if keychain_available is None:
        keychain_available = sys.platform == "darwin" and Path("/usr/bin/security").is_file()

    blocker_code: str | None = None
    blocker_message_ja: str
    next_actions_ja: list[str]
    if not app_key_configured:
        blocker_code = "SIM_OAUTH_APP_KEY_NOT_SET"
        if keychain_entry_present:
            blocker_message_ja = (
                "既存Saxo OAuthのKeychain entryはありますが、SIM OAuth App Keyは"
                "Operator UIの起動環境にありません。App KeyはKeychain credentialから"
                "復元できないため、C2 OAuth接続を開始できません。"
            )
            next_actions_ja = [
                "Portalで必要な操作は1つだけです: Application Managementで以前使用したSIM PKCE applicationを開き、App Keyを1回コピーする",
                "画面の「1. App Key設定」へ貼り付け、「安全に保存してOAuthを有効化」を押す",
                "保存後は再起動せず、そのまま「2. C2 OAuth接続」へ進む",
            ]
        else:
            blocker_message_ja = (
                "SIM OAuth App KeyがOperator UIの起動環境に未設定です。"
                "このためC2 OAuth接続を開始できません。"
            )
            next_actions_ja = [
                "Saxo Developer PortalでSIMのAuthorization Code Grant (PKCE) applicationを確認する",
                f"redirect URIを{SAXO_PORTAL_REDIRECT_URI}、tradingをdisabledにする",
                "画面の「1. App Key設定」へ貼り付け、「安全に保存してOAuthを有効化」を押す",
            ]
    elif not app_key_format_valid:
        blocker_code = "SIM_OAUTH_APP_KEY_FORMAT_INVALID"
        blocker_message_ja = (
            "SIM OAuth App Keyは設定されていますが、許容される形式ではありません。"
            "値は表示していません。"
        )
        next_actions_ja = [
            "Saxo Developer Portalの対象SIM PKCE applicationからApp Keyを再確認する",
            "Operator UIの起動環境だけを修正して再起動する",
        ]
    elif not callback_port_valid:
        blocker_code = "SIM_OAUTH_CALLBACK_PORT_INVALID"
        blocker_message_ja = "Operator UIのcallback portが許容範囲外です。"
        next_actions_ja = ["Operator UIを--port 8765で再起動する"]
    elif not keychain_available:
        blocker_code = "MACOS_KEYCHAIN_UNAVAILABLE"
        blocker_message_ja = (
            "refresh credentialの保存先となるmacOS Keychainを利用できません。"
            "tokenを別の保存先へfallbackしません。"
        )
        next_actions_ja = ["macOS上でOperator UIを起動し、Keychainを利用可能にする"]
    elif auth_status in {
        "AUTH_KEYCHAIN_VALUE_INVALID",
        "AUTH_KEYCHAIN_READ_FAILED",
        "AUTH_APP_KEY_MISMATCH",
    }:
        blocker_code = auth_status
        blocker_message_ja = (
            "App Keyは設定済みですが、既存のKeychain credentialを安全に利用できません。"
        )
        next_actions_ja = [
            "C2 OAuth runbookのKeychain recovery手順を確認し、Operator UIから再認証する"
        ]
    elif auth_status == "AUTH_READY":
        blocker_message_ja = "SIM OAuthの技術設定と認証は完了しています。"
        next_actions_ja = [
            "画面のSIM/trading-disabled確認を選び、「初回SIM観測を開始」を押す。provider/gate未決定でも15 GETの技術観測は実行できる"
        ]
    else:
        blocker_message_ja = (
            "ローカルの技術設定は揃っています。Saxo側のPKCE・redirect URI・"
            "trading disabledを確認後、C2用Saxo OAuth接続を押してください。"
        )
        next_actions_ja = ["C2用Saxo OAuth接続を押し、初回だけSaxo SIMで認証する"]

    technical_configuration_ready = (
        app_key_format_valid
        and callback_port_valid
        and keychain_available
        and blocker_code is None
    )
    result = {
        "status": "READY" if technical_configuration_ready else "BLOCKED_CONFIG",
        "technical_configuration_ready": technical_configuration_ready,
        "blocker_code": blocker_code,
        "blocker_message_ja": blocker_message_ja,
        "next_actions_ja": next_actions_ja,
        "environment": "SIM",
        "authorization_grant": "Authorization Code Grant (PKCE)",
        "app_key_environment_variable": OAUTH_APP_KEY_ENVIRONMENT_KEY,
        "app_key_configured": app_key_configured,
        "app_key_format_valid": app_key_format_valid,
        "app_key_source": app_key_source or (
            "PROCESS_ENVIRONMENT" if environment_app_key_configured else "NOT_SET"
        ),
        "app_key_keychain_entry_present": app_key_keychain_entry_present,
        "app_key_keychain_service": APP_KEY_KEYCHAIN_SERVICE,
        "app_key_value_exposed": False,
        "portal_redirect_uri_required": SAXO_PORTAL_REDIRECT_URI,
        "runtime_callback_uri": f"http://localhost:{port}{CALLBACK_PATH}",
        "portal_trading_setting_required": "disabled",
        "portal_settings_verification": "USER_CONFIRMATION_REQUIRED",
        "application_management_url": SAXO_APPLICATION_MANAGEMENT_URL,
        "keychain_service_entry_present": keychain_entry_present,
        "keychain_available": keychain_available,
        "refresh_credential_storage": "macOS Keychain only",
        "browser_app_key_input_allowed": not app_key_configured,
        "browser_token_input_allowed": False,
        "browser_credential_input_allowed": False,
        "token_values_exposed": False,
        "saxo_api_gets_performed": 0,
        "orders_or_prechecks_sent": 0,
    }
    app_key = ""
    return result
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[^\s\"']+")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def operator_periodic_scope_profile() -> str:
    """Select the candidate scope only after its persisted activation gate passes."""

    readiness = candidate_scope_readiness()
    return (
        CANDIDATE_READY_SCOPE_PROFILE
        if readiness.get("status") == "PASS"
        else ACTIVE_SCOPE_PROFILE
    )


def sanitized_line(value: str, token: str) -> str:
    selected = value.replace(token, "<redacted>") if token else value
    selected = _BEARER_PATTERN.sub("Bearer <redacted>", selected)
    return _JWT_PATTERN.sub("<redacted-jwt>", selected).rstrip("\r\n")


def child_environment(token: str) -> dict[str, str]:
    selected = os.environ.copy()
    selected.pop(TOKEN_ENVIRONMENT_KEY, None)
    selected[TOKEN_ENVIRONMENT_KEY] = token
    return selected


def oauth_child_environment() -> dict[str, str]:
    """Inherit the AppKey but never hand a static access token to the OAuth job."""

    selected = os.environ.copy()
    selected.pop(TOKEN_ENVIRONMENT_KEY, None)
    return selected


class JobAlreadyRunning(RuntimeError):
    pass


class InvalidAccessToken(ValueError):
    pass


class ReconcileJobManager:
    """Run one fixed reconcile command and expose only sanitized progress."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        command: Iterable[str] = RECONCILE_COMMAND,
        cwd: Path | None = None,
    ) -> None:
        self._popen_factory = popen_factory
        self._command = tuple(command)
        self._cwd = cwd or project_root()
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None

    def start(self, access_token: str) -> dict[str, Any]:
        token = access_token.strip()
        if not token or len(token) > 8_192 or any(ord(character) < 32 for character in token):
            raise InvalidAccessToken("有効なSaxo SIM tokenを入力してください。")

        return self._start(
            command=self._command,
            environment=child_environment(token),
            redaction_token=token,
        )

    def start_oauth(self, *, callback_port: int) -> dict[str, Any]:
        if not 1_024 <= callback_port <= 65_535:
            raise ValueError("callback port must be between 1024 and 65535")
        return self._start(
            command=(
                *self._command,
                "--auth-mode", "keychain",
                "--callback-port", str(callback_port),
            ),
            environment=oauth_child_environment(),
            redaction_token="",
        )

    def _start(
        self,
        *,
        command: Iterable[str],
        environment: dict[str, str],
        redaction_token: str,
    ) -> dict[str, Any]:
        selected_command = tuple(command)
        with self._lock:
            if self._current is not None and self._current["status"] == "RUNNING":
                raise JobAlreadyRunning("reconcile jobは既に実行中です。")
            job_id = f"db3-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
            process = self._popen_factory(
                list(selected_command),
                cwd=self._cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=False,
            )
            self._current = {
                "job_id": job_id,
                "status": "RUNNING",
                "started_at_utc": utc_now(),
                "finished_at_utc": None,
                "exit_code": None,
                "command_id": "market_db.incremental_update.reconcile",
                "orders_or_prechecks_sent": 0,
                "output": deque(maxlen=MAX_OUTPUT_LINES),
            }
            worker = threading.Thread(
                target=self._collect,
                args=(job_id, process, redaction_token),
                name=f"saxo-db-{job_id}",
                daemon=True,
            )
            worker.start()
        return self.status()

    def _collect(self, job_id: str, process: subprocess.Popen[str], token: str) -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    safe_line = sanitized_line(line, token)
                    if safe_line:
                        with self._lock:
                            if self._current is not None and self._current["job_id"] == job_id:
                                self._current["output"].append(safe_line)
            exit_code = int(process.wait())
            final_status = "PASS" if exit_code == 0 else "FAILED"
        except Exception as exc:  # Output only the exception class, never its token-bearing message.
            exit_code = 1
            final_status = "FAILED"
            with self._lock:
                if self._current is not None and self._current["job_id"] == job_id:
                    self._current["output"].append(f"operator runner failed: {type(exc).__name__}")
        finally:
            token = ""
        with self._lock:
            if self._current is not None and self._current["job_id"] == job_id:
                self._current["status"] = final_status
                self._current["exit_code"] = exit_code
                self._current["finished_at_utc"] = utc_now()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._current is None:
                return {
                    "job_id": None,
                    "status": "IDLE",
                    "orders_or_prechecks_sent": 0,
                    "output": [],
                }
            return {
                key: list(value) if key == "output" else value
                for key, value in self._current.items()
            }


def allowed_browser_request(host: str, origin: str | None, port: int) -> bool:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in allowed_hosts or origin is None:
        return False
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == port
            and not parsed.path.rstrip("/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def operator_html(csrf_token: str, script_nonce: str) -> bytes:
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="csrf-token" content="{csrf_token}">
  <title>saxo_db DB3 Operator</title>
  <style nonce="{script_nonce}">
    :root {{ color-scheme: light; --ink:#10231c; --muted:#5d6f67; --paper:#f3f1e8; --card:#fffdf7; --line:#d8d4c5; --green:#116149; --red:#9f2f2f; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#dce9df,transparent 42%),var(--paper); }}
    main {{ width:min(900px,calc(100% - 32px)); margin:48px auto; }}
    header {{ margin-bottom:24px; }}
    h1 {{ margin:0 0 8px; font:600 34px/1.15 Georgia,serif; }}
    p {{ color:var(--muted); }}
    .badge {{ display:inline-block; padding:5px 10px; border:1px solid #91ad9f; border-radius:999px; color:var(--green); background:#edf7f0; font-weight:700; font-size:12px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:24px; box-shadow:0 14px 40px rgba(32,50,42,.09); margin-top:18px; }}
    label {{ display:block; font-weight:700; margin-bottom:8px; }}
    input,select,textarea {{ width:100%; border:1px solid #a9b2ac; border-radius:10px; padding:11px 12px; font:14px ui-monospace,SFMono-Regular,Menlo,monospace; background:white; }}
    textarea {{ min-height:82px; resize:vertical; }}
    button {{ margin-top:14px; border:0; border-radius:10px; padding:12px 18px; background:var(--green); color:white; font-weight:800; cursor:pointer; }}
    button:disabled {{ opacity:.55; cursor:wait; }}
    .notice {{ padding:12px 14px; border-left:4px solid var(--green); background:#edf7f0; color:#294b3e; }}
    .warning {{ padding:12px 14px; border-left:4px solid var(--red); background:#fff0ed; color:#732424; font-weight:700; }}
    .config-grid {{ display:grid; grid-template-columns:minmax(190px,1fr) 2fr; gap:8px 16px; margin:14px 0; }}
    .config-grid dt {{ font-weight:800; }} .config-grid dd {{ margin:0; color:var(--muted); }}
    .command {{ min-height:0; margin-top:10px; }}
    [hidden] {{ display:none !important; }}
    .status {{ display:flex; gap:10px; align-items:center; margin-bottom:12px; font-weight:800; }}
    .dot {{ width:10px; height:10px; border-radius:50%; background:#7d8b85; }}
    .dot.running {{ background:#d38818; }} .dot.pass {{ background:#14905f; }} .dot.failed {{ background:var(--red); }}
    pre {{ margin:0; min-height:120px; max-height:440px; overflow:auto; white-space:pre-wrap; word-break:break-word; border-radius:12px; padding:16px; color:#dcebe3; background:#10231c; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    .error {{ color:var(--red); font-weight:700; }}
    .console-links {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:14px; }}
    .console-links a {{ display:inline-flex; padding:10px 14px; border:1px solid #91ad9f; border-radius:10px; color:var(--green); background:#edf7f0; text-decoration:none; font-weight:800; }}
    .decision-card {{ border:1px solid var(--line); border-radius:14px; padding:16px; margin:14px 0; background:#fff; }}
    .decision-card h4 {{ margin:0 0 8px; }}
    .decision-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 14px; }}
    .decision-grid .wide {{ grid-column:1 / -1; }}
    .decision-card ul {{ margin-top:6px; }}
    details {{ margin:12px 0; }}
    summary {{ cursor:pointer; font-weight:800; color:var(--green); }}
    .mini {{ font-size:13px; color:var(--muted); }}
    @media (max-width:700px) {{ .decision-grid {{ grid-template-columns:1fr; }} .decision-grid .wide {{ grid-column:auto; }} }}
  </style>
</head>
<body>
<main>
  <header><span class="badge">SIM / GET ONLY / LOOPBACK</span><h1>DB3 Acquisition Operator</h1><p>定期取得と認証を管理します。DataVersion変更は警告として記録され、この画面から自動reconcileしません。</p></header>
  <section class="card">
    <h2>データ管理・可視化</h2>
    <p>管理中の商品、時系列データの意味、期間、品質、チャートを確認できます。</p>
    <div class="console-links"><a href="http://127.0.0.1:8766/ui/overview">Data Consoleを開く</a><a href="http://127.0.0.1:8766/ui/catalog">商品・データ辞書を開く</a></div>
  </section>
  <section class="card">
    <h2>無人定期更新</h2>
    <div class="notice">ここは既存DB3の市場データ定期更新です。開始・停止ボタンはC2 SIM Readには作用しません。</div>
    <div class="notice">OAuthではrefresh credentialだけをmacOS Keychainへ保存します。access tokenはscheduler processのメモリだけで使用します。</div>
    <div class="notice">現在の一時scopeはEURUSDとETF 11系列です。USDJPYはprovider品質問題の訂正版DataVersion確認まで取得対象外です。</div>
    <p>認証: <strong id="auth-state">確認中</strong> ／ scheduler: <strong id="periodic-state">確認中</strong></p>
    <button id="oauth-start" type="button">Saxo OAuth接続</button>
    <button id="periodic-start" type="button">既存DB3定期更新を開始</button>
    <button id="periodic-stop" type="button">既存DB3定期更新を停止</button>
    <p id="periodic-message" aria-live="polite"></p>
  </section>
  <section class="card">
    <div class="notice">revisionの確認とデータ置換は分離されています。Read APIで警告と証跡をreviewし、承認記録後に専用CLIのapplyを明示実行してください。</div>
    <button id="oauth-reconcile" type="button" disabled>自動reconcileは無効です</button>
    <p id="oauth-reconcile-message" aria-live="polite">DataVersion検知だけではschedulerや系列を停止しません。</p>
  </section>
  <section class="card">
    <h2>C2 SIM Read実行準備</h2>
    <div class="notice">初回SIM観測と、allocation/PnL向けprovider・fee・SLA承認は別工程です。AUTH_READYなら、後続条件が未決定でもGET-onlyの技術観測を1回開始できます。</div>
    <p><strong>操作順:</strong> App Key設定 → C2 OAuth接続 → 初回SIM観測 → 後続provider/gate決定。LIVE注文は対象外です。</p>
    <h3>現在の技術設定</h3>
    <div id="c2-config-blocker" class="warning" aria-live="polite">OAuth設定を確認中です。</div>
    <dl class="config-grid">
      <dt>SIM OAuth App Key</dt><dd id="c2-app-key-state">確認中（値は常に非表示）</dd>
      <dt>認可方式</dt><dd>Authorization Code Grant (PKCE)</dd>
      <dt>Saxo Portal redirect URI</dt><dd><code id="c2-portal-redirect-uri">http://localhost/saxo/oauth/callback</code></dd>
      <dt>実行時callback</dt><dd><code id="c2-runtime-callback-uri">確認中</code></dd>
      <dt>Saxo Portal trading</dt><dd><strong>disabled必須</strong>（画面から自動確認できないため接続前に人が確認）</dd>
      <dt>credential保存先</dt><dd id="c2-keychain-state">macOS Keychainを確認中</dd>
    </dl>
    <div id="c2-portal-action" class="warning">
      <strong>Portalで必要な操作:</strong>
      <span id="c2-portal-action-text">診断中です。</span>
      <a href="https://www.developer.saxo/openapi/appmanagement" target="_blank" rel="noopener noreferrer">Saxo Application Managementを開く</a>
    </div>
    <p><strong>次の1手:</strong> <span id="c2-config-actions">診断待ちです。</span></p>
    <h3>1. App Key設定</h3>
    <div id="c2-app-key-setup" hidden>
      <p>App KeyはPKCE public client identifierです。保存ボタンを押した時だけ、OAuth credentialとは別のmacOS Keychain entryへ保存します。値は再表示・log・DB・Git・browser storageへ保存しません。</p>
      <label for="c2-app-key-input">Saxo SIM PKCE App Key</label>
      <input id="c2-app-key-input" type="password" maxlength="256" autocomplete="new-password" spellcheck="false" autocapitalize="off">
      <button id="c2-app-key-save" type="button">安全に保存してOAuthを有効化</button>
      <p id="c2-app-key-message" aria-live="polite"></p>
    </div>
    <div id="c2-app-key-configured" class="notice" hidden>
      <p><strong>App Keyは設定済みです（値非表示）。</strong> 再起動後もmacOS Keychainから自動読込します。</p>
      <button id="c2-app-key-delete" type="button">App Key設定を削除・置換する</button>
      <p>削除は別確認後にApp Key専用entryだけへ作用し、OAuth refresh credentialやDB3 schedulerは変更しません。</p>
    </div>
    <p>readiness: <strong id="c2-readiness-state">確認中</strong></p>
    <p>認証方式: <strong id="c2-credential-mode">確認中</strong></p>
    <h3>2. C2 OAuth接続</h3>
    <p>状態: <strong id="c2-oauth-step-state">確認中</strong> ／ refresh維持: <strong id="c2-refresh-keeper-state">確認中</strong></p>
    <p>SIM固定・trading disabledのapplicationで初回だけ認証し、refresh credentialをmacOS Keychainへ保存します。この段階ではSaxo API GETを実行しません。</p>
    <button id="c2-oauth-start" type="button">C2用Saxo OAuth接続</button>
    <h3>3. 初回SIM観測開始</h3>
    <p>再実行readiness: <strong id="c2-start-step-state">確認中</strong> ／ 観測実行状態: <strong id="c2-observation-state">確認中</strong> ／ 実行回数: <strong id="c2-observation-attempt-count">0</strong></p>
    <p><strong>開始条件:</strong> AUTH_READY、SIM限定、Saxo Portalのtrading disabledを利用者が確認、kill switch OFF、明示クリック。</p>
    <p><strong>取得:</strong> GET allow-list 15件（session capabilities 1、accounts 1、balances 1、instrument detail 11、11 ETF atomic InfoPrice 1）。response形式、account/instrument identity、11 ETF集合と、提供されたreference priceの正値・UTC時刻・PriceSource/PriceTypeを検査します。C2は低頻度用途なので<code>Mid</code>/<code>Bid</code>/<code>Ask</code>のどれか一つで足り、遅延<code>Indicative</code>を正常扱いします。</p>
    <p><strong>行わないこと:</strong> raw保存、receipt/DB登録、periodic、provider/SLA判定、allocation、PnL、注文、precheck、取消、資金・口座変更。</p>
    <label><input id="c2-observation-ack" type="checkbox" style="width:auto"> このSaxo applicationがSIM限定・trading disabledであり、GET-only観測15件だけを開始することを確認しました。</label>
    <button id="c2-start" type="button" disabled>初回SIM観測を開始</button>
    <p id="c2-message" aria-live="polite"></p>
    <div id="c2-observation-summary" class="notice" aria-live="polite">観測履歴を確認中です。</div>
    <p><strong>理由・次の操作:</strong> <span id="c2-observation-next-action">確認中です。</span></p>
    <pre id="c2-observation-history">sanitizedな最終結果はまだありません。</pre>
    <p class="mini">最終1件だけをGit管理外のowner-only runtime監査へ保存します。raw response、token、account ID、価格値、DB receiptは保存しません。</p>
    <article id="c2-noaccess-investigation" class="decision-card">
      <h4>ETF11 quoteのNoAccess調査結果</h4>
      <p><strong>原因:</strong> 米国通常取引時間中の再観測でもETF11全件が<code>PriceType=NoAccess</code>でした。Saxo公式定義では、現在の利用者・applicationに対象価格feedの権限がない状態です。OAuth Read権限とETF identity参照は成功しており、App Key不良、市場閉場、価格値破損ではありません。</p>
      <p><strong>代替:</strong> feed権限があればInfoPriceは<code>Indicative</code>（遅延を含む）等を返し得ます。ただし、streaming PricesやChartへ切り替えても同じfeed権限を回避できません。</p>
      <p><strong>C2の必要水準:</strong> 四半期リバランスの通常監視は1時間ごとの遅延<code>Indicative</code>価格、またはsaxo_dbの正規日次終値で足ります。ティック、リアルタイム、二方向Bid/Askは初回観測・通常監視・低頻度paper評価の必須条件にしません。</p>
      <p><strong>段階別判定:</strong> 初回の接続・identity確認は<code>SUCCEEDED / PASS_WITH_WARNINGS</code>です。現在の<code>NoAccess</code>は純SIMの非FX制約としてquote availabilityだけへ記録し、低頻度paper評価全体は日次終値経路で継続できます。実約定確認が必要な将来段階とLIVE注文は対象外です。</p>
      <p><strong>日次fallback取得経路:</strong> localhost Read APIの<code>GET /api/v1/bars</code>へ11 ETFごとに<code>layer=1d</code>と期間を指定し、<code>native_ohlc</code>の<code>close</code>を取得します。これはmanaged daily closeであり、official close、total return、execution priceとは主張しません。更新・freshnessは既存DB3の系列管理で別判定します。</p>
      <p class="notice"><strong>現在必要な利用者設定:</strong> なし。C2はsaxo_db Read APIの日次終値を使う低頻度経路で進めます。Saxoの遅延InfoPriceも使いたい場合だけ、Developer PortalのLive ApplicationsからSIMをLIVE accountへlinkし、LIVE側SaxoTraderのOpenAPI Accessでmarket-data免責へ同意する一つの設定フローが必要です。今回は実行しません。</p>
      <p class="mini">公式根拠: <a href="https://openapi.help.saxo/hc/en-us/articles/4418427366289-How-do-I-enable-market-data" target="_blank" rel="noopener noreferrer">OpenAPI market data有効化とSIM制約</a>・<a href="https://openapi.help.saxo/hc/en-us/articles/4405160773661-Why-do-I-get-NoAccess-instead-of-prices" target="_blank" rel="noopener noreferrer">純SIM非FXのNoAccess</a>・<a href="https://openapi.help.saxo/hc/en-us/articles/4417064381457-How-can-I-get-Stocks-ETFs-CFD-and-other-non-FX-on-my-demo-account" target="_blank" rel="noopener noreferrer">SIMとLIVE accountのlink</a>・<a href="https://www.developer.saxo/openapi/learn/pricing" target="_blank" rel="noopener noreferrer">Indicative / delayed price</a>。詳細は<code>docs/c2_sim_quote_noaccess_resolution_20260731.md</code>。</p>
    </article>
    <h3>4. 後続段階: provider / allocation・paper評価gate</h3>
    <p>状態: <strong id="c2-decision-step-state">確認中</strong></p>
    <p>運用gate: <strong id="c2-gate-state">確認中</strong></p>
    <p>provider決定: <strong id="c2-provider-state">確認中</strong></p>
    <div class="notice">ここで未決定のprovider、current total-return、official close、calendar/provider SLA、distribution lookback、account fee、PnL品質、receipt登録は、初回SIM観測を止めません。SIM allocation/paper評価へ進む前に必要です。</div>
    <div class="warning">保存はこの画面で利用者がボタンを押した時だけ行います。App Key・token・refresh credentialはこの欄へ入力しないでください。</div>
    <article class="decision-card provider-decision" data-role="SIGNAL_TOTAL_RETURN_DAILY">
      <h4>SIGNAL_TOTAL_RETURN_DAILY — シグナル用adjusted total-return日足</h4>
      <p>必要条件: 11 ETFの分配金込みadjusted total-return、訂正履歴、利用許諾、lineage、content identity、publication SLA。</p>
      <p><strong>候補と推奨:</strong> licensed market-data providerを証拠確認後に採用。Saxo終値＋発行体分配の自作はfallback、既存Yahoo snapshotはSIM_RESEARCH_ONLYのためcurrentへ昇格しません。</p>
      <p class="warning"><strong>現在の推奨:</strong> 特定provider・契約・SLAの証拠が揃うまで「保留」を選ぶ。</p>
      <p class="mini">未解決: provider/利用許諾、11 ETF coverage、訂正履歴、ordered-content identity、SLA receipt。</p>
      <div class="decision-grid">
        <p><label>判断<select class="provider-action"><option value="KEEP_BLOCKED">保留（推奨）</option><option value="APPROVE">証拠付きで承認</option><option value="REJECT">このroleでは不採用</option></select></label></p>
        <p><label>承認者・判断者<input class="provider-actor" maxlength="200" autocomplete="off" placeholder="例: data owner"></label></p>
        <p class="wide"><label>判断根拠<textarea class="provider-rationale" maxlength="2000" placeholder="参照した契約・receipt・判断理由"></textarea></label></p>
      </div>
      <details><summary>「証拠付きで承認」に必要なprovider情報</summary>
        <div class="decision-grid provider-evidence">
          <p><label>provider_id<input data-field="provider_id" maxlength="1000"></label></p>
          <p><label>provider legal name<input data-field="provider_legal_name" maxlength="1000"></label></p>
          <p class="wide"><label>source contract reference<input data-field="source_contract_reference" maxlength="1000"></label></p>
          <p class="wide"><label>license / redistribution status<input data-field="license_and_redistribution_status" maxlength="1000"></label></p>
          <p><label>definition_id<input data-field="definition_id" maxlength="1000"></label></p>
          <p><label>coverage_start<input data-field="coverage_start" maxlength="1000" placeholder="YYYY-MM-DD"></label></p>
          <p class="wide"><label>publication SLA<input data-field="publication_sla" maxlength="1000"></label></p>
          <p class="wide"><label>revision policy<input data-field="revision_policy" maxlength="1000"></label></p>
          <p class="wide"><label>lineage method<input data-field="lineage_method" maxlength="1000"></label></p>
          <p class="wide"><label>content identity method<input data-field="content_identity_method" maxlength="1000"></label></p>
        </div>
      </details>
      <button class="provider-save" type="button">このprovider判断を記録</button>
      <p class="provider-message" aria-live="polite"></p>
    </article>
    <article class="decision-card provider-decision" data-role="VALUATION_PRICE_DAILY">
      <h4>VALUATION_PRICE_DAILY — 評価用official-close日足</h4>
      <p>必要条件: primary listing exchangeの未調整official close、venue、session date、revision identity、11 ETF coverage。</p>
      <p><strong>候補と推奨:</strong> licensed official-close feedを証拠確認後に採用。発行体ページとSaxo Chart 1Dはparity evidenceに限定します。</p>
      <p class="warning"><strong>現在の推奨:</strong> official-close定義・provider契約・parity方針が揃うまで「保留」を選ぶ。</p>
      <p class="mini">未解決: provider、全ticker/venue identity、Saxo 1D parity、不一致時のinstrument/session隔離、SLA。</p>
      <div class="decision-grid">
        <p><label>判断<select class="provider-action"><option value="KEEP_BLOCKED">保留（推奨）</option><option value="APPROVE">証拠付きで承認</option><option value="REJECT">このroleでは不採用</option></select></label></p>
        <p><label>承認者・判断者<input class="provider-actor" maxlength="200" autocomplete="off" placeholder="例: data owner"></label></p>
        <p class="wide"><label>判断根拠<textarea class="provider-rationale" maxlength="2000" placeholder="参照した契約・receipt・判断理由"></textarea></label></p>
      </div>
      <details><summary>「証拠付きで承認」に必要なprovider情報</summary>
        <div class="decision-grid provider-evidence">
          <p><label>provider_id<input data-field="provider_id" maxlength="1000"></label></p>
          <p><label>provider legal name<input data-field="provider_legal_name" maxlength="1000"></label></p>
          <p class="wide"><label>source contract reference<input data-field="source_contract_reference" maxlength="1000"></label></p>
          <p class="wide"><label>license / redistribution status<input data-field="license_and_redistribution_status" maxlength="1000"></label></p>
          <p><label>definition_id<input data-field="definition_id" maxlength="1000"></label></p>
          <p><label>coverage_start<input data-field="coverage_start" maxlength="1000" placeholder="YYYY-MM-DD"></label></p>
          <p class="wide"><label>publication SLA<input data-field="publication_sla" maxlength="1000"></label></p>
          <p class="wide"><label>revision policy<input data-field="revision_policy" maxlength="1000"></label></p>
          <p class="wide"><label>lineage method<input data-field="lineage_method" maxlength="1000"></label></p>
          <p class="wide"><label>content identity method<input data-field="content_identity_method" maxlength="1000"></label></p>
        </div>
      </details>
      <button class="provider-save" type="button">このprovider判断を記録</button>
      <p class="provider-message" aria-live="polite"></p>
    </article>
    <article class="decision-card" id="c2-gate-decision">
      <h4>SIM allocation / paper evaluation向け運用gate</h4>
      <p><strong>現在の低頻度方針:</strong> 四半期リバランスでは、通常監視を1時間ごとの遅延<code>Indicative</code>価格または日次終値とします。quote age/spanは最大25時間、delayは60分まで許容し、二方向Bid/Askを要求しません。</p>
      <p class="notice"><strong>リアルタイムfeed不要:</strong> <code>DelayedByMinutes &gt; 0</code>と<code>PriceType=Indicative</code>は正常な観測値です。NoAccess時は日次終値fallbackを使い、初回観測・通常監視・低頻度paper評価全体を止めません。</p>
      <p class="mini">未解決: account/quote receiptは旧AUTH_NOT_READY、issuer revision monitor、account-specific fee schedule、upstream SLA。</p>
      <div class="decision-grid">
        <p><label>判断<select id="c2-gate-action"><option value="KEEP_BLOCKED">保留（推奨）</option><option value="ACCEPT">入力値で承認</option><option value="REJECT">運用gateを不採用</option></select></label></p>
        <p><label>承認者・判断者<input id="c2-gate-actor" maxlength="200" autocomplete="off" placeholder="例: data owner"></label></p>
        <p class="wide"><label>判断根拠<textarea id="c2-gate-rationale" maxlength="2000" placeholder="参照したreceipt・契約・許容理由"></textarea></label></p>
      </div>
      <details><summary>「入力値で承認」に必要な運用値</summary>
        <div class="decision-grid">
          <p><label>accepted base currencies<input id="gate-base-currencies" placeholder="例: EUR（account receiptと一致させる）"></label></p>
          <p><label>accepted PriceType<input id="gate-price-types" value="Indicative,Tradable" placeholder="Indicative,Tradable"></label></p>
          <p><label>max quote age（秒）<input id="gate-quote-age" type="number" min="0.001" step="any" value="90000"></label></p>
          <p><label>max atomic span（秒）<input id="gate-atomic-span" type="number" min="0.001" step="any" value="90000"></label></p>
          <p><label>max delayed by（分）<input id="gate-delay" type="number" min="0" step="1" value="60"></label></p>
          <p><label>SIM delayed quote<select id="gate-allow-delayed"><option value="true">正常値として許容（推奨）</option></select></label></p>
          <p class="wide">価格評価mode: <code>LOW_FREQUENCY_DELAYED_OR_DAILY</code> ／ 二方向Bid/Ask: <strong>必須にしない</strong></p>
          <p class="wide"><label>fee UNKNOWN policy<select id="gate-fee-policy"><option value="AVAILABLE_WITH_WARNING_SIM_RESEARCH_ONLY">SIM研究限定warning（推奨）</option><option value="BLOCK_CONSUMER">consumerを停止</option></select></label></p>
          <p><label>issuer revision lookback（営業日）<input id="gate-issuer-lookback" type="number" min="1" step="1"></label></p>
          <p><label>cash correction lookback（暦日）<input id="gate-cash-lookback" type="number" min="1" step="1"></label></p>
          <p class="wide"><label>negative-event state<select id="gate-negative-state"><option value="true">必須（推奨）</option><option value="false">必須にしない</option></select></label></p>
          <p class="wide"><label>role別max lag秒（JSON object）<textarea id="gate-sla-json" placeholder='例: {{"PROPOSAL_PRICE_SNAPSHOT":10,"INSTRUMENT_REFERENCE":86400}}'></textarea></label></p>
        </div>
      </details>
      <button id="c2-gate-save" type="button">この運用gate判断を記録</button>
      <p id="c2-gate-message" aria-live="polite"></p>
    </article>
    <p class="mini">保存先: <code>.runtime/c2/provider_decision.json</code> ／ <code>.runtime/c2/operational_gate_decision.json</code>。owner-only・atomic writeで、repo・DB・browser storageには保存しません。</p>
    <h3>5. LIVE_ORDER_ELIGIBILITY</h3>
    <div class="warning">PROHIBITED。今回の初回SIM観測や後続decisionを完了しても、注文・precheck・口座操作は有効になりません。</div>
    <p id="c2-readiness-actions"></p>
    <p>C2手動access token入力: <strong>表示しない・受付APIなし</strong> ／ kill switch: <strong id="c2-kill-switch">確認中</strong></p>
  </section>
  <section class="card">
    <div class="notice">汎用token入力によるreconcileもreview-first policyでは無効です。</div>
    <p><label for="token">Saxo SIM token（reconcile用途は無効）</label><input id="token" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" disabled></p>
    <button id="start" type="button" disabled>自動reconcileは無効です</button>
    <p id="message" aria-live="polite"></p>
  </section>
  <section class="card">
    <div class="status"><span id="dot" class="dot"></span><span id="state">IDLE</span></div>
    <pre id="output">jobはまだ開始されていません。</pre>
  </section>
</main>
<script nonce="{script_nonce}">
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const tokenInput = document.querySelector('#token');
const startButton = document.querySelector('#start');
const message = document.querySelector('#message');
const state = document.querySelector('#state');
const dot = document.querySelector('#dot');
const output = document.querySelector('#output');
const authState = document.querySelector('#auth-state');
const periodicState = document.querySelector('#periodic-state');
const oauthStart = document.querySelector('#oauth-start');
const periodicStart = document.querySelector('#periodic-start');
const periodicStop = document.querySelector('#periodic-stop');
const periodicMessage = document.querySelector('#periodic-message');
const oauthReconcile = document.querySelector('#oauth-reconcile');
const oauthReconcileMessage = document.querySelector('#oauth-reconcile-message');
const c2ReadinessState = document.querySelector('#c2-readiness-state');
const c2CredentialMode = document.querySelector('#c2-credential-mode');
const c2OAuthStepState = document.querySelector('#c2-oauth-step-state');
const c2RefreshKeeperState = document.querySelector('#c2-refresh-keeper-state');
const c2DecisionStepState = document.querySelector('#c2-decision-step-state');
const c2StartStepState = document.querySelector('#c2-start-step-state');
const c2OAuthStart = document.querySelector('#c2-oauth-start');
const c2GateState = document.querySelector('#c2-gate-state');
const c2ProviderState = document.querySelector('#c2-provider-state');
const c2KillSwitch = document.querySelector('#c2-kill-switch');
const c2ReadinessActions = document.querySelector('#c2-readiness-actions');
const c2Start = document.querySelector('#c2-start');
const c2Message = document.querySelector('#c2-message');
const c2ObservationAck = document.querySelector('#c2-observation-ack');
const c2ObservationState = document.querySelector('#c2-observation-state');
const c2ObservationAttemptCount = document.querySelector('#c2-observation-attempt-count');
const c2ObservationSummary = document.querySelector('#c2-observation-summary');
const c2ObservationNextAction = document.querySelector('#c2-observation-next-action');
const c2ObservationHistory = document.querySelector('#c2-observation-history');
const c2ConfigBlocker = document.querySelector('#c2-config-blocker');
const c2AppKeyState = document.querySelector('#c2-app-key-state');
const c2PortalRedirectUri = document.querySelector('#c2-portal-redirect-uri');
const c2RuntimeCallbackUri = document.querySelector('#c2-runtime-callback-uri');
const c2KeychainState = document.querySelector('#c2-keychain-state');
const c2ConfigActions = document.querySelector('#c2-config-actions');
const c2PortalAction = document.querySelector('#c2-portal-action');
const c2PortalActionText = document.querySelector('#c2-portal-action-text');
const c2AppKeySetup = document.querySelector('#c2-app-key-setup');
const c2AppKeyConfigured = document.querySelector('#c2-app-key-configured');
const c2AppKeyInput = document.querySelector('#c2-app-key-input');
const c2AppKeySave = document.querySelector('#c2-app-key-save');
const c2AppKeyDelete = document.querySelector('#c2-app-key-delete');
const c2AppKeyMessage = document.querySelector('#c2-app-key-message');
const c2GateAction = document.querySelector('#c2-gate-action');
const c2GateActor = document.querySelector('#c2-gate-actor');
const c2GateRationale = document.querySelector('#c2-gate-rationale');
const c2GateSave = document.querySelector('#c2-gate-save');
const c2GateMessage = document.querySelector('#c2-gate-message');
let pollTimer = null;
let c2ObservationReady = false;

function updateObservationButton() {{
  c2Start.disabled = !(c2ObservationReady && c2ObservationAck.checked);
}}

function renderC2Observation(result) {{
  c2ObservationState.textContent = result.status || 'IDLE';
  c2ObservationAttemptCount.textContent = String(result.attempt_count || 0);
  const last = result.last_observation;
  if (result.status === 'RUNNING') {{
    c2ObservationSummary.className = 'notice';
    c2ObservationSummary.textContent = `RUNNING: ${{result.started_at_utc || '開始時刻確認中'}} からGET-only観測を実行中です。`;
  }} else if (result.status === 'SUCCEEDED') {{
    c2ObservationSummary.className = 'notice';
    c2ObservationSummary.textContent = `SUCCEEDED: GET=${{last.request_count}}、write=${{last.write_request_count}}、DB/receipt/order=0。`;
  }} else if (result.status === 'FAILED') {{
    c2ObservationSummary.className = 'warning';
    const timestamp = result.legacy_timestamp_unavailable ? '実行時刻は旧メモリ記録のため不明' : (result.finished_at_utc || '終了時刻不明');
    c2ObservationSummary.textContent = `FAILED: ${{last.error_code || last.status || 'UNKNOWN'}}、GET=${{last.request_count ?? '不明'}}、write=${{last.write_request_count ?? '不明'}}。${{timestamp}}。`;
  }} else if (result.status === 'READY') {{
    c2ObservationSummary.className = 'notice';
    c2ObservationSummary.textContent = 'READY: まだ観測結果はありません。確認checkboxと開始ボタンでのみ実行します。';
  }} else {{
    c2ObservationSummary.className = 'warning';
    c2ObservationSummary.textContent = 'IDLE: 認証またはkill switch状態を確認してください。';
  }}
  c2ObservationNextAction.textContent = [result.failure_reason_ja, result.next_action_ja].filter(Boolean).join(' ');
  c2ObservationHistory.textContent = last ? JSON.stringify(last, null, 2) : 'sanitizedな最終結果はまだありません。';
}}

async function readC2ObservationStatus() {{
  const response = await fetch('/api/c2/sim-read/observation', {{ cache:'no-store', credentials:'same-origin' }});
  renderC2Observation(await response.json());
}}

function render(job) {{
  state.textContent = job.status;
  dot.className = `dot ${{job.status.toLowerCase()}}`;
  output.textContent = (job.output || []).join('\\n') || 'sanitized outputを待機しています。';
  output.scrollTop = output.scrollHeight;
  startButton.disabled = true;
  oauthReconcile.disabled = true;
  if (job.status !== 'RUNNING' && pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
}}

async function readStatus() {{
  const response = await fetch('/api/status', {{ cache:'no-store', credentials:'same-origin' }});
  render(await response.json());
}}

async function readOperationalStatus() {{
  const [authResponse, periodicResponse] = await Promise.all([
    fetch('/api/oauth/status', {{ cache:'no-store', credentials:'same-origin' }}),
    fetch('/api/periodic/status', {{ cache:'no-store', credentials:'same-origin' }})
  ]);
  const auth = await authResponse.json();
  const periodic = await periodicResponse.json();
  authState.textContent = auth.status;
  periodicState.textContent = periodic.status;
  oauthStart.disabled = auth.status === 'AUTH_CONFIG_MISSING';
  periodicStart.disabled = auth.status !== 'AUTH_READY';
}}

async function readC2Readiness() {{
  const response = await fetch('/api/c2/sim-read/readiness', {{ cache:'no-store', credentials:'same-origin' }});
  const result = await response.json();
  const workflow = result.workflow_steps || [];
  const config = result.oauth_configuration || {{}};
  c2ConfigBlocker.textContent = config.blocker_message_ja || 'OAuth設定の診断情報を取得できません。';
  c2ConfigBlocker.className = config.technical_configuration_ready ? 'notice' : 'warning';
  c2AppKeyState.textContent = config.app_key_configured ? '設定済み（値は非表示）' : '未設定';
  c2AppKeySetup.hidden = config.app_key_configured === true;
  c2AppKeyConfigured.hidden = config.app_key_configured !== true;
  c2PortalRedirectUri.textContent = config.portal_redirect_uri_required || 'http://localhost/saxo/oauth/callback';
  c2RuntimeCallbackUri.textContent = config.runtime_callback_uri || '確認できません';
  c2KeychainState.textContent = config.keychain_available ? '利用可能（refresh credentialのみ）' : '利用不可';
  if (config.keychain_service_entry_present && !config.app_key_configured) {{
    c2PortalAction.className = 'warning';
    c2PortalActionText.textContent = '1つだけです。既存SIM PKCE applicationを開き、App Keyをコピーしてください。アプリの作成・変更やOAuth loginはまだ行いません。';
  }} else if (config.app_key_configured) {{
    c2PortalAction.className = 'notice';
    c2PortalActionText.textContent = 'App Keyは設定済みです。接続前にredirect URIとtrading disabledを確認してください。';
  }} else {{
    c2PortalAction.className = 'warning';
    c2PortalActionText.textContent = 'SIM PKCE applicationの存在確認が必要です。作成・変更は明示承認後に行ってください。';
  }}
  c2ConfigActions.textContent = (config.next_actions_ja || []).join(' → ') || 'runbookを確認してください。';
  c2ReadinessState.textContent = result.status;
  c2CredentialMode.textContent = result.credential_mode;
  c2OAuthStepState.textContent = (workflow.find(item => item.id === 'OAUTH_CONNECTION') || {{}}).status || 'UNKNOWN';
  c2RefreshKeeperState.textContent = (result.oauth_refresh_keeper || {{}}).status || 'UNKNOWN';
  c2DecisionStepState.textContent = (workflow.find(item => item.id === 'PROVIDER_AND_OPERATIONAL_GATE_DECISION') || {{}}).status || 'UNKNOWN';
  c2StartStepState.textContent = (workflow.find(item => item.id === 'SIM_OBSERVATION_START') || {{}}).status || 'UNKNOWN';
  c2GateState.textContent = result.operational_gate_status;
  c2ProviderState.textContent = Object.entries(result.provider_decision_statuses || {{}}).map(([key,value]) => `${{key}}=${{value}}`).join(' ／ ');
  c2KillSwitch.textContent = result.kill_switch_engaged ? 'ENGAGED' : 'OFF';
  c2ReadinessActions.textContent = (result.user_actions || []).join(' ／ ') || '明示開始待ちです。';
  c2OAuthStart.disabled = result.oauth_connection_allowed !== true;
  c2OAuthStart.textContent = result.auth_ready ? 'Saxo OAuthを再接続' : 'C2用Saxo OAuth接続';
  c2ObservationReady = result.sim_observation_start_allowed === true;
  updateObservationButton();
}}

async function readC2DecisionStatus() {{
  const response = await fetch('/api/c2/decisions', {{ cache:'no-store', credentials:'same-origin' }});
  const result = await response.json();
  const provider = result.current_provider_decisions || {{ decisions:[] }};
  document.querySelectorAll('.provider-decision').forEach(card => {{
    const role = card.dataset.role;
    const observed = (provider.decisions || []).find(item => item.dataset_role === role);
    const target = card.querySelector('.provider-message');
    if (observed && !target.dataset.userMessage) target.textContent = `現在の保存状態: ${{observed.status}}`;
  }});
  if (!c2GateMessage.dataset.userMessage) {{
    c2GateMessage.textContent = `現在の保存状態: ${{(result.current_operational_gate || {{}}).status || 'UNKNOWN'}}`;
  }}
}}

async function postDecision(path, payload) {{
  const response = await fetch(path, {{
    method:'POST', credentials:'same-origin', cache:'no-store',
    headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},
    body:JSON.stringify(payload)
  }});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.status || `HTTP ${{response.status}}`);
  return result;
}}

async function saveProviderDecision(card) {{
  const button = card.querySelector('.provider-save');
  const messageTarget = card.querySelector('.provider-message');
  const action = card.querySelector('.provider-action').value;
  const evidence = {{}};
  if (action === 'APPROVE') {{
    card.querySelectorAll('.provider-evidence [data-field]').forEach(input => {{ evidence[input.dataset.field] = input.value.trim(); }});
  }}
  button.disabled = true;
  messageTarget.dataset.userMessage = 'true';
  messageTarget.className = 'provider-message';
  messageTarget.textContent = '判断を検証して安全に保存しています…';
  try {{
    const result = await postDecision('/api/c2/decisions/provider', {{
      dataset_role:card.dataset.role,
      action,
      approved_by:card.querySelector('.provider-actor').value.trim(),
      rationale:card.querySelector('.provider-rationale').value.trim(),
      evidence
    }});
    messageTarget.textContent = `保存済み: ${{result.decision_status}} ／ ${{result.reviewed_at_utc}} UTC`;
    await readC2Readiness();
    await readC2DecisionStatus();
  }} catch (error) {{ messageTarget.className='provider-message error'; messageTarget.textContent=error.message; }}
  finally {{ button.disabled=false; }}
}}

function commaValues(selector, uppercase=false) {{
  return document.querySelector(selector).value.split(',').map(value => value.trim()).filter(Boolean).map(value => uppercase ? value.toUpperCase() : value);
}}

async function saveGateDecision() {{
  const action = c2GateAction.value;
  let gate = {{}};
  c2GateSave.disabled = true;
  c2GateMessage.dataset.userMessage = 'true';
  c2GateMessage.className = '';
  c2GateMessage.textContent = '運用gateを検証して安全に保存しています…';
  try {{
    if (action === 'ACCEPT') {{
      let roleMaxLag;
      try {{ roleMaxLag = JSON.parse(document.querySelector('#gate-sla-json').value); }}
      catch (_) {{ throw new Error('role別max lag秒はJSON objectで入力してください。'); }}
      gate = {{
        accepted_base_currencies:commaValues('#gate-base-currencies', true),
        evaluation_mode:'LOW_FREQUENCY_DELAYED_OR_DAILY',
        max_quote_age_seconds:Number(document.querySelector('#gate-quote-age').value),
        max_atomic_span_seconds:Number(document.querySelector('#gate-atomic-span').value),
        max_delayed_by_minutes:Number(document.querySelector('#gate-delay').value),
        allow_sim_delayed_quotes:document.querySelector('#gate-allow-delayed').value === 'true',
        accepted_price_types:commaValues('#gate-price-types'),
        require_two_sided_bid_ask:false,
        fee_unknown_policy:document.querySelector('#gate-fee-policy').value,
        issuer_revision_lookback_business_days:Number(document.querySelector('#gate-issuer-lookback').value),
        cash_correction_lookback_calendar_days:Number(document.querySelector('#gate-cash-lookback').value),
        require_negative_event_state:document.querySelector('#gate-negative-state').value === 'true',
        role_max_lag_seconds:roleMaxLag
      }};
    }}
    const result = await postDecision('/api/c2/decisions/gate', {{
      action,
      accepted_by:c2GateActor.value.trim(),
      rationale:c2GateRationale.value.trim(),
      gate
    }});
    c2GateMessage.textContent = `保存済み: ${{result.gate_status}} ／ ${{result.reviewed_at_utc}} UTC`;
    await readC2Readiness();
    await readC2DecisionStatus();
  }} catch (error) {{ c2GateMessage.className='error'; c2GateMessage.textContent=error.message; }}
  finally {{ c2GateSave.disabled=false; }}
}}

async function startC2Observation() {{
  if (!c2ObservationAck.checked) {{
    c2Message.className='error';
    c2Message.textContent='SIM限定・trading disabled・GET-only観測の確認が必要です。';
    return;
  }}
  c2Start.disabled = true;
  c2Message.className='';
  c2ObservationState.textContent='RUNNING';
  c2Message.textContent='GET-onlyの初回SIM観測15件を実行しています。sanitizedな最終結果だけをruntime監査へ保存し、DB/receiptへは保存しません…';
  try {{
    const result = await postDecision('/api/c2/sim-read/observe', {{confirmation:'SIM_APP_TRADING_DISABLED_GET_ONLY'}});
    const account = result.account_context || {{}};
    const instruments = result.instrument_observation || {{}};
    const quotes = result.quote_observation || {{}};
    c2Message.textContent = `観測 ${{result.status}}: GET=${{result.request_count}}、write=${{result.write_request_count}}、account currency=${{(account.account_currencies || []).join('/')}}、instrument=${{instruments.instrument_count}}、quote=${{quotes.quote_count}}、max delay=${{quotes.max_delayed_by_minutes}}分。DB/receipt/注文=0。`;
    c2ObservationAck.checked = false;
    await readC2Readiness();
  }} catch (error) {{ c2Message.className='error'; c2Message.textContent=error.message; }}
  finally {{ await readC2ObservationStatus().catch(() => {{}}); updateObservationButton(); }}
}}

async function postOperation(path) {{
  const response = await fetch(path, {{
    method:'POST', credentials:'same-origin', cache:'no-store',
    headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}}, body:'{{}}'
  }});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.status || `HTTP ${{response.status}}`);
  return result;
}}

async function saveAppKey() {{
  let appKey = c2AppKeyInput.value.trim();
  if (!appKey) {{ c2AppKeyMessage.className='error'; c2AppKeyMessage.textContent='App Keyを入力してください。'; return; }}
  let requestBody = JSON.stringify({{ app_key: appKey }});
  c2AppKeyInput.value = '';
  appKey = '';
  c2AppKeySave.disabled = true;
  c2AppKeyMessage.className = '';
  c2AppKeyMessage.textContent = 'macOS Keychainへ安全に保存しています…';
  try {{
    const response = await fetch('/api/c2/oauth/app-key', {{
      method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}}, body:requestBody
    }});
    requestBody = '';
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.status || `HTTP ${{response.status}}`);
    c2AppKeyMessage.textContent = 'App Keyを保存しました。値は再表示されません。続けてC2用Saxo OAuth接続を押してください。';
    await readOperationalStatus();
    await readC2Readiness();
  }} catch (error) {{
    requestBody = '';
    c2AppKeyMessage.className='error';
    c2AppKeyMessage.textContent=error.message;
    c2AppKeySave.disabled=false;
  }}
}}

async function deleteAppKey() {{
  if (!window.confirm('App Key専用Keychain設定を削除します。OAuth refresh credentialとDB3 schedulerは変更しません。続行しますか？')) return;
  c2AppKeyDelete.disabled = true;
  try {{
    const response = await fetch('/api/c2/oauth/app-key/delete', {{
      method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},
      body:JSON.stringify({{ confirm:'DELETE_C2_OAUTH_APP_KEY_CONFIGURATION' }})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.status || `HTTP ${{response.status}}`);
    c2AppKeyMessage.className='';
    c2AppKeyMessage.textContent='App Key設定を削除しました。置換する場合は新しいApp Keyを入力してください。';
    await readOperationalStatus();
    await readC2Readiness();
  }} catch (error) {{ c2AppKeyMessage.className='error'; c2AppKeyMessage.textContent=error.message; }}
  finally {{ c2AppKeyDelete.disabled=false; }}
}}

async function beginOAuth(messageTarget) {{
  messageTarget.className = '';
  messageTarget.textContent = 'Saxo認証画面へ移動します…';
  try {{
    const result = await postOperation('/api/oauth/start');
    window.location.assign(result.authorization_url);
  }} catch (error) {{ messageTarget.className='error'; messageTarget.textContent=error.message; }}
}}

oauthStart.addEventListener('click', async () => {{
  await beginOAuth(periodicMessage);
}});

c2OAuthStart.addEventListener('click', async () => {{
  await beginOAuth(c2Message);
}});

c2AppKeySave.addEventListener('click', saveAppKey);
c2AppKeyDelete.addEventListener('click', deleteAppKey);
c2ObservationAck.addEventListener('change', updateObservationButton);
c2Start.addEventListener('click', startC2Observation);
document.querySelectorAll('.provider-save').forEach(button => button.addEventListener('click', () => saveProviderDecision(button.closest('.provider-decision'))));
c2GateSave.addEventListener('click', saveGateDecision);

periodicStart.addEventListener('click', async () => {{
  try {{
    const result = await postOperation('/api/periodic/start');
    periodicMessage.className = '';
    periodicMessage.textContent = `scheduler: ${{result.status}}`;
    await readOperationalStatus();
  }} catch (error) {{ periodicMessage.className='error'; periodicMessage.textContent=error.message; }}
}});

periodicStop.addEventListener('click', async () => {{
  try {{
    const result = await postOperation('/api/periodic/stop');
    periodicMessage.className = '';
    periodicMessage.textContent = `scheduler stop: ${{result.status}}`;
    await readOperationalStatus();
  }} catch (error) {{ periodicMessage.className='error'; periodicMessage.textContent=error.message; }}
}});

oauthReconcile.addEventListener('click', async () => {{
  oauthReconcile.disabled = true;
  oauthReconcileMessage.className = '';
  oauthReconcileMessage.textContent = 'OAuth credentialから固定reconcile jobを登録しています…';
  try {{
    const result = await postOperation('/api/reconcile/oauth');
    render(result);
    oauthReconcileMessage.textContent = 'jobを開始しました。token値は保存・表示されません。';
    if (!pollTimer) pollTimer = setInterval(readStatus, 2000);
  }} catch (error) {{
    oauthReconcileMessage.className='error';
    oauthReconcileMessage.textContent=error.message;
    oauthReconcile.disabled = false;
  }}
}});

startButton.addEventListener('click', async () => {{
  let token = tokenInput.value.trim();
  if (!token) {{ message.className = 'error'; message.textContent = 'tokenを入力してください。'; return; }}
  let requestBody = JSON.stringify({{ token }});
  tokenInput.value = '';
  token = '';
  startButton.disabled = true;
  message.className = '';
  message.textContent = 'reconcile jobを登録しています…';
  try {{
    const response = await fetch('/api/reconcile', {{
      method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}}, body:requestBody
    }});
    requestBody = '';
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
    render(result);
    message.textContent = 'jobを開始しました。画面を閉じてもserver process内で継続します。';
    if (!pollTimer) pollTimer = setInterval(readStatus, 2000);
  }} catch (error) {{
    requestBody = '';
    message.className = 'error';
    message.textContent = error.message;
    startButton.disabled = false;
  }}
}});

readStatus().catch(() => {{ message.className='error'; message.textContent='status取得に失敗しました。'; }});
readOperationalStatus().catch(() => {{ periodicMessage.className='error'; periodicMessage.textContent='運用status取得に失敗しました。'; }});
readC2Readiness().catch(() => {{ c2Message.className='error'; c2Message.textContent='C2 readiness取得に失敗しました。'; }});
readC2DecisionStatus().catch(() => {{ c2GateMessage.className='error'; c2GateMessage.textContent='C2 decision status取得に失敗しました。'; }});
readC2ObservationStatus().catch(() => {{ c2ObservationSummary.className='warning'; c2ObservationSummary.textContent='観測履歴の取得に失敗しました。'; }});
setInterval(() => readOperationalStatus().catch(() => {{}}), 5000);
setInterval(() => readC2ObservationStatus().catch(() => {{}}), 2000);
</script>
</body>
</html>""".encode("utf-8")


class OperatorState:
    def __init__(
        self,
        manager: ReconcileJobManager,
        port: int,
        *,
        app_key_store: Any | None = None,
        oauth_manager_factory: Callable[[OAuthConfig], SaxoOAuthManager] = SaxoOAuthManager,
    ) -> None:
        self.manager = manager
        self.port = port
        self.oauth_manager_factory = oauth_manager_factory
        self.csrf_token = secrets.token_urlsafe(32)
        self.script_nonce = secrets.token_urlsafe(24)
        self.oauth_lock = threading.Lock()
        self.oauth_keychain_entry_present = oauth_keychain_service_entry_present()
        self.pending_oauth: PendingAuthorization | None = None
        self.app_key_store = app_key_store or MacOSKeychainStore(
            service=APP_KEY_KEYCHAIN_SERVICE
        )
        self.app_key_source = "NOT_SET"
        self.app_key_keychain_entry_present = False
        self.oauth_manager: SaxoOAuthManager | None = None
        self.oauth_config_error: str | None = None
        self.c2_oauth_adapter: C2SIMOAuthCredentialAdapter | None = None
        self.c2_oauth_refresh_keeper: C2OAuthRefreshKeeper | None = None
        self._reload_oauth_configuration()
        self.c2_decision_lock = threading.Lock()
        self.c2_observation_lock = threading.Lock()
        self.c2_observation_state_lock = threading.Lock()
        self.c2_observation_audit = load_c2_observation_audit()
        if self.c2_observation_audit["state"] == "RUNNING":
            interrupted = {
                "status": "BLOCKED_INTERFACE_OPERATIONAL",
                "error_code": "BLOCKED_INTERFACE_OPERATIONAL_OBSERVATION_INTERRUPTED",
                "failed_endpoint_id": None,
                "request_count": 0,
                "write_request_count": 0,
                "raw_response_saved": False,
                "receipt_registration_performed": False,
                "db_writes_performed": 0,
                "periodic_execution_started": False,
                "orders_or_prechecks_sent": 0,
                "credential_values_exposed": False,
            }
            self.c2_observation_audit.update(
                {
                    "state": "FAILED",
                    "finished_at_utc": utc_now(),
                    "captured_at_utc": utc_now(),
                    "last_observation": interrupted,
                }
            )
            self.c2_observation_audit = save_c2_observation_audit(
                self.c2_observation_audit
            )
        self.c2_operational_gates = load_operational_gates()
        self.c2_provider_decisions = load_provider_decisions()

    def _stop_refresh_keeper_for_reconfiguration(self) -> None:
        if self.c2_oauth_refresh_keeper is not None:
            self.c2_oauth_refresh_keeper.stop()
        self.c2_oauth_refresh_keeper = None
        self.c2_oauth_adapter = None

    def _load_app_key(self) -> tuple[str | None, str]:
        environment_value = os.environ.get(OAUTH_APP_KEY_ENVIRONMENT_KEY, "").strip()
        if environment_value:
            self.app_key_keychain_entry_present = False
            return environment_value, "PROCESS_ENVIRONMENT"
        try:
            stored = self.app_key_store.get(APP_KEY_KEYCHAIN_ACCOUNT)
        except SaxoAuthError as exc:
            self.app_key_keychain_entry_present = False
            self.oauth_config_error = exc.code
            return None, "NOT_SET"
        self.app_key_keychain_entry_present = stored is not None
        if stored is None:
            return None, "NOT_SET"
        try:
            value = stored.decode("utf-8").strip()
        except UnicodeDecodeError:
            self.oauth_config_error = "AUTH_APP_KEY_KEYCHAIN_VALUE_INVALID"
            return None, "KEYCHAIN_INVALID"
        return value, "MACOS_KEYCHAIN"

    def _reload_oauth_configuration(self) -> None:
        self._stop_refresh_keeper_for_reconfiguration()
        self.oauth_manager = None
        self.oauth_config_error = None
        app_key, source = self._load_app_key()
        self.app_key_source = source
        if app_key is None:
            self.oauth_config_error = self.oauth_config_error or "AUTH_CONFIG_MISSING"
            return
        try:
            config = OAuthConfig(app_key, callback_port=self.port)
            self.oauth_manager = self.oauth_manager_factory(config)
            self.c2_oauth_adapter = C2SIMOAuthCredentialAdapter(
                config, manager=self.oauth_manager
            )
            self.c2_oauth_refresh_keeper = C2OAuthRefreshKeeper(
                self.c2_oauth_adapter
            )
        except SaxoAuthError as exc:
            self.oauth_manager = None
            self.oauth_config_error = exc.code
        finally:
            app_key = ""

    def save_oauth_app_key(self, app_key: str) -> dict[str, Any]:
        selected = app_key.strip()
        if (
            not selected
            or len(selected) > 256
            or any(ord(character) < 32 for character in selected)
        ):
            raise SaxoAuthError("AUTH_APP_KEY_INPUT_INVALID")
        if self.app_key_keychain_entry_present:
            raise SaxoAuthError("AUTH_APP_KEY_ALREADY_CONFIGURED_DELETE_FIRST")
        OAuthConfig(selected, callback_port=self.port)
        self.app_key_store.put(APP_KEY_KEYCHAIN_ACCOUNT, selected.encode("utf-8"))
        selected = ""
        app_key = ""
        self._reload_oauth_configuration()
        result = self.oauth_status()
        return {
            "status": "PASS",
            "auth_status": result["status"],
            "app_key_configured": result["configuration"]["app_key_configured"],
            "app_key_source": result["configuration"]["app_key_source"],
            "app_key_value_exposed": False,
            "oauth_started": False,
            "saxo_api_gets_performed": 0,
            "db_writes_performed": 0,
            "orders_or_prechecks_sent": 0,
        }

    def delete_oauth_app_key(self, confirmation: str) -> dict[str, Any]:
        if confirmation != APP_KEY_DELETE_CONFIRMATION:
            raise SaxoAuthError("AUTH_APP_KEY_DELETE_CONFIRMATION_REQUIRED")
        self.app_key_store.delete(APP_KEY_KEYCHAIN_ACCOUNT)
        self._reload_oauth_configuration()
        return {
            "status": "PASS",
            "app_key_configured": self.oauth_manager is not None,
            "app_key_value_exposed": False,
            "oauth_started": False,
            "saxo_api_gets_performed": 0,
            "db_writes_performed": 0,
            "orders_or_prechecks_sent": 0,
        }

    def oauth_status(self) -> dict[str, Any]:
        if self.oauth_manager is None:
            observed = {
                "status": self.oauth_config_error or "AUTH_CONFIG_MISSING",
                "token_values_exposed": False,
                "orders_or_prechecks_sent": 0,
            }
        else:
            observed = self.oauth_manager.status()
        observed["configuration"] = oauth_configuration_diagnostics(
            self.port,
            str(observed.get("status") or "AUTH_CONFIG_MISSING"),
            keychain_entry_present=self.oauth_keychain_entry_present,
            app_key_configured_override=self.oauth_manager is not None,
            app_key_source=self.app_key_source,
            app_key_keychain_entry_present=self.app_key_keychain_entry_present,
        )
        return observed

    def begin_oauth(self) -> dict[str, Any]:
        if self.oauth_manager is None:
            raise SaxoAuthError(self.oauth_config_error or "AUTH_CONFIG_MISSING")
        with self.oauth_lock:
            self.pending_oauth = self.oauth_manager.begin_authorization()
            return {
                "status": "AUTHORIZATION_REQUIRED",
                "authorization_url": self.pending_oauth.authorization_url,
                "token_values_exposed": False,
                "orders_or_prechecks_sent": 0,
            }

    def complete_oauth(self, observed_state: str, code: str, error: str) -> dict[str, Any]:
        if self.oauth_manager is None:
            raise SaxoAuthError(self.oauth_config_error or "AUTH_CONFIG_MISSING")
        with self.oauth_lock:
            pending = self.pending_oauth
            self.pending_oauth = None
        if pending is None or not secrets.compare_digest(observed_state, pending.state):
            raise SaxoAuthError("AUTH_CALLBACK_STATE_MISMATCH")
        if error or not code:
            raise SaxoAuthError("AUTHORIZATION_DENIED")
        result = self.oauth_manager.complete_authorization(pending, code)
        if self.c2_oauth_refresh_keeper is not None:
            self.c2_oauth_refresh_keeper.start()
        return result

    def start_reconcile_with_oauth(self) -> dict[str, Any]:
        raise SaxoAuthError("REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY")

    def c2_readiness(self) -> dict[str, Any]:
        result = c2_sim_read_readiness(
            auth_status=self.oauth_status(),
            credential_slot_status={
                "status": "DEPRECATED",
                "credential_present": False,
                "credential_persisted": False,
                "credential_values_exposed": False,
            },
            operational_gates=self.c2_operational_gates,
            provider_decisions=self.c2_provider_decisions,
        )
        result["oauth_refresh_keeper"] = (
            {
                "status": "UNAVAILABLE",
                "purpose": "OAUTH_REFRESH_ONLY",
                "saxo_api_gets_performed": 0,
                "receipt_registration_performed": False,
                "periodic_data_execution_performed": False,
                "orders_or_prechecks_sent": 0,
                "credential_values_exposed": False,
            }
            if self.c2_oauth_refresh_keeper is None
            else self.c2_oauth_refresh_keeper.status()
        )
        return result

    def c2_decision_status(self) -> dict[str, Any]:
        guidance = c2_decision_guidance()
        guidance["current_provider_decisions"] = json.loads(
            json.dumps(self.c2_provider_decisions)
        )
        guidance["current_operational_gate"] = json.loads(
            json.dumps(self.c2_operational_gates)
        )
        readiness = self.c2_readiness()
        guidance["readiness"] = {
            "status": readiness["status"],
            "auth_ready": readiness["auth_ready"],
            "provider_decisions_ready": readiness["provider_decisions_ready"],
            "operational_gate_status": readiness["operational_gate_status"],
            "sim_observation_start_allowed": readiness[
                "sim_observation_start_allowed"
            ],
            "c2_data_execution_allowed": readiness["c2_data_execution_allowed"],
            "sim_allocation_paper_evaluation_allowed": readiness[
                "sim_allocation_paper_evaluation_allowed"
            ],
            "live_order_eligibility_allowed": readiness[
                "live_order_eligibility_allowed"
            ],
            "explicit_start_required": readiness["explicit_start_required"],
            "automatic_start_allowed": readiness["automatic_start_allowed"],
        }
        return guidance

    def c2_observation_status(self) -> dict[str, Any]:
        with self.c2_observation_state_lock:
            audit = json.loads(json.dumps(self.c2_observation_audit))
        readiness = self.c2_readiness()
        retry_allowed = (
            readiness.get("sim_observation_start_allowed") is True
            and audit["state"] != "RUNNING"
        )
        if audit["state"] == "IDLE":
            audit["state"] = (
                "READY" if readiness.get("sim_observation_start_allowed") is True else "IDLE"
            )
        return {
            "status": audit["state"],
            "attempt_count": audit["attempt_count"],
            "started_at_utc": audit["started_at_utc"],
            "finished_at_utc": audit["finished_at_utc"],
            "captured_at_utc": audit["captured_at_utc"],
            "legacy_timestamp_unavailable": audit["legacy_timestamp_unavailable"],
            "last_observation": audit["last_observation"],
            "result_persisted": audit["last_observation"] is not None,
            "persistence_scope": "SANITIZED_RUNTIME_LAST_RESULT_ONLY",
            "raw_response_saved": False,
            "receipt_registration_performed": False,
            "db_writes_performed": 0,
            "periodic_execution_started": False,
            "orders_or_prechecks_sent": 0,
            "credential_values_exposed": False,
            **c2_observation_operator_guidance(
                audit, retry_allowed=retry_allowed
            ),
        }

    def start_c2_sim_observation(self, confirmation: str) -> dict[str, Any]:
        if confirmation != READ_ONLY_ACK:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_READ_ONLY_ACK_REQUIRED"
            )
        readiness = self.c2_readiness()
        if readiness.get("sim_observation_start_allowed") is not True:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_SIM_OBSERVATION_NOT_READY"
            )
        if self.c2_oauth_adapter is None:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_OAUTH_ADAPTER_UNAVAILABLE"
            )
        if not self.c2_observation_lock.acquire(blocking=False):
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_SIM_OBSERVATION_ALREADY_RUNNING"
            )
        try:
            started_at_utc = utc_now()
            with self.c2_observation_state_lock:
                running = json.loads(json.dumps(self.c2_observation_audit))
                running.update(
                    {
                        "state": "RUNNING",
                        "attempt_count": int(running["attempt_count"]) + 1,
                        "started_at_utc": started_at_utc,
                        "finished_at_utc": None,
                        "captured_at_utc": started_at_utc,
                        "legacy_timestamp_unavailable": False,
                    }
                )
                self.c2_observation_audit = save_c2_observation_audit(running)
            try:
                with self.c2_oauth_adapter.open_observation_session(
                    read_only_ack=READ_ONLY_ACK
                ) as session:
                    result = run_initial_sim_observation_session(session)
            except C2SIMReadOperationalError as exc:
                result = {
                    "status": "BLOCKED_INTERFACE_OPERATIONAL",
                    "error_code": exc.code,
                    "failed_endpoint_id": exc.endpoint_id,
                    "request_count": 0,
                    "write_request_count": 0,
                    "raw_response_saved": False,
                    "receipt_registration_performed": False,
                    "db_writes_performed": 0,
                    "periodic_execution_started": False,
                    "orders_or_prechecks_sent": 0,
                    "credential_values_exposed": False,
                }
            sanitized = sanitize_c2_observation_result(result)
            final_state = (
                "SUCCEEDED"
                if sanitized["status"] in {"PASS", "PASS_WITH_WARNINGS"}
                else "FAILED"
            )
            finished_at_utc = utc_now()
            with self.c2_observation_state_lock:
                completed = json.loads(json.dumps(self.c2_observation_audit))
                completed.update(
                    {
                        "state": final_state,
                        "finished_at_utc": finished_at_utc,
                        "captured_at_utc": finished_at_utc,
                        "last_observation": sanitized,
                    }
                )
                self.c2_observation_audit = save_c2_observation_audit(completed)
            return sanitized
        finally:
            self.c2_observation_lock.release()

    def save_c2_provider_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"dataset_role", "action", "approved_by", "rationale", "evidence"}
        if set(payload) != allowed:
            raise C2DecisionError("C2_PROVIDER_DECISION_REQUEST_INVALID")
        role = payload.get("dataset_role")
        action = payload.get("action")
        if role not in PROVIDER_ROLES or action not in DECISION_ACTIONS:
            raise C2DecisionError("C2_PROVIDER_DECISION_REQUEST_INVALID")
        approved_by = _decision_text(payload.get("approved_by"), "approved_by", maximum=200)
        rationale = _decision_text(payload.get("rationale"), "rationale", maximum=2_000)
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping):
            raise C2DecisionError("C2_PROVIDER_DECISION_REQUEST_INVALID")
        if action == "APPROVE" and set(evidence) != PROVIDER_APPROVAL_FIELDS:
            raise C2DecisionError("C2_PROVIDER_DECISION_EVIDENCE_MISSING")
        if action != "APPROVE" and evidence:
            raise C2DecisionError("C2_PROVIDER_DECISION_REQUEST_INVALID")
        now = utc_now()
        with self.c2_decision_lock:
            selected = json.loads(json.dumps(self.c2_provider_decisions))
            decision = next(
                item for item in selected["decisions"] if item["dataset_role"] == role
            )
            decision.update(
                {
                    "status": {
                        "KEEP_BLOCKED": "DECISION_REQUIRED",
                        "APPROVE": "APPROVED",
                        "REJECT": "REJECTED",
                    }[action],
                    "review_action": action,
                    "reviewed_by": approved_by,
                    "reviewed_at_utc": now,
                    "decision_basis": rationale,
                    "notes": rationale,
                }
            )
            if action == "APPROVE":
                decision.update(
                    {
                        field: _decision_text(evidence.get(field), field, maximum=1_000)
                        for field in PROVIDER_APPROVAL_FIELDS
                    }
                )
                decision["instrument_set"] = list(ETF11)
                decision["approved_by"] = approved_by
                decision["approved_at_utc"] = now
            else:
                decision["approved_by"] = None
                decision["approved_at_utc"] = None
            self.c2_provider_decisions = save_provider_decisions(selected)
        return {
            "status": "PASS",
            "dataset_role": role,
            "decision_status": decision["status"],
            "review_action": action,
            "reviewed_by": approved_by,
            "reviewed_at_utc": now,
            "provider_and_gate_decisions_ready": self.c2_readiness()[
                "provider_and_gate_decisions_ready"
            ],
            "secret_values_exposed": False,
            "saxo_api_gets_performed": 0,
            "receipt_registration_performed": False,
            "db_writes_performed": 0,
            "db3_scheduler_changed": False,
            "orders_or_prechecks_sent": 0,
        }

    def save_c2_operational_gate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"action", "accepted_by", "rationale", "gate"}
        if set(payload) != allowed:
            raise C2DecisionError("C2_OPERATIONAL_GATE_REQUEST_INVALID")
        action = payload.get("action")
        if action not in {"KEEP_BLOCKED", "ACCEPT", "REJECT"}:
            raise C2DecisionError("C2_OPERATIONAL_GATE_REQUEST_INVALID")
        accepted_by = _decision_text(payload.get("accepted_by"), "accepted_by", maximum=200)
        rationale = _decision_text(payload.get("rationale"), "rationale", maximum=2_000)
        gate = payload.get("gate")
        if not isinstance(gate, Mapping):
            raise C2DecisionError("C2_OPERATIONAL_GATE_REQUEST_INVALID")
        if action != "ACCEPT" and gate:
            raise C2DecisionError("C2_OPERATIONAL_GATE_REQUEST_INVALID")
        now = utc_now()
        with self.c2_decision_lock:
            selected = json.loads(json.dumps(self.c2_operational_gates))
            selected.update(
                {
                    "status": {
                        "KEEP_BLOCKED": "DECISION_REQUIRED",
                        "ACCEPT": "ACCEPTED",
                        "REJECT": "REJECTED",
                    }[action],
                    "review_action": action,
                    "reviewed_by": accepted_by,
                    "reviewed_at_utc": now,
                    "decision_basis": rationale,
                    "notes": rationale,
                }
            )
            if action == "ACCEPT":
                if set(gate) != {
                    "accepted_base_currencies", "evaluation_mode",
                    "max_quote_age_seconds",
                    "max_atomic_span_seconds", "max_delayed_by_minutes",
                    "allow_sim_delayed_quotes", "accepted_price_types",
                    "require_two_sided_bid_ask",
                    "fee_unknown_policy", "issuer_revision_lookback_business_days",
                    "cash_correction_lookback_calendar_days",
                    "require_negative_event_state", "role_max_lag_seconds",
                }:
                    raise C2DecisionError("C2_OPERATIONAL_GATE_REQUEST_INVALID")
                selected["account_context"] = {
                    "environment": "SIM",
                    "require_all_11_etfs": True,
                    "accepted_base_currencies": gate["accepted_base_currencies"],
                }
                selected["quote"] = {
                    "evaluation_mode": gate["evaluation_mode"],
                    "max_quote_age_seconds": gate["max_quote_age_seconds"],
                    "max_atomic_span_seconds": gate["max_atomic_span_seconds"],
                    "max_delayed_by_minutes": gate["max_delayed_by_minutes"],
                    "allow_sim_delayed_quotes": gate["allow_sim_delayed_quotes"],
                    "accepted_price_types": gate["accepted_price_types"],
                    "require_two_sided_bid_ask": gate[
                        "require_two_sided_bid_ask"
                    ],
                }
                selected["fee"]["unknown_policy"] = gate["fee_unknown_policy"]
                selected["distribution_revision"] = {
                    "issuer_revision_lookback_business_days": gate[
                        "issuer_revision_lookback_business_days"
                    ],
                    "cash_correction_lookback_calendar_days": gate[
                        "cash_correction_lookback_calendar_days"
                    ],
                    "require_negative_event_state": gate[
                        "require_negative_event_state"
                    ],
                }
                selected["sla"]["role_max_lag_seconds"] = gate[
                    "role_max_lag_seconds"
                ]
                selected["accepted_by"] = accepted_by
                selected["accepted_at_utc"] = now
            else:
                selected["accepted_by"] = None
                selected["accepted_at_utc"] = None
            self.c2_operational_gates = save_operational_gates(selected)
        return {
            "status": "PASS",
            "gate_status": selected["status"],
            "review_action": action,
            "reviewed_by": accepted_by,
            "reviewed_at_utc": now,
            "provider_and_gate_decisions_ready": self.c2_readiness()[
                "provider_and_gate_decisions_ready"
            ],
            "secret_values_exposed": False,
            "saxo_api_gets_performed": 0,
            "receipt_registration_performed": False,
            "db_writes_performed": 0,
            "db3_scheduler_changed": False,
            "orders_or_prechecks_sent": 0,
        }

    def resume_c2_oauth_refresh_if_ready(self) -> dict[str, Any]:
        if (
            self.c2_oauth_refresh_keeper is not None
            and self.c2_oauth_adapter is not None
            and self.c2_oauth_adapter.status().get("automatic_refresh_allowed") is True
        ):
            return self.c2_oauth_refresh_keeper.start()
        return self.c2_readiness()["oauth_refresh_keeper"]

    def stop_c2_oauth_refresh(self) -> None:
        if self.c2_oauth_refresh_keeper is not None:
            self.c2_oauth_refresh_keeper.stop()


def make_handler(state: OperatorState) -> type[BaseHTTPRequestHandler]:
    class OperatorRequestHandler(BaseHTTPRequestHandler):
        server_version = "saxo-db-operator"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                f"frame-ancestors 'none'; script-src 'nonce-{state.script_nonce}'; "
                f"style-src 'nonce-{state.script_nonce}'",
            )

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _oauth_result_page(self, status: int, message: str) -> None:
            body = (
                "<!doctype html><html lang='ja'><meta charset='utf-8'>"
                f"<title>saxo_db OAuth</title><p>{message}</p><p><a href='/'>operatorへ戻る</a></p></html>"
            ).encode("utf-8")
            self._send(status, body, "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlsplit(self.path)
            if parsed.path == CALLBACK_PATH:
                query = parse_qs(parsed.query, keep_blank_values=True)
                observed_state = (query.get("state") or [""])[0]
                code = (query.get("code") or [""])[0]
                error = (query.get("error") or [""])[0]
                try:
                    state.complete_oauth(observed_state, code, error)
                    self._oauth_result_page(200, "Saxo OAuth接続が完了しました。")
                except SaxoAuthError as exc:
                    self._oauth_result_page(400, f"Saxo OAuth接続に失敗しました: {exc.code}")
                return
            if self.path == "/":
                self._send(200, operator_html(state.csrf_token, state.script_nonce), "text/html; charset=utf-8")
                return
            if self.path == "/api/status":
                self._json(200, state.manager.status())
                return
            if self.path == "/api/oauth/status":
                self._json(200, state.oauth_status())
                return
            if self.path == "/api/periodic/status":
                self._json(200, periodic_service_status())
                return
            if self.path == "/api/c2/sim-read/readiness":
                self._json(200, state.c2_readiness())
                return
            if self.path == "/api/c2/decisions":
                self._json(200, state.c2_decision_status())
                return
            if self.path == "/api/c2/sim-read/observation":
                self._json(200, state.c2_observation_status())
                return
            if self.path == "/health":
                self._json(
                    200,
                    {
                        "status": "PASS",
                        "service_id": "saxo_db.operator_ui",
                        "bind": "loopback",
                        "port": state.port,
                        "orders_or_prechecks_sent": 0,
                    },
                )
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            allowed_paths = {
                "/api/reconcile", "/api/reconcile/oauth", "/api/oauth/start",
                "/api/periodic/start", "/api/periodic/stop",
                "/api/c2/oauth/app-key", "/api/c2/oauth/app-key/delete",
                "/api/c2/decisions/provider", "/api/c2/decisions/gate",
                "/api/c2/sim-read/observe",
            }
            if self.path not in allowed_paths:
                self._json(404, {"error": "not found"})
                return
            if not allowed_browser_request(
                self.headers.get("Host", ""), self.headers.get("Origin"), state.port
            ):
                self._json(403, {"error": "loopback origin required"})
                return
            if not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), state.csrf_token
            ):
                self._json(403, {"error": "invalid request token"})
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._json(415, {"error": "application/json required"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if not 1 <= content_length <= MAX_REQUEST_BYTES:
                self._json(413, {"error": "invalid request size"})
                return
            if self.path in {"/api/reconcile", "/api/reconcile/oauth"}:
                self.rfile.read(content_length)
                self._json(
                    409,
                    {"error": "REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY"},
                )
                return
            if self.path in {
                "/api/c2/oauth/app-key",
                "/api/c2/oauth/app-key/delete",
            }:
                try:
                    payload = json.loads(self.rfile.read(content_length))
                    if not isinstance(payload, dict):
                        raise ValueError
                    if self.path == "/api/c2/oauth/app-key":
                        if set(payload) != {"app_key"} or not isinstance(
                            payload.get("app_key"), str
                        ):
                            raise ValueError
                        app_key = payload.pop("app_key")
                        result = state.save_oauth_app_key(app_key)
                        app_key = ""
                    else:
                        if set(payload) != {"confirm"} or not isinstance(
                            payload.get("confirm"), str
                        ):
                            raise ValueError
                        result = state.delete_oauth_app_key(payload["confirm"])
                    payload.clear()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    self._json(400, {"error": "App Key設定requestが不正です。"})
                    return
                except SaxoAuthError as exc:
                    self._json(409, {"error": exc.code})
                    return
                self._json(200, result)
                return
            if self.path == "/api/c2/sim-read/observe":
                try:
                    payload = json.loads(self.rfile.read(content_length))
                    if not isinstance(payload, dict) or set(payload) != {"confirmation"} or not isinstance(
                        payload.get("confirmation"), str
                    ):
                        raise ValueError
                    result = state.start_c2_sim_observation(payload["confirmation"])
                    payload.clear()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    self._json(400, {"error": "SIM observation requestが不正です。"})
                    return
                except C2SIMReadOperationalError as exc:
                    self._json(409, {"error": exc.code})
                    return
                response_status = (
                    200 if result.get("status") in {"PASS", "PASS_WITH_WARNINGS"} else 409
                )
                self._json(response_status, result)
                return
            if self.path in {
                "/api/c2/decisions/provider",
                "/api/c2/decisions/gate",
            }:
                try:
                    payload = json.loads(self.rfile.read(content_length))
                    if not isinstance(payload, dict):
                        raise ValueError
                    if self.path == "/api/c2/decisions/provider":
                        result = state.save_c2_provider_decision(payload)
                    else:
                        result = state.save_c2_operational_gate(payload)
                    payload.clear()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    self._json(400, {"error": "C2 decision requestが不正です。"})
                    return
                except C2DecisionError as exc:
                    self._json(409, {"error": str(exc)})
                    return
                self._json(200, result)
                return
            if self.path != "/api/reconcile":
                try:
                    payload = json.loads(self.rfile.read(content_length))
                    if payload != {}:
                        raise ValueError
                    if self.path == "/api/oauth/start":
                        result = state.begin_oauth()
                    elif self.path == "/api/reconcile/oauth":
                        result = state.start_reconcile_with_oauth()
                    elif self.path == "/api/periodic/start":
                        result = start_periodic_service(
                            callback_port=state.port,
                            scope_profile=operator_periodic_scope_profile(),
                        )
                    else:
                        result = stop_periodic_service()
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    self._json(400, {"error": "empty JSON object required"})
                    return
                except SaxoAuthError as exc:
                    self._json(409, {"error": exc.code})
                    return
                response_status = 202 if result.get("status") in {"PASS", "AUTHORIZATION_REQUIRED"} else 409
                self._json(response_status, result)
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
                    raise ValueError
                access_token = payload.pop("token")
                result = state.manager.start(access_token)
                access_token = ""
                payload.clear()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, InvalidAccessToken):
                self._json(400, {"error": "有効なSaxo SIM tokenを入力してください。"})
                return
            except JobAlreadyRunning as exc:
                self._json(409, {"error": str(exc)})
                return
            except OSError as exc:
                self._json(500, {"error": f"job start failed: {type(exc).__name__}"})
                return
            self._json(202, result)

    return OperatorRequestHandler


def serve(port: int = DEFAULT_PORT) -> None:
    if not 1_024 <= port <= 65_535:
        raise ValueError("port must be between 1024 and 65535")
    state = OperatorState(ReconcileJobManager(), port)
    state.resume_c2_oauth_refresh_if_ready()
    server = ThreadingHTTPServer((LOOPBACK_HOST, port), make_handler(state))
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "status": "READY",
                "url": f"http://{LOOPBACK_HOST}:{port}/",
                "token_persisted": False,
                "orders_or_prechecks_sent": 0,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_c2_oauth_refresh()
        server.server_close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local-only DB3 reconciliation operator UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
