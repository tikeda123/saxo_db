from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from market_db.c2_sim_read_session import (
    EphemeralSIMReadSession,
    load_ephemeral_session_contract,
    load_low_frequency_price_policy,
    load_observation_start_contract,
    run_ephemeral_sim_read_observation,
    run_initial_sim_observation_session,
)
from market_db.instrument_registry import load_canonical_instruments
from market_db.saxo_client import SaxoAPIError
from market_db.strategy_external_contract import validate_strategy_external_receipt
from tests.test_c2_external_decisions import accepted_operational_gates
from tests.test_c2_sim_oauth import approved_provider_decisions


NOW = datetime(2026, 7, 31, 2, 0, 3, tzinfo=timezone.utc)


def test_checked_in_ephemeral_session_contract_is_strict_sim_read_only():
    contract = load_ephemeral_session_contract()
    assert contract["environment"] == "SIM"
    assert contract["write_methods_allowed"] == []
    assert contract["credential_persistence_allowed"] is False
    assert contract["automatic_receipt_registration"] is False
    assert len(contract["instrument_keys"]) == 11


def test_initial_observation_contract_is_explicit_get_only_and_defers_downstream_gates():
    contract = load_observation_start_contract()
    assert contract["environment"] == "SIM"
    assert sum(item["planned_calls"] for item in contract["allowed_get_plan"]) == 15
    assert contract["start_requirements"]["explicit_same_origin_user_action"] is True
    assert "SIGNAL_TOTAL_RETURN_DAILY provider selection" in contract[
        "non_blocking_for_initial_observation"
    ]
    assert contract["receipt_registration_allowed"] is False
    assert contract["periodic_execution_allowed"] is False
    assert contract["write_methods_allowed"] == []
    assert contract["orders_allowed"] is False


def test_low_frequency_price_policy_accepts_delayed_or_daily_without_bid_ask():
    policy = load_low_frequency_price_policy()
    assert policy["normal_monitoring_cadence"] == [
        "HOURLY_DELAYED_REFERENCE",
        "DAILY_CLOSE",
    ]
    assert policy["realtime_required"] is False
    assert policy["tick_data_required"] is False
    assert policy["two_sided_bid_ask_required"] is False
    assert "Indicative" in policy["accepted_infoprice_types"]
    assert policy["noaccess_policy"]["sim_paper_evaluation"] == (
        "USE_DAILY_CLOSE_FALLBACK"
    )
    assert policy["daily_close_fallback"]["owner"] == "saxo_db"


