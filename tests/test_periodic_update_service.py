from __future__ import annotations

from market_db.connection import project_root
from market_db.periodic_update import ACTIVE_SCOPE_PROFILE, CANDIDATE_READY_SCOPE_PROFILE
import market_db.periodic_update_service as service_module
from market_db.periodic_update_service import (
    _migrate_matched_identity,
    _safe_child_environment,
    command_identity_sha256,
    is_expected_process,
    managed_process_matches,
    start_service,
)
from market_db.read_api_preflight import (
    ProcessInfo,
    normalize_process_start_fingerprint,
    process_start_fingerprints_match,
)


def process_info(command=None):
    return ProcessInfo(
        1234,
        command or (
            f"{project_root()}/.venv/bin/python -m market_db.periodic_update "
            f"serve --callback-port 8764 --scope-profile {ACTIVE_SCOPE_PROFILE}"
        ),
        str(project_root().resolve()),
        "Fri Jul 24 12:00:00 2026",
    )


def test_periodic_service_identity_requires_repo_module_serve_and_port():
    valid = process_info()
    assert is_expected_process(
        valid, callback_port=8764, scope_profile=ACTIVE_SCOPE_PROFILE
    )
    assert not is_expected_process(
        process_info(valid.command.replace("serve", "status")), callback_port=8764,
        scope_profile=ACTIVE_SCOPE_PROFILE,
    )
    assert not is_expected_process(
        process_info(valid.command.replace("8764", "8765")), callback_port=8764,
        scope_profile=ACTIVE_SCOPE_PROFILE,
    )
    assert not is_expected_process(
        ProcessInfo(valid.pid, valid.command, "/tmp", valid.start_fingerprint),
        callback_port=8764, scope_profile=ACTIVE_SCOPE_PROFILE,
    )
    assert not is_expected_process(
        valid, callback_port=8764, scope_profile="all_managed_series_v1"
    )
    assert not is_expected_process(
        process_info(valid.command.replace(str(project_root() / ".venv/bin/python"), "/bin/sh")),
        callback_port=8764,
        scope_profile=ACTIVE_SCOPE_PROFILE,
    )


def test_managed_periodic_process_rejects_pid_reuse():
    info = process_info()
    state = {
        "schema_version": 2,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "scope_profile": ACTIVE_SCOPE_PROFILE,
        "start_fingerprint": info.start_fingerprint,
        "command_sha256": info.command_sha256,
    }
    assert managed_process_matches(state, info)
    state["start_fingerprint"] = "reused"
    assert not managed_process_matches(state, info)


def test_process_start_fingerprint_is_locale_independent():
    english = "Sat Jul 25 07:34:34 2026"
    japanese = "土 7/25 07:34:34 2026"
    assert normalize_process_start_fingerprint(english) == "2026-07-25T07:34:34"
    assert normalize_process_start_fingerprint(japanese) == "2026-07-25T07:34:34"
    assert process_start_fingerprints_match(english, japanese)


def test_managed_periodic_process_accepts_semantically_equal_locale_fingerprint():
    info = process_info()
    state = {
        "schema_version": 2,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "scope_profile": ACTIVE_SCOPE_PROFILE,
        "start_fingerprint": "金 7/24 12:00:00 2026",
        "command_sha256": info.command_sha256,
    }
    assert managed_process_matches(state, info)


def test_periodic_process_identity_rejects_cwd_command_and_start_mismatch():
    info = process_info()
    base = {
        "schema_version": 2,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "scope_profile": ACTIVE_SCOPE_PROFILE,
        "start_fingerprint": info.start_fingerprint,
        "command_sha256": info.command_sha256,
    }
    assert not managed_process_matches({**base, "cwd": "/tmp"}, info)
    assert not managed_process_matches({**base, "command_sha256": "0" * 64}, info)
    assert not managed_process_matches(
        {**base, "start_fingerprint": "Fri Jul 24 12:00:01 2026"}, info
    )


def test_legacy_identity_accepts_verified_macos_python_argv0_rewrite_and_migrates(
    monkeypatch,
):
    launch_executable = str(project_root() / ".venv/bin/python")
    observed_executable = "/framework/Resources/Python.app/Contents/MacOS/Python"
    tail = (
        "-m market_db.periodic_update serve --callback-port 8764 "
        f"--scope-profile {ACTIVE_SCOPE_PROFILE}"
    )
    launched = process_info(f"{launch_executable} {tail}")
    observed = process_info(f"{observed_executable} {tail}")
    state = {
        "schema_version": 2,
        "owner": "saxo_db.periodic_update_service",
        "pid": observed.pid,
        "cwd": observed.cwd,
        "callback_port": 8764,
        "scope_profile": ACTIVE_SCOPE_PROFILE,
        "start_fingerprint": observed.start_fingerprint,
        "command_sha256": launched.command_sha256,
    }
    monkeypatch.setattr(
        service_module,
        "_allowed_python_executables",
        lambda: frozenset({launch_executable, observed_executable}),
    )
    written = []
    monkeypatch.setattr(service_module, "_write_service_state", written.append)

    assert managed_process_matches(state, observed)
    migrated = _migrate_matched_identity(state, observed)

    assert migrated["schema_version"] == 3
    assert "command_sha256" not in migrated
    assert migrated["command_identity_sha256"] == command_identity_sha256(
        observed.command
    )
    assert written == [migrated]
    assert managed_process_matches(migrated, observed)


def test_semantic_identity_still_rejects_command_argument_change():
    info = process_info()
    state = {
        "schema_version": 3,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "scope_profile": ACTIVE_SCOPE_PROFILE,
        "start_fingerprint": info.start_fingerprint,
        "command_identity_sha256": command_identity_sha256(info.command),
    }
    assert managed_process_matches(state, info)
    changed = process_info(info.command.replace("8764", "8765"))
    assert not managed_process_matches(state, changed)


def test_periodic_child_environment_removes_market_credentials_but_keeps_oauth_app_key(monkeypatch):
    for key in (
        "SAXO_ACCESS_TOKEN", "SAXO_ACCOUNT_KEY", "SAXO_CLIENT_KEY", "SAXO_ACCOUNT_ID"
    ):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("SAXO_OAUTH_APP_KEY", "public-client-id")
    selected = _safe_child_environment()
    assert not any(key in selected for key in (
        "SAXO_ACCESS_TOKEN", "SAXO_ACCOUNT_KEY", "SAXO_CLIENT_KEY", "SAXO_ACCOUNT_ID"
    ))
    assert selected["SAXO_OAUTH_APP_KEY"] == "public-client-id"
    assert selected["LC_ALL"] == selected["LANG"] == "C"


def test_candidate_scope_start_is_blocked_before_process_spawn_until_all_pairs_publish(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "candidate_scope_readiness",
        lambda: {
            "status": "BLOCKED_CANDIDATE_SCOPE_NOT_READY",
            "candidate_states": {"audusd": {"publication_status": "STAGING"}},
            "orders_or_prechecks_sent": 0,
        },
    )

    result = start_service(scope_profile=CANDIDATE_READY_SCOPE_PROFILE)

    assert result["status"] == "BLOCKED_CANDIDATE_SCOPE_NOT_READY"
    assert result["readiness"]["orders_or_prechecks_sent"] == 0
