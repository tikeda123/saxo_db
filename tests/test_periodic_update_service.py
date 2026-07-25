from __future__ import annotations

from market_db.connection import project_root
from market_db.periodic_update_service import (
    _safe_child_environment,
    is_expected_process,
    managed_process_matches,
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
            "serve --callback-port 8764"
        ),
        str(project_root().resolve()),
        "Fri Jul 24 12:00:00 2026",
    )


def test_periodic_service_identity_requires_repo_module_serve_and_port():
    valid = process_info()
    assert is_expected_process(valid, callback_port=8764)
    assert not is_expected_process(
        process_info(valid.command.replace("serve", "status")), callback_port=8764
    )
    assert not is_expected_process(
        process_info(valid.command.replace("8764", "8765")), callback_port=8764
    )
    assert not is_expected_process(
        ProcessInfo(valid.pid, valid.command, "/tmp", valid.start_fingerprint), callback_port=8764
    )


def test_managed_periodic_process_rejects_pid_reuse():
    info = process_info()
    state = {
        "schema_version": 1,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
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
        "schema_version": 1,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "start_fingerprint": "金 7/24 12:00:00 2026",
        "command_sha256": info.command_sha256,
    }
    assert managed_process_matches(state, info)


def test_periodic_process_identity_rejects_cwd_command_and_start_mismatch():
    info = process_info()
    base = {
        "schema_version": 1,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": 8764,
        "start_fingerprint": info.start_fingerprint,
        "command_sha256": info.command_sha256,
    }
    assert not managed_process_matches({**base, "cwd": "/tmp"}, info)
    assert not managed_process_matches({**base, "command_sha256": "0" * 64}, info)
    assert not managed_process_matches(
        {**base, "start_fingerprint": "Fri Jul 24 12:00:01 2026"}, info
    )


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