class FakeSIMReadClient:
    def __init__(
        self,
        token: str,
        *,
        fail_endpoint: str | None = None,
        omit_last_quote: bool = False,
        balance_currency: str = "USD",
        quote_mode: str = "normal",
    ):
        self.token = token
        self.fail_endpoint = fail_endpoint
        self.omit_last_quote = omit_last_quote
        self.balance_currency = balance_currency
        self.quote_mode = quote_mode
        self.request_count = 0
        self.write_request_count = 0
        self.instruments = tuple(
            item
            for item in load_canonical_instruments()
            if item.key in {"spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld"}
        )

    def _read(self, endpoint: str):
        self.request_count += 1
        if self.fail_endpoint == endpoint:
            raise SaxoAPIError("BLOCKED_TOKEN_EXPIRED", 401)

    def session_capabilities(self):
        self._read("session_capabilities")
        return {"DataLevel": "Full", "TradeLevel": "FullTradingAndChat"}

    def accounts_me(self):
        self._read("accounts_me")
        return {
            "Data": [
                {
                    "AccountKey": "account-key-must-not-escape",
                    "ClientKey": "client-key-must-not-escape",
                    "Currency": "USD",
                    "CurrencyDecimals": 2,
                    "AccountType": "Normal",
                }
            ]
        }

    def balances_me(self):
        self._read("balances_me")
        return {
            "Currency": self.balance_currency,
            "CurrencyDecimals": 2,
            "TotalValue": 999999,
        }

    def instrument_detail(self, uic: int, asset_type: str):
        self._read("instrument_detail")
        item = next(selected for selected in self.instruments if selected.uic == uic)
        return {
            "Identifier": uic,
            "AssetType": asset_type,
            "Symbol": item.symbol,
            "CurrencyCode": item.currency,
            "ExchangeId": item.symbol.split(":", 1)[-1].upper(),
            "IsTradable": True,
            "TradableOn": ["SIM_CONTEXT"],
            "AmountType": "Quantity",
            "MinimumTradeSize": 1,
            "MinimumOrderValue": None,
            "AmountDecimals": 0,
        }

    def info_prices(self, *, uics, asset_type: str, amount: int):
        self._read("info_prices")
        selected = list(uics[:-1] if self.omit_last_quote else uics)
        def quote_for(index: int):
            if self.quote_mode == "delayed_indicative_mid_only":
                return {
                    "Bid": None,
                    "Ask": None,
                    "Mid": "100.01",
                    "DelayedByMinutes": 15,
                    "ErrorCode": "None",
                    "LastUpdated": "2026-07-31T01:45:00Z",
                    "PriceSource": "SIM_DELAYED",
                    "PriceTypeBid": "Indicative",
                    "PriceTypeAsk": "Indicative",
                    "MarketState": "Open",
                }
            if index == 0 and self.quote_mode == "closed_no_market":
                return {
                    "Bid": None,
                    "Ask": None,
                    "DelayedByMinutes": 0,
                    "ErrorCode": "None",
                    "PriceTypeBid": "NoMarket",
                    "PriceTypeAsk": "NoMarket",
                    "MarketState": "Closed",
                }
            if index == 0 and self.quote_mode == "missing_tradable_bid":
                return {
                    "Bid": None,
                    "Ask": "100.02",
                    "DelayedByMinutes": 0,
                    "LastUpdated": "2026-07-31T02:00:00Z",
                    "PriceSource": "SIM",
                    "PriceTypeBid": "Tradable",
                    "PriceTypeAsk": "Tradable",
                    "MarketState": "Open",
                }
            return {
                "Bid": "100.00",
                "Ask": "100.02",
                "BidSize": 10,
                "AskSize": 12,
                "DelayedByMinutes": 0,
                "ErrorCode": "None",
                "LastUpdated": "2026-07-31T02:00:00Z",
                "PriceSource": "SIM",
                "PriceTypeBid": "Tradable",
                "PriceTypeAsk": "Tradable",
                "MarketState": "Open",
            }
        return {
            "Data": [
                {
                    "Uic": uic,
                    "Quote": quote_for(index),
                    "InstrumentPriceDetails": {
                        "IsMarketOpen": not (
                            index == 0 and self.quote_mode == "closed_no_market"
                        )
                    },
                }
                for index, uic in enumerate(selected)
            ]
        }


