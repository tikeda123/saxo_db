"""Repository-owned lifecycle for the unattended periodic updater."""

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
from typing import Any, Iterable

from .connection import project_root
from .periodic_update import load_state as load_scheduler_state
from .periodic_update import runtime_dir
from .read_api_preflight import (
    ProcessInfo,
    SystemReadinessProbe,
    normalize_process_start_fingerprint,
    process_start_fingerprints_match,
)
from .read_api_service import postgres_healthy
from .saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoAuthError, SaxoOAuthManager


SERVICE_STATE_FILENAME = "service.json"
LOG_FILENAME = "periodic_update.log"
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.2
REMOVED_CREDENTIAL_ENV_KEYS = (
    "SAXO_ACCESS_TOKEN",
    "SAXO_ACCOUNT_KEY",
    "SAXO_CLIENT_KEY",
    "SAXO_ACCOUNT_ID",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def service_state_path() -> Path:
    return runtime_dir() / SERVICE_STATE_FILENAME


def log_path() -> Path:
    return runtime_dir() / LOG_FILENAME


def _ensure_runtime_dir() -> None:
    selected = runtime_dir()
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(selected, 0o700)


def _load_service_state() -> dict[str, Any] | None:
    selected = service_state_path()
    if not selected.is_file() or selected.is_symlink():
        return None
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_service_state(payload: dict[str, Any]) -> None:
    _ensure_runtime_dir()
    selected = service_state_path()
    temporary = selected.with_name(f".{SERVICE_STATE_FILENAME}.{os.getpid()}.tmp")
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


def _remove_service_state() -> None:
    selected = service_state_path()
    if selected.is_file() and not selected.is_symlink():
        selected.unlink()


def _safe_child_environment() -> dict[str, str]:
    selected = os.environ.copy()
    for key in REMOVED_CREDENTIAL_ENV_KEYS:
        selected.pop(key, None)
    selected["PYTHONUNBUFFERED"] = "1"
    selected["LC_ALL"] = "C"
    selected["LANG"] = "C"
    return selected


def is_expected_process(info: ProcessInfo | None, *, callback_port: int) -> bool:
    if info is None or info.cwd != str(project_root().resolve()):
        return False
    try:
        tokens = shlex.split(info.command)
    except ValueError:
        return False
    module_match = any(
        tokens[index:index + 2] == ["-m", "market_db.periodic_update"]
        for index in range(max(0, len(tokens) - 1))
    )
    serve_match = "serve" in tokens
    port_match = any(
        tokens[index:index + 2] == ["--callback-port", str(callback_port)]
        for index in range(max(0, len(tokens) - 1))
    )
    return module_match and serve_match and port_match


def managed_process_matches(state: dict[str, Any] | None, info: ProcessInfo | None) -> bool:
    if state is None or info is None:
        return False
    port = state.get("callback_port")
    return (
        state.get("schema_version") == 1
        and state.get("owner") == "saxo_db.periodic_update_service"
        and state.get("pid") == info.pid
        and state.get("cwd") == info.cwd
        and process_start_fingerprints_match(
            state.get("start_fingerprint"), info.start_fingerprint
        )
        and state.get("command_sha256") == info.command_sha256
        and isinstance(port, int)
        and is_expected_process(info, callback_port=port)
    )


def _migrate_matched_start_fingerprint(
    state: dict[str, Any], info: ProcessInfo
) -> dict[str, Any]:
    """Rewrite only a fully matched legacy locale-dependent fingerprint."""

    canonical = normalize_process_start_fingerprint(info.start_fingerprint)
    stored = state.get("start_fingerprint")
    if canonical is None or stored == canonical:
        return state
    migrated = dict(state)
    migrated["start_fingerprint"] = canonical
    migrated["identity_migrated_at_utc"] = _utc_now()
    _write_service_state(migrated)
    return migrated


def _result(operation: str, status: str, **values: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "checked_at_utc": _utc_now(),
        "orders_or_prechecks_sent": 0,
        **values,
    }


def _wait_process(probe: SystemReadinessProbe, pid: int) -> ProcessInfo | None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        info = probe.process_info(pid)
        if info is not None:
            return info
        time.sleep(POLL_SECONDS)
    return None


def _wait_gone(probe: SystemReadinessProbe, pid: int) -> bool:
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if probe.process_info(pid) is None:
            return True
        time.sleep(POLL_SECONDS)
    return probe.process_info(pid) is None


def _auth_readiness(callback_port: int) -> dict[str, Any]:
    try:
        manager = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=callback_port))
        return manager.status()
    except SaxoAuthError as exc:
        return {
            "status": exc.code,
            "environment": "SIM",
            "token_values_exposed": False,
            "orders_or_prechecks_sent": 0,
        }


