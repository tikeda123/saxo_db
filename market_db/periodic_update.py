"""Unattended, fail-closed scheduler for the S6V5A priority 1H series."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .connection import MARKET_DB, connect, project_root
from .incremental_update import (
    S6V5A_PRIORITY_INSTRUMENT_KEYS,
    run_full_refetch,
    run_incremental,
)
from .saxo_auth import OAuthConfig, SaxoAuthError, SaxoOAuthManager
from .saxo_client import SaxoClient
from .session_calendar import SessionInterval, generate_fx_sessions
from .total_return_update import provider_gate


EQUITY_KEYS = ("spy", "iwm", "efa", "eem", "vnq")
FX_KEYS = ("eurusd",)
RUNTIME_RELATIVE_PATH = Path(".runtime/periodic_update")
STATE_FILENAME = "state.json"
LOCK_FILENAME = "service.lock"
POLL_SECONDS = 15.0
RETRY_SECONDS = 30.0
MAX_CATCHUP_AGE = timedelta(hours=6)
FIRST_PUBLISH_OFFSET = timedelta(hours=1, seconds=15)
EQUITY_DEADLINE_OFFSET = timedelta(hours=1, minutes=3)
FX_PUBLISH_MINUTE = 3
FX_DEADLINE_MINUTE = 10


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
        }


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
                instrument_keys=FX_KEYS,
                expected_latest_complete={"eurusd": bar_start},
                trigger="scheduled_s6v5a_fx_hourly",
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
            expected = {key: bar_start for key in EQUITY_KEYS}
            due_fx = [
                value for value in fx_bar_starts
                if value + timedelta(hours=1, minutes=FX_PUBLISH_MINUTE) <= due
            ]
            if due_fx:
                expected["eurusd"] = due_fx[-1]
            slots.append(
                ScheduleSlot(
                    slot_id=f"equity-{session.session_date.isoformat()}-{bar_start.strftime('%H%MZ')}",
                    kind="equity_regular_1h",
                    due_at_utc=due,
                    deadline_utc=deadline,
                    instrument_keys=S6V5A_PRIORITY_INSTRUMENT_KEYS,
                    expected_latest_complete=expected,
                    trigger="scheduled_s6v5a_equity_regular_1h",
                )
            )
    return tuple(sorted(slots, key=lambda item: (item.due_at_utc, item.kind)))


def schedule_around(now_utc: datetime) -> tuple[ScheduleSlot, ...]:
    selected = now_utc.astimezone(timezone.utc)
    sessions = load_equity_sessions(
        (selected - timedelta(days=2)).date(),
        (selected + timedelta(days=3)).date(),
    )
    return build_schedule_slots(selected, sessions)


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


def _error_domain(error_code: str | None) -> str:
    selected = str(error_code or "")
    if not selected:
        return "none"
    if (
        selected.startswith("AUTH_")
        or selected.startswith("BLOCKED_TOKEN")
        or selected.startswith("BLOCKED_PERMISSION")
    ):
        return "interface_auth"
    if selected in {
        "BLOCKED_RATE_LIMIT", "FAILED_NETWORK", "FAILED_SERVICE_UNAVAILABLE",
        "FAILED_INVALID_JSON", "FAILED_JSON_NOT_OBJECT",
    } or selected.startswith("FAILED_HTTP_"):
        return "interface_operational"
    if selected in {"DATA_NOT_READY", "INSUFFICIENT_INCREMENTAL_CHART_DATA"}:
        return "data_not_ready"
    if selected in {"BLOCKED_FULL_REFETCH_REQUIRED", "BLOCKED_REPEATED_DATA_VERSION_CHANGE"}:
        return "source_revision"
    return "data_quality"


class PeriodicExecutor:
    def __init__(
        self,
        oauth_manager: SaxoOAuthManager,
        *,
        incremental_runner: Callable[..., dict[str, Any]] = run_incremental,
        full_refetch_runner: Callable[..., dict[str, Any]] = run_full_refetch,
        watermark_loader: Callable[[Iterable[str]], dict[str, datetime]] = latest_complete_watermarks,
    ) -> None:
        self.oauth_manager = oauth_manager
        self.incremental_runner = incremental_runner
        self.full_refetch_runner = full_refetch_runner
        self.watermark_loader = watermark_loader

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

            if result.get("error_code") == "BLOCKED_FULL_REFETCH_REQUIRED":
                failed_key = result.get("failed_instrument_key")
                if not isinstance(failed_key, str) or failed_key not in slot.instrument_keys:
                    raise RuntimeError("SCHEDULED_REFETCH_TARGET_INVALID")
                refetch = self.full_refetch_runner(
                    failed_key,
                    client=client,
                    trigger="scheduled_s6v5a_data_version_recovery",
                )
                steps.append({"operation": "full_refetch", **refetch})
                if refetch.get("status") == "PASS":
                    result = self._run_incremental(slot, client)
                    steps.append({"operation": "incremental_after_full_refetch", **result})

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
            return {
                "status": status,
                "error_code": error_code,
                "error_domain": _error_domain(error_code),
                "slot": slot.public_dict(),
                "watermark_gate": watermark,
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


def _initial_state(now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "owner": "saxo_db.periodic_update",
        "service_status": "RUNNING",
        "pid": os.getpid(),
        "started_at_utc": _utc_text(now),
        "checked_at_utc": _utc_text(now),
        "completed_slots": [],
        "last_job": None,
        "next_slots": [],
        "total_return": provider_gate(),
        "orders_or_prechecks_sent": 0,
    }


def _latest_due_per_kind(
    slots: Iterable[ScheduleSlot],
    now: datetime,
    completed: set[str],
) -> tuple[ScheduleSlot, ...]:
    candidates: dict[str, ScheduleSlot] = {}
    for slot in slots:
        if slot.slot_id in completed or slot.due_at_utc > now:
            continue
        if now - slot.due_at_utc > MAX_CATCHUP_AGE:
            continue
        previous = candidates.get(slot.kind)
        if previous is None or slot.due_at_utc > previous.due_at_utc:
            candidates[slot.kind] = slot
    return tuple(sorted(candidates.values(), key=lambda item: item.due_at_utc))


def _completed_through(
    slots: Iterable[ScheduleSlot], selected: ScheduleSlot
) -> set[str]:
    """Mark superseded catch-up slots so restart executes only the latest per kind."""

    return {
        slot.slot_id
        for slot in slots
        if slot.kind == selected.kind and slot.due_at_utc <= selected.due_at_utc
    }


def _slot_sla_status(slot: ScheduleSlot, started_at_utc: datetime) -> str:
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
) -> None:
    _ensure_runtime_dir()
    lock_path = runtime_dir() / LOCK_FILENAME
    lock_stream = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_stream.close()
        raise RuntimeError("PERIODIC_UPDATE_ALREADY_RUNNING") from None

    stopper = stop_event or threading.Event()
    state = load_state() or _initial_state(clock())
    state.update({"service_status": "RUNNING", "pid": os.getpid()})
    completed = set(str(value) for value in state.get("completed_slots", []))
    next_attempt: dict[str, datetime] = {}
    write_state(state)
    try:
        while not stopper.is_set():
            now = clock().astimezone(timezone.utc)
            slots = schedule_around(now)
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
            candidates = _latest_due_per_kind(slots, now, completed)
            selected = next(
                (
                    slot for slot in candidates
                    if next_attempt.get(slot.slot_id, datetime.min.replace(tzinfo=timezone.utc)) <= now
                ),
                None,
            )
            if selected is not None:
                result = executor.execute(selected)
                result["sla_status"] = _slot_sla_status(selected, now)
                state["last_job"] = result
                if result.get("status") == "PASS":
                    completed.update(_completed_through(slots, selected))
                    state["completed_slots"] = sorted(completed)[-256:]
                    next_attempt.pop(selected.slot_id, None)
                else:
                    next_attempt[selected.slot_id] = now + timedelta(seconds=RETRY_SECONDS)
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


def immediate_slot(now: datetime) -> ScheduleSlot:
    selected = now.astimezone(timezone.utc)
    return ScheduleSlot(
        slot_id=f"manual-{selected.strftime('%Y%m%dT%H%M%SZ')}",
        kind="manual_s6v5a_priority",
        due_at_utc=selected,
        deadline_utc=selected + timedelta(minutes=10),
        instrument_keys=S6V5A_PRIORITY_INSTRUMENT_KEYS,
        expected_latest_complete={},
        trigger="manual_s6v5a_priority",
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6V5A priority periodic market-data update")
    parser.add_argument("command", choices=("serve", "run-once", "status", "schedule"))
    parser.add_argument("--callback-port", type=int, default=8764)
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
        slots = schedule_around(now)
        result = {
            "status": "PASS",
            "checked_at_utc": _utc_text(now),
            "slots": [slot.public_dict() for slot in slots if slot.due_at_utc >= now][:12],
            "orders_or_prechecks_sent": 0,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    try:
        oauth = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=args.callback_port))
        executor = PeriodicExecutor(oauth)
        if args.command == "run-once":
            result = executor.execute(immediate_slot(now))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("status") == "PASS" else 1

        stop = threading.Event()

        def request_stop(_signum: int, _frame: Any) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        run_daemon(executor, stop_event=stop)
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
