from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import threading

import market_db.periodic_update as periodic_update_module
from market_db.connection import project_root
from market_db.periodic_update import (
    ACTIVE_SCOPE_PROFILE,
    BOND_CREDIT_KEYS,
    CANDIDATE_READY_SCOPE_PROFILE,
    EQUITY_KEYS,
    ETF_KEYS,
    FX_RESEARCH_CANDIDATE_KEYS,
    FX_KEYS,
    GOLD_KEYS,
    EquitySession,
    PeriodicExecutor,
    ScheduleSlot,
    _completed_through,
    _latest_due_per_kind,
    _record_terminal_blocker,
    _slot_sla_status,
    _terminal_blocker_still_applies,
    build_schedule_slots,
    evaluate_expected_watermarks,
    fully_contained_hour_starts,
    retry_disposition,
    run_daemon,
)
from market_db.session_calendar import SessionInterval, generate_equity_sessions, generate_fx_sessions
from market_db.validate import manifest_artifact_state, periodic_update_manifest_baseline_is_valid


class FakeOAuth:
    def __init__(self):
        self.force_refresh_calls = []

    def access_token(self, *, force_refresh=False):
        self.force_refresh_calls.append(force_refresh)
        return "memory-only-access-token"


def equity_session(day=date(2026, 7, 24)):
    return EquitySession(
        day,
        datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc),
        "OPEN",
    )


def test_equity_schedule_starts_at_close_plus_15_seconds_and_deadlines_at_three_minutes():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    slots = build_schedule_slots(now, [equity_session()])
    equity = [slot for slot in slots if slot.kind == "equity_regular_1h"]

    assert len(equity) == 30
    first_spy = next(slot for slot in equity if slot.slot_id.endswith("1330Z-spy"))
    assert first_spy.due_at_utc == datetime(2026, 7, 24, 14, 30, 15, tzinfo=timezone.utc)
    assert first_spy.deadline_utc == datetime(2026, 7, 24, 14, 33, tzinfo=timezone.utc)
    assert first_spy.expected_latest_complete["spy"] == datetime(
        2026, 7, 24, 13, 30, tzinfo=timezone.utc
    )
    assert first_spy.instrument_keys == ("spy",)
    assert {slot.instrument_keys for slot in equity[:5]} == {
        (key,) for key in EQUITY_KEYS
    }


