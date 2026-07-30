"""Deterministic session calendars used by DB3 coverage and derivation.

The US-equity calendar is rule based and includes exceptional closures.  The
reviewed FxSpot calendar freezes Saxo's published standard-FX daily maintenance
boundary in America/New_York; special-pair and holiday overrides remain outside
the supported instrument set and must be sourced from Saxo's schedule endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .connection import MARKET_DB, connect
from .instrument_registry import load_canonical_instruments, load_research_candidate_instruments


EQUITY_CALENDAR_ID = "XNYS_US_EQUITY"
FX_CALENDAR_ID = "SBFX_24X5"
CALENDAR_START = date(2002, 9, 25)
NY = ZoneInfo("America/New_York")
FX_SESSION_BOUNDARY = time(17, 0)
FX_SCHEDULE_SOURCE = "https://www.home.saxo/rates-and-conditions/forex/trading-conditions"


@dataclass(frozen=True)
class SessionInterval:
    session_date: date
    open_time_utc: datetime | None
    close_time_utc: datetime | None
    status: str


def _observed(fixed: date) -> date:
    if fixed.weekday() == 5:
        return fixed - timedelta(days=1)
    if fixed.weekday() == 6:
        return fixed + timedelta(days=1)
    return fixed


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    selected = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return selected - timedelta(days=(selected.weekday() - weekday) % 7)


def easter_sunday(year: int) -> date:
    """Return Gregorian Easter using the Meeus/Jones/Butcher algorithm."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def equity_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    exceptional = {
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
        date(2025, 1, 9),
    }
    return holidays | {value for value in exceptional if value.year == year}


def is_equity_holiday(value: date) -> bool:
    # New Year's Day can be observed on 31 December of the preceding year.
    return any(value in equity_holidays(year) for year in (value.year - 1, value.year, value.year + 1))


def _previous_weekday(value: date) -> date:
    selected = value - timedelta(days=1)
    while selected.weekday() >= 5:
        selected -= timedelta(days=1)
    return selected


