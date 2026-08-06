from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from market_db.bootstrap_seed import (
    BootstrapSeedError,
    EXPECTED_INSTRUMENT_KEYS,
    FIRST_DATA_DEPENDENT_MIGRATION,
    REQUIRED_MIGRATION,
    SEED_DIRECTORY,
    _assert_import_boundary,
    load_seed,
    verify_seed,
)
from market_db.connection import project_root


def test_repository_seed_is_small_synthetic_and_valid() -> None:
    result = verify_seed()

    assert result == {
        "schema_version": 1,
        "seed_id": "saxo_db_synthetic_bootstrap_v1",
        "status": "PASS",
        "eligibility": "SYNTHETIC_BOOTSTRAP_ONLY",
        "contains_upstream_market_data": False,
        "files": 3,
        "rows": 55,
        "size_bytes": 2323,
        "errors": [],
        "saxo_api_requests": 0,
        "orders_or_prechecks_sent": 0,
    }
    loaded = load_seed()
    assert {
        row["instrument_key"] for row in loaded["instruments.csv"]["rows"]
    } == EXPECTED_INSTRUMENT_KEYS
    assert all(
        ":SYNTHETIC" in row["symbol"]
        for row in loaded["instruments.csv"]["rows"]
    )


def test_seed_rejects_secret_like_content_before_import(tmp_path: Path) -> None:
    target = tmp_path / SEED_DIRECTORY
    shutil.copytree(project_root() / SEED_DIRECTORY, target)
    with (target / "instruments.csv").open("a", encoding="utf-8") as stream:
        stream.write("access_token,must-not-be-present\n")

    with pytest.raises(BootstrapSeedError, match="secret-like content"):
        verify_seed(tmp_path)


def test_import_contract_is_before_data_dependent_mapping() -> None:
    assert REQUIRED_MIGRATION == "0018"
    assert FIRST_DATA_DEPENDENT_MIGRATION == "0019"


class _BoundaryCursor:
    def __init__(self, migrations: list[str], counts: list[int]) -> None:
        self._migrations = migrations
        self._counts = iter(counts)
        self._last_statement = ""

    def execute(self, statement: str, _parameters=None) -> None:
        self._last_statement = statement

    def fetchall(self) -> list[tuple[str]]:
        return [(migration,) for migration in self._migrations]

    def fetchone(self) -> tuple[int]:
        assert self._last_statement.startswith("SELECT COUNT(*) FROM ")
        return (next(self._counts),)


def test_import_boundary_accepts_only_empty_database_at_0018() -> None:
    _assert_import_boundary(_BoundaryCursor(["0001", "0018"], [0] * 7))

    with pytest.raises(BootstrapSeedError, match="0018_NOT_APPLIED"):
        _assert_import_boundary(_BoundaryCursor(["0001"], [0] * 7))
    with pytest.raises(BootstrapSeedError, match="0019_ALREADY_APPLIED"):
        _assert_import_boundary(_BoundaryCursor(["0001", "0018", "0019"], [0] * 7))
    with pytest.raises(BootstrapSeedError, match="BLOCKED_NONEMPTY_DATABASE"):
        _assert_import_boundary(_BoundaryCursor(["0001", "0018"], [0, 1, 0, 0, 0, 0, 0]))


def test_seed_import_module_has_no_network_or_provider_request_path() -> None:
    source = (project_root() / "market_db" / "bootstrap_seed.py").read_text(
        encoding="utf-8"
    )
    assert "gateway.saxobank.com" not in source
    assert "requests." not in source
    assert "urllib." not in source
    assert "SYNTHETIC_BOOTSTRAP_NOT_MARKET_DATA" in source
    assert "NOT_EVALUATED" in source
