"""Unattended, fail-closed scheduler for the S6V5A priority 1H series."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .connection import MARKET_DB, connect, project_root
from .incremental_update import (
    run_full_refetch,
    run_incremental,
)
from .saxo_auth import OAuthConfig, SaxoAuthError, SaxoOAuthManager
from .saxo_client import SaxoClient
from .session_calendar import SessionInterval, generate_fx_sessions
from .total_return_update import provider_gate


EQUITY_REIT_KEYS = ("spy", "iwm", "efa", "eem", "vnq")
BOND_CREDIT_KEYS = ("shy", "ief", "tlt", "tip", "lqd")
GOLD_KEYS = ("gld",)
ETF_KEYS = EQUITY_REIT_KEYS + BOND_CREDIT_KEYS + GOLD_KEYS
EQUITY_KEYS = EQUITY_REIT_KEYS
SCHEDULED_FX_KEYS = ("eurusd", "usdjpy")
FX_KEYS = SCHEDULED_FX_KEYS
FX_RESEARCH_CANDIDATE_KEYS = ("audusd", "usdcad", "usdchf")
FULL_SCOPE_PROFILE = "all_managed_series_v1"
USDJPY_QUARANTINE_SCOPE_PROFILE = "all_except_usdjpy_provider_quarantine_20260727"
CANDIDATE_READY_SCOPE_PROFILE = "all_except_usdjpy_with_fx_research_candidates_20260727"
ACTIVE_SCOPE_PROFILE = USDJPY_QUARANTINE_SCOPE_PROFILE
RUNTIME_RELATIVE_PATH = Path(".runtime/periodic_update")
STATE_FILENAME = "state.json"
LOCK_FILENAME = "service.lock"
POLL_SECONDS = 15.0
RETRY_SECONDS = 30.0
MAX_TRANSIENT_SLOT_ATTEMPTS = 4
MAX_CATCHUP_AGE = timedelta(hours=6)
COMPLETED_SLOT_HISTORY_LIMIT = 1024
FIRST_PUBLISH_OFFSET = timedelta(hours=1, seconds=15)
EQUITY_DEADLINE_OFFSET = timedelta(hours=1, minutes=3)
FX_PUBLISH_MINUTE = 3
FX_DEADLINE_MINUTE = 10
CANDIDATE_FX_PUBLISH_MINUTE = 6
CANDIDATE_FX_DEADLINE_MINUTE = 15
ETF_DAILY_CLOSE_PUBLISH_OFFSET = timedelta(minutes=45)
ETF_DAILY_CLOSE_DEADLINE_OFFSET = timedelta(minutes=90)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EquitySession:
    session_date: date
    open_time_utc: datetime
    close_time_utc: datetime
    session_status: str


@dataclass(frozen=True)
class ScheduleSlot:
    slot_id: str
    kind: str
    due_at_utc: datetime
    deadline_utc: datetime
    instrument_keys: tuple[str, ...]
    expected_latest_complete: Mapping[str, datetime]
    trigger: str
    expected_latest_session: Mapping[str, date] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "kind": self.kind,
            "due_at_utc": _utc_text(self.due_at_utc),
            "deadline_utc": _utc_text(self.deadline_utc),
            "instrument_keys": list(self.instrument_keys),
            "expected_latest_complete": {
                key: _utc_text(value) for key, value in self.expected_latest_complete.items()
            },
            "expected_latest_session": {
                key: value.isoformat() for key, value in self.expected_latest_session.items()
            },
        }


@dataclass(frozen=True)
class SchedulerScope:
    profile_id: str
    included_instrument_keys: tuple[str, ...]
    excluded_instrument_keys: tuple[str, ...]
    allowed_schedule_kinds: tuple[str, ...]
    reason: str
    release_condition: str
    config_relative_path: str = "specs/source_collection/periodic_scheduler_scope_v1.json"

    def public_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": "ACTIVE_TEMPORARY",
            "included_instrument_keys": list(self.included_instrument_keys),
            "excluded_instrument_keys": list(self.excluded_instrument_keys),
            "allowed_schedule_kinds": list(self.allowed_schedule_kinds),
            "reason": self.reason,
            "release_condition": self.release_condition,
            "config_relative_path": self.config_relative_path,
            "orders_or_prechecks_sent": 0,
        }


SCHEDULER_SCOPES = {
    FULL_SCOPE_PROFILE: SchedulerScope(
        profile_id=FULL_SCOPE_PROFILE,
        included_instrument_keys=ETF_KEYS + SCHEDULED_FX_KEYS,
        excluded_instrument_keys=(),
        allowed_schedule_kinds=(
            "equity_regular_1h",
            "bond_credit_regular_1h",
            "gold_regular_1h",
            "etf_daily_close",
            "fx_hourly",
        ),
        reason="full managed-series schedule",
        release_condition="not applicable",
    ),
    USDJPY_QUARANTINE_SCOPE_PROFILE: SchedulerScope(
        profile_id=USDJPY_QUARANTINE_SCOPE_PROFILE,
        included_instrument_keys=ETF_KEYS + ("eurusd",),
        excluded_instrument_keys=("usdjpy",),
        allowed_schedule_kinds=(
            "equity_regular_1h",
            "bond_credit_regular_1h",
            "gold_regular_1h",
            "etf_daily_close",
            "fx_hourly",
        ),
        reason="USDJPY provider DataVersion content-quality quarantine",
        release_condition=(
            "provider-corrected USDJPY DataVersion, guarded full-refetch PASS, "
            "and two normal PASS runs"
        ),
    ),
    CANDIDATE_READY_SCOPE_PROFILE: SchedulerScope(
        profile_id=CANDIDATE_READY_SCOPE_PROFILE,
        included_instrument_keys=ETF_KEYS + ("eurusd",) + FX_RESEARCH_CANDIDATE_KEYS,
        excluded_instrument_keys=("usdjpy",),
        allowed_schedule_kinds=(
            "equity_regular_1h",
            "bond_credit_regular_1h",
            "gold_regular_1h",
            "etf_daily_close",
            "fx_hourly",
            "fx_research_candidates_hourly",
        ),
        reason="USDJPY quarantined; reviewed FX research candidates passed two normal runs",
        release_condition="all three research candidates remain PUBLISHED with two normal PASS runs",
        config_relative_path=(
            "specs/source_collection/fx_research_candidate_scheduler_scope_v1.json"
        ),
    ),
}


def scheduler_scope(profile_id: str) -> SchedulerScope:
    try:
        return SCHEDULER_SCOPES[profile_id]
    except KeyError:
        raise ValueError("UNKNOWN_SCHEDULER_SCOPE_PROFILE") from None


def _apply_scheduler_scope(
    slots: Iterable[ScheduleSlot], profile_id: str
) -> tuple[ScheduleSlot, ...]:
    scope = scheduler_scope(profile_id)
    included = set(scope.included_instrument_keys)
    allowed_kinds = set(scope.allowed_schedule_kinds)
    selected: list[ScheduleSlot] = []
    for slot in slots:
        if slot.kind not in allowed_kinds:
            continue
        instrument_keys = tuple(key for key in slot.instrument_keys if key in included)
        if not instrument_keys:
            continue
        expected = {
            key: value for key, value in slot.expected_latest_complete.items()
            if key in included
        }
        expected_sessions = {
            key: value for key, value in slot.expected_latest_session.items()
            if key in included
        }
        trigger = slot.trigger
        if profile_id in {
            USDJPY_QUARANTINE_SCOPE_PROFILE,
            CANDIDATE_READY_SCOPE_PROFILE,
        }:
            trigger = f"{trigger}_usdjpy_quarantined"
        # Every scheduled lane is instrument-scoped.  A source revision in
        # one ETF must not suppress the remaining instruments in its category.
        for instrument_key in instrument_keys:
            selected.append(
                ScheduleSlot(
                    slot_id=f"{slot.slot_id}-{instrument_key}",
                    kind=slot.kind,
                    due_at_utc=slot.due_at_utc,
                    deadline_utc=slot.deadline_utc,
                    instrument_keys=(instrument_key,),
                    expected_latest_complete={instrument_key: expected[instrument_key]},
                    trigger=f"{trigger}_{instrument_key}",
                    expected_latest_session=(
                        {instrument_key: expected_sessions[instrument_key]}
                        if instrument_key in expected_sessions else {}
                    ),
                )
            )
    return tuple(selected)


def runtime_dir() -> Path:
    return project_root() / RUNTIME_RELATIVE_PATH


def state_path() -> Path:
    return runtime_dir() / STATE_FILENAME


def _ensure_runtime_dir() -> None:
    selected = runtime_dir()
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(selected, 0o700)


def load_state() -> dict[str, Any] | None:
    selected = state_path()
    if not selected.is_file() or selected.is_symlink():
        return None
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_state(payload: Mapping[str, Any]) -> None:
    _ensure_runtime_dir()
    selected = state_path()
    temporary = selected.with_name(f".{STATE_FILENAME}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selected)
        os.chmod(selected, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_equity_sessions(lower: date, upper: date) -> tuple[EquitySession, ...]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_periodic_schedule") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT session_date, open_time_utc, close_time_utc, session_status
                FROM catalog.session_interval
                WHERE session_calendar_id='XNYS_US_EQUITY'
                  AND session_date BETWEEN %s AND %s
                  AND interval_sequence=0
                ORDER BY session_date
                """,
                (lower, upper),
            )
            rows = cursor.fetchall()
    return tuple(EquitySession(row[0], row[1], row[2], str(row[3])) for row in rows)


