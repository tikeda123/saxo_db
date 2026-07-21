from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_db.connection import project_root
from market_db.read_api_preflight import (
    ALLOWED_REQUEST_PATHS,
    BLOCKED_API_CONTRACT_MISMATCH,
    BLOCKED_DATABASE_UNHEALTHY,
    BLOCKED_PORT_CONFLICT,
    BLOCKED_READ_API_NOT_RUNNING,
    BLOCKED_READ_ONLY_BOUNDARY,
    FAILED_PREFLIGHT_INTERNAL,
    PASS,
    ProcessInfo,
    check_readiness,
    is_expected_process,
    managed_process_matches,
    readiness_exit_code,
)
from market_db.read_api_service import _safe_child_environment, _terminate_owned


class FakeProbe:
    def __init__(self, case: dict):
        self.case = case
        self.requests: list[str] = []
        self.info = ProcessInfo(
            321,
            "/repo/.venv/bin/python -m market_db.read_api --port 8766",
            str(project_root().resolve()),
            "Tue Jul 21 00:00:00 2026",
        )

    def listener_pids(self, port: int) -> list[int]:
        listener = self.case.get("listener")
        if listener == "absent":
            return []
        return [999] if listener == "other" else [321]

    def process_info(self, pid: int):
        if pid == 999:
            return ProcessInfo(pid, "/usr/bin/python -m http.server 8766", "/tmp", "other")
        return self.info

    def managed_state(self):
        return {
            "schema_version": 1,
            "pid": self.info.pid,
            "port": 8766,
            "cwd": self.info.cwd,
            "start_fingerprint": self.info.start_fingerprint,
            "command_sha256": self.info.command_sha256,
        }

    def http_json(self, path: str):
        self.requests.append(path)
        if path == "/":
            revision = "9.9" if self.case.get("root") == "wrong_revision" else "1.2"
            return 200, {
                "service": "saxo_db_read_api", "status": "PASS", "read_only": True,
                "api_version": 1, "contract_revision": revision,
            }
        if path == "/health":
            mode = self.case.get("health")
            role = "saxo_migrator" if mode == "wrong_role" else "saxo_app_reader"
            read_only = "off" if mode == "read_only_off" else "on"
            status = "FAIL" if mode == "unhealthy" else "PASS"
            return (503 if status == "FAIL" else 200), {
                "status": status,
                "database": {
                    "database_name": "saxo_market", "role_name": role,
                    "transaction_read_only": read_only, "statement_timeout": "30s",
                },
            }
        status = int(self.case.get("bars", 400)) if path.endswith("/bars") else int(
            self.case.get("total_return", 400)
        )
        return status, {
            "status": "FAILED",
            "error_code": "INVALID_REQUEST" if status == 400 else "NOT_FOUND",
        }

    def openapi_contract(self):
        return self.case.get("openapi") != "invalid", "a" * 64


def _fixture_cases():
    path = Path(__file__).parent / "fixtures/read_api_operational_readiness_v1/cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("fixture", _fixture_cases(), ids=lambda item: item["id"])
def test_readiness_fixture_statuses_are_fail_closed(fixture):
    probe = FakeProbe(fixture["input"])
    result = check_readiness(probe)
    assert result["status"] == fixture["expected_status"]
    assert readiness_exit_code(result["status"]) == fixture["expected_exit_code"]
    assert result["data_inspection"]["performed"] is False
    assert result["data_inspection"]["market_rows_received"] == 0
    assert result["data_inspection"]["metadata_rows_received"] == 0
    assert set(result["data_inspection"]["request_paths"]) <= set(ALLOWED_REQUEST_PATHS)


def test_preflight_healthy_probe_uses_only_non_data_requests():
    probe = FakeProbe({
        "listener": "expected", "root": "valid", "health": "valid",
        "bars": 400, "total_return": 400, "openapi": "valid",
    })
    result = check_readiness(probe)
    assert result["status"] == PASS
    assert result["service"]["managed"] is True
    assert probe.requests == list(ALLOWED_REQUEST_PATHS)
    assert result["contract"]["bars_route_present"] is True
    assert result["contract"]["total_return_route_present"] is True


def test_process_identity_requires_repo_cwd_module_and_fixed_port():
    valid = FakeProbe({"listener": "expected"}).info
    assert is_expected_process(valid)
    assert not is_expected_process(ProcessInfo(valid.pid, valid.command, "/tmp", valid.start_fingerprint))
    assert not is_expected_process(ProcessInfo(valid.pid, valid.command.replace("8766", "8767"), valid.cwd, valid.start_fingerprint))
    assert not is_expected_process(ProcessInfo(valid.pid, valid.command.replace("market_db.read_api", "http.server"), valid.cwd, valid.start_fingerprint))


def test_managed_state_rejects_pid_reuse_and_stale_fingerprint():
    probe = FakeProbe({"listener": "expected"})
    state = probe.managed_state()
    assert managed_process_matches(state, probe.info)
    state["start_fingerprint"] = "reused"
    assert not managed_process_matches(state, probe.info)


def test_stale_state_never_signals_unmatched_process(monkeypatch):
    probe = FakeProbe({"listener": "expected"})
    state = probe.managed_state()
    state["command_sha256"] = "0" * 64
    signals = []
    monkeypatch.setattr("market_db.read_api_service.os.kill", lambda pid, sig: signals.append((pid, sig)))
    assert _terminate_owned(state, probe, 0.01) is False
    assert signals == []


def test_child_environment_removes_all_market_credentials(monkeypatch):
    for key in ("SAXO_ACCESS_TOKEN", "SAXO_ACCOUNT_KEY", "SAXO_CLIENT_KEY", "SAXO_ACCOUNT_ID"):
        monkeypatch.setenv(key, "secret")
    selected = _safe_child_environment()
    assert not any(key in selected for key in (
        "SAXO_ACCESS_TOKEN", "SAXO_ACCOUNT_KEY", "SAXO_CLIENT_KEY", "SAXO_ACCOUNT_ID"
    ))


def test_contract_and_schema_expose_required_statuses_and_zero_data_invariants():
    contract = json.loads(
        (project_root() / "specs/read_api_operational_readiness_v1.json").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (project_root() / "specs/read_api_operational_readiness_v1.schema.json").read_text(encoding="utf-8")
    )
    expected = {
        PASS, BLOCKED_READ_API_NOT_RUNNING, BLOCKED_PORT_CONFLICT,
        BLOCKED_DATABASE_UNHEALTHY, BLOCKED_READ_ONLY_BOUNDARY,
        BLOCKED_API_CONTRACT_MISMATCH, FAILED_PREFLIGHT_INTERNAL,
    }
    assert set(contract["status_codes"]) == expected
    assert set(schema["properties"]["status"]["enum"]) == expected
    assert contract["preflight"]["market_data_queries"] == 0
    assert contract["preflight"]["metadata_queries"] == 0
