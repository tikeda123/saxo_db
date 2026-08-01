"""Ephemeral, non-persistent Saxo SIM read-session validation for C2.

This module never starts OAuth, reads Keychain credentials, accepts tokens from
argv/environment/files, writes receipts, or calls a Saxo write endpoint.  A
caller must supply a short-lived access token and an in-memory fingerprint key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .c2_external_decisions import (
    C2DecisionError,
    validate_operational_gates,
    validate_provider_decisions,
)
from .connection import project_root
from .instrument_registry import (
    CanonicalInstrument,
    InstrumentDriftError,
    load_canonical_instruments,
    validate_detail,
)
from .saxo_client import SaxoAPIError, SaxoClient
from .strategy_external_contract import (
    canonical_json_sha256,
    finalize_strategy_external_receipt,
    load_strategy_external_contract,
)


SESSION_CONTRACT_ID = "c2_saxo_sim_ephemeral_read_session_v1"
SESSION_CONTRACT_RELATIVE_PATH = "specs/c2_saxo_sim_ephemeral_read_session_v1.json"
OBSERVATION_CONTRACT_ID = "c2_sim_observation_start_v1"
OBSERVATION_CONTRACT_RELATIVE_PATH = "specs/c2_sim_observation_start_v1.json"
LOW_FREQUENCY_PRICE_POLICY_ID = "c2_low_frequency_price_policy_v1"
LOW_FREQUENCY_PRICE_POLICY_RELATIVE_PATH = "specs/c2_low_frequency_price_policy_v1.json"
MAX_SESSION_SECONDS = 900
MIN_REMAINING_TOKEN_SECONDS = 5
ETF11_KEYS = (
    "spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld"
)
READ_ENDPOINT_IDS = (
    "session_capabilities", "accounts_me", "balances_me", "instrument_detail",
    "info_prices", "historical_transactions",
)


def load_ephemeral_session_contract() -> dict[str, Any]:
    path = project_root() / SESSION_CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("C2_SIM_READ_SESSION_CONTRACT_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("C2_SIM_READ_SESSION_CONTRACT_NOT_VERIFIED") from exc
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("session_contract_id") == SESSION_CONTRACT_ID
        and value.get("environment") == "SIM"
        and value.get("max_session_seconds") == MAX_SESSION_SECONDS
        and tuple(value.get("allowed_endpoint_ids", ())) == READ_ENDPOINT_IDS
        and tuple(value.get("instrument_keys", ())) == ETF11_KEYS
        and value.get("write_methods_allowed") == []
        and value.get("credential_persistence_allowed") is False
        and value.get("automatic_receipt_registration") is False
    ):
        raise RuntimeError("C2_SIM_READ_SESSION_CONTRACT_NOT_VERIFIED")
    return value


def load_observation_start_contract() -> dict[str, Any]:
    path = project_root() / OBSERVATION_CONTRACT_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("C2_SIM_OBSERVATION_CONTRACT_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("C2_SIM_OBSERVATION_CONTRACT_NOT_VERIFIED") from exc
    plan = value.get("allowed_get_plan") if isinstance(value, dict) else None
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("contract_id") == OBSERVATION_CONTRACT_ID
        and value.get("environment") == "SIM"
        and value.get("start_requirements", {}).get("auth_status") == "AUTH_READY"
        and value.get("start_requirements", {}).get("explicit_same_origin_user_action") is True
        and value.get("start_requirements", {}).get("csrf_required") is True
        and isinstance(plan, list)
        and sum(item.get("planned_calls", 0) for item in plan if isinstance(item, dict)) == 15
        and value.get("raw_response_persistence_allowed") is False
        and value.get("sanitized_last_result_runtime_audit", {}).get("allowed") is True
        and value.get("sanitized_last_result_runtime_audit", {}).get("retention") == "last_result_only"
        and value.get("receipt_registration_allowed") is False
        and value.get("periodic_execution_allowed") is False
        and value.get("write_methods_allowed") == []
        and value.get("orders_allowed") is False
        and value.get("prechecks_allowed") is False
        and value.get("account_mutations_allowed") is False
    ):
        raise RuntimeError("C2_SIM_OBSERVATION_CONTRACT_NOT_VERIFIED")
    return value


def load_low_frequency_price_policy() -> dict[str, Any]:
    path = project_root() / LOW_FREQUENCY_PRICE_POLICY_RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("C2_LOW_FREQUENCY_PRICE_POLICY_NOT_VERIFIED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("C2_LOW_FREQUENCY_PRICE_POLICY_NOT_VERIFIED") from exc
    noaccess = value.get("noaccess_policy") if isinstance(value, dict) else None
    fallback = value.get("daily_close_fallback") if isinstance(value, dict) else None
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("policy_id") == LOW_FREQUENCY_PRICE_POLICY_ID
        and value.get("realtime_required") is False
        and value.get("tick_data_required") is False
        and value.get("two_sided_bid_ask_required") is False
        and value.get("delayed_by_minutes_greater_than_zero_is_normal") is True
        and "Indicative" in value.get("accepted_infoprice_types", [])
        and value.get("minimum_numeric_price_fields") == 1
        and isinstance(noaccess, dict)
        and noaccess.get("normal_monitoring") == "USE_DAILY_CLOSE_FALLBACK"
        and noaccess.get("sim_paper_evaluation") == "USE_DAILY_CLOSE_FALLBACK"
        and isinstance(fallback, dict)
        and fallback.get("owner") == "saxo_db"
        and fallback.get("realtime_feed_required") is False
        and value.get("orders_prechecks_cancels_account_mutations_allowed") is False
    ):
        raise RuntimeError("C2_LOW_FREQUENCY_PRICE_POLICY_NOT_VERIFIED")
    return value


class SIMReadClient(Protocol):
    request_count: int
    write_request_count: int

    def session_capabilities(self) -> dict[str, Any]: ...
    def accounts_me(self) -> dict[str, Any]: ...
    def balances_me(self) -> dict[str, Any]: ...
    def instrument_detail(self, uic: int, asset_type: str) -> dict[str, Any]: ...
    def info_prices(
        self, *, uics: list[int] | tuple[int, ...], asset_type: str, amount: int
    ) -> dict[str, Any]: ...


class C2SIMReadError(RuntimeError):
    def __init__(self, code: str, *, endpoint_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.endpoint_id = endpoint_id


class C2SIMReadOperationalError(C2SIMReadError):
    pass


class C2SIMReadContractBlocked(C2SIMReadError):
    pass


class C2SIMReadDataNotReady(C2SIMReadError):
    pass


class C2SIMReadDataQualityError(C2SIMReadError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("UTC timestamp is required")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise C2SIMReadDataQualityError("QUOTE_LAST_UPDATED_INVALID")
    try:
        selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise C2SIMReadDataQualityError("QUOTE_LAST_UPDATED_INVALID") from exc
    if selected.tzinfo is None or selected.utcoffset() != timezone.utc.utcoffset(selected):
        raise C2SIMReadDataQualityError("QUOTE_LAST_UPDATED_INVALID")
    return selected.astimezone(timezone.utc)


def _data_rows(payload: Mapping[str, Any], *, code: str) -> list[dict[str, Any]]:
    rows = payload.get("Data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise C2SIMReadDataQualityError(code)
    return [dict(row) for row in rows]


def _decimal_text(value: Any, *, code: str, positive: bool = False) -> str:
    try:
        selected = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise C2SIMReadDataQualityError(code) from exc
    if not selected.is_finite() or (positive and selected <= 0):
        raise C2SIMReadDataQualityError(code)
    return format(selected, "f")


def _fingerprint(account_rows: list[dict[str, Any]], key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("an in-memory fingerprint key of at least 32 bytes is required")
    identifiers: list[str] = []
    for row in account_rows:
        for field in ("AccountKey", "ClientKey"):
            if row.get(field):
                identifiers.append(f"{field}:{row[field]}")
    if not identifiers:
        raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_ACCOUNT_ID_MISSING")
    return hmac.new(
        key, "\n".join(sorted(set(identifiers))).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _etf11() -> tuple[CanonicalInstrument, ...]:
    selected = tuple(
        item for item in load_canonical_instruments() if item.key in ETF11_KEYS
    )
    if tuple(item.key for item in selected) != ETF11_KEYS:
        raise RuntimeError("ETF11 canonical order is not verified")
    return selected


@dataclass(frozen=True)
class PreflightEvidence:
    observed_at_utc: str
    account_fingerprint: str
    data_level: str
    account_count: int
    account_currency: str
    currency_decimals: int


@dataclass(frozen=True)
class ObservationPreflightEvidence:
    observed_at_utc: str
    account_fingerprint: str
    data_level: str
    account_count: int
    account_currencies: tuple[str, ...]
    balance_currency: str | None
    currency_decimals: int


class EphemeralSIMReadSession:
    """Own a bounded in-memory Saxo client and emit only redacted evidence."""

    def __init__(
        self,
        access_token: str,
        *,
        access_expires_at_utc: datetime,
        fingerprint_key: bytes,
        client_factory: Callable[[str], SIMReadClient] = SaxoClient,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        operational_guard: Callable[[], None] | None = None,
    ):
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("a non-empty in-memory access token is required")
        now = _utc(clock())
        token_expiry = _utc(access_expires_at_utc)
        if token_expiry <= now + timedelta(seconds=MIN_REMAINING_TOKEN_SECONDS):
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_TOKEN_EXPIRED")
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise ValueError("an in-memory fingerprint key of at least 32 bytes is required")
        self._client: SIMReadClient | None = client_factory(access_token)
        self._clock = clock
        self._operational_guard = operational_guard
        self._fingerprint_key: bytes | None = fingerprint_key
        self._lease_expires_at = min(
            token_expiry, now + timedelta(seconds=MAX_SESSION_SECONDS)
        )
        self._events: list[dict[str, Any]] = []
        self._closed = False

    def __repr__(self) -> str:
        return (
            "EphemeralSIMReadSession(environment='SIM', access_token=<redacted>, "
            f"closed={self._closed})"
        )

    def __enter__(self) -> "EphemeralSIMReadSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._events]

    @property
    def request_count(self) -> int:
        return 0 if self._client is None else int(self._client.request_count)

    @property
    def write_request_count(self) -> int:
        return 0 if self._client is None else int(self._client.write_request_count)

    def close(self) -> None:
        self._client = None
        self._fingerprint_key = None
        self._operational_guard = None
        self._closed = True

    def _assert_active(self) -> SIMReadClient:
        if self._operational_guard is not None:
            self._operational_guard()
        if self._closed or self._client is None:
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_SESSION_CLOSED")
        if _utc(self._clock()) >= self._lease_expires_at:
            self.close()
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_SESSION_EXPIRED")
        if self._client.write_request_count != 0:
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_WRITE_COUNTER_NONZERO")
        return self._client

    def _call(self, endpoint_id: str, operation: Callable[[SIMReadClient], dict[str, Any]]) -> dict[str, Any]:
        if endpoint_id not in READ_ENDPOINT_IDS:
            raise ValueError("endpoint is not allow-listed by the C2 session contract")
        started = _utc(self._clock())
        try:
            client = self._assert_active()
            payload = operation(client)
            if not isinstance(payload, dict):
                raise C2SIMReadDataQualityError("SAXO_RESPONSE_NOT_OBJECT", endpoint_id=endpoint_id)
            if client.write_request_count != 0:
                raise C2SIMReadOperationalError(
                    "BLOCKED_INTERFACE_OPERATIONAL_WRITE_COUNTER_NONZERO",
                    endpoint_id=endpoint_id,
                )
        except SaxoAPIError as exc:
            self._events.append(
                {
                    "endpoint_id": endpoint_id,
                    "started_at_utc": _utc_text(started),
                    "finished_at_utc": _utc_text(_utc(self._clock())),
                    "status": "BLOCKED_INTERFACE_OPERATIONAL",
                    "error_code": exc.code,
                }
            )
            raise C2SIMReadOperationalError(exc.code, endpoint_id=endpoint_id) from None
        except C2SIMReadError:
            raise
        except Exception:
            self._events.append(
                {
                    "endpoint_id": endpoint_id,
                    "started_at_utc": _utc_text(started),
                    "finished_at_utc": _utc_text(_utc(self._clock())),
                    "status": "BLOCKED_INTERFACE_OPERATIONAL",
                    "error_code": "BLOCKED_INTERFACE_OPERATIONAL_UNEXPECTED",
                }
            )
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_UNEXPECTED", endpoint_id=endpoint_id
            ) from None
        finished = _utc(self._clock())
        self._events.append(
            {
                "endpoint_id": endpoint_id,
                "started_at_utc": _utc_text(started),
                "finished_at_utc": _utc_text(finished),
                "status": "PASS",
                "error_code": None,
            }
        )
        return payload

    def preflight_observation(self) -> ObservationPreflightEvidence:
        """Validate technical SIM/account response shape without policy acceptance gates."""

        capabilities = self._call(
            "session_capabilities", lambda client: client.session_capabilities()
        )
        data_level = capabilities.get("DataLevel")
        if not isinstance(data_level, str) or data_level.casefold() in {
            "none", "nodata", "noaccess"
        }:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_DATA_CAPABILITY_MISSING",
                endpoint_id="session_capabilities",
            )
        accounts = self._call("accounts_me", lambda client: client.accounts_me())
        balances = self._call("balances_me", lambda client: client.balances_me())
        account_rows = _data_rows(accounts, code="ACCOUNT_RESPONSE_SHAPE_INVALID")
        if not account_rows:
            raise C2SIMReadOperationalError(
                "BLOCKED_INTERFACE_OPERATIONAL_ACCOUNT_CONTEXT_EMPTY",
                endpoint_id="accounts_me",
            )
        account_currencies = {
            str(row.get("Currency", "")).upper() for row in account_rows if row.get("Currency")
        }
        if not account_currencies:
            raise C2SIMReadDataQualityError("ACCOUNT_CURRENCY_MISSING")
        balance_currency = (
            str(balances["Currency"]).upper()
            if isinstance(balances.get("Currency"), str) and balances.get("Currency")
            else None
        )
        if balance_currency is not None and balance_currency not in account_currencies:
            raise C2SIMReadDataQualityError("ACCOUNT_CURRENCY_IDENTITY_MISMATCH")
        decimals = balances.get("CurrencyDecimals")
        if decimals is None:
            observed_decimals = {
                int(row["CurrencyDecimals"])
                for row in account_rows if row.get("CurrencyDecimals") is not None
            }
            if len(observed_decimals) != 1:
                raise C2SIMReadDataQualityError("ACCOUNT_CURRENCY_DECIMALS_INVALID")
            decimals = next(iter(observed_decimals))
        if not isinstance(decimals, int) or not 0 <= decimals <= 8:
            raise C2SIMReadDataQualityError("ACCOUNT_CURRENCY_DECIMALS_INVALID")
        if self._fingerprint_key is None:
            raise C2SIMReadOperationalError("BLOCKED_INTERFACE_OPERATIONAL_SESSION_CLOSED")
        return ObservationPreflightEvidence(
            observed_at_utc=_utc_text(_utc(self._clock())),
            account_fingerprint=_fingerprint(account_rows, self._fingerprint_key),
            data_level=data_level,
            account_count=len(account_rows),
            account_currencies=tuple(sorted(account_currencies)),
            balance_currency=balance_currency,
            currency_decimals=decimals,
        )

    def preflight(self, gates: Mapping[str, Any]) -> PreflightEvidence:
        observed = self.preflight_observation()
        currencies = set(observed.account_currencies)
        if observed.balance_currency is not None:
            currencies.add(observed.balance_currency)
        if len(currencies) != 1:
            raise C2SIMReadContractBlocked("BLOCKED_EXTERNAL_CONTRACT_ACCOUNT_CONTEXT_AMBIGUOUS")
        currency = next(iter(currencies))
        accepted_currencies = {
            str(item).upper() for item in gates["account_context"]["accepted_base_currencies"]
        }
        if currency not in accepted_currencies:
            raise C2SIMReadContractBlocked("BLOCKED_EXTERNAL_CONTRACT_ACCOUNT_CURRENCY_NOT_ACCEPTED")
        return PreflightEvidence(
            observed_at_utc=observed.observed_at_utc,
            account_fingerprint=observed.account_fingerprint,
            data_level=observed.data_level,
            account_count=observed.account_count,
            account_currency=currency,
            currency_decimals=observed.currency_decimals,
        )

    def _instrument_reference(self, *, require_trading_contract: bool) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for instrument in _etf11():
            detail = self._call(
                "instrument_detail",
                lambda client, selected=instrument: client.instrument_detail(
                    selected.uic, selected.asset_type
                ),
            )
            try:
                observed = validate_detail(instrument, detail)
            except (InstrumentDriftError, TypeError, ValueError) as exc:
                code = exc.args[0] if isinstance(exc, InstrumentDriftError) else "INSTRUMENT_DETAIL_INVALID"
                raise C2SIMReadDataQualityError(str(code), endpoint_id="instrument_detail") from None
            tradable_on = detail.get("TradableOn")
            tradable_in_context = isinstance(tradable_on, list) and bool(tradable_on)
            is_tradable = detail.get("IsTradable")
            record = {
                "instrument_key": instrument.key,
                "ticker": instrument.symbol.split(":", 1)[0].upper(),
                "uic": instrument.uic,
                "asset_type": instrument.asset_type,
                "symbol": observed["symbol"],
                "currency": observed["currency"],
                "exchange_id": observed["exchange_id"],
                "is_tradable": is_tradable is not False,
                "tradable_in_account_context": tradable_in_context,
                "tradable_account_count": len(tradable_on) if isinstance(tradable_on, list) else 0,
                "quantity_type": detail.get("AmountType", detail.get("DefaultAmountType")),
                "minimum_trade_size": detail.get("MinimumTradeSize"),
                "minimum_trade_value": detail.get("MinimumOrderValue"),
                "amount_decimals": detail.get("AmountDecimals"),
            }
            if require_trading_contract and (
                record["is_tradable"] is not True or not record["tradable_in_account_context"]
            ):
                raise C2SIMReadContractBlocked(
                    f"BLOCKED_EXTERNAL_CONTRACT_INSTRUMENT_NOT_TRADABLE:{instrument.key}",
                    endpoint_id="instrument_detail",
                )
            if require_trading_contract and (
                record["minimum_trade_size"] is None or record["amount_decimals"] is None
            ):
                raise C2SIMReadContractBlocked(
                    f"BLOCKED_EXTERNAL_CONTRACT_QUANTITY_RULES_INCOMPLETE:{instrument.key}",
                    endpoint_id="instrument_detail",
                )
            normalized.append(record)
        return normalized

    def instrument_reference(self) -> list[dict[str, Any]]:
        return self._instrument_reference(require_trading_contract=True)

    def instrument_reference_observation(self) -> list[dict[str, Any]]:
        return self._instrument_reference(require_trading_contract=False)

    def _atomic_quotes(
        self, gates: Mapping[str, Any] | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        instruments = _etf11()
        started = _utc(self._clock())
        payload = self._call(
            "info_prices",
            lambda client: client.info_prices(
                uics=[item.uic for item in instruments], asset_type="Etf", amount=1
            ),
        )
        finished = _utc(self._clock())
        rows = _data_rows(payload, code="QUOTE_RESPONSE_SHAPE_INVALID")
        expected = {item.uic: item for item in instruments}
        observed: dict[int, dict[str, Any]] = {}
        accepted_price_types = (
            None if gates is None else set(gates["quote"]["accepted_price_types"])
        )
        require_two_sided_bid_ask = (
            False
            if gates is None
            else gates["quote"]["require_two_sided_bid_ask"] is True
        )
        timestamps: list[datetime] = []
        delays: list[int] = []
        ages: list[float] = []
        observation_warnings: set[str] = set()
        unavailable_instrument_keys: list[str] = []
        missing_bid_count = 0
        missing_ask_count = 0
        valid_two_sided_quote_count = 0
        valid_reference_price_count = 0
        single_sided_reference_count = 0
        observed_error_codes: set[str] = set()
        observed_market_states: set[str] = set()
        for row in rows:
            raw_uic = row.get("Uic", row.get("Identifier"))
            if not isinstance(raw_uic, int) or raw_uic not in expected or raw_uic in observed:
                raise C2SIMReadDataQualityError("QUOTE_UIC_SET_INVALID", endpoint_id="info_prices")
            quote = row.get("Quote")
            if not isinstance(quote, dict):
                raise C2SIMReadDataQualityError("QUOTE_BODY_INVALID", endpoint_id="info_prices")
            instrument = expected[raw_uic]
            price_type_bid = quote.get("PriceTypeBid")
            price_type_ask = quote.get("PriceTypeAsk")
            quote_error = quote.get("ErrorCode")
            market_state = quote.get("MarketState")
            price_details = row.get("InstrumentPriceDetails")
            is_market_open = (
                price_details.get("IsMarketOpen")
                if isinstance(price_details, dict)
                else None
            )
            if isinstance(quote_error, str) and quote_error:
                observed_error_codes.add(quote_error)
            if isinstance(market_state, str) and market_state:
                observed_market_states.add(market_state)
            unavailable_price_types = {"NoAccess", "NoMarket", "Pending", "None"}
            declared_unavailable_types = {
                item
                for item in (price_type_bid, price_type_ask)
                if item in unavailable_price_types
            }
            quote_error_declared = quote_error not in (None, "", "None")
            market_closed = (
                is_market_open is False
                or (
                    isinstance(market_state, str)
                    and market_state.casefold() in {"closed", "unavailable", "halted"}
                )
            )
            declared_unavailable = bool(
                declared_unavailable_types or quote_error_declared or market_closed
            )
            if declared_unavailable:
                unavailable_instrument_keys.append(instrument.key)

            def optional_price(field: str, code: str) -> Decimal | None:
                raw_value = quote.get(field)
                if raw_value is None:
                    return None
                return Decimal(_decimal_text(raw_value, code=code, positive=True))

            bid = optional_price("Bid", "QUOTE_BID_INVALID")
            ask = optional_price("Ask", "QUOTE_ASK_INVALID")
            mid = optional_price("Mid", "QUOTE_MID_INVALID")
            if bid is None:
                missing_bid_count += 1
            if ask is None:
                missing_ask_count += 1
            if bid is not None and ask is not None and ask < bid:
                raise C2SIMReadDataQualityError(
                    "QUOTE_BID_ASK_CROSSED", endpoint_id="info_prices"
                )

            evaluation_price: Decimal | None = None
            evaluation_price_field: str | None = None
            normalized_mid: Decimal | None = None
            if mid is not None:
                evaluation_price = mid
                evaluation_price_field = "Mid"
                normalized_mid = mid
            elif bid is not None and ask is not None:
                evaluation_price = (bid + ask) / Decimal("2")
                evaluation_price_field = "BidAskMid"
                normalized_mid = evaluation_price
            elif bid is not None:
                evaluation_price = bid
                evaluation_price_field = "Bid"
            elif ask is not None:
                evaluation_price = ask
                evaluation_price_field = "Ask"

            if evaluation_price is None:
                if not declared_unavailable:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_REFERENCE_PRICE_MISSING", endpoint_id="info_prices"
                    )
                for value in declared_unavailable_types:
                    observation_warnings.add(
                        f"SIM_QUOTE_UNAVAILABLE_{value.upper()}"
                    )
                if quote_error_declared:
                    observation_warnings.add("SIM_QUOTE_ERROR_REPORTED")
                if market_closed:
                    observation_warnings.add("SIM_QUOTE_MARKET_CLOSED")
                observation_warnings.add("SIM_QUOTE_DAILY_CLOSE_FALLBACK_REQUIRED")
            else:
                valid_reference_price_count += 1
                if bid is not None and ask is not None:
                    valid_two_sided_quote_count += 1
                elif bid is None or ask is None:
                    single_sided_reference_count += 1
                    observation_warnings.add(
                        "SIM_QUOTE_SINGLE_SIDED_REFERENCE_ACCEPTED"
                    )
                if require_two_sided_bid_ask and (bid is None or ask is None):
                    code = "QUOTE_BID_INVALID" if bid is None else "QUOTE_ASK_INVALID"
                    raise C2SIMReadDataQualityError(code, endpoint_id="info_prices")

            last_updated: datetime | None = None
            age_seconds: float | None = None
            raw_last_updated = quote.get("LastUpdated", row.get("LastUpdated"))
            try:
                last_updated = _parse_utc(raw_last_updated)
                age_seconds = (finished - last_updated).total_seconds()
                if age_seconds < 0:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_TIMESTAMP_IN_FUTURE", endpoint_id="info_prices"
                    )
            except C2SIMReadDataQualityError:
                if evaluation_price is not None:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_LAST_UPDATED_INVALID", endpoint_id="info_prices"
                    ) from None
                observation_warnings.add("SIM_QUOTE_TIMESTAMP_UNAVAILABLE")
            delay = quote.get("DelayedByMinutes", row.get("DelayedByMinutes"))
            if not isinstance(delay, int) or delay < 0:
                if evaluation_price is not None:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_DELAY_INVALID", endpoint_id="info_prices"
                    )
                delay = None
                observation_warnings.add("SIM_QUOTE_DELAY_UNAVAILABLE")
            available_price_types = [
                item
                for item in (price_type_bid, price_type_ask)
                if isinstance(item, str)
                and item
                and item not in unavailable_price_types
            ]
            if not available_price_types:
                if evaluation_price is not None:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_PRICE_TYPE_MISSING", endpoint_id="info_prices"
                    )
                observation_warnings.add("SIM_QUOTE_PRICE_TYPE_UNAVAILABLE")
            if accepted_price_types is not None and evaluation_price is not None and any(
                item not in accepted_price_types for item in available_price_types
            ):
                raise C2SIMReadContractBlocked(
                    "BLOCKED_EXTERNAL_CONTRACT_QUOTE_PRICE_TYPE_NOT_ACCEPTED",
                    endpoint_id="info_prices",
                )
            if evaluation_price is not None and "Indicative" in available_price_types:
                observation_warnings.add("SIM_QUOTE_INDICATIVE_ACCEPTED")
            if gates is None:
                if "OldIndicative" in {price_type_bid, price_type_ask}:
                    observation_warnings.add("SIM_QUOTE_OLD_INDICATIVE")
                for value in declared_unavailable_types:
                    observation_warnings.add(
                        f"SIM_QUOTE_UNAVAILABLE_{value.upper()}"
                    )
            price_source = quote.get("PriceSource")
            if not isinstance(price_source, str) or not price_source:
                if evaluation_price is not None:
                    raise C2SIMReadDataQualityError(
                        "QUOTE_PRICE_SOURCE_MISSING", endpoint_id="info_prices"
                    )
                price_source = None
                observation_warnings.add("SIM_QUOTE_PRICE_SOURCE_UNAVAILABLE")
            observed[raw_uic] = {
                "instrument_key": instrument.key,
                "ticker": instrument.symbol.split(":", 1)[0].upper(),
                "uic": raw_uic,
                "asset_type": "Etf",
                "last_updated": None if last_updated is None else _utc_text(last_updated),
                "price_source": price_source,
                "amount": 1,
                "bid": None if bid is None else format(bid, "f"),
                "ask": None if ask is None else format(ask, "f"),
                "mid": None if normalized_mid is None else format(normalized_mid, "f"),
                "evaluation_price": (
                    None if evaluation_price is None else format(evaluation_price, "f")
                ),
                "evaluation_price_field": evaluation_price_field,
                "evaluation_mode": "LOW_FREQUENCY_DELAYED_OR_DAILY",
                "bid_size": None if quote.get("BidSize") is None else _decimal_text(
                    quote["BidSize"], code="QUOTE_BID_SIZE_INVALID"
                ),
                "ask_size": None if quote.get("AskSize") is None else _decimal_text(
                    quote["AskSize"], code="QUOTE_ASK_SIZE_INVALID"
                ),
                "delayed_by_minutes": delay,
                "error_code": None,
                "market_state": quote.get("MarketState"),
                "price_type_bid": price_type_bid,
                "price_type_ask": price_type_ask,
                "quote_error_code": quote_error,
                "is_market_open": is_market_open,
            }
            if last_updated is not None:
                timestamps.append(last_updated)
            if delay is not None:
                delays.append(delay)
            if age_seconds is not None:
                ages.append(age_seconds)
        if set(observed) != set(expected) or len(observed) != 11:
            raise C2SIMReadDataQualityError("QUOTE_UIC_SET_INVALID", endpoint_id="info_prices")
        last_updated_span = (
            None
            if not timestamps
            else (max(timestamps) - min(timestamps)).total_seconds()
        )
        wall_span = (finished - started).total_seconds()
        max_delay = None if not delays else max(delays)
        max_age = None if not ages else max(ages)
        if gates is not None:
            if valid_reference_price_count and (
                max_delay is None or max_age is None or last_updated_span is None
            ):
                raise C2SIMReadDataQualityError(
                    "QUOTE_REQUIRED_METRICS_MISSING", endpoint_id="info_prices"
                )
            if max_delay is not None and max_delay > gates["quote"]["max_delayed_by_minutes"]:
                raise C2SIMReadDataNotReady("DATA_NOT_READY_QUOTE_DELAY_EXCEEDED", endpoint_id="info_prices")
            if max_age is not None and max_age > gates["quote"]["max_quote_age_seconds"]:
                raise C2SIMReadDataNotReady("DATA_NOT_READY_QUOTE_AGE_EXCEEDED", endpoint_id="info_prices")
            if max(last_updated_span or 0, wall_span) > gates["quote"]["max_atomic_span_seconds"]:
                raise C2SIMReadDataNotReady("DATA_NOT_READY_QUOTE_ATOMIC_SPAN_EXCEEDED", endpoint_id="info_prices")
            if max_delay is not None and max_delay > 0 and gates["quote"]["allow_sim_delayed_quotes"] is not True:
                raise C2SIMReadDataNotReady("DATA_NOT_READY_SIM_DELAY_NOT_ACCEPTED", endpoint_id="info_prices")
            if max_delay is not None and max_delay > 0:
                observation_warnings.add("SIM_DELAYED_QUOTE_ACCEPTED_BY_POLICY")
        ordered = [observed[item.uic] for item in instruments]
        metrics = {
            "instrument_count": len(ordered),
            "observed_at_utc": _utc_text(finished),
            "atomic_observation_started_at_utc": _utc_text(started),
            "atomic_observation_finished_at_utc": _utc_text(finished),
            "atomic_wall_span_seconds": wall_span,
            "last_updated_span_seconds": last_updated_span,
            "max_quote_age_seconds": max_age,
            "max_delayed_by_minutes": max_delay,
            "valid_two_sided_quote_count": valid_two_sided_quote_count,
            "valid_reference_price_count": valid_reference_price_count,
            "single_sided_reference_count": single_sided_reference_count,
            "unavailable_quote_count": len(unavailable_instrument_keys),
            "unavailable_instrument_keys": unavailable_instrument_keys,
            "missing_bid_count": missing_bid_count,
            "missing_ask_count": missing_ask_count,
            "observed_error_codes": sorted(observed_error_codes),
            "observed_market_states": sorted(observed_market_states),
            "warning_ids": sorted(observation_warnings),
            "observed_price_types": sorted(
                {
                    str(item["price_type_bid"])
                    for item in ordered
                    if item["price_type_bid"] is not None
                }
                | {
                    str(item["price_type_ask"])
                    for item in ordered
                    if item["price_type_ask"] is not None
                }
            ),
            "observed_price_sources": sorted(
                {
                    str(item["price_source"])
                    for item in ordered
                    if item["price_source"] is not None
                }
            ),
            "atomic_uic_sha256": canonical_json_sha256([item.uic for item in instruments]),
        }
        return ordered, metrics

    def atomic_quotes(self, gates: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._atomic_quotes(gates)

    def atomic_quotes_observation(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._atomic_quotes(None)


def _receipt(
    *,
    observed: datetime,
    receipt_id: str,
    contract_id: str,
    dataset_role: str,
    availability_state: str,
    freshness_state: str,
    quality_state: str,
    blocker_ids: list[str],
    payload: Mapping[str, Any],
    provider_id: str = "SAXO_OPENAPI_SIM",
    provider_data_version: str | None = None,
    lineage_id: str | None = None,
    ordered_content_sha256: str | None = None,
    dataset_id: str | None = None,
    account_fingerprint: str | None = None,
    warning_ids: list[str] | None = None,
) -> dict[str, Any]:
    available = availability_state in {"AVAILABLE", "AVAILABLE_WITH_WARNINGS"}
    return finalize_strategy_external_receipt(
        {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "contract_id": contract_id,
            "dataset_role": dataset_role,
            "availability_state": availability_state,
            "dataset_id": dataset_id,
            "provider_id": provider_id,
            "provider_data_version": provider_data_version,
            "lineage_id": lineage_id,
            "manifest_sha256": load_strategy_external_contract()[1],
            "ordered_content_sha256": ordered_content_sha256,
            "calendar_id": None,
            "source_as_of": observed.date().isoformat(),
            "source_observed_at_utc": _utc_text(observed),
            "available_at_utc": _utc_text(observed),
            "accepted_at_utc": _utc_text(observed) if available else None,
            "expected_by_utc": None,
            "published_at_utc": None,
            "freshness_state": freshness_state,
            "quality_state": quality_state,
            "revision_state": "CURRENT_ACCEPTED" if available else "NOT_EVALUATED",
            "cost_confidence": "NOT_APPLICABLE",
            "warning_ids": warning_ids or [],
            "blocker_ids": blocker_ids,
            "values_modified": False,
            "interpolation_performed": False,
            "account_fingerprint": account_fingerprint,
            "payload": dict(payload),
            "supersedes_receipt_id": None,
        }
    )


def _failure_receipts(
    *,
    observed: datetime,
    status: str,
    code: str,
    endpoint_id: str | None,
    events: list[dict[str, Any]],
    request_count: int,
    write_request_count: int,
) -> list[dict[str, Any]]:
    if status == "BLOCKED_INTERFACE_OPERATIONAL":
        freshness, quality = "BLOCKED_INTERFACE_OPERATIONAL", "NOT_EVALUATED"
    elif status == "DATA_NOT_READY":
        freshness, quality = "DATA_NOT_READY", "NOT_EVALUATED"
    elif status == "FAIL_DATA_QUALITY":
        freshness, quality = "NOT_EVALUATED_SLA", "FAIL_DATA_QUALITY"
    else:
        freshness, quality = "NOT_EVALUATED_SLA", "NOT_EVALUATED"
    stamp = observed.strftime("%Y%m%dT%H%M%S%fZ")
    roles = (
        ("c2_edc06_instrument_reference_v1", "INSTRUMENT_REFERENCE", "reference"),
        ("c2_edc07_proposal_price_snapshot_v1", "PROPOSAL_PRICE_SNAPSHOT", "quote"),
        ("c2_edc09_currency_and_amount_unit_v1", "CURRENCY_AND_AMOUNT_UNIT", "quantum"),
    )
    payload = {
        "session_contract_id": SESSION_CONTRACT_ID,
        "failure_domain": status,
        "failed_endpoint_id": endpoint_id,
        "request_count": request_count,
        "write_request_count": write_request_count,
        "events": events,
        "raw_response_saved": False,
        "credentials_saved": False,
    }
    return [
        _receipt(
            observed=observed,
            receipt_id=f"c2-{stamp}-sim-read-{suffix}",
            contract_id=contract_id,
            dataset_role=role,
            availability_state=status,
            freshness_state=freshness,
            quality_state=quality,
            blocker_ids=[code],
            payload=payload,
        )
        for contract_id, role, suffix in roles
    ]


def _success_receipts(
    *,
    observed: datetime,
    preflight: PreflightEvidence,
    instruments: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    metrics: Mapping[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stamp = observed.strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_id = f"c2-sim-read-{stamp}"
    reference_hash = canonical_json_sha256(instruments)
    quote_hash = canonical_json_sha256(quotes)
    max_delay = metrics.get("max_delayed_by_minutes")
    delayed = isinstance(max_delay, int) and max_delay > 0
    metric_warnings = list(metrics.get("warning_ids") or [])
    warnings = sorted(
        set(metric_warnings)
        | ({"SIM_DELAYED_QUOTE_ACCEPTED_BY_POLICY"} if delayed else set())
    )
    availability = "AVAILABLE_WITH_WARNINGS" if warnings else "AVAILABLE"
    quality = "PASS_WITH_WARNINGS" if warnings else "PASS"
    reference_id = f"c2-{stamp}-sim-read-reference"
    reference_payload = {
        "receipt_id": reference_id,
        "observed_at_utc": _utc_text(observed),
        "environment": "SIM",
        "account_fingerprint": preflight.account_fingerprint,
        "source_endpoint_revision": "Saxo OpenAPI reference/account GET v1",
        "normalized_sha256": reference_hash,
        "data_level": preflight.data_level,
        "account_count": preflight.account_count,
        "instruments": instruments,
        "events": events,
        "raw_response_saved": False,
        "credentials_saved": False,
    }
    receipts = [
        _receipt(
            observed=observed,
            receipt_id=reference_id,
            contract_id="c2_edc06_instrument_reference_v1",
            dataset_role="INSTRUMENT_REFERENCE",
            availability_state="AVAILABLE",
            freshness_state="CURRENT",
            quality_state="PASS",
            blocker_ids=[],
            payload=reference_payload,
            provider_data_version=reference_hash,
            lineage_id="c2-sim-ephemeral-reference-observation-v1",
            ordered_content_sha256=reference_hash,
            dataset_id=snapshot_id,
            account_fingerprint=preflight.account_fingerprint,
        )
    ]
    quantum_id = f"c2-{stamp}-sim-read-quantum"
    quantum_payload = {
        "receipt_id": quantum_id,
        "account_fingerprint": preflight.account_fingerprint,
        "account_currency": preflight.account_currency,
        "currency_decimals": preflight.currency_decimals,
        "strategy_quantity_rule": "OBSERVED_PROVIDER_RULES_ONLY_NO_ORDER_PERMISSION",
        "minimum_trade_size": {
            item["instrument_key"]: item["minimum_trade_size"] for item in instruments
        },
        "minimum_trade_value": {
            item["instrument_key"]: item["minimum_trade_value"] for item in instruments
        },
        "amount_decimals": {
            item["instrument_key"]: item["amount_decimals"] for item in instruments
        },
        "source_observed_at_utc": _utc_text(observed),
        "raw_response_saved": False,
        "credentials_saved": False,
    }
    receipts.append(
        _receipt(
            observed=observed,
            receipt_id=quantum_id,
            contract_id="c2_edc09_currency_and_amount_unit_v1",
            dataset_role="CURRENCY_AND_AMOUNT_UNIT",
            availability_state="AVAILABLE",
            freshness_state="CURRENT",
            quality_state="PASS",
            blocker_ids=[],
            payload=quantum_payload,
            provider_data_version=reference_hash,
            lineage_id="c2-sim-ephemeral-account-quantum-observation-v1",
            ordered_content_sha256=canonical_json_sha256(quantum_payload),
            dataset_id=snapshot_id,
            account_fingerprint=preflight.account_fingerprint,
        )
    )
    for quote in quotes:
        quote_available = quote.get("evaluation_price") is not None
        quote_id = f"c2-{stamp}-sim-read-quote-{quote['instrument_key']}"
        quote_payload = {
            "snapshot_id": snapshot_id,
            "observed_at_utc": _utc_text(observed),
            "account_fingerprint": preflight.account_fingerprint,
            **quote,
            **dict(metrics),
            "atomic_ordered_content_sha256": quote_hash,
            "raw_response_saved": False,
            "credentials_saved": False,
        }
        receipts.append(
            _receipt(
                observed=observed,
                receipt_id=quote_id,
                contract_id="c2_edc07_proposal_price_snapshot_v1",
                dataset_role="PROPOSAL_PRICE_SNAPSHOT",
                availability_state=(availability if quote_available else "DATA_NOT_READY"),
                freshness_state=("DELAYED" if quote_available and delayed else (
                    "CURRENT" if quote_available else "DATA_NOT_READY"
                )),
                quality_state=(quality if quote_available else "NOT_EVALUATED"),
                blocker_ids=[],
                warning_ids=warnings,
                payload=quote_payload,
                provider_data_version=quote_hash,
                lineage_id="c2-sim-ephemeral-atomic-infoprice-observation-v1",
                ordered_content_sha256=quote_hash,
                dataset_id=snapshot_id,
                account_fingerprint=preflight.account_fingerprint,
            )
        )
    return receipts


def run_ephemeral_sim_read_observation(
    *,
    access_token: str,
    access_expires_at_utc: datetime,
    fingerprint_key: bytes,
    operational_gates: Mapping[str, Any],
    provider_decisions: Mapping[str, Any],
    client_factory: Callable[[str], SIMReadClient] = SaxoClient,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Collect a redacted ETF11 SIM read bundle; never register it automatically."""

    load_ephemeral_session_contract()
    load_low_frequency_price_policy()
    try:
        validate_operational_gates(operational_gates, require_accepted=True)
        validate_provider_decisions(provider_decisions, require_approved=True)
    except C2DecisionError as exc:
        observed = _utc(clock())
        receipts = _failure_receipts(
            observed=observed,
            status="BLOCKED_EXTERNAL_CONTRACT",
            code=str(exc),
            endpoint_id=None,
            events=[],
            request_count=0,
            write_request_count=0,
        )
        return {
            "status": "BLOCKED_EXTERNAL_CONTRACT",
            "session_contract_id": SESSION_CONTRACT_ID,
            "receipt_count": len(receipts),
            "receipts": receipts,
            "request_count": 0,
            "write_request_count": 0,
            "credentials_saved": False,
            "registration_performed": False,
        }
    session: EphemeralSIMReadSession | None = None
    try:
        session = EphemeralSIMReadSession(
            access_token,
            access_expires_at_utc=access_expires_at_utc,
            fingerprint_key=fingerprint_key,
            client_factory=client_factory,
            clock=clock,
        )
        preflight = session.preflight(operational_gates)
        instruments = session.instrument_reference()
        quotes, metrics = session.atomic_quotes(operational_gates)
        observed = _utc(clock())
        receipts = _success_receipts(
            observed=observed,
            preflight=preflight,
            instruments=instruments,
            quotes=quotes,
            metrics=metrics,
            events=session.events,
        )
        status = "AVAILABLE_WITH_WARNINGS" if metrics["warning_ids"] else "AVAILABLE"
    except C2SIMReadOperationalError as exc:
        observed = _utc(clock())
        receipts = _failure_receipts(
            observed=observed,
            status="BLOCKED_INTERFACE_OPERATIONAL",
            code=exc.code,
            endpoint_id=exc.endpoint_id,
            events=[] if session is None else session.events,
            request_count=0 if session is None else session.request_count,
            write_request_count=0 if session is None else session.write_request_count,
        )
        status = "BLOCKED_INTERFACE_OPERATIONAL"
    except C2SIMReadContractBlocked as exc:
        observed = _utc(clock())
        receipts = _failure_receipts(
            observed=observed,
            status="BLOCKED_EXTERNAL_CONTRACT",
            code=exc.code,
            endpoint_id=exc.endpoint_id,
            events=[] if session is None else session.events,
            request_count=0 if session is None else session.request_count,
            write_request_count=0 if session is None else session.write_request_count,
        )
        status = "BLOCKED_EXTERNAL_CONTRACT"
    except C2SIMReadDataNotReady as exc:
        observed = _utc(clock())
        receipts = _failure_receipts(
            observed=observed,
            status="DATA_NOT_READY",
            code=exc.code,
            endpoint_id=exc.endpoint_id,
            events=[] if session is None else session.events,
            request_count=0 if session is None else session.request_count,
            write_request_count=0 if session is None else session.write_request_count,
        )
        status = "DATA_NOT_READY"
    except C2SIMReadDataQualityError as exc:
        observed = _utc(clock())
        receipts = _failure_receipts(
            observed=observed,
            status="FAIL_DATA_QUALITY",
            code=exc.code,
            endpoint_id=exc.endpoint_id,
            events=[] if session is None else session.events,
            request_count=0 if session is None else session.request_count,
            write_request_count=0 if session is None else session.write_request_count,
        )
        status = "FAIL_DATA_QUALITY"
    finally:
        request_count = 0 if session is None else session.request_count
        write_request_count = 0 if session is None else session.write_request_count
        if session is not None:
            session.close()
    return {
        "status": status,
        "session_contract_id": SESSION_CONTRACT_ID,
        "receipt_count": len(receipts),
        "receipts": receipts,
        "request_count": request_count,
        "write_request_count": write_request_count,
        "credentials_saved": False,
        "registration_performed": False,
    }