def fully_contained_hour_starts(
    open_time_utc: datetime,
    close_time_utc: datetime,
    *,
    align_to_utc_hour: bool,
    opening_delay: timedelta = timedelta(0),
    closing_lead: timedelta = timedelta(0),
) -> tuple[datetime, ...]:
    """Return only 1H bars whose complete [start,end] lies in the session."""

    opening = open_time_utc.astimezone(timezone.utc) + opening_delay
    closing = close_time_utc.astimezone(timezone.utc) - closing_lead
    if align_to_utc_hour:
        current = opening.replace(minute=0, second=0, microsecond=0)
        if current < opening:
            current += timedelta(hours=1)
    else:
        current = opening
    values: list[datetime] = []
    while current + timedelta(hours=1) <= closing:
        values.append(current)
        current += timedelta(hours=1)
    return tuple(values)


def _default_fx_sessions(now_utc: datetime) -> tuple[SessionInterval, ...]:
    selected = now_utc.astimezone(timezone.utc)
    return tuple(
        generate_fx_sessions(
            (selected - timedelta(days=3)).date(),
            (selected + timedelta(days=4)).date(),
        )
    )


def build_schedule_slots(
    now_utc: datetime,
    equity_sessions: Iterable[EquitySession],
    fx_sessions: Iterable[SessionInterval] | None = None,
    *,
    scope_profile: str = FULL_SCOPE_PROFILE,
) -> tuple[ScheduleSlot, ...]:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    selected_now = now_utc.astimezone(timezone.utc)
    slots: list[ScheduleSlot] = []

    selected_fx_sessions = tuple(fx_sessions or _default_fx_sessions(selected_now))
    fx_bar_starts = sorted(
        {
            start
            for session in selected_fx_sessions
            if session.status in {"OPEN", "SHORT_SESSION"}
            and session.open_time_utc is not None
            and session.close_time_utc is not None
            for start in fully_contained_hour_starts(
                session.open_time_utc,
                session.close_time_utc,
                align_to_utc_hour=True,
                opening_delay=timedelta(minutes=5),
                closing_lead=timedelta(minutes=1),
            )
        }
    )
    schedule_lower = selected_now - timedelta(hours=7)
    schedule_upper = selected_now + timedelta(hours=4)
    for bar_start in fx_bar_starts:
        due = bar_start + timedelta(hours=1, minutes=FX_PUBLISH_MINUTE)
        if due < schedule_lower or due > schedule_upper:
            continue
        slots.append(
            ScheduleSlot(
                slot_id=f"fx-{due.strftime('%Y%m%dT%H%M%SZ')}",
                kind="fx_hourly",
                due_at_utc=due,
                deadline_utc=bar_start + timedelta(hours=1, minutes=FX_DEADLINE_MINUTE),
                instrument_keys=SCHEDULED_FX_KEYS,
                expected_latest_complete={key: bar_start for key in SCHEDULED_FX_KEYS},
                trigger="scheduled_fx_hourly",
            )
        )
        candidate_due = bar_start + timedelta(
            hours=1, minutes=CANDIDATE_FX_PUBLISH_MINUTE
        )
        for instrument_key in FX_RESEARCH_CANDIDATE_KEYS:
            slots.append(
                ScheduleSlot(
                    slot_id=(
                        f"fx-research-{instrument_key}-"
                        f"{candidate_due.strftime('%Y%m%dT%H%M%SZ')}"
                    ),
                    kind="fx_research_candidates_hourly",
                    due_at_utc=candidate_due,
                    deadline_utc=bar_start + timedelta(
                        hours=1, minutes=CANDIDATE_FX_DEADLINE_MINUTE
                    ),
                    instrument_keys=(instrument_key,),
                    expected_latest_complete={instrument_key: bar_start},
                    trigger=f"scheduled_fx_research_candidate_{instrument_key}",
                )
            )

    for session in equity_sessions:
        if session.session_status not in {"OPEN", "SHORT_SESSION"}:
            continue
        bar_start = session.open_time_utc.astimezone(timezone.utc)
        close_time = session.close_time_utc.astimezone(timezone.utc)
        for bar_start in fully_contained_hour_starts(
            bar_start, close_time, align_to_utc_hour=False
        ):
            due = bar_start + FIRST_PUBLISH_OFFSET
            deadline = bar_start + EQUITY_DEADLINE_OFFSET
            slots.append(
                ScheduleSlot(
                    slot_id=f"equity-{session.session_date.isoformat()}-{bar_start.strftime('%H%MZ')}",
                    kind="equity_regular_1h",
                    due_at_utc=due,
                    deadline_utc=deadline,
                    instrument_keys=EQUITY_REIT_KEYS,
                    expected_latest_complete={key: bar_start for key in EQUITY_REIT_KEYS},
                    trigger="scheduled_equity_reit_regular_1h",
                )
            )
            slots.append(
                ScheduleSlot(
                    slot_id=f"bond-credit-{session.session_date.isoformat()}-{bar_start.strftime('%H%MZ')}",
                    kind="bond_credit_regular_1h",
                    due_at_utc=due,
                    deadline_utc=deadline,
                    instrument_keys=BOND_CREDIT_KEYS,
                    expected_latest_complete={key: bar_start for key in BOND_CREDIT_KEYS},
                    trigger="scheduled_bond_credit_regular_1h",
                )
            )
            slots.append(
                ScheduleSlot(
                    slot_id=f"gold-{session.session_date.isoformat()}-{bar_start.strftime('%H%MZ')}",
                    kind="gold_regular_1h",
                    due_at_utc=due,
                    deadline_utc=deadline,
                    instrument_keys=GOLD_KEYS,
                    expected_latest_complete={key: bar_start for key in GOLD_KEYS},
                    trigger="scheduled_gold_regular_1h",
                )
            )
        # C2 is a low-frequency paper workflow.  One independent post-close
        # lane refreshes each ETF after Saxo has had time to publish the final
        # (possibly partial-hour) regular-session bar.  It reuses the canonical
        # 1H raw -> curated -> derived 1D path; it never requests a quote feed.
        session_seconds = int((close_time - session.open_time_utc).total_seconds())
        expected_slots = max(1, (session_seconds + 3599) // 3600)
        final_bar_start = session.open_time_utc.astimezone(timezone.utc) + timedelta(
            hours=expected_slots - 1
        )
        daily_due = close_time + ETF_DAILY_CLOSE_PUBLISH_OFFSET
        slots.append(
            ScheduleSlot(
                slot_id=f"etf-daily-close-{session.session_date.isoformat()}",
                kind="etf_daily_close",
                due_at_utc=daily_due,
                deadline_utc=close_time + ETF_DAILY_CLOSE_DEADLINE_OFFSET,
                instrument_keys=ETF_KEYS,
                expected_latest_complete={key: final_bar_start for key in ETF_KEYS},
                trigger="scheduled_etf_daily_close",
                expected_latest_session={key: session.session_date for key in ETF_KEYS},
            )
        )
    ordered = tuple(sorted(slots, key=lambda item: (item.due_at_utc, item.kind)))
    return _apply_scheduler_scope(ordered, scope_profile)


def schedule_around(
    now_utc: datetime, *, scope_profile: str = ACTIVE_SCOPE_PROFILE
) -> tuple[ScheduleSlot, ...]:
    selected = now_utc.astimezone(timezone.utc)
    sessions = load_equity_sessions(
        (selected - timedelta(days=2)).date(),
        (selected + timedelta(days=3)).date(),
    )
    return build_schedule_slots(selected, sessions, scope_profile=scope_profile)


def latest_complete_watermarks(instrument_keys: Iterable[str]) -> dict[str, datetime]:
    selected = tuple(str(key).lower() for key in instrument_keys)
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_periodic_watermarks") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT lower(i.market_key), w.latest_complete_time_utc
                FROM ops.watermark w
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND w.horizon_minutes=60 AND lower(i.market_key)=ANY(%s)
                """,
                (list(selected),),
            )
            rows = cursor.fetchall()
    return {str(key): value for key, value in rows if value is not None}


def latest_complete_daily_sessions(instrument_keys: Iterable[str]) -> dict[str, date]:
    selected = tuple(str(key).lower() for key in instrument_keys)
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_periodic_daily_sessions"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT b.instrument_key,MAX(b.session_date)
                FROM analytics.v_c2_daily_close_status_latest b
                WHERE b.derivation_status IN ('PASS','PASS_WITH_IMPUTATION_WARNING')
                  AND b.instrument_key=ANY(%s)
                GROUP BY b.instrument_key
                """,
                (list(selected),),
            )
            rows = cursor.fetchall()
    return {str(key): value for key, value in rows if value is not None}


