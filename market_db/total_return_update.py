"""Yahoo Finance total-return acquisition for the one-month SIM research run."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .connection import MARKET_DB, connect, project_root


PROFILE_PATH = Path("specs/source_collection/s6v5a_periodic_update_v1.json")
LEGACY_MANIFEST_PATH = Path("manifests/etf11_source_dataset_manifest.json")
REQUIRED_TICKERS = ("SPY", "IWM", "EFA", "EEM", "VNQ")
PROVIDER = "Yahoo Finance chart endpoint"
RESEARCH_ELIGIBILITY = "SIM_RESEARCH_ONLY"
VALUE_FIELDS = (
    "close_unadjusted",
    "adjusted_close",
    "dividend_cash",
    "split_factor",
)
QUALITY_VALUE_FIELDS = (
    "open_unadjusted",
    "high_unadjusted",
    "low_unadjusted",
    "close_unadjusted",
    "adjusted_close",
    "total_return_index",
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


class TotalReturnUpdateError(RuntimeError):
    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class FetchedTicker:
    ticker: str
    raw_bytes: bytes
    raw_sha256: str
    rows: tuple[dict[str, Any], ...]
    currency: str
    exchange_timezone: str


def provider_gate(path: Path | None = None) -> dict[str, Any]:
    selected = path or project_root() / PROFILE_PATH
    payload = json.loads(selected.read_text(encoding="utf-8"))
    contract = payload.get("total_return", {})
    provider = contract.get("provider")
    if (
        contract.get("status") == "READY_SIM_RESEARCH_ONLY"
        and contract.get("scheduled") is False
        and isinstance(provider, str)
        and provider.strip()
        and contract.get("research_eligibility") == RESEARCH_ELIGIBILITY
    ):
        return {
            "status": "READY_SIM_RESEARCH_ONLY",
            "scheduled": False,
            "operator_decision_required": False,
            "provider": provider,
            "research_eligibility": RESEARCH_ELIGIBILITY,
            "acquisition_mode": "operator_one_shot",
            "development_dataset_promoted": False,
        }
    if (
        contract.get("status") == "READY"
        and contract.get("scheduled") is True
        and isinstance(provider, str)
        and provider.strip()
    ):
        return {
            "status": "READY",
            "scheduled": True,
            "operator_decision_required": False,
            "provider": provider,
            "development_dataset_promoted": False,
        }
    return {
        "status": "BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED",
        "scheduled": False,
        "operator_decision_required": True,
        "required_provider_contract": list(contract.get("required_provider_contract", [])),
        "development_dataset_promoted": False,
    }


def classify_provider_error(http_status: int | None) -> dict[str, Any]:
    """Keep provider transport/auth failures out of data-quality findings."""

    if http_status in {401, 403}:
        domain = "interface_auth"
    else:
        domain = "interface_operational"
    return {
        "status": "BLOCKED",
        "error_domain": domain,
        "quality_status": "NOT_EVALUATED",
        "publish_current_dataset": False,
    }


def _decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _date(row: Mapping[str, Any]) -> date | None:
    value = row.get("date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _canonical_row(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "ticker": str(row["ticker"]).upper(),
        "date": str(row["date"]),
        **{field: str(row[field]) for field in VALUE_FIELDS},
        "provider_revision": str(row["provider_revision"]),
    }


def evaluate_total_return_batch(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fail closed on structural, price, order, and corporate-action defects."""

    selected = list(rows)
    errors: list[str] = []
    seen: set[tuple[str, date]] = set()
    previous: dict[str, date] = {}
    canonical: list[dict[str, str]] = []
    for index, row in enumerate(selected):
        ticker = str(row.get("ticker", "")).upper()
        session_date = _date(row)
        if ticker not in REQUIRED_TICKERS:
            errors.append(f"UNSUPPORTED_TICKER:{index}")
        if session_date is None:
            errors.append(f"INVALID_DATE:{index}")
        elif (ticker, session_date) in seen:
            errors.append(f"DUPLICATE_DATE:{ticker}:{session_date.isoformat()}")
        else:
            seen.add((ticker, session_date))
            if ticker in previous and session_date < previous[ticker]:
                errors.append(f"DATE_REVERSAL:{ticker}:{session_date.isoformat()}")
            previous[ticker] = session_date

        values = {field: _decimal(row, field) for field in VALUE_FIELDS}
        if values["close_unadjusted"] is None or values["close_unadjusted"] <= 0:
            errors.append(f"NONPOSITIVE_CLOSE:{index}")
        if values["adjusted_close"] is None or values["adjusted_close"] <= 0:
            errors.append(f"NONPOSITIVE_ADJUSTED_CLOSE:{index}")
        if values["dividend_cash"] is None or values["dividend_cash"] < 0:
            errors.append(f"INVALID_DIVIDEND:{index}")
        if values["split_factor"] is None or values["split_factor"] <= 0:
            errors.append(f"INVALID_SPLIT_FACTOR:{index}")
        if not str(row.get("provider_revision", "")).strip():
            errors.append(f"PROVIDER_REVISION_MISSING:{index}")
        try:
            canonical.append(_canonical_row(row))
        except KeyError:
            errors.append(f"REQUIRED_FIELD_MISSING:{index}")

    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "status": "PASS" if not errors else "FAIL",
        "row_count": len(selected),
        "errors": sorted(set(errors)),
        "duplicate_count": sum(error.startswith("DUPLICATE_DATE:") for error in errors),
        "ordered_content_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def evaluate_sim_research_quality(
    rows: Iterable[Mapping[str, Any]], expected_sessions: Iterable[date]
) -> dict[str, Any]:
    """Evaluate the explicit minimum gate before a SIM current dataset is published."""

    selected = list(rows)
    base = evaluate_total_return_batch(selected)
    errors = list(base["errors"])
    dates_by_ticker: dict[str, set[date]] = {ticker: set() for ticker in REQUIRED_TICKERS}
    for index, row in enumerate(selected):
        ticker = str(row.get("ticker", "")).upper()
        session_date = _date(row)
        if ticker in dates_by_ticker and session_date is not None:
            dates_by_ticker[ticker].add(session_date)
        values = {field: _decimal(row, field) for field in QUALITY_VALUE_FIELDS}
        for field, value in values.items():
            if value is None:
                errors.append(f"NULL_REQUIRED_VALUE:{ticker}:{field}:{index}")
            elif value <= 0:
                errors.append(f"NONPOSITIVE_VALUE:{ticker}:{field}:{index}")
        volume = _decimal(row, "volume")
        if volume is not None and volume < 0:
            errors.append(f"NEGATIVE_VOLUME:{ticker}:{index}")
        open_value = values["open_unadjusted"]
        high_value = values["high_unadjusted"]
        low_value = values["low_unadjusted"]
        close_value = values["close_unadjusted"]
        if None not in {open_value, high_value, low_value, close_value}:
            assert open_value is not None and high_value is not None
            assert low_value is not None and close_value is not None
            if high_value < max(open_value, low_value, close_value):
                errors.append(f"OHLC_HIGH_INCONSISTENT:{ticker}:{index}")
            if low_value > min(open_value, high_value, close_value):
                errors.append(f"OHLC_LOW_INCONSISTENT:{ticker}:{index}")

    expected = set(expected_sessions)
    missing_by_ticker = {
        ticker: sorted(value.isoformat() for value in expected - dates_by_ticker[ticker])
        for ticker in REQUIRED_TICKERS
    }
    for ticker, missing in missing_by_ticker.items():
        if missing:
            errors.append(f"MISSING_EXPECTED_SESSION:{ticker}:{','.join(missing)}")
        if not dates_by_ticker[ticker]:
            errors.append(f"TICKER_MISSING:{ticker}")

    unique_errors = sorted(set(errors))
    return {
        **base,
        "status": "PASS" if not unique_errors else "FAIL",
        "errors": unique_errors,
        "missing_count": sum(len(value) for value in missing_by_ticker.values()),
        "missing_by_ticker": missing_by_ticker,
        "null_or_nonpositive_count": sum(
            value.startswith(("NULL_REQUIRED_VALUE:", "NONPOSITIVE_VALUE:"))
            for value in unique_errors
        ),
        "latest_session_date": {
            ticker: max(values).isoformat() if values else None
            for ticker, values in dates_by_ticker.items()
        },
    }