def test_ephemeral_session_repr_and_close_never_expose_or_persist_token():
    client = FakeSIMReadClient("top-secret")
    session = EphemeralSIMReadSession(
        "top-secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"f" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert "top-secret" not in repr(session)
    session.close()
    assert session.request_count == 0
    assert session.write_request_count == 0


def test_etf11_atomic_observation_builds_valid_redacted_receipts_without_registration():
    client = FakeSIMReadClient("top-secret-token")
    result = run_ephemeral_sim_read_observation(
        access_token="top-secret-token",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"k" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert result["status"] == "AVAILABLE"
    assert result["receipt_count"] == 13
    assert result["request_count"] == 15
    assert result["write_request_count"] == 0
    assert result["credentials_saved"] is False
    assert result["registration_performed"] is False
    for receipt in result["receipts"]:
        validate_strategy_external_receipt(receipt)
    quote_receipts = [
        item for item in result["receipts"]
        if item["dataset_role"] == "PROPOSAL_PRICE_SNAPSHOT"
    ]
    assert len(quote_receipts) == 11
    assert len({item["payload"]["snapshot_id"] for item in quote_receipts}) == 1
    assert {item["payload"]["instrument_count"] for item in quote_receipts} == {11}
    serialized = json.dumps(result, sort_keys=True)
    assert "top-secret-token" not in serialized
    assert "account-key-must-not-escape" not in serialized
    assert "client-key-must-not-escape" not in serialized
    assert "SIM_CONTEXT" not in serialized
    assert "tradable_on" not in serialized.casefold()
    assert "999999" not in serialized


def test_low_frequency_paper_contract_accepts_delayed_indicative_mid_without_bid_ask():
    client = FakeSIMReadClient(
        "top-secret-token", quote_mode="delayed_indicative_mid_only"
    )
    result = run_ephemeral_sim_read_observation(
        access_token="top-secret-token",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"l" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )

    assert result["status"] == "AVAILABLE_WITH_WARNINGS"
    assert result["request_count"] == 15
    assert result["write_request_count"] == 0
    quote_receipts = [
        item for item in result["receipts"]
        if item["dataset_role"] == "PROPOSAL_PRICE_SNAPSHOT"
    ]
    assert len(quote_receipts) == 11
    assert all(item["availability_state"] == "AVAILABLE_WITH_WARNINGS" for item in quote_receipts)
    assert all(item["freshness_state"] == "DELAYED" for item in quote_receipts)
    assert all(item["quality_state"] == "PASS_WITH_WARNINGS" for item in quote_receipts)
    assert all(item["payload"]["evaluation_price_field"] == "Mid" for item in quote_receipts)
    assert all(item["payload"]["bid"] is None for item in quote_receipts)
    assert all(item["payload"]["ask"] is None for item in quote_receipts)
    assert all(item["payload"]["delayed_by_minutes"] == 15 for item in quote_receipts)
    assert all("SIM_QUOTE_INDICATIVE_ACCEPTED" in item["warning_ids"] for item in quote_receipts)
    assert result["registration_performed"] is False


def test_initial_sim_observation_runs_15_gets_without_provider_gate_or_receipt_registration():
    client = FakeSIMReadClient("top-secret-token")
    with EphemeralSIMReadSession(
        "top-secret-token",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"m" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    ) as session:
        result = run_initial_sim_observation_session(session, clock=lambda: NOW)
    assert result["status"] == "PASS"
    assert result["request_count"] == 15
    assert result["write_request_count"] == 0
    assert result["minimum_format_identity_quote_checks"] == "PASS"
    assert result["instrument_observation"]["instrument_count"] == 11
    assert result["instrument_observation"]["trading_eligibility_gate_applied"] is False
    assert result["quote_observation"]["quote_count"] == 11
    assert result["quote_observation"]["price_values_exposed"] is False
    assert result["downstream_stage_status"] == "DECISION_REQUIRED_NON_BLOCKING_FOR_OBSERVATION"
    assert result["receipt_registration_performed"] is False
    assert result["db_writes_performed"] == 0
    assert result["periodic_execution_started"] is False
    assert result["orders_or_prechecks_sent"] == 0
    serialized = json.dumps(result, sort_keys=True)
    assert "top-secret-token" not in serialized
    assert "account-key-must-not-escape" not in serialized
    assert "client-key-must-not-escape" not in serialized
    assert '"bid"' not in serialized
    assert '"ask"' not in serialized


def test_initial_sim_observation_keeps_missing_quote_as_quality_failure_without_receipts():
    client = FakeSIMReadClient("secret", omit_last_quote=True)
    with EphemeralSIMReadSession(
        "secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"n" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    ) as session:
        result = run_initial_sim_observation_session(session, clock=lambda: NOW)
    assert result["status"] == "FAIL_DATA_QUALITY"
    assert result["error_code"] == "QUOTE_UIC_SET_INVALID"
    assert result["request_count"] == 15
    assert result["receipt_registration_performed"] is False
    assert result["orders_or_prechecks_sent"] == 0


def test_initial_sim_observation_treats_declared_closed_no_market_as_warning():
    client = FakeSIMReadClient("secret", quote_mode="closed_no_market")
    with EphemeralSIMReadSession(
        "secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"p" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    ) as session:
        result = run_initial_sim_observation_session(session, clock=lambda: NOW)
    assert result["status"] == "PASS_WITH_WARNINGS"
    assert result["request_count"] == 15
    assert result["minimum_format_identity_quote_checks"] == "PASS_WITH_WARNINGS"
    assert result["quote_observation"]["valid_two_sided_quote_count"] == 10
    assert result["quote_observation"]["unavailable_quote_count"] == 1
    assert result["quote_observation"]["unavailable_instrument_keys"] == ["spy"]
    assert result["quote_observation"]["missing_bid_count"] == 1
    assert result["quote_observation"]["missing_ask_count"] == 1
    assert "SIM_QUOTE_UNAVAILABLE_NOMARKET" in result["warning_ids"]
    assert "SIM_QUOTE_MARKET_CLOSED" in result["warning_ids"]
    assert result["write_request_count"] == 0
    assert result["receipt_registration_performed"] is False
    assert result["orders_or_prechecks_sent"] == 0


def test_initial_sim_observation_accepts_single_sided_reference_without_requiring_bid_ask():
    client = FakeSIMReadClient("secret", quote_mode="missing_tradable_bid")
    with EphemeralSIMReadSession(
        "secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"q" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    ) as session:
        result = run_initial_sim_observation_session(session, clock=lambda: NOW)
    assert result["status"] == "PASS_WITH_WARNINGS"
    assert "SIM_QUOTE_SINGLE_SIDED_REFERENCE_ACCEPTED" in result["warning_ids"]
    assert result["quote_observation"]["valid_reference_price_count"] == 11
    assert result["quote_observation"]["single_sided_reference_count"] == 1
    assert result["request_count"] == 15
    assert result["write_request_count"] == 0


def test_low_frequency_paper_contract_uses_daily_fallback_for_unavailable_quote():
    client = FakeSIMReadClient("secret", quote_mode="closed_no_market")
    result = run_ephemeral_sim_read_observation(
        access_token="secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"r" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert result["status"] == "AVAILABLE_WITH_WARNINGS"
    assert result["request_count"] == 15
    assert result["write_request_count"] == 0
    quote_receipts = [
        item for item in result["receipts"]
        if item["dataset_role"] == "PROPOSAL_PRICE_SNAPSHOT"
    ]
    unavailable = next(item for item in quote_receipts if item["payload"]["instrument_key"] == "spy")
    assert unavailable["availability_state"] == "DATA_NOT_READY"
    assert unavailable["quality_state"] == "NOT_EVALUATED"
    assert "SIM_QUOTE_DAILY_CLOSE_FALLBACK_REQUIRED" in unavailable["warning_ids"]


def test_initial_sim_observation_rejects_account_balance_identity_mismatch():
    client = FakeSIMReadClient("secret", balance_currency="EUR")
    with EphemeralSIMReadSession(
        "secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"o" * 32,
        client_factory=lambda token: client,
        clock=lambda: NOW,
    ) as session:
        result = run_initial_sim_observation_session(session, clock=lambda: NOW)
    assert result["status"] == "FAIL_DATA_QUALITY"
    assert result["error_code"] == "ACCOUNT_CURRENCY_IDENTITY_MISMATCH"
    assert result["request_count"] == 3
    assert result["receipt_registration_performed"] is False
    assert result["db_writes_performed"] == 0
    assert result["orders_or_prechecks_sent"] == 0


def test_auth_failure_remains_interface_blocked_and_uses_sanitized_receipts():
    client = FakeSIMReadClient("secret", fail_endpoint="session_capabilities")
    result = run_ephemeral_sim_read_observation(
        access_token="secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"z" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert result["status"] == "BLOCKED_INTERFACE_OPERATIONAL"
    assert result["receipt_count"] == 3
    assert result["write_request_count"] == 0
    assert all(
        receipt["availability_state"] == "BLOCKED_INTERFACE_OPERATIONAL"
        and receipt["quality_state"] == "NOT_EVALUATED"
        and receipt["blocker_ids"] == ["BLOCKED_TOKEN_EXPIRED"]
        for receipt in result["receipts"]
    )
    assert "secret" not in json.dumps(result, sort_keys=True)


def test_missing_atomic_quote_is_data_quality_failure_not_interface_failure():
    client = FakeSIMReadClient("secret", omit_last_quote=True)
    result = run_ephemeral_sim_read_observation(
        access_token="secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"z" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: client,
        clock=lambda: NOW,
    )
    assert result["status"] == "FAIL_DATA_QUALITY"
    assert all(
        receipt["availability_state"] == "FAIL_DATA_QUALITY"
        and receipt["quality_state"] == "FAIL_DATA_QUALITY"
        and receipt["blocker_ids"] == ["QUOTE_UIC_SET_INVALID"]
        for receipt in result["receipts"]
    )


def test_unaccepted_gate_performs_no_client_construction_or_api_call():
    called = []
    result = run_ephemeral_sim_read_observation(
        access_token="secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"z" * 32,
        operational_gates={
            "schema_version": 1,
            "decision_type": "C2_OPERATIONAL_GATES",
            "status": "DECISION_REQUIRED",
            "account_context": {},
            "quote": {},
            "fee": {"unknown_policy": None},
            "distribution_revision": {},
            "sla": {},
        },
        provider_decisions=approved_provider_decisions(),
        client_factory=lambda token: called.append(token),
        clock=lambda: NOW,
    )
    assert result["status"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert result["request_count"] == 0
    assert result["registration_performed"] is False
    assert called == []
    assert "secret" not in json.dumps(result, sort_keys=True)


def test_unapproved_provider_decision_performs_no_client_construction_or_api_call():
    from market_db.c2_external_decisions import load_provider_decision_template

    called = []
    result = run_ephemeral_sim_read_observation(
        access_token="secret",
        access_expires_at_utc=NOW + timedelta(minutes=10),
        fingerprint_key=b"z" * 32,
        operational_gates=accepted_operational_gates(),
        provider_decisions=load_provider_decision_template(),
        client_factory=lambda token: called.append(token),
        clock=lambda: NOW,
    )
    assert result["status"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert result["request_count"] == 0
    assert result["registration_performed"] is False
    assert called == []
    assert "secret" not in json.dumps(result, sort_keys=True)