def start_service(*, callback_port: int = DEFAULT_CALLBACK_PORT) -> dict[str, Any]:
    probe = SystemReadinessProbe()
    previous = _load_service_state()
    if previous is not None:
        pid = previous.get("pid")
        info = probe.process_info(pid) if isinstance(pid, int) else None
        if managed_process_matches(previous, info):
            previous = _migrate_matched_start_fingerprint(previous, info)
            return _result("start", "PASS", idempotent=True, pid=pid, scheduler=load_scheduler_state())
        if info is not None:
            return _result("start", "BLOCKED_STALE_PID", idempotent=False)
        _remove_service_state()

    if not postgres_healthy():
        return _result("start", "BLOCKED_DATABASE_UNHEALTHY", idempotent=False)
    auth = _auth_readiness(callback_port)
    if auth.get("status") != "AUTH_READY":
        return _result("start", str(auth.get("status")), idempotent=False, auth=auth)

    _ensure_runtime_dir()
    command = [
        sys.executable, "-m", "market_db.periodic_update", "serve",
        "--callback-port", str(callback_port),
    ]
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
        return _result("start", "FAILED_SERVICE_START", idempotent=False)

    info = _wait_process(probe, process.pid)
    if info is None or not is_expected_process(info, callback_port=callback_port):
        process.terminate()
        return _result("start", "FAILED_SERVICE_IDENTITY", idempotent=False)
    service_state = {
        "schema_version": 1,
        "owner": "saxo_db.periodic_update_service",
        "pid": info.pid,
        "cwd": info.cwd,
        "callback_port": callback_port,
        "start_fingerprint": info.start_fingerprint,
        "command_sha256": info.command_sha256,
        "started_at_utc": _utc_now(),
        "app_key_fingerprint": auth.get("app_key_fingerprint"),
    }
    _write_service_state(service_state)

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        scheduler = load_scheduler_state()
        if (
            scheduler is not None
            and scheduler.get("service_status") == "RUNNING"
            and scheduler.get("pid") == process.pid
        ):
            return _result(
                "start", "PASS", idempotent=False, pid=process.pid,
                auth=auth, scheduler=scheduler,
            )
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    if probe.process_info(process.pid) is not None:
        os.kill(process.pid, signal.SIGTERM)
        _wait_gone(probe, process.pid)
    _remove_service_state()
    return _result("start", "FAILED_SERVICE_START_TIMEOUT", idempotent=False)


def status_service() -> dict[str, Any]:
    probe = SystemReadinessProbe()
    state = _load_service_state()
    if state is None:
        return _result("status", "STOPPED", managed=False, scheduler=load_scheduler_state())
    pid = state.get("pid")
    info = probe.process_info(pid) if isinstance(pid, int) else None
    if not managed_process_matches(state, info):
        return _result(
            "status", "BLOCKED_STALE_PID", managed=False,
            state_removed=False, scheduler=load_scheduler_state(),
        )
    state = _migrate_matched_start_fingerprint(state, info)
    return _result(
        "status", "PASS", managed=True, pid=pid,
        scheduler=load_scheduler_state(),
    )


def stop_service() -> dict[str, Any]:
    probe = SystemReadinessProbe()
    state = _load_service_state()
    if state is None:
        return _result("stop", "PASS", idempotent=True)
    pid = state.get("pid")
    info = probe.process_info(pid) if isinstance(pid, int) else None
    if not managed_process_matches(state, info):
        if info is None:
            _remove_service_state()
        return _result("stop", "BLOCKED_STALE_PID", idempotent=False, state_removed=info is None)
    os.kill(pid, signal.SIGTERM)
    if not _wait_gone(probe, pid):
        return _result("stop", "BLOCKED_STOP_TIMEOUT", idempotent=False)
    _remove_service_state()
    return _result("stop", "PASS", idempotent=False, pid=pid)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage unattended Saxo periodic updates")
    parser.add_argument("operation", choices=("start", "status", "stop"))
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.operation == "start":
        result = start_service(callback_port=args.callback_port)
    elif args.operation == "stop":
        result = stop_service()
    else:
        result = status_service()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    status = str(result["status"])
    return 0 if status in {"PASS", "STOPPED"} else (2 if status.startswith("BLOCKED_") or status.startswith("AUTH_") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