def watermark_revision(instrument_keys: Iterable[str]) -> str:
    """Return a secret-free revision for terminal-blocker release decisions."""

    selected = tuple(sorted({str(key).lower() for key in instrument_keys}))
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_periodic_watermark_revision"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT lower(i.market_key), w.data_status, w.data_version,
                       w.latest_complete_time_utc, w.last_ingestion_run_id,
                       w.updated_at_utc
                FROM catalog.instrument i
                LEFT JOIN ops.watermark w
                  ON w.instrument_id=i.instrument_id AND w.horizon_minutes=60
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND lower(i.market_key)=ANY(%s)
                ORDER BY lower(i.market_key)
                """,
                (list(selected),),
            )
            rows = cursor.fetchall()
    observed = {
        str(key): {
            "data_status": status,
            "data_version": version,
            "latest_complete_time_utc": (
                None if latest is None else _utc_text(latest)
            ),
            "last_ingestion_run_id": run_id,
            "updated_at_utc": None if updated is None else _utc_text(updated),
        }
        for key, status, version, latest, run_id, updated in rows
    }
    payload = {
        "instrument_keys": list(selected),
        "watermarks": [
            {"instrument_key": key, **observed.get(key, {"missing": True})}
            for key in selected
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_expected_watermarks(
    expected: Mapping[str, datetime],
    observed: Mapping[str, datetime],
) -> dict[str, Any]:
    lagging = {
        key: {
            "expected_at_or_after": _utc_text(value),
            "observed": None if observed.get(key) is None else _utc_text(observed[key]),
        }
        for key, value in expected.items()
        if observed.get(key) is None or observed[key] < value
    }
    return {
        "status": "PASS" if not lagging else "DATA_NOT_READY",
        "lagging": lagging,
        "observed": {key: _utc_text(value) for key, value in observed.items()},
    }


def evaluate_expected_sessions(
    expected: Mapping[str, date], observed: Mapping[str, date]
) -> dict[str, Any]:
    lagging = {
        key: {
            "expected_at_or_after": value.isoformat(),
            "observed": None if observed.get(key) is None else observed[key].isoformat(),
        }
        for key, value in expected.items()
        if observed.get(key) is None or observed[key] < value
    }
    return {
        "status": "PASS" if not lagging else "DATA_NOT_READY",
        "lagging": lagging,
        "observed": {key: value.isoformat() for key, value in observed.items()},
    }


def _error_domain(error_code: str | None) -> str:
    selected = str(error_code or "")
    if not selected:
        return "none"
    if selected.startswith("RETRY_EXHAUSTED:"):
        return _error_domain(selected.split(":", 1)[1])
    if (
        selected.startswith("AUTH_")
        or selected.startswith("BLOCKED_TOKEN")
        or selected.startswith("BLOCKED_PERMISSION")
    ):
        return "interface_auth"
    if selected in {
        "BLOCKED_RATE_LIMIT", "FAILED_NETWORK", "FAILED_SERVICE_UNAVAILABLE",
        "FAILED_INVALID_JSON", "FAILED_JSON_NOT_OBJECT",
        "FAILED_INSUFFICIENTPRIVILEGE",
    } or selected.startswith("FAILED_HTTP_"):
        return "interface_operational"
    if selected in {"DATA_NOT_READY", "INSUFFICIENT_INCREMENTAL_CHART_DATA"}:
        return "data_not_ready"
    if selected in {
        "BLOCKED_CANONICAL_WATERMARK_SET",
        "BLOCKED_FULL_REFETCH_REQUIRED",
        "BLOCKED_BOUNDED_REVISION_REQUIRED",
        "BLOCKED_REPEATED_DATA_VERSION_CHANGE",
        "BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED",
    }:
        return "source_revision"
    return "data_quality"


TRANSIENT_RETRY_CODES = frozenset(
    {
        "BLOCKED_RATE_LIMIT",
        "FAILED_NETWORK",
        "DATA_NOT_READY",
        "INSUFFICIENT_INCREMENTAL_CHART_DATA",
    }
)
TERMINAL_OPERATOR_CODES = frozenset(
    {
        "BLOCKED_CANONICAL_WATERMARK_SET",
        "BLOCKED_FULL_REFETCH_REQUIRED",
        "BLOCKED_BOUNDED_REVISION_REQUIRED",
        "BLOCKED_REPEATED_DATA_VERSION_CHANGE",
        "BLOCKED_RECONCILIATION_LIMIT",
        "BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED",
        "BLOCKED_FULL_REFETCH_HISTORY_TRUNCATED",
        "BLOCKED_FULL_REFETCH_STATE_MISSING",
        "BLOCKED_FULL_REFETCH_NOT_REQUIRED",
    }
)


def retry_disposition(error_code: str | None) -> str:
    """Classify scheduler retries without converting a block into success."""

    selected = str(error_code or "")
    if selected in TRANSIENT_RETRY_CODES or selected.startswith("FAILED_HTTP_429"):
        return "TRANSIENT_RETRY"
    if selected in TERMINAL_OPERATOR_CODES or selected.startswith("BLOCKED_INSTRUMENT_DRIFT"):
        return "OPERATOR_ACTION_REQUIRED"
    return "OPERATOR_ACTION_REQUIRED"


def _terminal_blocker_signature(
    instrument_keys: Iterable[str], error_code: str, selected_watermark_revision: str
) -> str:
    payload = {
        "instrument_keys": sorted({str(key).lower() for key in instrument_keys}),
        "error_code": error_code,
        "watermark_revision": selected_watermark_revision,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _operator_recovery(error_code: str) -> tuple[str, str]:
    if error_code == "BLOCKED_CANONICAL_WATERMARK_SET":
        return (
            "run guarded reconcile while the scheduler is stopped",
            "watermark revision changes after reconcile/full-refetch",
        )
    if error_code == "BLOCKED_FULL_REFETCH_REQUIRED":
        return (
            "run guarded single-instrument full-refetch via reconcile",
            "watermark DataVersion and revision change",
        )
    if error_code == "BLOCKED_BOUNDED_REVISION_REQUIRED":
        return (
            "run bounded single-instrument DataVersion reconciliation",
            "bounded revision APPLIED or instrument-scoped full-refetch fallback completed",
        )
    if error_code == "BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED":
        return (
            "run guarded single-instrument full-refetch; bounded comparison was inconclusive",
            "guarded full-refetch PASS for the affected instrument",
        )
    if error_code.startswith("BLOCKED_INSTRUMENT_DRIFT"):
        return (
            "review the frozen canonical instrument identity; do not substitute symbols",
            "canonical registry or reviewed provider identity changes",
        )
    return (
        "review the recorded blocker and complete the documented recovery",
        "watermark revision or operator-reviewed prerequisite changes",
    )


def _record_terminal_blocker(
    previous: Mapping[str, Any] | None,
    slot: ScheduleSlot,
    result: Mapping[str, Any],
    selected_watermark_revision: str,
    observed_at: datetime,
) -> dict[str, Any]:
    error_code = str(result.get("error_code") or "SCHEDULED_INCREMENTAL_FAILED")
    affected_keys = _failed_instrument_keys(slot, result)
    signature = _terminal_blocker_signature(affected_keys, error_code, selected_watermark_revision)
    same = previous is not None and previous.get("signature") == signature
    action, resume_condition = _operator_recovery(error_code)
    return {
        "status": "INSTRUMENT_DEGRADED_OPERATOR_ACTION_REQUIRED",
        "signature": signature,
        "origin_slot_id": slot.slot_id,
        "instrument_keys": list(affected_keys),
        "error_code": error_code,
        "error_domain": result.get("error_domain") or _error_domain(error_code),
        "watermark_revision": selected_watermark_revision,
        "required_action": action,
        "resume_condition": resume_condition,
        "first_observed_at_utc": (
            previous.get("first_observed_at_utc") if same else _utc_text(observed_at)
        ),
        "last_observed_at_utc": _utc_text(observed_at),
        "observation_count": int(previous.get("observation_count", 0)) + 1 if same else 1,
        "ingestion_run_id": next(
            (
                step.get("database_ingestion_run_id")
                for step in reversed(list(result.get("steps") or []))
                if step.get("database_ingestion_run_id") is not None
            ),
            None,
        ),
        "orders_or_prechecks_sent": 0,
    }


def _terminal_blocker_still_applies(
    blocker: Mapping[str, Any] | None,
    slot: ScheduleSlot,
    selected_watermark_revision: str,
) -> bool:
    # A completed-bar publication delay belongs to the originating slot.  It
    # must remain visible as evidence, but must not suppress the next hourly or
    # post-close daily attempt for the same instrument indefinitely.
    if blocker and blocker.get("origin_slot_id") != slot.slot_id:
        if blocker.get("error_code") == "RETRY_EXHAUSTED:DATA_NOT_READY":
            return False
        if blocker.get("error_domain") in {"interface_auth", "interface_operational"}:
            return False
    return bool(
        blocker
        and blocker.get("status") in {
            "BLOCKED_OPERATOR_ACTION_REQUIRED",
            "INSTRUMENT_DEGRADED_OPERATOR_ACTION_REQUIRED",
        }
        and bool(set(blocker.get("instrument_keys") or ()) & set(slot.instrument_keys))
        and blocker.get("watermark_revision") == selected_watermark_revision
    )


def _failed_instrument_keys(
    slot: ScheduleSlot, result: Mapping[str, Any]
) -> tuple[str, ...]:
    explicit = result.get("failed_instrument_keys")
    if isinstance(explicit, list) and explicit and all(isinstance(key, str) for key in explicit):
        return tuple(sorted({key.lower() for key in explicit}))
    for step in reversed(list(result.get("steps") or [])):
        keys = step.get("failed_instrument_keys")
        if isinstance(keys, list) and keys and all(isinstance(key, str) for key in keys):
            return tuple(sorted({key.lower() for key in keys}))
        key = step.get("failed_instrument_key")
        if isinstance(key, str) and key:
            return (key.lower(),)
    return tuple(sorted({key.lower() for key in slot.instrument_keys}))


class PeriodicExecutor:
    def __init__(
        self,
        oauth_manager: SaxoOAuthManager,
        *,
        incremental_runner: Callable[..., dict[str, Any]] = run_incremental,
        full_refetch_runner: Callable[..., dict[str, Any]] = run_full_refetch,
        revision_reconcile_runner: Callable[..., dict[str, Any]] | None = None,
        watermark_loader: Callable[[Iterable[str]], dict[str, datetime]] = latest_complete_watermarks,
        daily_session_loader: Callable[[Iterable[str]], dict[str, date]] = (
            latest_complete_daily_sessions
        ),
    ) -> None:
        self.oauth_manager = oauth_manager
        self.incremental_runner = incremental_runner
        self.full_refetch_runner = full_refetch_runner
        self.revision_reconcile_runner = revision_reconcile_runner
        self.watermark_loader = watermark_loader
        self.daily_session_loader = daily_session_loader

    def _client(self, *, force_refresh: bool = False) -> SaxoClient:
        token = self.oauth_manager.access_token(force_refresh=force_refresh)
        try:
            return SaxoClient(token)
        finally:
            token = ""

    def _run_incremental(self, slot: ScheduleSlot, client: SaxoClient) -> dict[str, Any]:
        return self.incremental_runner(
            client=client,
            instrument_keys=slot.instrument_keys,
            trigger=slot.trigger,
        )

    def execute(self, slot: ScheduleSlot) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        steps: list[dict[str, Any]] = []
        try:
            client = self._client()
            result = self._run_incremental(slot, client)
            steps.append({"operation": "incremental", **result})

            if result.get("error_code") == "BLOCKED_TOKEN_EXPIRED":
                client = self._client(force_refresh=True)
                result = self._run_incremental(slot, client)
                steps.append({"operation": "incremental_after_token_refresh", **result})

            if result.get("warning_code") == "DATA_VERSION_REVISION_REVIEW_PENDING":
                observed = self.watermark_loader(slot.instrument_keys)
                return {
                    "status": "PASS",
                    "error_code": None,
                    "warning_code": "DATA_VERSION_REVISION_REVIEW_PENDING",
                    "error_domain": "none",
                    "slot": slot.public_dict(),
                    "watermark_gate": {
                        "status": "NOT_ADVANCED_REVISION_REVIEW_PENDING",
                        "observed": {
                            key: _utc_text(value) for key, value in observed.items()
                        },
                        "data_advanced": False,
                    },
                    "revision_event_id": result.get("revision_event_id"),
                    "review_status": result.get("review_status"),
                    "availability_status": result.get("availability_status"),
                    "started_at_utc": _utc_text(started_at),
                    "finished_at_utc": _utc_text(datetime.now(timezone.utc)),
                    "orders_or_prechecks_sent": 0,
                    "steps": steps,
                }

            if result.get("status") != "PASS":
                error_code = str(result.get("error_code") or "SCHEDULED_INCREMENTAL_FAILED")
                return {
                    "status": str(result.get("status") or "FAILED"),
                    "error_code": error_code,
                    "error_domain": _error_domain(error_code),
                    "slot": slot.public_dict(),
                    "started_at_utc": _utc_text(started_at),
                    "finished_at_utc": _utc_text(datetime.now(timezone.utc)),
                    "orders_or_prechecks_sent": 0,
                    "steps": steps,
                }

            observed = self.watermark_loader(slot.instrument_keys)
            watermark = evaluate_expected_watermarks(slot.expected_latest_complete, observed)
            status = str(watermark["status"])
            error_code = None if status == "PASS" else "DATA_NOT_READY"
            daily_close_gate = None
            if status == "PASS" and slot.expected_latest_session:
                observed_sessions = self.daily_session_loader(slot.instrument_keys)
                daily_close_gate = evaluate_expected_sessions(
                    slot.expected_latest_session, observed_sessions
                )
                status = str(daily_close_gate["status"])
                error_code = None if status == "PASS" else "DATA_NOT_READY"
            return {
                "status": status,
                "error_code": error_code,
                "error_domain": _error_domain(error_code),
                "slot": slot.public_dict(),
                "watermark_gate": watermark,
                "daily_close_gate": daily_close_gate,
                "started_at_utc": _utc_text(started_at),
                "finished_at_utc": _utc_text(datetime.now(timezone.utc)),
                "orders_or_prechecks_sent": 0,
                "steps": steps,
            }
        except SaxoAuthError as exc:
            return {
                "status": "BLOCKED",
                "error_code": exc.code,
                "error_domain": _error_domain(exc.code),
                "slot": slot.public_dict(),
                "started_at_utc": _utc_text(started_at),
                "finished_at_utc": _utc_text(datetime.now(timezone.utc)),
                "orders_or_prechecks_sent": 0,
                "steps": steps,
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "error_code": f"SCHEDULED_{type(exc).__name__.upper()}",
                "error_domain": "interface_operational",
                "slot": slot.public_dict(),
                "started_at_utc": _utc_text(started_at),
                "finished_at_utc": _utc_text(datetime.now(timezone.utc)),
                "orders_or_prechecks_sent": 0,
                "steps": steps,
            }


def _initial_state(now: datetime, scope_profile: str) -> dict[str, Any]:
    scope = scheduler_scope(scope_profile)
    return {
        "schema_version": 3,
        "owner": "saxo_db.periodic_update",
        "service_status": "RUNNING",
        "pid": os.getpid(),
        "started_at_utc": _utc_text(now),
        "checked_at_utc": _utc_text(now),
        "completed_slots": [],
        "last_job": None,
        "next_slots": [],
        "terminal_blockers": {},
        "transient_attempts": {},
        "total_return": provider_gate(),
        "scheduler_scope": scope.public_dict(),
        "orders_or_prechecks_sent": 0,
    }


def _latest_due_per_kind(
    slots: Iterable[ScheduleSlot],
    now: datetime,
    completed: set[str],
) -> tuple[ScheduleSlot, ...]:
    candidates: dict[tuple[str, tuple[str, ...]], ScheduleSlot] = {}
    for slot in slots:
        if slot.slot_id in completed or slot.due_at_utc > now:
            continue
        if now - slot.due_at_utc > MAX_CATCHUP_AGE:
            continue
        lane = (slot.kind, tuple(sorted(slot.instrument_keys)))
        previous = candidates.get(lane)
        if previous is None or slot.due_at_utc > previous.due_at_utc:
            candidates[lane] = slot
    return tuple(sorted(candidates.values(), key=lambda item: item.due_at_utc))


def _completed_through(
    slots: Iterable[ScheduleSlot], selected: ScheduleSlot
) -> set[str]:
    """Mark superseded catch-up slots so restart executes only the latest per kind."""

    return {
        slot.slot_id
        for slot in slots
        if slot.kind == selected.kind
        and tuple(sorted(slot.instrument_keys)) == tuple(sorted(selected.instrument_keys))
        and slot.due_at_utc <= selected.due_at_utc
    }


def _append_completed_slots(
    previous: Iterable[str], additions: Iterable[str]
) -> list[str]:
    """Keep a bounded completion history in completion order, not lexical order."""

    selected: list[str] = []
    seen: set[str] = set()
    for value in (*tuple(previous), *tuple(additions)):
        slot_id = str(value)
        if slot_id in seen:
            selected.remove(slot_id)
        else:
            seen.add(slot_id)
        selected.append(slot_id)
    return selected[-COMPLETED_SLOT_HISTORY_LIMIT:]


def candidate_scope_readiness() -> dict[str, Any]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_candidate_scope_gate") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.market_key,p.publication_status,p.consecutive_normal_passes,
                       p.blocker_code,p.quality_status,p.coverage_status,
                       p.freshness_status,p.consumer_availability_status,
                       p.research_policy_id
                FROM catalog.instrument i
                JOIN catalog.series_publication_state p USING (instrument_id)
                WHERE i.market_key=ANY(%s) AND p.horizon_minutes=60
                  AND p.price_basis='bid_ask_mid'
                ORDER BY i.market_key
                """,
                (list(FX_RESEARCH_CANDIDATE_KEYS),),
            )
            rows = cursor.fetchall()
    states = {
        str(key): {
            "publication_status": str(status),
            "consecutive_normal_passes": int(passes),
            "blocker_code": blocker,
            "quality_status": str(quality),
            "coverage_status": str(coverage),
            "freshness_status": str(freshness),
            "consumer_availability_status": str(availability),
            "research_policy_id": policy_id,
        }
        for key, status, passes, blocker, quality, coverage, freshness,
            availability, policy_id in rows
    }
    ready = all(
        states.get(key, {}).get("publication_status") == "PUBLISHED"
        and states.get(key, {}).get("consecutive_normal_passes") == 2
        and states.get(key, {}).get("quality_status") in {"PASS", "WARN"}
        and states.get(key, {}).get("coverage_status") in {"PASS", "WARN"}
        and states.get(key, {}).get("freshness_status") == "PASS"
        and states.get(key, {}).get("blocker_code") is None
        and states.get(key, {}).get("consumer_availability_status")
            == "AVAILABLE_WITH_WARNINGS"
        and states.get(key, {}).get("research_policy_id")
            == "fx_research_candidate_user_approved_warnings_v1"
        for key in FX_RESEARCH_CANDIDATE_KEYS
    )
    return {
        "status": "PASS" if ready else "BLOCKED_CANDIDATE_SCOPE_NOT_READY",
        "candidate_states": states,
        "orders_or_prechecks_sent": 0,
    }


