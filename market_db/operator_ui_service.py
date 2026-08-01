"""Strict repository-owned lifecycle for the loopback Operator UI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .connection import project_root
from .operator_ui import DEFAULT_PORT, LOOPBACK_HOST
from .read_api_preflight import ProcessInfo, SystemReadinessProbe


RUNTIME_RELATIVE_PATH = Path(".runtime/operator_ui")
STATE_FILENAME = "service.json"
LOG_FILENAME = "operator_ui.log"
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.2
PASS = "PASS"
STOPPED = "STOPPED"
BLOCKED_PORT_CONFLICT = "BLOCKED_PORT_CONFLICT_UNKNOWN_PROCESS"
BLOCKED_STOP_TIMEOUT = "BLOCKED_OPERATOR_UI_STOP_TIMEOUT"
FAILED_START = "FAILED_OPERATOR_UI_START"
REMOVED_CREDENTIAL_ENV_KEYS = (
    "SAXO_ACCESS_TOKEN",
    "SAXO_ACCOUNT_KEY",
    "SAXO_CLIENT_KEY",
    "SAXO_ACCOUNT_ID",
    "SAXO_OAUTH_APP_KEY",
)


class OperatorServiceProbe(Protocol):
    def listener_pids(self, port: int) -> list[int]: ...

    def process_info(self, pid: int) -> ProcessInfo | None: ...

    def http_json(self, path: str) -> tuple[int, dict[str, Any]]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_dir() -> Path:
    return project_root() / RUNTIME_RELATIVE_PATH


def state_path() -> Path:
    return runtime_dir() / STATE_FILENAME


def log_path() -> Path:
    return runtime_dir() / LOG_FILENAME


def _ensure_runtime_dir() -> None:
    selected = runtime_dir()
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(selected, 0o700)


def _write_state(info: ProcessInfo, port: int) -> None:
    _ensure_runtime_dir()
    payload = {
        "schema_version": 1,
        "owner": "saxo_db.operator_ui_service",
        "pid": info.pid,
        "port": port,
        "cwd": info.cwd,
        "start_fingerprint": info.start_fingerprint,
        "command_sha256": info.command_sha256,
        "started_at_utc": _utc_now(),
    }
    selected = state_path()
    temporary = selected.with_name(f".{STATE_FILENAME}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selected)
        os.chmod(selected, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_state() -> None:
    selected = state_path()
    if selected.is_file() and not selected.is_symlink():
        selected.unlink()


def _safe_child_environment() -> dict[str, str]:
    selected = os.environ.copy()
    for key in REMOVED_CREDENTIAL_ENV_KEYS:
        selected.pop(key, None)
    selected.update({"PYTHONUNBUFFERED": "1", "LC_ALL": "C", "LANG": "C"})
    return selected


def is_expected_operator_process(info: ProcessInfo | None, *, port: int) -> bool:
    if info is None or info.cwd != str(project_root().resolve()):
        return False
    try:
        tokens = shlex.split(info.command)
    except ValueError:
        return False
    module_match = any(
        tokens[index:index + 2] == ["-m", "market_db.operator_ui"]
        for index in range(max(0, len(tokens) - 1))
    )
    port_match = any(
        tokens[index:index + 2] == ["--port", str(port)]
        for index in range(max(0, len(tokens) - 1))
    )
    return module_match and port_match


def _health_matches(probe: OperatorServiceProbe, *, port: int) -> bool:
    try:
        status, payload = probe.http_json("/health")
    except Exception:
        return False
    service_id = payload.get("service_id")
    return (
        status == 200
        and payload.get("status") == PASS
        and payload.get("bind") == "loopback"
        and (service_id is None or service_id == "saxo_db.operator_ui")
        and (payload.get("port") is None or payload.get("port") == port)
    )


def _sanitized_identity(info: ProcessInfo | None, *, port: int) -> dict[str, Any]:
    if info is None:
        return {
            "pid": None,
            "cwd": None,
            "repo_cwd_match": False,
            "command_match": False,
            "command_sha256": None,
        }
    return {
        "pid": info.pid,
        "cwd": info.cwd,
        "repo_cwd_match": info.cwd == str(project_root().resolve()),
        "command_match": is_expected_operator_process(info, port=port),
        "command_sha256": info.command_sha256,
    }


def inspect_service(
    *,
    port: int = DEFAULT_PORT,
    probe: OperatorServiceProbe | None = None,
) -> dict[str, Any]:
    selected = probe or SystemReadinessProbe(port=port)
    listeners = selected.listener_pids(port)
    if not listeners:
        return {
            "status": STOPPED,
            "port": port,
            "listener_count": 0,
            "safe_to_start": True,
            "safe_to_stop": False,
        }
    identities = [_sanitized_identity(selected.process_info(pid), port=port) for pid in listeners]
    if len(listeners) != 1:
        return {
            "status": BLOCKED_PORT_CONFLICT,
            "port": port,
            "listener_count": len(listeners),
            "processes": identities,
            "safe_to_start": False,
            "safe_to_stop": False,
        }
    info = selected.process_info(listeners[0])
    command_and_cwd_match = is_expected_operator_process(info, port=port)
    health_match = _health_matches(selected, port=port) if command_and_cwd_match else False
    if not command_and_cwd_match or not health_match:
        return {
            "status": BLOCKED_PORT_CONFLICT,
            "port": port,
            "listener_count": 1,
            "processes": identities,
            "health_match": health_match,
            "safe_to_start": False,
            "safe_to_stop": False,
        }
    return {
        "status": PASS,
        "port": port,
        "listener_count": 1,
        "process": identities[0],
        "health_match": True,
        "safe_to_start": False,
        "safe_to_stop": True,
        "url": f"http://{LOOPBACK_HOST}:{port}/",
    }


def _same_process(first: ProcessInfo, second: ProcessInfo | None, *, port: int) -> bool:
    return (
        second is not None
        and first.pid == second.pid
        and first.cwd == second.cwd
        and first.start_fingerprint == second.start_fingerprint
        and first.command_sha256 == second.command_sha256
        and is_expected_operator_process(second, port=port)
    )


def _wait_until_gone(probe: OperatorServiceProbe, pid: int) -> bool:
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if probe.process_info(pid) is None:
            return True
        time.sleep(POLL_SECONDS)
    return probe.process_info(pid) is None


def _terminate_strictly_matched(
    *,
    port: int,
    probe: OperatorServiceProbe,
    kill_func: Callable[[int, int], None] = os.kill,
) -> dict[str, Any]:
    inspected = inspect_service(port=port, probe=probe)
    if inspected["status"] != PASS:
        return inspected
    pid = inspected["process"]["pid"]
    assert isinstance(pid, int)
    first = probe.process_info(pid)
    listeners = probe.listener_pids(port)
    second = probe.process_info(pid)
    if (
        first is None
        or listeners != [pid]
        or not _same_process(first, second, port=port)
        or not _health_matches(probe, port=port)
    ):
        return {
            "status": BLOCKED_PORT_CONFLICT,
            "port": port,
            "safe_to_stop": False,
            "diagnostic": "PROCESS_IDENTITY_CHANGED_BEFORE_STOP",
        }
    kill_func(pid, signal.SIGTERM)
    if not _wait_until_gone(probe, pid):
        return {"status": BLOCKED_STOP_TIMEOUT, "port": port, "safe_to_stop": False}
    _remove_state()
    return {"status": PASS, "port": port, "stopped_pid": pid}


def start_service(
    *,
    port: int = DEFAULT_PORT,
    probe: OperatorServiceProbe | None = None,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> dict[str, Any]:
    selected = probe or SystemReadinessProbe(port=port)
    inspected = inspect_service(port=port, probe=selected)
    if inspected["status"] == PASS:
        return {"operation": "start", "idempotent": True, **inspected}
    if inspected["status"] != STOPPED:
        return {"operation": "start", "idempotent": False, **inspected}
    _ensure_runtime_dir()
    command = [sys.executable, "-m", "market_db.operator_ui", "--port", str(port)]
    descriptor = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "ab", buffering=0) as output:
            process = popen_factory(
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
        return {"operation": "start", "status": FAILED_START, "port": port}

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        info = selected.process_info(process.pid)
        if info is not None and is_expected_operator_process(info, port=port):
            _write_state(info, port)
        current = inspect_service(port=port, probe=selected)
        if current["status"] == PASS:
            return {
                "operation": "start",
                "idempotent": False,
                "started_pid": process.pid,
                "db_writes_performed": 0,
                "orders_or_prechecks_sent": 0,
                **current,
            }
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    info = selected.process_info(process.pid)
    if info is not None and is_expected_operator_process(info, port=port):
        process.terminate()
    _remove_state()
    return {"operation": "start", "status": FAILED_START, "port": port}


def restart_service(
    *,
    port: int = DEFAULT_PORT,
    probe: OperatorServiceProbe | None = None,
    kill_func: Callable[[int, int], None] = os.kill,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> dict[str, Any]:
    selected = probe or SystemReadinessProbe(port=port)
    before = inspect_service(port=port, probe=selected)
    if before["status"] == BLOCKED_PORT_CONFLICT:
        return {"operation": "restart", "restarted": False, **before}
    if before["status"] == PASS:
        stopped = _terminate_strictly_matched(
            port=port, probe=selected, kill_func=kill_func
        )
        if stopped["status"] != PASS:
            return {"operation": "restart", "restarted": False, **stopped}
    started = start_service(port=port, probe=selected, popen_factory=popen_factory)
    return {
        **started,
        "operation": "restart",
        "restarted": started.get("status") == PASS,
    }


def stop_service(
    *,
    port: int = DEFAULT_PORT,
    probe: OperatorServiceProbe | None = None,
) -> dict[str, Any]:
    selected = probe or SystemReadinessProbe(port=port)
    inspected = inspect_service(port=port, probe=selected)
    if inspected["status"] == STOPPED:
        return {"operation": "stop", "status": PASS, "idempotent": True, "port": port}
    result = _terminate_strictly_matched(port=port, probe=selected)
    return {"operation": "stop", "idempotent": False, **result}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe loopback Operator UI lifecycle")
    parser.add_argument("command", choices=("start", "restart", "status", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not 1_024 <= args.port <= 65_535:
        result = {"operation": args.command, "status": "BLOCKED_INVALID_PORT"}
    elif args.command == "start":
        result = start_service(port=args.port)
    elif args.command == "restart":
        result = restart_service(port=args.port)
    elif args.command == "stop":
        result = stop_service(port=args.port)
    else:
        result = {"operation": "status", **inspect_service(port=args.port)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {PASS, STOPPED} else 2


if __name__ == "__main__":
    raise SystemExit(main())
