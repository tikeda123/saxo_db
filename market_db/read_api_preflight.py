"""Non-data operational readiness gate for the loopback Read API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .connection import MARKET_DB, project_root
from .read_api import API_VERSION, CONTRACT_REVISION, DEFAULT_PORT, LOOPBACK_HOST


SCHEMA_VERSION = 1
EXPECTED_ROLE = "saxo_app_reader"
EXPECTED_READ_ONLY = "on"
EXPECTED_STATEMENT_TIMEOUT = "30s"
EXPECTED_MODULE = "market_db.read_api"
OPENAPI_RELATIVE_PATH = "specs/read_api_v1_openapi.yaml"
ALLOWED_REQUEST_PATHS = (
    "/",
    "/health",
    "/api/v1/bars",
    "/api/v1/total-return",
)
PASS = "PASS"
BLOCKED_READ_API_NOT_RUNNING = "BLOCKED_READ_API_NOT_RUNNING"
BLOCKED_PORT_CONFLICT = "BLOCKED_PORT_CONFLICT"
BLOCKED_DATABASE_UNHEALTHY = "BLOCKED_DATABASE_UNHEALTHY"
BLOCKED_READ_ONLY_BOUNDARY = "BLOCKED_READ_ONLY_BOUNDARY"
BLOCKED_API_CONTRACT_MISMATCH = "BLOCKED_API_CONTRACT_MISMATCH"
FAILED_PREFLIGHT_INTERNAL = "FAILED_PREFLIGHT_INTERNAL"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command: str
    cwd: str | None
    start_fingerprint: str

    @property
    def command_sha256(self) -> str:
        return sha256_bytes(self.command.encode("utf-8"))


class ProbeError(RuntimeError):
    pass


class ReadinessProbe(Protocol):
    def listener_pids(self, port: int) -> list[int]: ...

    def process_info(self, pid: int) -> ProcessInfo | None: ...

    def managed_state(self) -> dict[str, Any] | None: ...

    def http_json(self, path: str) -> tuple[int, dict[str, Any]]: ...

    def openapi_contract(self) -> tuple[bool, str | None]: ...


def _run_text(command: list[str]) -> str:
    environment = os.environ.copy()
    # macOS ps(1) localizes lstart.  Process identity must not change merely
    # because the operator shell uses a different locale.
    environment.update({"LC_ALL": "C", "LANG": "C"})
    completed = subprocess.run(
        command,
        cwd=project_root(),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=3,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


_ENGLISH_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_ENGLISH_LSTART = re.compile(
    r"^[A-Za-z]{3}\s+(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+(?P<year>\d{4})$"
)
_JAPANESE_LSTART = re.compile(
    r"^[日月火水木金土]\s+(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+(?P<year>\d{4})$"
)
_CANONICAL_LSTART = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def normalize_process_start_fingerprint(value: str) -> str | None:
    """Normalize macOS ps lstart output without consulting process locale."""

    selected = " ".join(str(value).split())
    if _CANONICAL_LSTART.fullmatch(selected):
        return selected
    english = _ENGLISH_LSTART.fullmatch(selected)
    if english is not None:
        month = _ENGLISH_MONTHS.get(english.group("month").title())
        if month is None:
            return None
        values = {key: int(english.group(key)) for key in ("year", "day", "hour", "minute", "second")}
        values["month"] = month
    else:
        japanese = _JAPANESE_LSTART.fullmatch(selected)
        if japanese is None:
            return None
        values = {
            key: int(japanese.group(key))
            for key in ("year", "month", "day", "hour", "minute", "second")
        }
    try:
        parsed = datetime(**values)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def process_start_fingerprints_match(stored: object, observed: object) -> bool:
    if not isinstance(stored, str) or not isinstance(observed, str):
        return False
    stored_normalized = normalize_process_start_fingerprint(stored)
    observed_normalized = normalize_process_start_fingerprint(observed)
    if stored_normalized is not None and observed_normalized is not None:
        return stored_normalized == observed_normalized
    # Unknown legacy forms are accepted only when byte-for-byte equal.
    return stored == observed


def _process_cwd(pid: int) -> str | None:
    output = _run_text(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in output.splitlines():
        if line.startswith("n") and len(line) > 1:
            return str(Path(line[1:]).resolve())
    return None


class SystemReadinessProbe:
    def __init__(self, *, port: int = DEFAULT_PORT, timeout_seconds: float = 2.0) -> None:
        self.port = port
        self.timeout_seconds = timeout_seconds

    def listener_pids(self, port: int) -> list[int]:
        output = _run_text(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
        )
        return sorted({int(line) for line in output.splitlines() if line.isdigit()})

    def process_info(self, pid: int) -> ProcessInfo | None:
        if pid < 1:
            return None
        command = _run_text(["ps", "-p", str(pid), "-o", "command="])
        started = _run_text(["ps", "-p", str(pid), "-o", "lstart="])
        if not command or not started:
            return None
        normalized = normalize_process_start_fingerprint(started)
        if normalized is None:
            return None
        return ProcessInfo(pid, command, _process_cwd(pid), normalized)

    def managed_state(self) -> dict[str, Any] | None:
        path = project_root() / ".runtime/read_api/state.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def http_json(self, path: str) -> tuple[int, dict[str, Any]]:
        if path not in ALLOWED_REQUEST_PATHS:
            raise ProbeError("request path is not allow-listed")
        request = Request(
            f"http://{LOOPBACK_HOST}:{self.port}{path}",
            headers={"Accept": "application/json", "User-Agent": "saxo-db-readiness-v1"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                content = response.read(16_385)
        except HTTPError as exc:
            status = int(exc.code)
            content = exc.read(16_385)
        except (URLError, TimeoutError, OSError) as exc:
            raise ProbeError(type(exc).__name__) from exc
        if len(content) > 16_384:
            raise ProbeError("response too large")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProbeError("response is not JSON") from exc
        if not isinstance(payload, dict):
            raise ProbeError("response is not an object")
        return status, payload

    def openapi_contract(self) -> tuple[bool, str | None]:
        path = project_root() / OPENAPI_RELATIVE_PATH
        if not path.is_file() or path.is_symlink():
            return False, None
        content = path.read_bytes()
        text = content.decode("utf-8")
        required = (
            "openapi: 3.0.3",
            "  /api/v1/bars:",
            "  /api/v1/total-return:",
            "contract_revision: {type: string, enum: ['1.2']}",
        )
        return all(item in text for item in required), sha256_bytes(content)


def is_expected_process(info: ProcessInfo | None, *, port: int = DEFAULT_PORT) -> bool:
    if info is None or info.cwd != str(project_root().resolve()):
        return False
    try:
        tokens = shlex.split(info.command)
    except ValueError:
        return False
    if len(tokens) != 5:
        return False
    executable = str(Path(tokens[0]).resolve())
    allowed = {str(Path(item).resolve()) for item in allowed_python_executables()}
    return executable in allowed and tokens[1:] == [
        "-m", EXPECTED_MODULE, "--port", str(port)
    ]


def allowed_python_executables() -> frozenset[str]:
    """Return this runtime's venv, base, and macOS framework launch paths."""

    candidates = {str(Path(sys.executable))}
    base_executable = getattr(sys, "_base_executable", None)
    if isinstance(base_executable, str) and base_executable:
        candidates.add(base_executable)
    allowed: set[str] = set()
    for value in candidates:
        selected = Path(value)
        allowed.add(str(selected))
        try:
            resolved = selected.resolve(strict=True)
        except OSError:
            continue
        allowed.add(str(resolved))
        if resolved.parent.name == "bin" and resolved.name.lower().startswith("python"):
            framework_python = (
                resolved.parent.parent
                / "Resources/Python.app/Contents/MacOS/Python"
            )
            if framework_python.is_file():
                allowed.add(str(framework_python.resolve()))
    return frozenset(allowed)