def run_initial_sim_observation_session(
    session: Any,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Run the 15-GET technical observation without downstream provider gates."""

    load_ephemeral_session_contract()
    load_observation_start_contract()
    load_low_frequency_price_policy()
    try:
        preflight = session.preflight_observation()
        instruments = session.instrument_reference_observation()
        quotes, metrics = session.atomic_quotes_observation()
        warnings = list(metrics.get("warning_ids", []))
        if (
            metrics["max_delayed_by_minutes"] is not None
            and metrics["max_delayed_by_minutes"] > 0
        ):
            warnings.append("SIM_QUOTE_DELAY_OBSERVED_DOWNSTREAM_REVIEW_REQUIRED")
        warnings = sorted(set(warnings))
        status = "PASS_WITH_WARNINGS" if warnings else "PASS"
        result = {
            "status": status,
            "observation_contract_id": OBSERVATION_CONTRACT_ID,
            "observed_at_utc": _utc_text(_utc(clock())),
            "minimum_format_identity_quote_checks": status,
            "account_context": {
                "account_count": preflight.account_count,
                "account_currencies": list(preflight.account_currencies),
                "balance_currency": preflight.balance_currency,
                "currency_decimals": preflight.currency_decimals,
                "data_level": preflight.data_level,
                "raw_identifiers_exposed": False,
            },
            "instrument_observation": {
                "instrument_count": len(instruments),
                "instrument_keys": [item["instrument_key"] for item in instruments],
                "identity_check": "PASS",
                "trading_eligibility_gate_applied": False,
            },
            "quote_observation": {
                "quote_count": len(quotes),
                "identity_and_reference_price_check": "PASS",
                "max_quote_age_seconds": metrics["max_quote_age_seconds"],
                "max_delayed_by_minutes": metrics["max_delayed_by_minutes"],
                "last_updated_span_seconds": metrics["last_updated_span_seconds"],
                "atomic_wall_span_seconds": metrics["atomic_wall_span_seconds"],
                "observed_price_types": metrics["observed_price_types"],
                "observed_price_sources": metrics["observed_price_sources"],
                "valid_two_sided_quote_count": metrics[
                    "valid_two_sided_quote_count"
                ],
                "valid_reference_price_count": metrics[
                    "valid_reference_price_count"
                ],
                "single_sided_reference_count": metrics[
                    "single_sided_reference_count"
                ],
                "unavailable_quote_count": metrics["unavailable_quote_count"],
                "unavailable_instrument_keys": metrics[
                    "unavailable_instrument_keys"
                ],
                "missing_bid_count": metrics["missing_bid_count"],
                "missing_ask_count": metrics["missing_ask_count"],
                "observed_error_codes": metrics["observed_error_codes"],
                "observed_market_states": metrics["observed_market_states"],
                "price_values_exposed": False,
            },
            "warning_ids": warnings,
            "downstream_stage_status": "DECISION_REQUIRED_NON_BLOCKING_FOR_OBSERVATION",
            "raw_response_saved": False,
            "receipt_registration_performed": False,
            "db_writes_performed": 0,
            "periodic_execution_started": False,
        }
    except C2SIMReadOperationalError as exc:
        result = {
            "status": "BLOCKED_INTERFACE_OPERATIONAL",
            "error_code": exc.code,
            "failed_endpoint_id": exc.endpoint_id,
        }
    except C2SIMReadContractBlocked as exc:
        result = {
            "status": "BLOCKED_EXTERNAL_CONTRACT",
            "error_code": exc.code,
            "failed_endpoint_id": exc.endpoint_id,
        }
    except C2SIMReadDataNotReady as exc:
        result = {
            "status": "DATA_NOT_READY",
            "error_code": exc.code,
            "failed_endpoint_id": exc.endpoint_id,
        }
    except C2SIMReadDataQualityError as exc:
        result = {
            "status": "FAIL_DATA_QUALITY",
            "error_code": exc.code,
            "failed_endpoint_id": exc.endpoint_id,
        }
    request_count = int(session.request_count)
    write_request_count = int(session.write_request_count)
    if write_request_count != 0:
        return {
            "status": "BLOCKED_INTERFACE_OPERATIONAL",
            "error_code": "BLOCKED_INTERFACE_OPERATIONAL_WRITE_COUNTER_NONZERO",
            "request_count": request_count,
            "write_request_count": write_request_count,
            "raw_response_saved": False,
            "receipt_registration_performed": False,
            "db_writes_performed": 0,
            "periodic_execution_started": False,
            "orders_or_prechecks_sent": 0,
            "credential_values_exposed": False,
        }
    return {
        **result,
        "request_count": request_count,
        "write_request_count": write_request_count,
        "raw_response_saved": False,
        "receipt_registration_performed": False,
        "db_writes_performed": 0,
        "periodic_execution_started": False,
        "orders_or_prechecks_sent": 0,
        "credential_values_exposed": False,
    }
