"""Safe repository-owned lifecycle for the loopback Read API."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .connection import project_root
from .read_api import DEFAULT_PORT, LOOPBACK_HOST
from .read_api_preflight import (
    BLOCKED_DATABASE_UNHEALTHY,
    BLOCKED_PORT_CONFLICT,
    BLOCKED_READ_API_NOT_RUNNING,
    FAILED_PREFLIGHT_INTERNAL,
    PASS,
    ProcessInfo,
    SystemReadinessProbe,
    check_readiness,
    command_identity_sha256,
    is_expected_process,
    managed_process_matches,
    normalize_process_start_fingerprint,
    readiness_exit_code,
    utc_now,
)


RUNTIME_RELATIVE_PATH = Path(".runtime/read_api")
STATE_FILENAME = "state.json"
LOG_FILENAME = "read_api.log"
START_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.2
REMOVED_CREDENTIAL_ENV_KEYS = (
    "SAXO_ACCESS_TOKEN",
    "SAXO_ACCOUNT_KEY",
    "SAXO_CLIENT_KEY",
    "SAXO_ACCOUNT_ID",
)


def runtime_dir() -> Path:
    return project_root() / RUNTIME_RELATIVE_PATH


def state_path() -> Path:
    return runtime_dir() / STATE_FILENAME


def log_path() -> Path:
    return runtime_dir() / LOG_FILENAME


def _ensure_runtime_dir() -> None:
    path = runtime_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _load_state() -> dict[str, Any] | None:
    path = state_path()
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_state(payload: dict[str, Any]) -> None:
    _ensure_runtime_dir()
    path = state_path()
    temporary = path.with_name(f".{STATE_FILENAME}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_state() -> None:
    path = state_path()
    if path.is_file() and not path.is_symlink():
        path.unlink()


def _safe_child_environment() -> dict[str, str]:
    selected = os.environ.copy()
    for key in REMOVED_CREDENTIAL_ENV_KEYS:
        selected.pop(key, None)
    selected["PYTHONUNBUFFERED"] = "1"
    return selected


def postgres_healthy() -> bool:
    try:
        completed = subprocess.run(
            [
                "docker", "compose", "-p", "saxo-market-data", "ps",
                "--format", "json", "postgres",
            ],
            cwd=project_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0 or not completed.stdout.strip():
        return False
    try:
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    return len(rows) == 1 and rows[0].get("State") == "running" and rows[0].get("Health") == "healthy"


def _process_state(info: ProcessInfo) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "owner": "saxo_db.read_api_service",
        "pid": info.pid,
        "port": DEFAULT_PORT,
        "host": LOOPBACK_HOST,
        "cwd": info.cwd,
        "start_fingerprint": info.start_fingerprint,
        "command_identity_sha256": command_identity_sha256(info.command),
        "started_at_utc": utc_now(),
        "command_id": "market_db.read_api",
    }


def _migrate_matched_identity(
    state: dict[str, Any], info: ProcessInfo
) -> dict[str, Any]:
    """Rewrite a fully matched legacy state using semantic command identity."""

    canonical = normalize_process_start_fingerprint(info.start_fingerprint)
    legacy = state.get("schema_version") == 1
    if canonical is None or (
        not legacy and state.get("start_fingerprint") == canonical
    ):
        return state
    migrated = dict(state)
    if legacy:
        migrated["schema_version"] = 2
        migrated["owner"] = "saxo_db.read_api_service"
        migrated.pop("command_sha256", None)
        migrated["command_identity_sha256"] = command_identity_sha256(info.command)
    migrated["start_fingerprint"] = canonical
    migrated["identity_migrated_at_utc"] = utc_now()
    _write_state(migrated)
    return migrated


def _result(operation: str, status: str, **values: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "checked_at_utc": utc_now(),
        **values,
    }


def _wait_for_process_info(
    probe: SystemReadinessProbe, pid: int, timeout_seconds: float = 2.0
) -> ProcessInfo | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        info = probe.process_info(pid)
        if info is not None:
            return info
        time.sleep(POLL_SECONDS)
    return None


def _wait_until_gone(
    probe: SystemReadinessProbe, pid: int, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if probe.process_info(pid) is None:
            return True
        time.sleep(POLL_SECONDS)
    return probe.process_info(pid) is None


def _terminate_owned(
    state: dict[str, Any], probe: SystemReadinessProbe, timeout_seconds: float
) -> bool:
    pid = state.get("pid")
    if not isinstance(pid, int) or pid < 1:
        return False
    info = probe.process_info(pid)
    if not managed_process_matches(state, info):
        return False
    os.kill(pid, signal.SIGTERM)
    return _wait_until_gone(probe, pid, timeout_seconds)


def start_service() -> dict[str, Any]:
    probe = SystemReadinessProbe()
    previous_state = _load_state()
    if previous_state is not None:
        previous_pid = previous_state.get("pid")
        previous_info = (
            probe.process_info(previous_pid)
            if isinstance(previous_pid, int) and previous_pid > 0
            else None
        )
        if managed_process_matches(previous_state, previous_info):
            _migrate_matched_identity(previous_state, previous_info)
    readiness = check_readiness(probe)
    if readiness["status"] == PASS:
        return _result(
            "start", PASS, idempotent=True,
            managed=readiness["service"]["managed"], readiness=readiness,
        )
    if readiness["status"] != BLOCKED_READ_API_NOT_RUNNING:
        return _result("start", str(readiness["status"]), idempotent=False, readiness=readiness)
    if not postgres_healthy():
        return _result(
            "start", BLOCKED_DATABASE_UNHEALTHY, idempotent=False,
            readiness=readiness,
        )

    previous_state = _load_state()
    if previous_state is not None:
        previous_pid = previous_state.get("pid")
        previous_info = (
            probe.process_info(previous_pid)
            if isinstance(previous_pid, int) and previous_pid > 0
            else None
        )
        if managed_process_matches(previous_state, previous_info):
            return _result(
                "start", BLOCKED_READ_API_NOT_RUNNING, idempotent=False,
                diagnostic_code="MANAGED_PROCESS_WITHOUT_HEALTHY_LISTENER",
            )
        _remove_state()

    _ensure_runtime_dir()
    command = [sys.executable, "-m", "market_db.read_api", "--port", str(DEFAULT_PORT)]
    descriptor = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "ab", buffering=0) as output:
            process = subprocess.Popen(
                command,
                cwd=project_root(),
                env=_safe_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
    except OSError:
        return _result("start", FAILED_PREFLIGHT_INTERNAL, idempotent=False)

    info = _wait_for_process_info(probe, process.pid)
    if info is None or not is_expected_process(info):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        _remove_state()
        return _result("start", FAILED_PREFLIGHT_INTERNAL, idempotent=False)
    state = _process_state(info)
    _write_state(state)

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    final_readiness = readiness
    while time.monotonic() < deadline:
        final_readiness = check_readiness(probe)
        if final_readiness["status"] == PASS:
            return _result(
                "start", PASS, idempotent=False, managed=True,
                pid=process.pid, readiness=final_readiness,
            )
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)

    _terminate_owned(state, probe, STOP_TIMEOUT_SECONDS)
    _remove_state()
    return _result(
        "start", str(final_readiness["status"]), idempotent=False,
        readiness=final_readiness,
    )


def status_service() -> dict[str, Any]:
    probe = SystemReadinessProbe()
    state = _load_state()
    if state is not None:
        pid = state.get("pid")
        info = probe.process_info(pid) if isinstance(pid, int) and pid > 0 else None
        if managed_process_matches(state, info):
            _migrate_matched_identity(state, info)
    return check_readiness(probe)


def stop_service() -> dict[str, Any]:
    probe = SystemReadinessProbe()
    state = _load_state()
    readiness_before = check_readiness(probe)
    if state is None:
        if readiness_before["status"] == BLOCKED_READ_API_NOT_RUNNING:
            return _result(
                "stop", PASS, idempotent=True, postgres_healthy=postgres_healthy()
            )
        if readiness_before["status"] == BLOCKED_PORT_CONFLICT:
            return _result("stop", BLOCKED_PORT_CONFLICT, idempotent=False)
        return _result(
            "stop", "BLOCKED_READ_API_NOT_MANAGED", idempotent=False,
            readiness=readiness_before,
        )

    pid = state.get("pid")
    info = probe.process_info(pid) if isinstance(pid, int) and pid > 0 else None
    if not managed_process_matches(state, info):
        if info is None:
            _remove_state()
        return _result(
            "stop", "BLOCKED_STALE_PID", idempotent=False,
            state_removed=info is None,
        )

    if not _terminate_owned(state, probe, STOP_TIMEOUT_SECONDS):
        return _result("stop", "BLOCKED_STOP_TIMEOUT", idempotent=False)
    _remove_state()
    listeners = probe.listener_pids(DEFAULT_PORT)
    if listeners:
        return _result("stop", BLOCKED_PORT_CONFLICT, idempotent=False)
    return _result(
        "stop", PASS, idempotent=False, pid=pid,
        postgres_healthy=postgres_healthy(), data_mutation_commands=0,
    )


def service_exit_code(status: str) -> int:
    return readiness_exit_code(status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the loopback Read API process")
    parser.add_argument("operation", choices=("start", "status", "stop"))
    parser.add_argument("--format", choices=("json",), default="json")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.operation == "start":
        result = start_service()
    elif args.operation == "stop":
        result = stop_service()
    else:
        result = status_service()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return service_exit_code(str(result["status"]))


if __name__ == "__main__":
    raise SystemExit(main())
