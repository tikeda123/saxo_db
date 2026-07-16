#!/usr/bin/env python3
"""Create local PostgreSQL role passwords without displaying their values."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from pathlib import Path


SECRET_NAMES = (
    "postgres_password",
    "saxo_migrator_password",
    "saxo_ingest_password",
    "saxo_app_reader_password",
    "saxo_analyst_reader_password",
    "saxo_ops_operator_password",
    "v13_research_reader_password",
    "v13_forward_writer_password",
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def ensure_secrets(directory: Path) -> tuple[int, int]:
    """Create missing secrets and validate existing ones.

    Returns ``(created, existing)``. Secret values are never returned.
    """

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    created = 0
    existing = 0

    for name in SECRET_NAMES:
        path = directory / name
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"unsafe secret path: {name}")
            if _mode(path) != 0o600:
                raise RuntimeError(f"secret mode must be 0600: {name}")
            if len(path.read_text(encoding="utf-8").strip()) < 48:
                raise RuntimeError(f"secret is unexpectedly short: {name}")
            existing += 1
            continue

        value = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        created += 1

    return created, existing


def validate_secrets(directory: Path) -> None:
    if not directory.is_dir() or _mode(directory) != 0o700:
        raise RuntimeError("secret directory must exist with mode 0700")
    for name in SECRET_NAMES:
        path = directory / name
        if not path.is_file() or path.is_symlink() or _mode(path) != 0o600:
            raise RuntimeError(f"invalid secret file: {name}")
        if len(path.read_text(encoding="utf-8").strip()) < 48:
            raise RuntimeError(f"secret is unexpectedly short: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate without creating")
    args = parser.parse_args()
    directory = project_root() / ".secrets"
    if args.check:
        validate_secrets(directory)
        print(f"secret files valid: {len(SECRET_NAMES)}")
    else:
        created, existing = ensure_secrets(directory)
        print(f"secret files ready: created={created} existing={existing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
