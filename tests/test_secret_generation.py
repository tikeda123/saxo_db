from __future__ import annotations

import stat

import pytest

from scripts.create_local_db_secrets import SECRET_NAMES, ensure_secrets, validate_secrets


def test_secret_generation_is_private_and_idempotent(tmp_path):
    directory = tmp_path / ".secrets"
    assert ensure_secrets(directory) == (len(SECRET_NAMES), 0)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    original = {name: (directory / name).read_bytes() for name in SECRET_NAMES}
    assert all(len(value.strip()) >= 48 for value in original.values())
    assert len(set(original.values())) == len(SECRET_NAMES)
    assert all(stat.S_IMODE((directory / name).stat().st_mode) == 0o600 for name in SECRET_NAMES)

    assert ensure_secrets(directory) == (0, len(SECRET_NAMES))
    assert original == {name: (directory / name).read_bytes() for name in SECRET_NAMES}
    validate_secrets(directory)


def test_secret_generation_rejects_unsafe_existing_file(tmp_path):
    directory = tmp_path / ".secrets"
    directory.mkdir()
    path = directory / SECRET_NAMES[0]
    path.write_text("short", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="unexpectedly short"):
        ensure_secrets(directory)
