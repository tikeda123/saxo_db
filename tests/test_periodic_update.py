from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

from market_db.connection import project_root
from market_db.periodic_update import (
    EQUITY_KEYS,
    EquitySession,
    PeriodicExecutor,
    ScheduleSlot,
    _completed_through,
    _latest_due_per_kind,
    _slot_sla_status,
    build_schedule_slots,
    evaluate_expected_watermarks,
    fully_contained_hour_starts,
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

    assert len(equity) == 6
    assert equity[0].due_at_utc == datetime(2026, 7, 24, 14, 30, 15, tzinfo=timezone.utc)
    assert equity[0].deadline_utc == datetime(2026, 7, 24, 14, 33, tzinfo=timezone.utc)
    assert equity[0].expected_latest_complete["spy"] == datetime(
        2026, 7, 24, 13, 30, tzinfo=timezone.utc
    )
    assert equity[0].expected_latest_complete["eurusd"] == datetime(
        2026, 7, 24, 13, 0, tzinfo=timezone.utc
    )
    assert equity[0].instrument_keys == ("spy", "iwm", "efa", "eem", "vnq", "eurusd")


def test_holiday_has_no_equity_slots_and_fx_remains_hourly():
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    holiday = EquitySession(date(2026, 7, 24), now, now, "HOLIDAY")
    slots = build_schedule_slots(now, [holiday])
    assert not [slot for slot in slots if slot.kind == "equity_regular_1h"]
    fx = [slot for slot in slots if slot.kind == "fx_hourly"]
    assert len(fx) > 0
    assert all(slot.due_at_utc.minute == 3 for slot in fx)


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
    assert fx[-1].expected_latest_complete["eurusd"] == datetime(
        2026, 7, 24, 19, tzinfo=timezone.utc
    )
    assert fx[-1].due_at_utc == datetime(2026, 7, 24, 20, 3, tzinfo=timezone.utc)


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


def test_executor_recovers_one_data_version_change_and_verifies_watermarks():
    due = datetime(2026, 7, 24, 14, 30, 15, tzinfo=timezone.utc)
    expected = {key: due - timedelta(hours=1) for key in EQUITY_KEYS}
    slot = ScheduleSlot(
        "slot", "equity_regular_1h", due, due + timedelta(minutes=3),
        EQUITY_KEYS, expected, "scheduled_test",
    )
    incremental_results = [
        {"status": "BLOCKED", "error_code": "BLOCKED_FULL_REFETCH_REQUIRED", "failed_instrument_key": "spy"},
        {"status": "PASS", "error_code": None, "database_ingestion_run_id": 2},
    ]
    calls = []

    def incremental_runner(**kwargs):
        calls.append(("incremental", kwargs))
        return incremental_results.pop(0)

    def full_refetch_runner(key, **kwargs):
        calls.append(("full_refetch", key, kwargs))
        return {"status": "PASS", "error_code": None, "instrument_key": key}

    executor = PeriodicExecutor(
        FakeOAuth(),
        incremental_runner=incremental_runner,
        full_refetch_runner=full_refetch_runner,
        watermark_loader=lambda keys: dict(expected),
    )
    result = executor.execute(slot)

    assert result["status"] == "PASS"
    assert [step["operation"] for step in result["steps"]] == [
        "incremental", "full_refetch", "incremental_after_full_refetch"
    ]
    assert calls[0][1]["instrument_keys"] == EQUITY_KEYS
    assert calls[1][1] == "spy"
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
