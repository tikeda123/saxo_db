#!/usr/bin/env python3
"""Read-only preflight for a fresh macOS saxo_db checkout.

The checker does not create files, access Saxo, connect to PostgreSQL, or
inspect credential values.  It only verifies host tools, repository contracts,
and (optionally) that local-only state was not copied into a fresh checkout.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


MINIMUM_PYTHON = (3, 12)
REQUIRED_REPOSITORY_FILES = (
    "README.md",
    "compose.yaml",
    "requirements.txt",
    "db/migrations/0001_bootstrap.sql",
    "scripts/create_local_db_secrets.py",
    "scripts/verify_bootstrap_seed.py",
    "bootstrap/seed/manifest.json",
)
REQUIRED_GITIGNORE_ENTRIES = (
    ".env",
    ".secrets/",
    ".venv/",
    ".runtime/",
    "backups/",
    "exports/",
    "*.log",
    "*.dump",
    "data/import/**/*.csv",
    "data/acquisition/runs/",
)
FRESH_CLONE_FORBIDDEN_PATHS = (
    ".env",
    ".secrets",
    ".venv",
    ".runtime",
    "backups",
    "exports",
    "pgdata",
    "data/acquisition/runs",
)


CommandRunner = Callable[[Sequence[str]], tuple[int, str]]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_version_command(command: Sequence[str]) -> tuple[int, str]:
    """Return one sanitized version line without forwarding stderr."""

    try:
        completed = subprocess.run(
            list(command),
            cwd=project_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    first_line = completed.stdout.strip().splitlines()
    return completed.returncode, first_line[0] if first_line else ""


def _check_command(
    check_id: str,
    command: Sequence[str],
    runner: CommandRunner,
) -> dict[str, str]:
    returncode, version = runner(command)
    if returncode != 0:
        return {"check_id": check_id, "status": "FAIL", "detail": "UNAVAILABLE"}
    return {
        "check_id": check_id,
        "status": "PASS",
        "detail": version or "AVAILABLE",
    }


def verify_environment(
    root: Path,
    *,
    expect_clean_clone: bool = False,
    check_docker_daemon: bool = True,
    runner: CommandRunner = run_version_command,
) -> dict[str, object]:
    checks: list[dict[str, str]] = []

    checks.append(
        {
            "check_id": "HOST_OS_MACOS",
            "status": "PASS" if platform.system() == "Darwin" else "FAIL",
            "detail": platform.system(),
        }
    )
    python_ok = sys.version_info[:2] >= MINIMUM_PYTHON
    checks.append(
        {
            "check_id": "PYTHON_3_12_OR_NEWER",
            "status": "PASS" if python_ok else "FAIL",
            "detail": platform.python_version(),
        }
    )
    checks.extend(
        (
            _check_command("GIT_AVAILABLE", ("git", "--version"), runner),
            _check_command("DOCKER_CLI_AVAILABLE", ("docker", "--version"), runner),
            _check_command(
                "DOCKER_COMPOSE_AVAILABLE", ("docker", "compose", "version"), runner
            ),
        )
    )
    if check_docker_daemon:
        daemon = _check_command(
            "DOCKER_DAEMON_AVAILABLE",
            ("docker", "info", "--format", "{{.ServerVersion}}"),
            runner,
        )
        if daemon["status"] == "PASS":
            daemon["detail"] = "AVAILABLE"
        checks.append(daemon)

    missing_files = [name for name in REQUIRED_REPOSITORY_FILES if not (root / name).is_file()]
    checks.append(
        {
            "check_id": "REPOSITORY_REQUIRED_FILES",
            "status": "PASS" if not missing_files else "FAIL",
            "detail": "PRESENT" if not missing_files else ",".join(missing_files),
        }
    )

    compose_path = root / "compose.yaml"
    compose_text = compose_path.read_text(encoding="utf-8") if compose_path.is_file() else ""
    compose_ok = all(
        value in compose_text
        for value in ("postgres:18.4-bookworm", '"127.0.0.1:54329:5432"', "saxo_pg18_data")
    )
    checks.append(
        {
            "check_id": "LOCAL_POSTGRES_COMPOSE_CONTRACT",
            "status": "PASS" if compose_ok else "FAIL",
            "detail": "POSTGRES_18_4_LOOPBACK" if compose_ok else "CONTRACT_MISMATCH",
        }
    )

    gitignore_path = root / ".gitignore"
    ignored = {
        line.strip()
        for line in gitignore_path.read_text(encoding="utf-8").splitlines()
        if gitignore_path.is_file() and line.strip() and not line.lstrip().startswith("#")
    } if gitignore_path.is_file() else set()
    missing_ignores = [entry for entry in REQUIRED_GITIGNORE_ENTRIES if entry not in ignored]
    checks.append(
        {
            "check_id": "LOCAL_STATE_GITIGNORE_CONTRACT",
            "status": "PASS" if not missing_ignores else "FAIL",
            "detail": "PROTECTED" if not missing_ignores else ",".join(missing_ignores),
        }
    )

    present_local_state = [name for name in FRESH_CLONE_FORBIDDEN_PATHS if (root / name).exists()]
    if expect_clean_clone:
        checks.append(
            {
                "check_id": "FRESH_CLONE_HAS_NO_COPIED_LOCAL_STATE",
                "status": "PASS" if not present_local_state else "FAIL",
                "detail": "ABSENT" if not present_local_state else ",".join(present_local_state),
            }
        )
    else:
        checks.append(
            {
                "check_id": "LOCAL_STATE_DISCOVERY",
                "status": "INFO",
                "detail": ",".join(present_local_state) if present_local_state else "NONE",
            }
        )

    failures = [check["check_id"] for check in checks if check["status"] == "FAIL"]
    return {
        "schema_version": 1,
        "status": "PASS" if not failures else "BLOCKED",
        "read_only": True,
        "saxo_api_requests": 0,
        "database_connections": 0,
        "database_writes": 0,
        "orders_or_prechecks_sent": 0,
        "root": str(root.resolve()),
        "checks": checks,
        "blockers": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only new-Mac environment preflight")
    parser.add_argument(
        "--expect-clean-clone",
        action="store_true",
        help="fail if local-only state appears to have been copied into the checkout",
    )
    parser.add_argument(
        "--skip-docker-daemon",
        action="store_true",
        help="check Docker CLI/Compose only; useful before Docker Desktop or Colima is started",
    )
    args = parser.parse_args()
    result = verify_environment(
        project_root(),
        expect_clean_clone=args.expect_clean_clone,
        check_docker_daemon=not args.skip_docker_daemon,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