def revision_keys(
    previous_rows: Iterable[Mapping[str, Any]],
    current_rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    def indexed(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
        return {
            (str(row["ticker"]).upper(), str(row["date"])): _canonical_row(row)
            for row in rows
        }

    previous = indexed(previous_rows)
    current = indexed(current_rows)
    return tuple(
        f"{ticker}:{session_date}"
        for ticker, session_date in sorted(previous.keys() & current.keys())
        if previous[(ticker, session_date)] != current[(ticker, session_date)]
    )


def _split_factor(event: Mapping[str, Any] | None) -> Decimal:
    if not event:
        return Decimal("1")
    numerator = event.get("numerator")
    denominator = event.get("denominator")
    try:
        if numerator is not None and denominator is not None:
            selected = Decimal(str(numerator)) / Decimal(str(denominator))
            if selected > 0:
                return selected
        ratio = str(event.get("splitRatio", ""))
        left, right = ratio.split(":", 1)
        selected = Decimal(left) / Decimal(right)
        return selected if selected > 0 else Decimal("0")
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return Decimal("0")


def parse_yahoo_chart(ticker: str, raw_bytes: bytes) -> FetchedTicker:
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(raw_bytes, parse_float=Decimal)
        chart = payload["chart"]
        if chart.get("error") is not None:
            raise TotalReturnUpdateError("YAHOO_RESPONSE_ERROR")
        result = chart["result"][0]
        meta = result["meta"]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TotalReturnUpdateError("YAHOO_RESPONSE_INVALID") from exc
    if str(meta.get("symbol", "")).upper() != ticker:
        raise TotalReturnUpdateError("YAHOO_SYMBOL_MISMATCH")
    timezone_name = str(meta.get("exchangeTimezoneName", ""))
    if timezone_name != "America/New_York":
        raise TotalReturnUpdateError("YAHOO_TIMEZONE_MISMATCH")
    currency = str(meta.get("currency", ""))
    if currency != "USD":
        raise TotalReturnUpdateError("YAHOO_CURRENCY_MISMATCH")
    lengths = [
        len(timestamps), len(quote.get("open", [])), len(quote.get("high", [])),
        len(quote.get("low", [])), len(quote.get("close", [])),
        len(quote.get("volume", [])), len(adjusted),
    ]
    if not lengths or len(set(lengths)) != 1 or lengths[0] == 0:
        raise TotalReturnUpdateError("YAHOO_ARRAY_LENGTH_MISMATCH")

    exchange_tz = ZoneInfo(timezone_name)
    events = result.get("events") or {}
    dividends: dict[date, Decimal] = {}
    for event in (events.get("dividends") or {}).values():
        event_date = datetime.fromtimestamp(int(event["date"]), exchange_tz).date()
        dividends[event_date] = dividends.get(event_date, Decimal("0")) + Decimal(
            str(event["amount"])
        )
    splits: dict[date, Decimal] = {}
    for event in (events.get("splits") or {}).values():
        event_date = datetime.fromtimestamp(int(event["date"]), exchange_tz).date()
        splits[event_date] = _split_factor(event)

    first_adjusted: Decimal | None = None
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        session_date = datetime.fromtimestamp(int(timestamp), exchange_tz).date()
        adjusted_close = adjusted[index]
        if first_adjusted is None and adjusted_close is not None:
            first_adjusted = Decimal(str(adjusted_close))
        if first_adjusted is None or first_adjusted <= 0:
            total_return_index: Decimal | None = None
        elif adjusted_close is None:
            total_return_index = None
        else:
            total_return_index = Decimal(str(adjusted_close)) / first_adjusted * Decimal("100")
        rows.append(
            {
                "ticker": ticker,
                "date": session_date,
                "currency": currency,
                "open_unadjusted": quote["open"][index],
                "high_unadjusted": quote["high"][index],
                "low_unadjusted": quote["low"][index],
                "close_unadjusted": quote["close"][index],
                "adjusted_close": adjusted_close,
                "total_return_index": total_return_index,
                "volume": quote["volume"][index],
                "dividend_cash": dividends.get(session_date, Decimal("0")),
                "split_factor": splits.get(session_date, Decimal("1")),
                "source": PROVIDER,
                "provider_revision": raw_sha256,
            }
        )
    return FetchedTicker(
        ticker=ticker,
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        rows=tuple(rows),
        currency=currency,
        exchange_timezone=timezone_name,
    )


def _fetch_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "saxo-db-sim-research/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise TotalReturnUpdateError(
            f"YAHOO_HTTP_{exc.code}", http_status=int(exc.code)
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise TotalReturnUpdateError("YAHOO_TRANSPORT_FAILED") from exc
    if status < 200 or status >= 300:
        raise TotalReturnUpdateError(f"YAHOO_HTTP_{status}", http_status=status)
    if not body:
        raise TotalReturnUpdateError("YAHOO_EMPTY_RESPONSE", http_status=status)
    return body


def fetch_yahoo_ticker(
    ticker: str,
    *,
    period2: int,
    fetcher: Callable[[str, float], bytes] = _fetch_bytes,
    attempts: int = 3,
) -> FetchedTicker:
    query = urllib.parse.urlencode(
        {
            "period1": 0,
            "period2": period2,
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
    )
    url = f"{YAHOO_CHART_URL.format(ticker=ticker)}?{query}"
    last_error: TotalReturnUpdateError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return parse_yahoo_chart(ticker, fetcher(url, 30.0))
        except TotalReturnUpdateError as exc:
            last_error = exc
            if exc.http_status in {401, 403, 404} or attempt == attempts:
                raise
            time.sleep(float(attempt))
    assert last_error is not None
    raise last_error


def _latest_completed_session() -> tuple[date, tuple[date, ...]]:
    legacy = json.loads((project_root() / LEGACY_MANIFEST_PATH).read_text(encoding="utf-8"))
    refresh_start = max(
        date.fromisoformat(legacy["etfs"][ticker]["last_date"])
        for ticker in REQUIRED_TICKERS
    )
    with connect("saxo_migrator", MARKET_DB, application_name="sim_total_return_calendar") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT MAX(session_date)
                FROM catalog.session_interval
                WHERE session_calendar_id='XNYS_US_EQUITY'
                  AND interval_sequence=0
                  AND session_status IN ('OPEN','SHORT_SESSION')
                  AND close_time_utc <= clock_timestamp()
                """
            )
            latest = cursor.fetchone()[0]
            if latest is None:
                raise TotalReturnUpdateError("XNYS_LATEST_SESSION_UNAVAILABLE")
            cursor.execute(
                """
                SELECT session_date
                FROM catalog.session_interval
                WHERE session_calendar_id='XNYS_US_EQUITY'
                  AND interval_sequence=0
                  AND session_status IN ('OPEN','SHORT_SESSION')
                  AND session_date > %s AND session_date <= %s
                ORDER BY session_date
                """,
                (refresh_start, latest),
            )
            expected = tuple(row[0] for row in cursor.fetchall())
    return latest, expected


def _dataset_id(now: datetime) -> str:
    return f"SIMTR_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"


def _write_exclusive(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()


def _manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _publish_dataset(
    dataset_id: str,
    manifest_relative_path: str,
    manifest_sha256: str,
    manifest: Mapping[str, Any],
    fetched: Sequence[FetchedTicker],
) -> int:
    rows_by_ticker = {item.ticker: item.rows for item in fetched}
    raw_info = manifest["raw_files"]
    total_rows = sum(len(item.rows) for item in fetched)
    with connect("saxo_migrator", MARKET_DB, application_name="sim_total_return_publish") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('sim_total_return_publish'))")
                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run (
                        trigger, environment, status, requested_series,
                        run_manifest_relative_path, last_success_step, metadata_json
                    ) VALUES (
                        'sim_research_total_return_one_shot','SIM','RUNNING',%s,%s,
                        'RAW_AND_QUALITY_VALIDATED',%s
                    ) RETURNING ingestion_run_id
                    """,
                    (
                        Jsonb(list(REQUIRED_TICKERS)),
                        manifest_relative_path,
                        Jsonb({"research_eligibility": RESEARCH_ELIGIBILITY}),
                    ),
                )
                run_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    UPDATE catalog.source_dataset
                    SET metadata_json=jsonb_set(metadata_json,'{current}','false'::jsonb,true)
                    WHERE dataset_kind='total_return'
                      AND research_eligibility=%s
                      AND COALESCE((metadata_json->>'current')::boolean,false)
                    """,
                    (RESEARCH_ELIGIBILITY,),
                )
                cursor.execute(
                    """
                    INSERT INTO catalog.source_dataset (
                        source_dataset_id,dataset_name,provider,environment,dataset_kind,
                        price_basis,canonical_horizon_minutes,
                        expected_update_interval_seconds,freshness_grace_seconds,
                        authoritative_layer,research_eligibility,active,
                        source_manifest_relative_path,source_manifest_sha256,metadata_json
                    ) VALUES (%s,%s,%s,'SIM','total_return','etf_total_return',1440,
                              86400,172800,'curated',%s,TRUE,%s,%s,%s)
                    """,
                    (
                        dataset_id,
                        "SIM research ETF total-return daily current",
                        PROVIDER,
                        RESEARCH_ELIGIBILITY,
                        manifest_relative_path,
                        manifest_sha256,
                        Jsonb(
                            {
                                "current": True,
                                "quality_status": "PASS",
                                "parity_status": "PASS",
                                "lineage": manifest["lineage"],
                                "latest_session_date": manifest["latest_session_date"],
                            }
                        ),
                    ),
                )
                source_file_ids: dict[str, int] = {}
                for item in fetched:
                    info = raw_info[item.ticker]
                    cursor.execute(
                        """
                        INSERT INTO ops.source_file (
                            ingestion_run_id,relative_path,sha256,size_bytes,row_count,
                            source_dataset_id
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        RETURNING source_file_id
                        """,
                        (
                            run_id,
                            info["relative_path"],
                            info["sha256"],
                            info["size_bytes"],
                            info["row_count"],
                            dataset_id,
                        ),
                    )
                    source_file_ids[item.ticker] = int(cursor.fetchone()[0])

                columns = """
                    source_dataset_id,ticker,date,currency,open_unadjusted,
                    high_unadjusted,low_unadjusted,close_unadjusted,adjusted_close,
                    total_return_index,volume,dividend_cash,split_factor,source,
                    quality_status,source_file_id
                """
                with cursor.copy(
                    f"COPY curated.etf_total_return_daily ({columns}) FROM STDIN"
                ) as copy:
                    for ticker in REQUIRED_TICKERS:
                        for row in rows_by_ticker[ticker]:
                            copy.write_row(
                                (
                                    dataset_id, ticker, row["date"], row["currency"],
                                    row["open_unadjusted"], row["high_unadjusted"],
                                    row["low_unadjusted"], row["close_unadjusted"],
                                    row["adjusted_close"], row["total_return_index"],
                                    row["volume"], row["dividend_cash"],
                                    row["split_factor"], row["source"], "PASS",
                                    source_file_ids[ticker],
                                )
                            )
                cursor.execute(
                    """
                    INSERT INTO catalog.series_instrument_mapping (
                        source_dataset_id,external_series_key,instrument_id,mapping_kind,
                        mapping_reason,approved_at_utc,approved_by,active
                    )
                    SELECT %s,v.external_series_key,i.instrument_id,'TICKER_EXACT',
                           'User-authorized one-month SIM research ticker mapping',
                           clock_timestamp(),'saxo-db-sim-research-total-return-v1',TRUE
                    FROM (VALUES
                        ('SPY','spy'),('IWM','iwm'),('EFA','efa'),('EEM','eem'),('VNQ','vnq')
                    ) AS v(external_series_key,market_key)
                    JOIN catalog.instrument i
                      ON i.market_key=v.market_key AND i.active_to_utc IS NULL
                    """,
                    (dataset_id,),
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM catalog.series_instrument_mapping
                    WHERE source_dataset_id=%s AND active
                    """,
                    (dataset_id,),
                )
                if int(cursor.fetchone()[0]) != len(REQUIRED_TICKERS):
                    raise TotalReturnUpdateError("TOTAL_RETURN_MAPPING_INCOMPLETE")
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run
                    SET finished_at_utc=clock_timestamp(),status='PASS',
                        successful_series=%s,inserted_rows=%s,
                        source_manifest_sha256=%s,last_success_step='READ_API_PUBLISHED',
                        metadata_json=metadata_json || %s
                    WHERE ingestion_run_id=%s
                    """,
                    (
                        len(REQUIRED_TICKERS), total_rows, manifest_sha256,
                        Jsonb({"source_dataset_id": dataset_id, "quality_status": "PASS"}),
                        run_id,
                    ),
                )
    return run_id


def run_once(*, now: datetime | None = None) -> dict[str, Any]:
    selected_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest_expected, expected_sessions = _latest_completed_session()
    period2_date = latest_expected + timedelta(days=1)
    period2 = int(
        datetime.combine(period2_date, datetime_time.min, tzinfo=timezone.utc).timestamp()
    )
    fetched = tuple(
        fetch_yahoo_ticker(ticker, period2=period2) for ticker in REQUIRED_TICKERS
    )
    rows = [row for item in fetched for row in item.rows]
    quality = evaluate_sim_research_quality(rows, expected_sessions)
    if quality["status"] != "PASS":
        return {
            "status": "FAILED",
            "error_domain": "data_quality",
            "error_code": "TOTAL_RETURN_QUALITY_FAILED",
            "quality_status": "FAIL",
            "publish_current_dataset": False,
            "quality": quality,
        }
    if any(value != latest_expected.isoformat() for value in quality["latest_session_date"].values()):
        return {
            "status": "FAILED",
            "error_domain": "data_quality",
            "error_code": "TOTAL_RETURN_LATEST_SESSION_MISMATCH",
            "quality_status": "FAIL",
            "publish_current_dataset": False,
            "quality": quality,
        }

    dataset_id = _dataset_id(selected_now)
    base_relative = Path("data/acquisition/runs") / dataset_id / "total_return"
    raw_files: dict[str, dict[str, Any]] = {}
    for item in fetched:
        relative_path = base_relative / "raw" / f"{item.ticker.lower()}.json"
        _write_exclusive(project_root() / relative_path, item.raw_bytes)
        raw_files[item.ticker] = {
            "relative_path": relative_path.as_posix(),
            "sha256": item.raw_sha256,
            "size_bytes": len(item.raw_bytes),
            "row_count": len(item.rows),
        }
    manifest_relative_path = (base_relative / "dataset_manifest.json").as_posix()
    manifest = {
        "schema_version": 1,
        "source_dataset_id": dataset_id,
        "dataset_kind": "total_return",
        "price_basis": "etf_total_return",
        "provider": PROVIDER,
        "environment": "SIM",
        "research_eligibility": RESEARCH_ELIGIBILITY,
        "current": True,
        "retrieved_at_utc": selected_now.isoformat().replace("+00:00", "Z"),
        "latest_expected_session_date": latest_expected.isoformat(),
        "latest_session_date": quality["latest_session_date"],
        "quality_status": "PASS",
        "quality": {
            "missing_count": quality["missing_count"],
            "duplicate_count": quality["duplicate_count"],
            "null_or_nonpositive_count": quality["null_or_nonpositive_count"],
            "ordered_content_sha256": quality["ordered_content_sha256"],
        },
        "lineage": {
            "raw": "Yahoo Finance chart JSON response retained byte-for-byte",
            "normalized": "daily quote arrays joined with dividends and splits by exchange session date",
            "curated": "adjusted close normalized to 100 at each ticker inception",
            "publication": "atomic catalog, source-file, curated-row, and approved-mapping transaction",
        },
        "parity_status": "PASS",
        "raw_files": raw_files,
    }
    body = _manifest_bytes(manifest)
    manifest_sha256 = hashlib.sha256(body).hexdigest()
    _write_exclusive(project_root() / manifest_relative_path, body)
    run_id = _publish_dataset(
        dataset_id, manifest_relative_path, manifest_sha256, manifest, fetched
    )
    return {
        "status": "PASS",
        "source_dataset_id": dataset_id,
        "database_ingestion_run_id": run_id,
        "quality_status": "PASS",
        "latest_session_date": quality["latest_session_date"],
        "manifest_relative_path": manifest_relative_path,
        "manifest_sha256": manifest_sha256,
        "row_count": len(rows),
        "publish_current_dataset": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Acquire and publish the one-month SIM research total-return dataset"
    )
    parser.add_argument("operation", choices=("run",))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(list(argv) if argv is not None else None)
    gate = provider_gate()
    if gate["status"] != "READY_SIM_RESEARCH_ONLY":
        print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
        return 3
    try:
        result = run_once()
    except TotalReturnUpdateError as exc:
        result = {
            **classify_provider_error(exc.http_status),
            "error_code": exc.code,
        }
    except Exception as exc:
        result = {
            **classify_provider_error(None),
            "error_code": f"TOTAL_RETURN_{type(exc).__name__.upper()}",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
