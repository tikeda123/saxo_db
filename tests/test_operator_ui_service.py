from __future__ import annotations

from market_db.connection import project_root
from market_db.operator_ui_service import (
    BLOCKED_PORT_CONFLICT,
    PASS,
    _safe_child_environment,
    _terminate_strictly_matched,
    inspect_service,
    is_expected_operator_process,
    restart_service,
)
from market_db.read_api_preflight import ProcessInfo


class FakeProbe:
    def __init__(self, info: ProcessInfo | None, health: dict | None = None):
        self.info = info
        self.health = health or {
            "status": "PASS",
            "service_id": "saxo_db.operator_ui",
            "bind": "loopback",
            "port": 8765,
        }

    def listener_pids(self, _port):
        return [] if self.info is None else [self.info.pid]

    def process_info(self, pid):
        return self.info if self.info is not None and self.info.pid == pid else None

    def http_json(self, path):
        assert path == "/health"
        return 200, self.health


def expected_info(pid: int = 41) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        command=".venv/bin/python -m market_db.operator_ui --port 8765",
        cwd=str(project_root().resolve()),
        start_fingerprint="2026-07-31T12:00:00",
    )


def test_operator_service_requires_repo_cwd_command_port_and_health():
    info = expected_info()
    assert is_expected_operator_process(info, port=8765)
    result = inspect_service(probe=FakeProbe(info))
    assert result["status"] == PASS
    assert result["safe_to_stop"] is True
    assert result["health_match"] is True

    wrong_command = ProcessInfo(
        info.pid,
        "python -m other.application --port 8765",
        info.cwd,
        info.start_fingerprint,
    )
    blocked = inspect_service(probe=FakeProbe(wrong_command))
    assert blocked["status"] == BLOCKED_PORT_CONFLICT
    assert blocked["safe_to_stop"] is False
    assert blocked["processes"][0]["command_match"] is False

    wrong_health = inspect_service(
        probe=FakeProbe(info, {"status": "PASS", "service_id": "other", "bind": "loopback"})
    )
    assert wrong_health["status"] == BLOCKED_PORT_CONFLICT
    assert wrong_health["safe_to_stop"] is False


def test_restart_never_kills_or_starts_unknown_port_owner():
    calls = []
    info = expected_info()
    unknown = ProcessInfo(
        info.pid,
        "python -m unknown --port 8765",
        "/tmp",
        info.start_fingerprint,
    )

    result = restart_service(
        probe=FakeProbe(unknown),
        kill_func=lambda *_: calls.append("kill"),
        popen_factory=lambda *_args, **_kwargs: calls.append("start"),
    )

    assert result["status"] == BLOCKED_PORT_CONFLICT
    assert result["restarted"] is False
    assert calls == []


def test_strict_termination_rechecks_identity_before_signal(monkeypatch):
    probe = FakeProbe(expected_info())
    signals = []

    def kill(pid, signal):
        signals.append((pid, signal))
        probe.info = None

    monkeypatch.setattr(
        "market_db.operator_ui_service._remove_state", lambda: None
    )
    result = _terminate_strictly_matched(
        port=8765,
        probe=probe,
        kill_func=kill,
    )
    assert result["status"] == PASS
    assert len(signals) == 1


def test_operator_child_environment_drops_all_credentials(monkeypatch):
    for name in (
        "SAXO_ACCESS_TOKEN",
        "SAXO_ACCOUNT_KEY",
        "SAXO_CLIENT_KEY",
        "SAXO_ACCOUNT_ID",
        "SAXO_OAUTH_APP_KEY",
    ):
        monkeypatch.setenv(name, "must-not-propagate")
    selected = _safe_child_environment()
    for name in (
        "SAXO_ACCESS_TOKEN",
        "SAXO_ACCOUNT_KEY",
        "SAXO_CLIENT_KEY",
        "SAXO_ACCOUNT_ID",
        "SAXO_OAUTH_APP_KEY",
    ):
        assert name not in selected