def equity_short_sessions(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    candidates = {thanksgiving + timedelta(days=1)}
    candidates.add(_previous_weekday(_observed(date(year, 12, 25))))
    candidates.add(_previous_weekday(_observed(date(year, 7, 4))))
    return {value for value in candidates if value.weekday() < 5 and not is_equity_holiday(value)}


def generate_equity_sessions(start: date, end: date) -> list[SessionInterval]:
    sessions: list[SessionInterval] = []
    selected = start
    while selected <= end:
        if selected.weekday() >= 5:
            selected += timedelta(days=1)
            continue
        if is_equity_holiday(selected):
            sessions.append(SessionInterval(selected, None, None, "HOLIDAY"))
        else:
            short = selected in equity_short_sessions(selected.year)
            open_local = datetime.combine(selected, time(9, 30), NY)
            close_local = datetime.combine(selected, time(13 if short else 16, 0), NY)
            sessions.append(
                SessionInterval(
                    selected,
                    open_local.astimezone(timezone.utc),
                    close_local.astimezone(timezone.utc),
                    "SHORT_SESSION" if short else "OPEN",
                )
            )
        selected += timedelta(days=1)
    return sessions


def generate_fx_sessions(start: date, end: date) -> list[SessionInterval]:
    sessions: list[SessionInterval] = []
    selected = start
    while selected <= end:
        if selected.weekday() < 5:
            # Keep the canonical trading-day boundary stable at 17:00 New York
            # for existing derived buckets.  The 16:59-17:04 maintenance gap is
            # excluded by the complete-1H slot rule, not by shifting this base
            # interval and silently changing 4H/1D derivations.
            open_local = datetime.combine(
                selected - timedelta(days=1), FX_SESSION_BOUNDARY, NY
            )
            close_local = datetime.combine(selected, FX_SESSION_BOUNDARY, NY)
            sessions.append(
                SessionInterval(
                    selected,
                    open_local.astimezone(timezone.utc),
                    close_local.astimezone(timezone.utc),
                    "OPEN",
                )
            )
        selected += timedelta(days=1)
    return sessions


def _calendar_sha(calendar_id: str, sessions: list[SessionInterval]) -> str:
    payload = [
        {
            "calendar": calendar_id,
            "date": item.session_date.isoformat(),
            "open": None if item.open_time_utc is None else item.open_time_utc.isoformat(),
            "close": None if item.close_time_utc is None else item.close_time_utc.isoformat(),
            "status": item.status,
        }
        for item in sessions
    ]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply_calendars(*, start: date = CALENDAR_START, end: date | None = None) -> dict[str, Any]:
    selected_end = end or date(datetime.now(timezone.utc).year + 1, 12, 31)
    if selected_end < start:
        raise ValueError("calendar end must not precede start")
    equity = generate_equity_sessions(start, selected_end)
    fx = generate_fx_sessions(start, selected_end)
    registry = (*load_canonical_instruments(), *load_research_candidate_instruments())

    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_calendar_apply") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                calendars = (
                    (
                        EQUITY_CALENDAR_ID,
                        "XNYS",
                        "Etf",
                        "America/New_York",
                        "db3_nyse_rules_v1",
                        equity,
                        {
                            "verification_status": "VERIFIED",
                            "source": "deterministic_nyse_rules_v1",
                            "exceptional_closures_included": True,
                        },
                    ),
                    (
                        FX_CALENDAR_ID,
                        None,
                        "FxSpot",
                        "America/New_York",
                        "saxo_fx_spot_trading_conditions_20260725_v1",
                        fx,
                        {
                            "verification_status": "VERIFIED",
                            "source": FX_SCHEDULE_SOURCE,
                            "supported_pairs": [
                                "EURUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"
                            ],
                            "daily_maintenance_new_york": "16:59-17:04",
                            "weekend_rule": "conservative Monday-Friday session days",
                            "dst_rule": "America/New_York zoneinfo",
                            "schedule_endpoint": "/ref/v1/instruments/tradingschedule/{Uic}/{AssetType}",
                        },
                    ),
                )
                for calendar_id, exchange_id, asset_type, timezone_name, version, sessions, metadata in calendars:
                    digest = _calendar_sha(calendar_id, sessions)
                    cursor.execute(
                        """
                        INSERT INTO catalog.session_calendar (
                            session_calendar_id, provider, exchange_id, asset_type,
                            timezone_name, schedule_version, effective_from, effective_to,
                            metadata_json
                        ) VALUES (%s,'Saxo OpenAPI',%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (session_calendar_id) DO UPDATE SET
                            exchange_id=EXCLUDED.exchange_id,
                            asset_type=EXCLUDED.asset_type,
                            timezone_name=EXCLUDED.timezone_name,
                            schedule_version=EXCLUDED.schedule_version,
                            effective_from=EXCLUDED.effective_from,
                            effective_to=EXCLUDED.effective_to,
                            metadata_json=EXCLUDED.metadata_json
                        """,
                        (
                            calendar_id,
                            exchange_id,
                            asset_type,
                            timezone_name,
                            version,
                            start,
                            selected_end,
                            Jsonb(metadata | {"source_sha256": digest}),
                        ),
                    )
                    cursor.execute(
                        "DELETE FROM catalog.session_interval WHERE session_calendar_id=%s",
                        (calendar_id,),
                    )
                    cursor.executemany(
                        """
                        INSERT INTO catalog.session_interval (
                            session_calendar_id, session_date, interval_sequence,
                            open_time_utc, close_time_utc, session_status, source_sha256
                        ) VALUES (%s,%s,0,%s,%s,%s,%s)
                        """,
                        [
                            (
                                calendar_id,
                                item.session_date,
                                item.open_time_utc,
                                item.close_time_utc,
                                item.status,
                                digest,
                            )
                            for item in sessions
                        ],
                    )

                assignments = 0
                for instrument in registry:
                    calendar_id = FX_CALENDAR_ID if instrument.asset_type == "FxSpot" else EQUITY_CALENDAR_ID
                    cursor.execute(
                        """
                        UPDATE catalog.instrument
                        SET session_calendar_id=%s
                        WHERE provider='Saxo OpenAPI' AND environment='SIM'
                          AND uic=%s AND asset_type=%s
                        """,
                        (calendar_id, instrument.uic, instrument.asset_type),
                    )
                    assignments += cursor.rowcount
    return {
        "calendar_end": selected_end.isoformat(),
        "calendar_start": start.isoformat(),
        "equity_intervals": len(equity),
        "fx_intervals": len(fx),
        "instrument_assignments": assignments,
        "status": "PASS" if assignments == len(registry) else "FAIL",
    }


def calendar_status() -> dict[str, Any]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_calendar_status") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT c.session_calendar_id, c.schedule_version,
                       c.metadata_json->>'verification_status',
                       COUNT(DISTINCT (si.session_date, si.interval_sequence)),
                       COUNT(DISTINCT i.instrument_id)
                FROM catalog.session_calendar c
                LEFT JOIN catalog.session_interval si USING (session_calendar_id)
                LEFT JOIN catalog.instrument i USING (session_calendar_id)
                WHERE c.session_calendar_id = ANY(%s)
                GROUP BY c.session_calendar_id, c.schedule_version, c.metadata_json
                ORDER BY c.session_calendar_id
                """,
                ([EQUITY_CALENDAR_ID, FX_CALENDAR_ID],),
            )
            rows = cursor.fetchall()
    return {
        "calendars": [
            {
                "calendar_id": row[0],
                "schedule_version": row[1],
                "verification_status": row[2],
                "intervals": int(row[3]),
                "instruments": int(row[4]),
            }
            for row in rows
        ]
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage DB3 session calendars")
    parser.add_argument("command", choices=("apply", "status"))
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = apply_calendars() if args.command == "apply" else calendar_status()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