def command_identity_sha256(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 2:
        return None
    payload = json.dumps(tokens[1:], ensure_ascii=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def _legacy_command_sha256_matches(state: dict[str, Any], info: ProcessInfo) -> bool:
    stored = state.get("command_sha256")
    if not isinstance(stored, str):
        return False
    if stored == info.command_sha256:
        return True
    tail = ["-m", EXPECTED_MODULE, "--port", str(DEFAULT_PORT)]
    return any(
        sha256_bytes(" ".join([launcher, *tail]).encode("utf-8")) == stored
        for launcher in allowed_python_executables()
    )


def managed_process_matches(
    state: dict[str, Any] | None, info: ProcessInfo | None
) -> bool:
    if state is None or info is None:
        return False
    common_match = (
        state.get("schema_version") in {1, 2}
        and state.get("pid") == info.pid
        and state.get("port") == DEFAULT_PORT
        and state.get("cwd") == info.cwd
        and process_start_fingerprints_match(
            state.get("start_fingerprint"), info.start_fingerprint
        )
        and is_expected_process(info)
    )
    if not common_match:
        return False
    if state.get("schema_version") == 2:
        return (
            state.get("owner") == "saxo_db.read_api_service"
            and state.get("command_identity_sha256")
            == command_identity_sha256(info.command)
        )
    return _legacy_command_sha256_matches(state, info)


def empty_result(status: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "checked_at_utc": utc_now(),
        "service": {
            "host": LOOPBACK_HOST,
            "port": DEFAULT_PORT,
            "process_running": False,
            "port_listening": False,
            "managed": False,
            "pid": None,
        },
        "health": {
            "http_status": None,
            "api_status": None,
            "database_name": None,
            "role_name": None,
            "transaction_read_only": None,
            "statement_timeout": None,
        },
        "contract": {
            "api_version": None,
            "contract_revision": None,
            "bars_route_present": False,
            "total_return_route_present": False,
            "openapi_sha256": None,
        },
        "data_inspection": {
            "performed": False,
            "market_rows_received": 0,
            "metadata_rows_received": 0,
            "request_paths": [],
        },
        "diagnostic_code": status,
    }


def readiness_exit_code(status: str) -> int:
    if status == PASS:
        return 0
    if status.startswith("BLOCKED_"):
        return 2
    return 1


def check_readiness(probe: ReadinessProbe | None = None) -> dict[str, Any]:
    selected = probe or SystemReadinessProbe()
    result = empty_result(BLOCKED_READ_API_NOT_RUNNING)
    try:
        listener_pids = selected.listener_pids(DEFAULT_PORT)
        if not listener_pids:
            return result

        process_rows = [selected.process_info(pid) for pid in listener_pids]
        expected = [row for row in process_rows if is_expected_process(row)]
        if len(listener_pids) != 1 or len(expected) != 1:
            result["status"] = BLOCKED_PORT_CONFLICT
            result["diagnostic_code"] = BLOCKED_PORT_CONFLICT
            result["service"]["port_listening"] = True
            return result

        info = expected[0]
        assert info is not None
        result["service"].update(
            {
                "process_running": True,
                "port_listening": True,
                "managed": managed_process_matches(selected.managed_state(), info),
                "pid": info.pid,
            }
        )

        try:
            root_status, root = selected.http_json("/")
            result["data_inspection"]["request_paths"].append("/")
            health_status, health = selected.http_json("/health")
            result["data_inspection"]["request_paths"].append("/health")
        except ProbeError:
            result["status"] = BLOCKED_DATABASE_UNHEALTHY
            result["diagnostic_code"] = BLOCKED_DATABASE_UNHEALTHY
            return result

        database = health.get("database") if isinstance(health.get("database"), dict) else {}
        result["health"].update(
            {
                "http_status": health_status,
                "api_status": health.get("status"),
                "database_name": database.get("database_name"),
                "role_name": database.get("role_name"),
                "transaction_read_only": database.get("transaction_read_only"),
                "statement_timeout": database.get("statement_timeout"),
            }
        )
        if health_status != 200 or health.get("status") != PASS:
            result["status"] = BLOCKED_DATABASE_UNHEALTHY
            result["diagnostic_code"] = BLOCKED_DATABASE_UNHEALTHY
            return result
        boundary_valid = (
            database.get("database_name") == MARKET_DB
            and database.get("role_name") == EXPECTED_ROLE
            and database.get("transaction_read_only") == EXPECTED_READ_ONLY
            and database.get("statement_timeout") == EXPECTED_STATEMENT_TIMEOUT
        )
        if not boundary_valid:
            result["status"] = BLOCKED_READ_ONLY_BOUNDARY
            result["diagnostic_code"] = BLOCKED_READ_ONLY_BOUNDARY
            return result

        result["contract"].update(
            {
                "api_version": root.get("api_version"),
                "contract_revision": root.get("contract_revision"),
            }
        )
        root_valid = (
            root_status == 200
            and root.get("status") == PASS
            and root.get("read_only") is True
            and root.get("api_version") == API_VERSION
            and root.get("contract_revision") == CONTRACT_REVISION
        )
        if not root_valid:
            result["status"] = BLOCKED_API_CONTRACT_MISMATCH
            result["diagnostic_code"] = BLOCKED_API_CONTRACT_MISMATCH
            return result

        try:
            bars_status, bars = selected.http_json("/api/v1/bars")
            result["data_inspection"]["request_paths"].append("/api/v1/bars")
            total_status, total = selected.http_json("/api/v1/total-return")
            result["data_inspection"]["request_paths"].append("/api/v1/total-return")
            openapi_valid, openapi_sha256 = selected.openapi_contract()
        except ProbeError:
            result["status"] = BLOCKED_API_CONTRACT_MISMATCH
            result["diagnostic_code"] = BLOCKED_API_CONTRACT_MISMATCH
            return result
        bars_present = bars_status == 400 and bars.get("error_code") == "INVALID_REQUEST"
        total_present = total_status == 400 and total.get("error_code") == "INVALID_REQUEST"
        result["contract"].update(
            {
                "bars_route_present": bars_present,
                "total_return_route_present": total_present,
                "openapi_sha256": openapi_sha256,
            }
        )
        if not (bars_present and total_present and openapi_valid):
            result["status"] = BLOCKED_API_CONTRACT_MISMATCH
            result["diagnostic_code"] = BLOCKED_API_CONTRACT_MISMATCH
            return result

        result["status"] = PASS
        result["diagnostic_code"] = None
        return result
    except Exception:
        failed = empty_result(FAILED_PREFLIGHT_INTERNAL)
        failed["diagnostic_code"] = FAILED_PREFLIGHT_INTERNAL
        return failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Read API without reading market data")
    parser.add_argument("--format", choices=("json",), default="json")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    build_parser().parse_args(list(argv) if argv is not None else None)
    result = check_readiness()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return readiness_exit_code(str(result["status"]))


if __name__ == "__main__":
    raise SystemExit(main())