def test_usdjpy_quarantine_scope_schedules_all_etfs_and_eurusd_only():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    slots = build_schedule_slots(
        now, [equity_session()], scope_profile=ACTIVE_SCOPE_PROFILE
    )

    categories = {
        kind: [slot for slot in slots if slot.kind == kind]
        for kind in (
            "equity_regular_1h",
            "bond_credit_regular_1h",
            "gold_regular_1h",
            "fx_hourly",
        )
    }
    assert {slot.instrument_keys for slot in categories["equity_regular_1h"]} == {
        (key,) for key in EQUITY_KEYS
    }
    assert {slot.instrument_keys for slot in categories["bond_credit_regular_1h"]} == {
        (key,) for key in BOND_CREDIT_KEYS
    }
    assert {slot.instrument_keys for slot in categories["gold_regular_1h"]} == {
        (key,) for key in GOLD_KEYS
    }
    assert {slot.instrument_keys for slot in categories["fx_hourly"]} == {("eurusd",)}
    assert set().union(*(set(slot.instrument_keys) for rows in categories.values() for slot in rows)) == (
        set(ETF_KEYS) | {"eurusd"}
    )
    assert all("usdjpy" not in slot.instrument_keys for slot in slots)
    assert all("usdjpy" not in slot.expected_latest_complete for slot in slots)
    assert not [slot for slot in slots if slot.kind == "fx_research_candidates_hourly"]
    assert all("_usdjpy_quarantined_" in slot.trigger for slot in slots)

    profile = json.loads(
        (
            project_root()
            / "specs/source_collection/periodic_scheduler_scope_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert profile["profile_id"] == ACTIVE_SCOPE_PROFILE
    configured = tuple(
        key
        for category in profile["included_categories"]
        for key in category["instrument_keys"]
    )
    assert configured == ETF_KEYS + ("eurusd",)
    assert [row["instrument_key"] for row in profile["excluded_instruments"]] == [
        "usdjpy"
    ]
    assert profile["security"]["orders"] == 0
    assert profile["security"]["prechecks"] == 0
    assert profile["security"]["write_requests"] == 0


def test_candidate_ready_scope_isolated_single_pair_lanes_and_keeps_usdjpy_excluded():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    slots = build_schedule_slots(
        now, [equity_session()], scope_profile=CANDIDATE_READY_SCOPE_PROFILE
    )
    candidates = [
        slot for slot in slots if slot.kind == "fx_research_candidates_hourly"
    ]

    assert candidates
    latest_due = max(slot.due_at_utc for slot in candidates)
    latest = [slot for slot in candidates if slot.due_at_utc == latest_due]
    assert [slot.instrument_keys for slot in latest] == [
        ("audusd",), ("usdcad",), ("usdchf",),
    ]
    assert all(slot.due_at_utc.minute == 6 for slot in candidates)
    assert all(slot.deadline_utc.minute == 15 for slot in candidates)
    assert all("usdjpy" not in slot.instrument_keys for slot in slots)

    due = _latest_due_per_kind(candidates, latest_due, set())
    assert {slot.instrument_keys for slot in due} == {
        (key,) for key in FX_RESEARCH_CANDIDATE_KEYS
    }
    audusd = next(slot for slot in due if slot.instrument_keys == ("audusd",))
    completed = _completed_through(candidates, audusd)
    assert completed
    assert all("audusd" in slot_id for slot_id in completed)


def test_holiday_has_no_equity_slots_and_fx_remains_hourly():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    holiday = EquitySession(date(2026, 7, 24), now, now, "HOLIDAY")
    slots = build_schedule_slots(now, [holiday])
    assert not [slot for slot in slots if slot.kind == "equity_regular_1h"]
    fx = [slot for slot in slots if slot.kind == "fx_hourly"]
    assert len(fx) > 0
    assert all(slot.due_at_utc.minute == 3 for slot in fx)
    assert {slot.instrument_keys for slot in fx} == {("eurusd",), ("usdjpy",)}
    assert all(set(slot.expected_latest_complete) == set(slot.instrument_keys) for slot in fx)


def test_normal_and_short_equity_sessions_exclude_incomplete_close_bar():
    normal = generate_equity_sessions(date(2026, 7, 24), date(2026, 7, 24))[0]
    short = generate_equity_sessions(date(2026, 11, 27), date(2026, 11, 27))[0]
    assert [value.strftime("%H:%M") for value in fully_contained_hour_starts(
        normal.open_time_utc, normal.close_time_utc, align_to_utc_hour=False
    )][-1] == "18:30"
    assert len(fully_contained_hour_starts(
        normal.open_time_utc, normal.close_time_utc, align_to_utc_hour=False
    )) == 6
    assert len(fully_contained_hour_starts(
        short.open_time_utc, short.close_time_utc, align_to_utc_hour=False
    )) == 3


def test_equity_dst_keeps_0930_new_york_first_bar():
    winter = generate_equity_sessions(date(2026, 2, 2), date(2026, 2, 2))[0]
    summer = generate_equity_sessions(date(2026, 7, 24), date(2026, 7, 24))[0]
    assert winter.open_time_utc.hour == 14
    assert summer.open_time_utc.hour == 13


def test_fx_schedule_excludes_weekend_and_daily_maintenance_hour():
    sessions = generate_fx_sessions(date(2026, 7, 20), date(2026, 7, 25))
    assert [item.session_date.weekday() for item in sessions] == [0, 1, 2, 3, 4]
    friday = sessions[-1]
    starts = fully_contained_hour_starts(
        friday.open_time_utc, friday.close_time_utc, align_to_utc_hour=True,
        opening_delay=timedelta(minutes=5), closing_lead=timedelta(minutes=1),
    )
    assert starts[-1] == datetime(2026, 7, 24, 19, tzinfo=timezone.utc)
    assert datetime(2026, 7, 24, 20, tzinfo=timezone.utc) not in starts


def test_fx_dst_changes_maintenance_boundary_but_keeps_utc_hour_alignment():
    winter = generate_fx_sessions(date(2026, 2, 2), date(2026, 2, 2))[0]
    summer = generate_fx_sessions(date(2026, 7, 20), date(2026, 7, 20))[0]
    assert winter.close_time_utc.hour == 22
    assert summer.close_time_utc.hour == 21
    assert all(value.minute == 0 for value in fully_contained_hour_starts(
        winter.open_time_utc, winter.close_time_utc, align_to_utc_hour=True,
        opening_delay=timedelta(minutes=5), closing_lead=timedelta(minutes=1),
    ))


def test_fx_scheduler_does_not_create_slots_after_friday_close():
    now = datetime(2026, 7, 24, 22, 30, tzinfo=timezone.utc)
    sessions = generate_fx_sessions(date(2026, 7, 23), date(2026, 7, 27))
    slots = build_schedule_slots(now, [], sessions)
    fx = [slot for slot in slots if slot.kind == "fx_hourly"]
    latest_due = max(slot.due_at_utc for slot in fx)
    latest = [slot for slot in fx if slot.due_at_utc == latest_due]
    eurusd = next(slot for slot in latest if slot.instrument_keys == ("eurusd",))
    usdjpy = next(slot for slot in latest if slot.instrument_keys == ("usdjpy",))
    assert eurusd.expected_latest_complete["eurusd"] == datetime(
        2026, 7, 24, 19, tzinfo=timezone.utc
    )
    assert usdjpy.expected_latest_complete["usdjpy"] == datetime(
        2026, 7, 24, 19, tzinfo=timezone.utc
    )
    assert latest_due == datetime(2026, 7, 24, 20, 3, tzinfo=timezone.utc)


def test_watermark_gate_distinguishes_data_not_ready_without_quality_failure():
    expected = {
        "spy": datetime(2026, 7, 24, 13, 30, tzinfo=timezone.utc),
        "eurusd": datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
    }
    observed = {
        "spy": datetime(2026, 7, 24, 12, 30, tzinfo=timezone.utc),
        "eurusd": datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc),
    }
    result = evaluate_expected_watermarks(expected, observed)
    assert result["status"] == "DATA_NOT_READY"
    assert set(result["lagging"]) == {"spy"}


def test_executor_records_revision_warning_without_reconcile_or_degradation():
    due = datetime(2026, 7, 24, 14, 30, 15, tzinfo=timezone.utc)
    expected = {"spy": due - timedelta(hours=1)}
    slot = ScheduleSlot(
        "slot", "equity_regular_1h", due, due + timedelta(minutes=3),
        ("spy",), expected, "scheduled_test",
    )
    incremental_results = [{
        "status": "PASS",
        "error_code": None,
        "warning_code": "DATA_VERSION_REVISION_REVIEW_PENDING",
        "revision_event_id": 51,
        "review_status": "PENDING_REVIEW",
        "availability_status": "AVAILABLE_WITH_REVISION_WARNING",
        "data_advanced": False,
    }]
    calls = []

    def incremental_runner(**kwargs):
        calls.append(("incremental", kwargs))
        return incremental_results.pop(0)

    def full_refetch_runner(key, **kwargs):
        calls.append(("full_refetch", key, kwargs))
        return {"status": "PASS", "error_code": None, "instrument_key": key}

    def revision_reconcile_runner(key, **kwargs):
        raise AssertionError("scheduler must not reconcile a DataVersion warning")

    executor = PeriodicExecutor(
        FakeOAuth(),
        incremental_runner=incremental_runner,
        full_refetch_runner=full_refetch_runner,
        revision_reconcile_runner=revision_reconcile_runner,
        watermark_loader=lambda keys: dict(expected),
    )
    result = executor.execute(slot)

    assert result["status"] == "PASS"
    assert result["error_code"] is None
    assert result["warning_code"] == "DATA_VERSION_REVISION_REVIEW_PENDING"
    assert result["watermark_gate"]["status"] == "NOT_ADVANCED_REVISION_REVIEW_PENDING"
    assert result["watermark_gate"]["data_advanced"] is False
    assert [step["operation"] for step in result["steps"]] == ["incremental"]
    assert calls[0][1]["instrument_keys"] == ("spy",)
    assert not [call for call in calls if call[0] == "full_refetch"]
    assert result["orders_or_prechecks_sent"] == 0


def test_executor_refreshes_once_after_401_and_classifies_auth_separately():
    now = datetime(2026, 7, 24, 14, tzinfo=timezone.utc)
    slot = ScheduleSlot("fx", "fx_hourly", now, now + timedelta(minutes=7), ("eurusd",), {}, "test")
    results = [
        {"status": "BLOCKED", "error_code": "BLOCKED_TOKEN_EXPIRED"},
        {"status": "PASS", "error_code": None},
    ]
    oauth = FakeOAuth()
    executor = PeriodicExecutor(
        oauth,
        incremental_runner=lambda **kwargs: results.pop(0),
        watermark_loader=lambda keys: {},
    )
    result = executor.execute(slot)

    assert result["status"] == "PASS"
    assert oauth.force_refresh_calls == [False, True]
    assert result["steps"][0]["error_code"] == "BLOCKED_TOKEN_EXPIRED"


def test_scheduler_restart_catches_up_only_latest_slot_and_records_deadline_miss():
    now = datetime(2026, 7, 24, 18, tzinfo=timezone.utc)
    slots = tuple(
        ScheduleSlot(
            f"fx-{hour}", "fx_hourly",
            now - timedelta(hours=hour),
            now - timedelta(hours=hour, minutes=-7),
            ("eurusd",), {}, "scheduled_test",
        )
        for hour in (3, 2, 1)
    )

    selected = _latest_due_per_kind(slots, now, set())
    assert [slot.slot_id for slot in selected] == ["fx-1"]
    assert _slot_sla_status(selected[0], now) == "MISS"
    completed = _completed_through(slots, selected[0])
    assert completed == {"fx-3", "fx-2", "fx-1"}
    assert _latest_due_per_kind(slots, now, completed) == ()


def test_terminal_blocker_signature_survives_json_restart_and_releases_on_watermark_change():
    now = datetime(2026, 7, 27, 4, 3, tzinfo=timezone.utc)
    slot = ScheduleSlot(
        "fx-20260727T040300Z", "fx_hourly", now, now + timedelta(minutes=7),
        FX_KEYS, {key: now - timedelta(hours=1) for key in FX_KEYS}, "scheduled_fx_hourly",
    )
    result = {
        "status": "BLOCKED",
        "error_code": "BLOCKED_CANONICAL_WATERMARK_SET",
        "error_domain": "source_revision",
        "steps": [{"database_ingestion_run_id": 777}],
    }
    blocker = _record_terminal_blocker(None, slot, result, "revision-a", now)
    restarted = json.loads(json.dumps(blocker))
    assert _terminal_blocker_still_applies(restarted, slot, "revision-a")
    assert not _terminal_blocker_still_applies(restarted, slot, "revision-b")
    assert blocker["status"] == "INSTRUMENT_DEGRADED_OPERATOR_ACTION_REQUIRED"
    assert blocker["instrument_keys"] == ["eurusd", "usdjpy"]
    assert blocker["required_action"].startswith("run guarded reconcile")
    assert retry_disposition(result["error_code"]) == "OPERATOR_ACTION_REQUIRED"
    assert retry_disposition("FAILED_NETWORK") == "TRANSIENT_RETRY"


def test_daemon_does_not_repeat_same_terminal_slot_after_restart(tmp_path, monkeypatch):
    now = datetime(2026, 7, 27, 4, 4, tzinfo=timezone.utc)
    slot = ScheduleSlot(
        "fx-20260727T040300Z", "fx_hourly", now - timedelta(minutes=1),
        now + timedelta(minutes=6), FX_KEYS,
        {key: now - timedelta(hours=1) for key in FX_KEYS}, "scheduled_fx_hourly",
    )
    calls = []

    class OAuth:
        def access_token(self):
            return "memory-only"

        def status(self):
            return {"status": "AUTH_READY", "token_values_exposed": False}

    class Executor:
        oauth_manager = OAuth()

        def execute(self, selected):
            calls.append(selected.slot_id)
            return {
                "status": "BLOCKED",
                "error_code": "BLOCKED_CANONICAL_WATERMARK_SET",
                "error_domain": "source_revision",
                "steps": [{"database_ingestion_run_id": 900 + len(calls)}],
                "orders_or_prechecks_sent": 0,
            }

    monkeypatch.setattr(periodic_update_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(
        periodic_update_module, "schedule_around", lambda selected, **kwargs: (slot,)
    )

    def one_daemon_run():
        stop = threading.Event()
        polls = {"count": 0}

        def fake_sleep(_seconds):
            polls["count"] += 1
            if polls["count"] >= 3:
                stop.set()

        run_daemon(
            Executor(), clock=lambda: now, sleep=fake_sleep, stop_event=stop,
            watermark_revision_loader=lambda keys: "revision-a",
        )

    one_daemon_run()
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    persisted_blocker = next(iter(persisted["terminal_blockers"].values()))
    persisted_blocker["error_domain"] = "data_quality"
    persisted_blocker["error_code"] = "FAILED_INSUFFICIENTPRIVILEGE"
    (tmp_path / "state.json").write_text(json.dumps(persisted), encoding="utf-8")
    one_daemon_run()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert calls == [slot.slot_id]
    blocker = next(iter(state["terminal_blockers"].values()))
    assert blocker["observation_count"] >= 2
    assert blocker["error_domain"] == "interface_operational"
    assert state["completed_slots"] == []
    assert state["last_job"]["sla_status"] == "BLOCKED"


def test_daemon_terminal_cause_blocks_later_hourly_slot_until_watermark_changes(tmp_path, monkeypatch):
    first = datetime(2026, 7, 27, 4, 3, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)
    slots = (
        ScheduleSlot("fx-first", "fx_hourly", first, first + timedelta(minutes=7), FX_KEYS, {}, "scheduled_fx_hourly"),
        ScheduleSlot("fx-second", "fx_hourly", second, second + timedelta(minutes=7), FX_KEYS, {}, "scheduled_fx_hourly"),
    )
    calls = []

    class OAuth:
        def access_token(self):
            return "memory-only"

        def status(self):
            return {"status": "AUTH_READY", "token_values_exposed": False}

    class Executor:
        oauth_manager = OAuth()

        def execute(self, selected):
            calls.append(selected.slot_id)
            return {
                "status": "BLOCKED",
                "error_code": "BLOCKED_CANONICAL_WATERMARK_SET",
                "steps": [{"failed_instrument_key": "eurusd", "database_ingestion_run_id": 1}],
                "orders_or_prechecks_sent": 0,
            }

    current = {"value": first + timedelta(minutes=1)}
    stop = threading.Event()
    polls = {"count": 0}

    def fake_sleep(_seconds):
        polls["count"] += 1
        current["value"] = second + timedelta(minutes=1)
        if polls["count"] >= 3:
            stop.set()

    monkeypatch.setattr(periodic_update_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(
        periodic_update_module, "schedule_around", lambda selected, **kwargs: slots
    )
    run_daemon(
        Executor(), clock=lambda: current["value"], sleep=fake_sleep, stop_event=stop,
        watermark_revision_loader=lambda keys: "unchanged-eurusd-revision",
    )

    assert calls == ["fx-first"]


def test_daemon_caps_transient_attempts_and_then_requires_operator(tmp_path, monkeypatch):
    due = datetime(2026, 7, 27, 4, 3, tzinfo=timezone.utc)
    slot = ScheduleSlot(
        "fx-transient", "fx_hourly", due, due + timedelta(minutes=7), FX_KEYS, {},
        "scheduled_fx_hourly",
    )
    current = {"value": due}
    calls = []
    stop = threading.Event()

    class OAuth:
        def access_token(self):
            return "memory-only"

        def status(self):
            return {"status": "AUTH_READY", "token_values_exposed": False}

    class Executor:
        oauth_manager = OAuth()

        def execute(self, selected):
            calls.append(selected.slot_id)
            return {
                "status": "FAILED",
                "error_code": "FAILED_NETWORK",
                "steps": [],
                "orders_or_prechecks_sent": 0,
            }

    polls = {"count": 0}

    def fake_sleep(_seconds):
        polls["count"] += 1
        current["value"] += timedelta(seconds=31)
        if polls["count"] >= 8:
            stop.set()

    monkeypatch.setattr(periodic_update_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(
        periodic_update_module, "schedule_around", lambda selected, **kwargs: (slot,)
    )
    run_daemon(
        Executor(), clock=lambda: current["value"], sleep=fake_sleep, stop_event=stop,
        watermark_revision_loader=lambda keys: "revision-a",
    )

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(calls) == periodic_update_module.MAX_TRANSIENT_SLOT_ATTEMPTS
    assert state["service_status"] == "STOPPED"
    blocker = next(iter(state["terminal_blockers"].values()))
    assert blocker["error_code"] == "RETRY_EXHAUSTED:FAILED_NETWORK"
    assert state["last_job"]["retry"]["disposition"] == "RETRY_EXHAUSTED_OPERATOR_ACTION_REQUIRED"


def test_periodic_profile_spec_is_get_only_exact_six_and_total_return_sim_research():
    payload = json.loads(
        (project_root() / "specs/source_collection/s6v5a_periodic_update_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["api_access"] == {
        "method_allowlist": ["GET"], "write_requests": 0, "orders_or_prechecks": 0
    }
    assert [row["instrument_key"] for row in payload["series"]] == [
        "spy", "iwm", "efa", "eem", "vnq", "eurusd"
    ]
    assert payload["schedule"]["first_regular_bar_deadline_et"] == "10:33:00"
    assert payload["schedule"]["configured_fx_instrument_keys"] == ["eurusd", "usdjpy"]
    assert payload["schedule"]["scheduled_fx_instrument_keys"] == ["eurusd"]
    assert payload["schedule"]["excluded_fx_instrument_keys"] == ["usdjpy"]
    assert payload["schedule"]["active_scheduler_scope"] == (
        "specs/source_collection/periodic_scheduler_scope_v1.json"
    )
    assert payload["schedule"]["terminal_blocker_persisted_across_restart"] is True
    fx_profile = json.loads(
        (project_root() / payload["schedule"]["scheduled_fx_universe_profile"]).read_text(
            encoding="utf-8"
        )
    )
    assert [(row["instrument_key"], row["uic"], row["asset_type"]) for row in fx_profile["universe"]] == [
        ("eurusd", 21, "FxSpot"), ("usdjpy", 42, "FxSpot")
    ]
    assert fx_profile["security"]["orders"] == 0
    total_return = payload["total_return"]
    assert total_return["status"] == "READY_SIM_RESEARCH_ONLY"
    assert total_return["scheduled"] is False
    assert total_return["provider"] == "Yahoo Finance chart endpoint"
    assert total_return["research_eligibility"] == "SIM_RESEARCH_ONLY"
    assert total_return["three_session_sla_required"] is False
    assert total_return["formal_provider_acceptance_required"] is False
    assert total_return["launch_agent_acceptance_required"] is False
    assert total_return["development_dataset_promoted"] is False
    assert total_return["operator_decision_required"] is False
    assert total_return["development_dataset_id"] == "20260712T135236Z"
    assert total_return["planned_schedule"] == {
        "t0_eod": True,
        "t1_morning_retry": True,
        "enabled_only_after_provider_contract_freeze": False,
        "sim_research_one_shot_enabled": True,
    }


def test_periodic_implementation_manifest_attests_current_artifacts():
    payload = json.loads(
        (project_root() / "manifests/periodic_market_data_update_implementation_manifest.json")
        .read_text(encoding="utf-8")
    )
    mismatches, valid_paths = manifest_artifact_state(payload)
    assert periodic_update_manifest_baseline_is_valid(payload)
    assert mismatches == []
    assert {
        "market_db/saxo_auth.py",
        "market_db/periodic_update.py",
        "market_db/periodic_update_service.py",
        "specs/source_collection/s6v5a_periodic_update_v1.json",
    } <= valid_paths
