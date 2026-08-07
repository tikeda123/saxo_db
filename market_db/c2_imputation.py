"""Bounded, auditable C2 ETF hourly-gap imputation planning.

The planner never mutates a Saxo/accepted bar.  It produces a separate C2
overlay proposal whose synthetic OHLC is the previous *actual* close and whose
provenance is sufficient to audit that choice.  Persistence is deliberately a
separate, explicitly migrated concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .normalize_bars import NormalizedBar


C2_IMPUTATION_POLICY_ID = "c2_etf_bounded_previous_valid_v1"
C2_IMPUTATION_SOURCE = "IMPUTED_PREVIOUS_VALID"
C2_CONFIRMED_REVIEW_ID = "c2_gld_tip_live_confirmed_gap_20260807"
C2_ETF_KEYS = frozenset(
    {"spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld"}
)
MAX_CONSECUTIVE_MISSING = 2
MAX_MISSING_PER_SESSION = 2


@dataclass(frozen=True)
class C2ConfirmedImputationScope:
    session_date: date
    missing_times_utc: tuple[datetime, ...]
    source_time_utc: datetime
    data_version: int


C2_CONFIRMED_IMPUTATION_SCOPE = {
    "tip": C2ConfirmedImputationScope(
        session_date=date(2026, 7, 29),
        missing_times_utc=(
            datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc),
        ),
        source_time_utc=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
        data_version=29759068,
    ),
    "gld": C2ConfirmedImputationScope(
        session_date=date(2026, 7, 29),
        missing_times_utc=(
            datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc),
        ),
        source_time_utc=datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc),
        data_version=29749768,
    ),
}


@dataclass(frozen=True)
class C2ImputedBar:
    instrument_key: str
    session_date: date
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: None
    source_kind: str
    reason: str
    source_time_utc: datetime
    consecutive_gap_index: int
    consecutive_gap_count: int
    candidate_data_version: int
    source_data_version: int
    source_payload_sha256: str
    source_artifact_relative_path: str
    policy_id: str = C2_IMPUTATION_POLICY_ID
    quality_status: str = "WARN"
    official_close_claim: bool = False
    total_return_claim: bool = False
    execution_price_claim: bool = False


@dataclass(frozen=True)
class C2ImputationPlan:
    instrument_key: str
    session_date: date
    status: str
    warning_ids: tuple[str, ...]
    blocker_ids: tuple[str, ...]
    expected_slot_count: int
    actual_slot_count: int
    imputed_rows: tuple[C2ImputedBar, ...]
    actual_terminal_close_required: bool = True


def _source_provenance_valid(bar: NormalizedBar) -> bool:
    path = bar.artifact_relative_path
    return bool(
        len(bar.payload_sha256) == 64
        and all(character in "0123456789abcdef" for character in bar.payload_sha256)
        and path
        and not path.startswith("/")
        and ".." not in path.split("/")
    )


def _valid_actual(bar: NormalizedBar) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close)
    return bool(
        bar.is_complete
        and all(value.is_finite() and value > 0 for value in values)
        and bar.high >= max(bar.open, bar.low, bar.close)
        and bar.low <= min(bar.open, bar.high, bar.close)
    )


def _blocked(
    instrument_key: str,
    session_date: date,
    code: str,
    expected: int,
    actual: int,
) -> C2ImputationPlan:
    return C2ImputationPlan(
        instrument_key=instrument_key,
        session_date=session_date,
        status=code,
        warning_ids=(),
        blocker_ids=(code,),
        expected_slot_count=expected,
        actual_slot_count=actual,
        imputed_rows=(),
    )


def plan_c2_session_imputation(
    *,
    instrument_key: str,
    session_date: date,
    expected_times_utc: Iterable[datetime],
    actual_bars: Iterable[NormalizedBar],
    calendar_verified: bool,
    previous_session_terminal_bar: NormalizedBar | None = None,
    previous_session_terminal_time_utc: datetime | None = None,
) -> C2ImputationPlan:
    """Plan a maximum-two-row C2-only overlay for one verified ETF session.

    A missing run must be bounded on the right by an actual row.  A run that
    starts at the session open additionally requires the previous verified
    session's *actual* terminal row.  The session terminal row itself may never
    be imputed because it supplies the managed daily close.
    """

    key = instrument_key.strip().lower()
    expected = tuple(sorted(expected_times_utc))
    bars = tuple(actual_bars)
    if key not in C2_ETF_KEYS:
        return _blocked(key, session_date, "BLOCKED_IMPUTATION_SCOPE", len(expected), 0)
    if not calendar_verified:
        return _blocked(key, session_date, "BLOCKED_CALENDAR_NOT_VERIFIED", len(expected), 0)
    if not expected or any(value.tzinfo is None for value in expected):
        return _blocked(key, session_date, "BLOCKED_EXPECTED_SESSION_INVALID", len(expected), 0)
    expected = tuple(value.astimezone(timezone.utc) for value in expected)
    if len(set(expected)) != len(expected):
        return _blocked(key, session_date, "BLOCKED_EXPECTED_SESSION_DUPLICATE", len(expected), 0)

    by_time: dict[datetime, NormalizedBar] = {}
    for bar in bars:
        if bar.time_utc.tzinfo is None:
            return _blocked(key, session_date, "BLOCKED_ACTUAL_TIME_INVALID", len(expected), len(by_time))
        time_utc = bar.time_utc.astimezone(timezone.utc)
        if time_utc in by_time:
            return _blocked(key, session_date, "BLOCKED_ACTUAL_DUPLICATE", len(expected), len(by_time))
        if time_utc in expected:
            by_time[time_utc] = bar

    if any(not _valid_actual(bar) for bar in by_time.values()):
        return _blocked(key, session_date, "BLOCKED_ACTUAL_QUALITY", len(expected), len(by_time))
    if expected[-1] not in by_time:
        return _blocked(
            key, session_date, "BLOCKED_DAILY_CLOSE_SOURCE_MISSING", len(expected), len(by_time)
        )

    missing_indexes = [index for index, value in enumerate(expected) if value not in by_time]
    if not missing_indexes:
        return C2ImputationPlan(
            instrument_key=key,
            session_date=session_date,
            status="PASS_NO_IMPUTATION",
            warning_ids=(),
            blocker_ids=(),
            expected_slot_count=len(expected),
            actual_slot_count=len(by_time),
            imputed_rows=(),
        )
    if len(missing_indexes) > MAX_MISSING_PER_SESSION:
        return _blocked(key, session_date, "BLOCKED_MISSING_PER_SESSION_LIMIT", len(expected), len(by_time))

    runs: list[list[int]] = []
    for index in missing_indexes:
        if not runs or index != runs[-1][-1] + 1:
            runs.append([index])
        else:
            runs[-1].append(index)
    if any(len(run) > MAX_CONSECUTIVE_MISSING for run in runs):
        return _blocked(key, session_date, "BLOCKED_CONSECUTIVE_MISSING_LIMIT", len(expected), len(by_time))

    confirmed_scope = C2_CONFIRMED_IMPUTATION_SCOPE.get(key)
    missing_times = tuple(expected[index] for index in missing_indexes)
    if (
        confirmed_scope is None
        or session_date != confirmed_scope.session_date
        or missing_times != confirmed_scope.missing_times_utc
    ):
        return _blocked(
            key,
            session_date,
            "BLOCKED_UNAPPROVED_IMPUTATION_SCOPE",
            len(expected),
            len(by_time),
        )

    candidate_versions = {bar.data_version for bar in by_time.values()}
    if None in candidate_versions or len(candidate_versions) != 1:
        return _blocked(key, session_date, "BLOCKED_DATA_VERSION_IDENTITY", len(expected), len(by_time))
    candidate_version = int(next(iter(candidate_versions)))
    if candidate_version != confirmed_scope.data_version:
        return _blocked(
            key,
            session_date,
            "BLOCKED_UNAPPROVED_IMPUTATION_DATA_VERSION",
            len(expected),
            len(by_time),
        )

    imputed: list[C2ImputedBar] = []
    for run in runs:
        right_index = run[-1] + 1
        if right_index >= len(expected) or expected[right_index] not in by_time:
            return _blocked(key, session_date, "BLOCKED_RIGHT_ACTUAL_ANCHOR_MISSING", len(expected), len(by_time))
        right_anchor = by_time[expected[right_index]]
        if right_anchor.data_version != candidate_version:
            return _blocked(key, session_date, "BLOCKED_DATA_VERSION_IDENTITY", len(expected), len(by_time))

        if run[0] == 0:
            left_anchor = previous_session_terminal_bar
            reason = "PROVIDER_SESSION_OPEN_ROWS_MISSING"
            expected_previous = (
                None
                if previous_session_terminal_time_utc is None
                or previous_session_terminal_time_utc.tzinfo is None
                else previous_session_terminal_time_utc.astimezone(timezone.utc)
            )
            if (
                left_anchor is None
                or not _valid_actual(left_anchor)
                or expected_previous is None
                or left_anchor.time_utc.astimezone(timezone.utc) != expected_previous
            ):
                return _blocked(
                    key,
                    session_date,
                    "BLOCKED_SESSION_START_WITHOUT_PREVIOUS_ACTUAL",
                    len(expected),
                    len(by_time),
                )
        else:
            left_anchor = by_time.get(expected[run[0] - 1])
            reason = "PROVIDER_INTERNAL_SESSION_ROWS_MISSING"
            if left_anchor is None or not _valid_actual(left_anchor):
                return _blocked(key, session_date, "BLOCKED_LEFT_ACTUAL_ANCHOR_MISSING", len(expected), len(by_time))
        if left_anchor.data_version != candidate_version:
            return _blocked(key, session_date, "BLOCKED_DATA_VERSION_IDENTITY", len(expected), len(by_time))
        if left_anchor.time_utc.astimezone(timezone.utc) != confirmed_scope.source_time_utc:
            return _blocked(
                key,
                session_date,
                "BLOCKED_UNAPPROVED_IMPUTATION_SOURCE",
                len(expected),
                len(by_time),
            )
        if not _source_provenance_valid(left_anchor):
            return _blocked(key, session_date, "BLOCKED_SOURCE_LINEAGE_MISSING", len(expected), len(by_time))

        for gap_index, expected_index in enumerate(run, start=1):
            value = left_anchor.close
            imputed.append(
                C2ImputedBar(
                    instrument_key=key,
                    session_date=session_date,
                    time_utc=expected[expected_index],
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=None,
                    source_kind=C2_IMPUTATION_SOURCE,
                    reason=reason,
                    source_time_utc=left_anchor.time_utc.astimezone(timezone.utc),
                    consecutive_gap_index=gap_index,
                    consecutive_gap_count=len(run),
                    candidate_data_version=candidate_version,
                    source_data_version=int(left_anchor.data_version),
                    source_payload_sha256=left_anchor.payload_sha256,
                    source_artifact_relative_path=left_anchor.artifact_relative_path,
                )
            )

    return C2ImputationPlan(
        instrument_key=key,
        session_date=session_date,
        status="PASS_WITH_IMPUTATION_WARNING",
        warning_ids=("C2_BOUNDED_IMPUTED_PREVIOUS_VALID",),
        blocker_ids=(),
        expected_slot_count=len(expected),
        actual_slot_count=len(by_time),
        imputed_rows=tuple(imputed),
    )


def persist_c2_imputation_plan(
    cursor: Any,
    *,
    instrument_id: int,
    session_calendar_id: str,
    review_id: str,
    plan: C2ImputationPlan,
    source_ingestion_run_ids: Mapping[datetime, int],
) -> int:
    """Append an approved plan to the overlay table without touching source rows."""

    if plan.status != "PASS_WITH_IMPUTATION_WARNING":
        return 0
    if review_id != C2_CONFIRMED_REVIEW_ID:
        raise ValueError("BLOCKED_UNAPPROVED_IMPUTATION_REVIEW")
    confirmed_scope = C2_CONFIRMED_IMPUTATION_SCOPE.get(plan.instrument_key)
    if confirmed_scope is None or tuple(row.time_utc for row in plan.imputed_rows) != confirmed_scope.missing_times_utc:
        raise ValueError("BLOCKED_UNAPPROVED_IMPUTATION_SCOPE")
    inserted = 0
    for row in plan.imputed_rows:
        source_run_id = source_ingestion_run_ids.get(row.source_time_utc)
        cursor.execute(
            """
            INSERT INTO derived.c2_market_bar_1h_imputation (
                policy_id,instrument_id,session_calendar_id,session_date,time_utc,
                horizon_minutes,price_basis,open,high,low,close,volume,
                source_kind,reason,source_time_utc,consecutive_gap_index,
                consecutive_gap_count,candidate_data_version,source_data_version,
                source_ingestion_run_id,source_payload_sha256,
                source_artifact_relative_path,quality_status,
                official_close_claim,total_return_claim,execution_price_claim,review_id
            ) VALUES (
                %s,%s,%s,%s,%s,60,'native_ohlc',%s,%s,%s,%s,NULL,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE,FALSE,%s
            )
            ON CONFLICT (policy_id,instrument_id,time_utc,candidate_data_version)
            DO NOTHING
            """,
            (
                row.policy_id,
                instrument_id,
                session_calendar_id,
                row.session_date,
                row.time_utc,
                row.open,
                row.high,
                row.low,
                row.close,
                row.source_kind,
                row.reason,
                row.source_time_utc,
                row.consecutive_gap_index,
                row.consecutive_gap_count,
                row.candidate_data_version,
                row.source_data_version,
                source_run_id,
                row.source_payload_sha256,
                row.source_artifact_relative_path,
                row.quality_status,
                review_id,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def _normalized_from_row(row: tuple[Any, ...]) -> tuple[NormalizedBar, int]:
    return (
        NormalizedBar(
            time_utc=row[0],
            open=row[1], high=row[2], low=row[3], close=row[4],
            open_bid=None, high_bid=None, low_bid=None, close_bid=None,
            open_ask=None, high_ask=None, low_ask=None, close_ask=None,
            volume=row[5], market_trading_state=row[6],
            price_basis="native_ohlc", is_complete=bool(row[7]),
            data_version=row[8], delayed_by_minutes=None,
            retrieved_at_utc=row[9], payload_sha256=str(row[11] or ""),
            artifact_relative_path=str(row[12] or ""),
        ),
        int(row[10]),
    )


def refresh_c2_imputation_overlay(
    cursor: Any,
    *,
    instrument_ids: Iterable[int],
    lookback_sessions: int = 3,
) -> dict[str, Any]:
    """Append eligible recent-session overlays inside the caller transaction.

    The function is a no-op until migration 0036 is applied.  A blocked plan is
    returned per instrument/session and never blocks or alters another series.
    """

    selected_ids = sorted({int(value) for value in instrument_ids})
    if not selected_ids:
        return {"status": "PASS", "inserted_rows": 0, "plans": []}
    cursor.execute("SELECT to_regclass('derived.c2_market_bar_1h_imputation')")
    if cursor.fetchone()[0] is None:
        return {
            "status": "NOT_APPLIED_SCHEMA",
            "required_migration": "0036_c2_bounded_imputation_overlay.sql",
            "inserted_rows": 0,
            "plans": [],
        }

    plan_results: list[dict[str, Any]] = []
    inserted_rows = 0
    for instrument_id in selected_ids:
        cursor.execute(
            """
            SELECT lower(i.market_key),i.asset_type,i.session_calendar_id,
                   c.metadata_json->>'verification_status'
            FROM catalog.instrument i
            LEFT JOIN catalog.session_calendar c USING (session_calendar_id)
            WHERE i.instrument_id=%s
            """,
            (instrument_id,),
        )
        identity = cursor.fetchone()
        if identity is None or str(identity[0]) not in C2_ETF_KEYS or str(identity[1]) != "Etf":
            continue
        key, _asset_type, calendar_id, verification = identity
        cursor.execute(
            """
            SELECT session_date,open_time_utc,close_time_utc
            FROM catalog.session_interval
            WHERE session_calendar_id=%s AND session_status <> 'HOLIDAY'
              AND close_time_utc <= clock_timestamp()
            ORDER BY session_date DESC,interval_sequence DESC
            LIMIT %s
            """,
            (calendar_id, lookback_sessions),
        )
        sessions = list(cursor.fetchall())
        for selected_date, session_open, session_close in reversed(sessions):
            cursor.execute(
                """
                SELECT open_time_utc,close_time_utc
                FROM catalog.session_interval
                WHERE session_calendar_id=%s AND session_status <> 'HOLIDAY'
                  AND close_time_utc <= %s
                ORDER BY close_time_utc DESC LIMIT 1
                """,
                (calendar_id, session_open),
            )
            previous_session = cursor.fetchone()
            previous_terminal_time = None
            if previous_session is not None:
                previous_open, previous_close = previous_session
                previous_duration = (previous_close - previous_open).total_seconds()
                previous_terminal_time = previous_open + timedelta(
                    hours=int((previous_duration - 1) // 3600)
                )
            cursor.execute(
                """
                SELECT b.time_utc,b.open,b.high,b.low,b.close,b.volume,
                       b.market_trading_state,b.is_complete,b.data_version,
                       b.retrieved_at_utc,b.latest_ingestion_run_id,
                       r.payload_sha256,s.relative_path
                FROM curated.market_bar b
                LEFT JOIN raw.market_bar_revision r
                  ON r.ingestion_run_id=b.latest_ingestion_run_id
                 AND r.instrument_id=b.instrument_id
                 AND r.horizon_minutes=b.horizon_minutes
                 AND r.time_utc=b.time_utc AND r.price_basis=b.price_basis
                LEFT JOIN ops.source_file s ON s.source_file_id=r.source_file_id
                WHERE b.instrument_id=%s AND b.horizon_minutes=60
                  AND b.price_basis='native_ohlc'
                  AND b.time_utc >= %s AND b.time_utc < %s
                ORDER BY b.time_utc
                """,
                (instrument_id, session_open, session_close),
            )
            actual_pairs = [_normalized_from_row(tuple(row)) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT b.time_utc,b.open,b.high,b.low,b.close,b.volume,
                       b.market_trading_state,b.is_complete,b.data_version,
                       b.retrieved_at_utc,b.latest_ingestion_run_id,
                       r.payload_sha256,s.relative_path
                FROM curated.market_bar b
                LEFT JOIN raw.market_bar_revision r
                  ON r.ingestion_run_id=b.latest_ingestion_run_id
                 AND r.instrument_id=b.instrument_id
                 AND r.horizon_minutes=b.horizon_minutes
                 AND r.time_utc=b.time_utc AND r.price_basis=b.price_basis
                LEFT JOIN ops.source_file s ON s.source_file_id=r.source_file_id
                WHERE b.instrument_id=%s AND b.horizon_minutes=60
                  AND b.price_basis='native_ohlc' AND b.time_utc=%s
                  AND b.is_complete AND b.quality_status='PASS'
                LIMIT 1
                """,
                (instrument_id, previous_terminal_time),
            )
            previous_row = cursor.fetchone()
            previous_pair = None if previous_row is None else _normalized_from_row(tuple(previous_row))
            expected: list[datetime] = []
            slot = session_open
            while slot < session_close:
                expected.append(slot)
                slot += timedelta(hours=1)
            plan = plan_c2_session_imputation(
                instrument_key=str(key),
                session_date=selected_date,
                expected_times_utc=expected,
                actual_bars=(pair[0] for pair in actual_pairs),
                calendar_verified=str(verification) == "VERIFIED",
                previous_session_terminal_bar=(
                    None if previous_pair is None else previous_pair[0]
                ),
                previous_session_terminal_time_utc=previous_terminal_time,
            )
            source_runs = {pair[0].time_utc: pair[1] for pair in actual_pairs}
            if previous_pair is not None:
                source_runs[previous_pair[0].time_utc] = previous_pair[1]
            inserted_rows += persist_c2_imputation_plan(
                cursor,
                instrument_id=instrument_id,
                session_calendar_id=str(calendar_id),
                review_id=C2_CONFIRMED_REVIEW_ID,
                plan=plan,
                source_ingestion_run_ids=source_runs,
            )
            plan_results.append(
                {
                    "instrument_key": str(key),
                    "session_date": selected_date.isoformat(),
                    "status": plan.status,
                    "imputed_rows": len(plan.imputed_rows),
                    "warning_ids": list(plan.warning_ids),
                    "blocker_ids": list(plan.blocker_ids),
                }
            )
    return {
        "status": (
            "PASS_WITH_WARNINGS"
            if any(item["warning_ids"] for item in plan_results)
            else "PASS"
        ),
        "inserted_rows": inserted_rows,
        "plans": plan_results,
    }
