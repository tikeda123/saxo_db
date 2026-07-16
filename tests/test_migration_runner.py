from __future__ import annotations

import hashlib

import pytest

from market_db.migrate import MigrationError, list_migrations, migration_number, migration_sha256


def test_repository_migrations_are_declared_and_ordered():
    paths = list_migrations()
    assert [migration_number(path) for path in paths] == [f"{number:04d}" for number in range(1, 9)]
    assert all(migration_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)


def test_unknown_migration_is_rejected(tmp_path):
    (tmp_path / "0009_unknown.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="not declared"):
        list_migrations(tmp_path)


def test_invalid_migration_filename_is_not_selected(tmp_path):
    (tmp_path / "not_a_migration.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        list_migrations(tmp_path)
