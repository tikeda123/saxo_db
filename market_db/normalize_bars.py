"""Decimal-preserving normalization for Saxo ETF and FX chart samples."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .instrument_registry import CanonicalInstrument


class BarQualityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrossedQuoteViolation:
    field: str
    bid: Decimal
    ask: Decimal


class FXBidAboveAskError(BarQualityError):
    def __init__(self, violations: tuple[CrossedQuoteViolation, ...]):
        super().__init__("FX_BID_ABOVE_ASK")
        self.violations = violations


@dataclass(frozen=True)
class NormalizedBar:
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    open_bid: Decimal | None
    high_bid: Decimal | None
    low_bid: Decimal | None
    close_bid: Decimal | None
    open_ask: Decimal | None
    high_ask: Decimal | None
    low_ask: Decimal | None
    close_ask: Decimal | None
    volume: Decimal | None
    market_trading_state: str | None
    price_basis: str
    is_complete: bool
    data_version: int | None
    delayed_by_minutes: int | None
    retrieved_at_utc: datetime
    payload_sha256: str
    artifact_relative_path: str


@dataclass(frozen=True)
class RejectedBar:
    time_utc: datetime
    error_code: str
    violations: tuple[CrossedQuoteViolation, ...]
    data_version: int | None
    payload_sha256: str
    artifact_relative_path: str


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise BarQualityError("TIME_NOT_UTC")
    return parsed.astimezone(timezone.utc)


def decimal_value(value: Any, field: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BarQualityError(f"INVALID_{field.upper()}") from None
    if not result.is_finite():
        raise BarQualityError(f"INVALID_{field.upper()}")
    return result


def _validate_ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> None:
    if min(open_, high, low, close) <= 0:
        raise BarQualityError("NONPOSITIVE_OHLC")
    if high < max(open_, low, close) or low > min(open_, high, close):
        raise BarQualityError("OHLC_VIOLATION")


def _normalize_sample(
    instrument: CanonicalInstrument,
    sample: dict[str, Any],
    *,
    retrieved_at_utc: datetime,
    payload_sha256: str,
    artifact_relative_path: str,
    data_version: int | None,
    delayed_by_minutes: int | None,
) -> NormalizedBar:
    time_utc = parse_utc(str(sample.get("Time", "")))
    crossed: list[CrossedQuoteViolation] = []
    if instrument.asset_type == "FxSpot":
        pairs: dict[str, tuple[Decimal, Decimal]] = {}
        for name in ("Open", "High", "Low", "Close"):
            bid = decimal_value(sample.get(f"{name}Bid"), f"{name}Bid")
            ask = decimal_value(sample.get(f"{name}Ask"), f"{name}Ask")
            assert bid is not None and ask is not None
            if bid > ask:
                crossed.append(CrossedQuoteViolation(name, bid, ask))
            pairs[name] = (bid, ask)
        values = {name: (bid + ask) / Decimal(2) for name, (bid, ask) in pairs.items()}
        open_bid, open_ask = pairs["Open"]
        high_bid, high_ask = pairs["High"]
        low_bid, low_ask = pairs["Low"]
        close_bid, close_ask = pairs["Close"]
    else:
        values = {
            name: decimal_value(sample.get(name), name)
            for name in ("Open", "High", "Low", "Close")
        }
        assert all(value is not None for value in values.values())
        open_bid = high_bid = low_bid = close_bid = None
        open_ask = high_ask = low_ask = close_ask = None
    open_ = values["Open"]
    high = values["High"]
    low = values["Low"]
    close = values["Close"]
    assert isinstance(open_, Decimal) and isinstance(high, Decimal)
    assert isinstance(low, Decimal) and isinstance(close, Decimal)
    _validate_ohlc(open_, high, low, close)
    volume = decimal_value(sample.get("Volume"), "Volume", nullable=True)
    if instrument.asset_type == "FxSpot" and crossed:
        raise FXBidAboveAskError(tuple(crossed))
    return NormalizedBar(
        time_utc=time_utc,
        open=open_, high=high, low=low, close=close,
        open_bid=open_bid, high_bid=high_bid, low_bid=low_bid, close_bid=close_bid,
        open_ask=open_ask, high_ask=high_ask, low_ask=low_ask, close_ask=close_ask,
        volume=volume,
        market_trading_state=sample.get("MarketTradingState"),
        price_basis=instrument.price_basis,
        is_complete=True,
        data_version=data_version,
        delayed_by_minutes=delayed_by_minutes,
        retrieved_at_utc=retrieved_at_utc.astimezone(timezone.utc),
        payload_sha256=payload_sha256,
        artifact_relative_path=artifact_relative_path,
    )


def normalize_chart_page(
    instrument: CanonicalInstrument,
    payload: dict[str, Any],
    *,
    retrieved_at_utc: datetime,
    payload_sha256: str,
    artifact_relative_path: str,
) -> list[NormalizedBar]:
    chart_info = payload.get("ChartInfo") or {}
    if int(chart_info.get("Horizon", -1)) != 60:
        raise BarQualityError("WRONG_CHART_HORIZON")
    data = payload.get("Data")
    if not isinstance(data, list):
        raise BarQualityError("MISSING_CHART_DATA")
    data_version = payload.get("DataVersion")
    delayed = chart_info.get("DelayedByMinutes")
    return [
        _normalize_sample(
            instrument,
            sample,
            retrieved_at_utc=retrieved_at_utc,
            payload_sha256=payload_sha256,
            artifact_relative_path=artifact_relative_path,
            data_version=None if data_version is None else int(data_version),
            delayed_by_minutes=None if delayed is None else int(delayed),
        )
        for sample in data
    ]


def normalize_chart_page_quarantining_fx_extrema(
    instrument: CanonicalInstrument,
    payload: dict[str, Any],
    *,
    retrieved_at_utc: datetime,
    payload_sha256: str,
    artifact_relative_path: str,
) -> tuple[list[NormalizedBar], list[RejectedBar]]:
    """Exclude crossed historical FX High/Low samples without modifying source values."""
    if instrument.asset_type != "FxSpot":
        raise ValueError("FX extrema quarantine is restricted to FxSpot")
    chart_info = payload.get("ChartInfo") or {}
    if int(chart_info.get("Horizon", -1)) != 60:
        raise BarQualityError("WRONG_CHART_HORIZON")
    data = payload.get("Data")
    if not isinstance(data, list):
        raise BarQualityError("MISSING_CHART_DATA")
    data_version_value = payload.get("DataVersion")
    data_version = None if data_version_value is None else int(data_version_value)
    delayed_value = chart_info.get("DelayedByMinutes")
    delayed = None if delayed_value is None else int(delayed_value)
    accepted: list[NormalizedBar] = []
    rejected: list[RejectedBar] = []
    for sample in data:
        try:
            accepted.append(
                _normalize_sample(
                    instrument,
                    sample,
                    retrieved_at_utc=retrieved_at_utc,
                    payload_sha256=payload_sha256,
                    artifact_relative_path=artifact_relative_path,
                    data_version=data_version,
                    delayed_by_minutes=delayed,
                )
            )
        except FXBidAboveAskError as exc:
            # Open and Close represent contemporaneous quotes and remain fatal.
            # Only historical interval extrema are eligible for quarantine.
            if any(item.field not in {"High", "Low"} for item in exc.violations):
                raise
            rejected.append(
                RejectedBar(
                    time_utc=parse_utc(str(sample.get("Time", ""))),
                    error_code=str(exc),
                    violations=exc.violations,
                    data_version=data_version,
                    payload_sha256=payload_sha256,
                    artifact_relative_path=artifact_relative_path,
                )
            )
    return accepted, rejected


def merge_pages(pages: Iterable[Iterable[NormalizedBar]]) -> list[NormalizedBar]:
    by_time: dict[datetime, NormalizedBar] = {}
    for page in pages:
        for bar in page:
            by_time[bar.time_utc] = bar
    ordered = [by_time[key] for key in sorted(by_time)]
    if not ordered:
        return []
    return [replace(bar, is_complete=index < len(ordered) - 1) for index, bar in enumerate(ordered)]