def _slot_sla_status(
    slot: ScheduleSlot, started_at_utc: datetime, result_status: str = "PASS"
) -> str:
    if result_status != "PASS":
        return "MISS" if started_at_utc > slot.deadline_utc else "BLOCKED"
    return "PASS" if started_at_utc <= slot.deadline_utc else "MISS"


def maintain_auth_session(manager: SaxoOAuthManager) -> dict[str, Any]:
    """Keep the rotating refresh chain alive even when no data job is due."""

    token = manager.access_token()
    try:
        return manager.status()
    finally:
        token = ""


def run_daemon(
    executor: PeriodicExecutor,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    watermark_revision_loader: Callable[[Iterable[str]], str] = watermark_revision,
    scope_profile: str = ACTIVE_SCOPE_PROFILE,
) -> None:
    scope = scheduler_scope(scope_profile)
    if scope_profile == CANDIDATE_READY_SCOPE_PROFILE:
        readiness = candidate_scope_readiness()
        if readiness["status"] != "PASS":
            raise RuntimeError("BLOCKED_CANDIDATE_SCOPE_NOT_READY")
    _ensure_runtime_dir()
    lock_path = runtime_dir() / LOCK_FILENAME
    lock_stream = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_stream.close()
        raise RuntimeError("PERIODIC_UPDATE_ALREADY_RUNNING") from None

    stopper = stop_event or threading.Event()
    state = load_state() or _initial_state(clock(), scope_profile)
    state.update(
        {
            "schema_version": 4,
            "service_status": "RUNNING",
            "pid": os.getpid(),
            "scheduler_scope": scope.public_dict(),
        }
    )
    completed = set(str(value) for value in state.get("completed_slots", []))
    completed_order = _append_completed_slots(
        (), (str(value) for value in state.get("completed_slots", []))
    )
    next_attempt: dict[str, datetime] = {}
    terminal_blockers = dict(state.get("terminal_blockers") or {})
    # Error-domain policy can be corrected independently of the immutable
    # blocker evidence.  Reclassify persisted entries on restart so an
    # interface/operational failure is not presented as a data-quality fault.
    for blocker_key, stored in list(terminal_blockers.items()):
        normalized = dict(stored)
        normalized["error_domain"] = _error_domain(normalized.get("error_code"))
        terminal_blockers[blocker_key] = normalized
    transient_attempts = {
        str(key): int(value) for key, value in dict(state.get("transient_attempts") or {}).items()
    }
    write_state(state)
    try:
        while not stopper.is_set():
            now = clock().astimezone(timezone.utc)
            slots = schedule_around(now, scope_profile=scope_profile)
            upcoming = [slot.public_dict() for slot in slots if slot.due_at_utc > now][:6]
            state.update({"checked_at_utc": _utc_text(now), "next_slots": upcoming})
            try:
                state["auth"] = maintain_auth_session(executor.oauth_manager)
            except SaxoAuthError as exc:
                state["auth"] = {
                    "status": exc.code,
                    "token_values_exposed": False,
                    "orders_or_prechecks_sent": 0,
                }
            # Reconciliation can advance a watermark while the service is
            # stopped.  Remove resolved legacy blockers before choosing a
            # lane so the restarted service does not report false degraded
            # state until that particular instrument happens to be due.
            for blocker_key, stored in list(terminal_blockers.items()):
                affected = tuple(
                    str(key) for key in stored.get("instrument_keys") or ()
                )
                if not affected:
                    terminal_blockers.pop(blocker_key, None)
                    continue
                current_revision = watermark_revision_loader(affected)
                if stored.get("watermark_revision") != current_revision:
                    terminal_blockers.pop(blocker_key, None)
            candidates = _latest_due_per_kind(slots, now, completed)
            selected = None
            for candidate in candidates:
                applicable_blocker = None
                for blocker_key, stored in list(terminal_blockers.items()):
                    affected = tuple(str(key) for key in stored.get("instrument_keys") or ())
                    if not (set(affected) & set(candidate.instrument_keys)):
                        continue
                    blocker_revision = watermark_revision_loader(affected)
                    if _terminal_blocker_still_applies(stored, candidate, blocker_revision):
                        applicable_blocker = dict(stored)
                        applicable_blocker["last_observed_at_utc"] = _utc_text(now)
                        applicable_blocker["observation_count"] = int(
                            applicable_blocker.get("observation_count", 0)
                        ) + 1
                        terminal_blockers[blocker_key] = applicable_blocker
                        break
                    terminal_blockers.pop(blocker_key, None)
                if applicable_blocker is not None:
                    continue
                if next_attempt.get(
                    candidate.slot_id, datetime.min.replace(tzinfo=timezone.utc)
                ) <= now:
                    selected = candidate
                    break
            if selected is not None:
                result = executor.execute(selected)
                result["sla_status"] = _slot_sla_status(
                    selected, now, str(result.get("status") or "FAILED")
                )
                state["last_job"] = result
                if result.get("status") == "PASS":
                    newly_completed = _completed_through(slots, selected)
                    completed.update(newly_completed)
                    scheduled_order = [
                        slot.slot_id for slot in slots if slot.slot_id in newly_completed
                    ]
                    completed_order = _append_completed_slots(
                        completed_order, scheduled_order
                    )
                    state["completed_slots"] = completed_order
                    next_attempt.pop(selected.slot_id, None)
                    transient_attempts.pop(selected.slot_id, None)
                    for blocker_key, stored in list(terminal_blockers.items()):
                        if set(stored.get("instrument_keys") or ()) & set(selected.instrument_keys):
                            terminal_blockers.pop(blocker_key, None)
                else:
                    disposition = retry_disposition(result.get("error_code"))
                    if disposition == "TRANSIENT_RETRY":
                        attempts = transient_attempts.get(selected.slot_id, 0) + 1
                        transient_attempts[selected.slot_id] = attempts
                        if attempts < MAX_TRANSIENT_SLOT_ATTEMPTS:
                            next_attempt[selected.slot_id] = now + timedelta(seconds=RETRY_SECONDS)
                            result["retry"] = {
                                "disposition": disposition,
                                "attempt": attempts,
                                "maximum_attempts": MAX_TRANSIENT_SLOT_ATTEMPTS,
                                "next_attempt_at_utc": _utc_text(next_attempt[selected.slot_id]),
                            }
                        else:
                            result["retry"] = {
                                "disposition": "RETRY_EXHAUSTED_OPERATOR_ACTION_REQUIRED",
                                "attempt": attempts,
                                "maximum_attempts": MAX_TRANSIENT_SLOT_ATTEMPTS,
                            }
                            blocker_result = {
                                **result,
                                "error_code": f"RETRY_EXHAUSTED:{result.get('error_code')}",
                            }
                            affected = _failed_instrument_keys(selected, blocker_result)
                            affected_revision = watermark_revision_loader(affected)
                            blocker = _record_terminal_blocker(
                                None, selected, blocker_result, affected_revision, now,
                            )
                            terminal_blockers[blocker["signature"]] = blocker
                    else:
                        next_attempt.pop(selected.slot_id, None)
                        transient_attempts.pop(selected.slot_id, None)
                        affected = _failed_instrument_keys(selected, result)
                        affected_revision = watermark_revision_loader(affected)
                        blocker = _record_terminal_blocker(
                            None, selected, result, affected_revision, now,
                        )
                        terminal_blockers[blocker["signature"]] = blocker
            state.pop("operator_action_required", None)
            operator_actions = [
                dict(value)
                for value in terminal_blockers.values()
            ]
            degraded_instruments = sorted(
                {
                    str(key)
                    for blocker in operator_actions
                    for key in blocker.get("instrument_keys") or ()
                }
            )
            if operator_actions:
                state["service_status"] = "RUNNING_DEGRADED"
                state["operator_actions_required"] = operator_actions
                state["degraded_instruments"] = degraded_instruments
            else:
                state["service_status"] = "RUNNING"
                state.pop("operator_actions_required", None)
                state.pop("degraded_instruments", None)
            state["terminal_blockers"] = terminal_blockers
            state["transient_attempts"] = transient_attempts
            write_state(state)
            sleep(POLL_SECONDS)
    finally:
        state.update(
            {
                "service_status": "STOPPED",
                "checked_at_utc": _utc_text(clock().astimezone(timezone.utc)),
            }
        )
        write_state(state)
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def immediate_slot(
    now: datetime, *, scope_profile: str = ACTIVE_SCOPE_PROFILE
) -> ScheduleSlot:
    selected = now.astimezone(timezone.utc)
    scope = scheduler_scope(scope_profile)
    return ScheduleSlot(
        slot_id=f"manual-{selected.strftime('%Y%m%dT%H%M%SZ')}",
        kind="manual_scope_update",
        due_at_utc=selected,
        deadline_utc=selected + timedelta(minutes=10),
        instrument_keys=scope.included_instrument_keys,
        expected_latest_complete={},
        trigger=f"manual_{scope.profile_id}",
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6V5A priority periodic market-data update")
    parser.add_argument("command", choices=("serve", "run-once", "status", "schedule"))
    parser.add_argument("--callback-port", type=int, default=8764)
    parser.add_argument(
        "--scope-profile",
        choices=tuple(SCHEDULER_SCOPES),
        default=ACTIVE_SCOPE_PROFILE,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "status":
        result = load_state() or {
            "schema_version": 1,
            "owner": "saxo_db.periodic_update",
            "service_status": "STOPPED",
            "orders_or_prechecks_sent": 0,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    now = datetime.now(timezone.utc)
    if args.command == "schedule":
        scope = scheduler_scope(args.scope_profile)
        slots = schedule_around(now, scope_profile=args.scope_profile)
        result = {
            "status": "PASS",
            "checked_at_utc": _utc_text(now),
            "scheduler_scope": scope.public_dict(),
            "slots": [slot.public_dict() for slot in slots if slot.due_at_utc >= now][:12],
            "orders_or_prechecks_sent": 0,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    try:
        oauth = SaxoOAuthManager(
            OAuthConfig.from_local_configuration(callback_port=args.callback_port)
        )
        executor = PeriodicExecutor(oauth)
        if args.command == "run-once":
            result = executor.execute(
                immediate_slot(now, scope_profile=args.scope_profile)
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("status") == "PASS" else 1

        stop = threading.Event()

        def request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        run_daemon(executor, stop_event=stop, scope_profile=args.scope_profile)
        return 0
    except (SaxoAuthError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, SaxoAuthError) else str(exc)
        result = {
            "status": "BLOCKED",
            "error_code": code,
            "error_domain": _error_domain(code),
            "orders_or_prechecks_sent": 0,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
